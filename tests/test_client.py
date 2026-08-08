"""API client tests, driven by a mock transport rather than the network.

Every behaviour that matters here is a failure behaviour — a page silently lost, a retry
that should not have happened, a truncated fetch that did not say so — and none of those
can be provoked reliably against the live registry. So the transport is faked and the
responses are shaped by hand.

The one thing not faked is the response *shape*: `totalCount`, `studies`, `nextPageToken`
are exactly what `docs/api-findings.md` recorded from real calls.

No network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cheiron.ctgov.cache import DiskCache, NullCache, cache_key
from cheiron.ctgov.client import (
    ApiError,
    CtGovClient,
    FetchResult,
    ReconciliationError,
)
from cheiron.ctgov.compiler import CompiledRequest, compile_plan
from cheiron.ctgov.retrieval import assemble
from cheiron.schemas.plan import Filters, Leg, Plan

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


def study(nct_id: str) -> dict[str, Any]:
    """A minimal but structurally real study record."""
    return {"protocolSection": {"identificationModule": {"nctId": nct_id}}}


def page(
    ids: list[str], *, total: int | None = None, next_token: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"studies": [study(i) for i in ids]}
    if total is not None:
        body["totalCount"] = total
    if next_token:
        body["nextPageToken"] = next_token
    return body


class Recorder:
    """A mock transport that serves canned pages and records what was asked for."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def call_count(self) -> int:
        return len(self.requests)


def make_client(recorder: Recorder, **kwargs: Any) -> CtGovClient:
    transport = httpx.MockTransport(recorder)
    return CtGovClient(httpx.AsyncClient(transport=transport), **kwargs)


def a_request(**filters: Any) -> CompiledRequest:
    plan = Plan(legs=[Leg(label="All", filters=Filters(**filters))], group_by="phases")
    return compile_plan(plan)[0]


def ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


# --------------------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------------------


async def test_single_page_fetch_reconciles() -> None:
    recorder = Recorder([ok(page(["NCT1", "NCT2"], total=2))])
    result = await make_client(recorder).fetch(a_request(condition="melanoma"))

    assert result.retrieved == 2
    assert result.matched == 2
    assert result.pages == 1
    assert result.truncated is False
    result.check()


async def test_pagination_follows_the_token_to_the_end() -> None:
    recorder = Recorder(
        [
            ok(page(["NCT1"], total=3, next_token="t1")),
            ok(page(["NCT2"], next_token="t2")),
            ok(page(["NCT3"])),
        ]
    )
    result = await make_client(recorder).fetch(a_request(condition="melanoma"))

    assert result.retrieved == 3
    assert result.matched == 3
    assert result.pages == 3
    result.check()


async def test_count_total_is_requested_once_and_only_once() -> None:
    """`countTotal` costs the registry a full count, so asking on every page is waste."""
    recorder = Recorder(
        [ok(page(["NCT1"], total=2, next_token="t1")), ok(page(["NCT2"]))]
    )
    await make_client(recorder).fetch(a_request(condition="melanoma"))

    assert "countTotal" in recorder.requests[0].url.params
    assert "countTotal" not in recorder.requests[1].url.params
    assert recorder.requests[1].url.params["pageToken"] == "t1"


# --------------------------------------------------------------------------------------
# Truncation — the chart becomes a sample, and has to say so
# --------------------------------------------------------------------------------------


async def test_hitting_the_page_cap_sets_truncated_rather_than_failing() -> None:
    recorder = Recorder([ok(page(["NCT1"], total=99, next_token="more"))])
    client = make_client(recorder, max_pages=2)

    result = await client.fetch(a_request(condition="melanoma"))

    assert result.pages == 2
    assert result.retrieved == 2
    assert result.matched == 99
    assert result.truncated is True
    result.check()  # a truncated fetch is short *for a known reason*, so it reconciles


async def test_a_short_fetch_without_truncation_raises() -> None:
    """The failure this whole reconciliation exists to catch: records lost mid-pagination.

    The registry says 50 matched, one page of 1 arrived, and there is no next token — so
    the cap did not stop us. A chart built on this would be wrong by 49 trials and would
    look entirely normal.
    """
    result = FetchResult(leg_label="All", studies=[study("NCT1")], matched=50, pages=1)
    with pytest.raises(ReconciliationError, match="without hitting the page cap"):
        result.check()


async def test_retrieving_more_than_matched_raises_even_when_truncated() -> None:
    result = FetchResult(
        leg_label="All", studies=[study("A"), study("B")], matched=1, pages=1, truncated=True
    )
    with pytest.raises(ReconciliationError, match="exceeds the registry"):
        result.check()


# --------------------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------------------


async def test_rate_limit_is_retried_and_then_succeeds() -> None:
    recorder = Recorder(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, text="slow down"),
            ok(page(["NCT1"], total=1)),
        ]
    )
    result = await make_client(recorder).fetch(a_request(condition="melanoma"))

    assert recorder.call_count == 2
    assert result.retrieved == 1


async def test_a_bad_query_is_not_retried_and_carries_the_registry_message() -> None:
    """The registry's own error text is unusually good; replacing it would lose the fix."""
    detail = "Error parsing query in advanced filter: Unknown area name: `NotAField`"
    recorder = Recorder([httpx.Response(400, text=detail)])

    with pytest.raises(ApiError) as caught:
        await make_client(recorder).fetch(a_request(condition="melanoma"))

    assert caught.value.status == 400
    assert "Unknown area name" in caught.value.detail
    assert recorder.call_count == 1, "a malformed query is not made valid by repetition"


async def test_transport_errors_are_retried() -> None:
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return ok(page(["NCT1"], total=1))

    client = CtGovClient(httpx.AsyncClient(transport=httpx.MockTransport(flaky)))
    result = await client.fetch(a_request(condition="melanoma"))

    assert attempts["n"] == 2
    assert result.retrieved == 1


# --------------------------------------------------------------------------------------
# Cache — off by default, on for the demo
# --------------------------------------------------------------------------------------


async def test_caching_is_off_by_default() -> None:
    """A live user query is answered against the registry as it is now, every time."""
    recorder = Recorder([ok(page(["NCT1"], total=1))])
    client = make_client(recorder)

    await client.fetch(a_request(condition="melanoma"))
    await client.fetch(a_request(condition="melanoma"))

    assert recorder.call_count == 2
    assert isinstance(client._cache, NullCache)


async def test_an_enabled_cache_serves_the_second_fetch_from_disk(tmp_path: Path) -> None:
    recorder = Recorder([ok(page(["NCT1"], total=1))])
    cache = DiskCache(tmp_path)
    client = make_client(recorder, cache=cache)

    first = await client.fetch(a_request(condition="melanoma"))
    second = await client.fetch(a_request(condition="melanoma"))

    assert recorder.call_count == 1, "the second fetch must not touch the network"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.studies == first.studies


async def test_cached_pages_are_keyed_per_page_not_per_query(tmp_path: Path) -> None:
    """Page 3 of a fetch must never be served page 1's records."""
    recorder = Recorder(
        [ok(page(["NCT1"], total=2, next_token="t1")), ok(page(["NCT2"]))]
    )
    client = make_client(recorder, cache=DiskCache(tmp_path))

    first = await client.fetch(a_request(condition="melanoma"))
    second = await client.fetch(a_request(condition="melanoma"))

    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in second.studies] == [
        "NCT1",
        "NCT2",
    ]
    assert second.studies == first.studies
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_cache_key_is_order_independent_but_page_sensitive() -> None:
    assert cache_key({"a": "1", "b": "2"}, None) == cache_key({"b": "2", "a": "1"}, None)
    assert cache_key({"a": "1"}, None) != cache_key({"a": "1"}, "token")


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    (tmp_path / "deadbeef.json").write_text("{not json")
    assert cache.get("deadbeef") is None


def test_clearing_the_cache_reports_what_it_removed(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    cache.put("one", {"a": 1})
    cache.put("two", {"b": 2})
    assert cache.clear() == 2
    assert cache.get("one") is None


# --------------------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------------------


async def test_legs_are_fetched_concurrently_and_kept_apart() -> None:
    plan = Plan(
        legs=[
            Leg(label="Pembrolizumab", filters=Filters(intervention="pembrolizumab")),
            Leg(label="Nivolumab", filters=Filters(intervention="nivolumab")),
        ],
        group_by="phases",
    )

    def by_drug(request: httpx.Request) -> httpx.Response:
        drug = request.url.params.get("query.intr")
        return ok(page([f"NCT-{drug}"], total=1))

    client = CtGovClient(httpx.AsyncClient(transport=httpx.MockTransport(by_drug)))
    results = await client.fetch_all(compile_plan(plan))

    assert [r.leg_label for r in results] == ["Pembrolizumab", "Nivolumab"]
    assert results[0].studies != results[1].studies


# --------------------------------------------------------------------------------------
# Wiring to the normalizer
# --------------------------------------------------------------------------------------


def test_assemble_normalizes_and_carries_exclusions_forward() -> None:
    """A record the normalizer rejects was still counted by the registry as retrieved.

    If it were dropped here the aggregator would reconcile against a total that had
    quietly shrunk, which is precisely the silent-loss failure the invariants exist for.
    """
    result = FetchResult(
        leg_label="All",
        studies=[study("NCT1"), {"protocolSection": {}}, "not a record"],  # type: ignore[list-item]
        matched=3,
        pages=1,
    )
    retrieval = assemble([result])

    assert len(retrieval.records_by_leg["All"]) == 1
    assert sum(retrieval.exclusions.values()) == 2
    assert retrieval.retrieved == 3
    assert retrieval.fetched_ids == {"NCT1"}


def test_assemble_keeps_legs_separate_and_unions_the_audit_trail() -> None:
    results = [
        FetchResult(leg_label="A", studies=[study("NCT1")], matched=1, pages=1, urls=["u1"]),
        FetchResult(leg_label="B", studies=[study("NCT1")], matched=1, pages=1, urls=["u2"]),
    ]
    retrieval = assemble(results)

    # The same trial matched both legs. It stays in both, because legs are populations.
    assert retrieval.records_by_leg["A"][0].nct_id == "NCT1"
    assert retrieval.records_by_leg["B"][0].nct_id == "NCT1"
    assert retrieval.matched == 2
    assert retrieval.urls == ["u1", "u2"]
    assert retrieval.fetched_ids == {"NCT1"}


def test_truncation_in_any_leg_marks_the_whole_retrieval() -> None:
    results = [
        FetchResult(leg_label="A", studies=[study("NCT1")], matched=1, pages=1),
        FetchResult(leg_label="B", studies=[study("NCT2")], matched=99, pages=1, truncated=True),
    ]
    assert assemble(results).truncated is True


def test_real_fixture_payload_survives_the_whole_retrieval_path() -> None:
    """One end-to-end pass over a real 229-site, 33-country record."""
    raw = json.loads((FIXTURE_DIR / "NCT06077760.json").read_text())
    retrieval = assemble([FetchResult(leg_label="All", studies=[raw], matched=1, pages=1)])

    record = retrieval.records_by_leg["All"][0]
    assert record.nct_id == "NCT06077760"
    assert record.get("phases") == "PHASE3"
    assert len(record.get("countries")) == 33
    assert retrieval.exclusions == {}
