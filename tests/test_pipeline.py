"""Pipeline and HTTP surface.

The pipeline's job is sequencing and, more importantly, deciding which of four response
shapes a request gets. Those decisions are what is tested here — with both halves faked, so
a routing choice is not hostage to what a live model happens to say.

One property runs through all of it: **the response shape never varies**. `conversational`
is the only type with a null visualization; `unsupported` and `no_results` carry a full
block with empty data, so a frontend has one render path rather than a well-tested one and
an empty-state one that nobody exercises.

No network, no API key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cheiron.agg.aggregator import InvariantError
from cheiron.ctgov.client import CtGovClient
from cheiron.llm.client import LLMSettings, Provider
from cheiron.llm.judge import JudgeVerdict
from cheiron.llm.router import Intent, RouterVerdict
from cheiron.llm.selector import ChartChoice
from cheiron.pipeline import Deps, analyze
from cheiron.schemas.plan import Filters, Leg, Plan
from cheiron.schemas.request import AnalyzeRequest
from cheiron.schemas.response import ResponseType, VizType

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


class FakeLLM:
    """Returns a scripted value per call, matched by the schema it was asked for."""

    def __init__(self, **by_schema: Any) -> None:
        self.by_schema = by_schema
        self.calls: list[str] = []
        self.settings = LLMSettings(
            provider=Provider.OPENAI, api_key="test", model_small="s", model_large="l"
        )

    async def complete(self, *, system, user, schema, tier, max_tokens=2048, **kwargs):
        self.calls.append(schema.__name__)
        value = self.by_schema.get(schema.__name__)
        if value is None:
            raise AssertionError(f"no scripted response for {schema.__name__}")
        if isinstance(value, Exception):
            raise value
        return value


def registry(studies: list[dict] | None = None, status: int = 200):
    """A mock ClinicalTrials.gov."""
    body = {"totalCount": len(studies or []), "studies": studies or []}

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="upstream failure")
        return httpx.Response(200, json=body)

    return CtGovClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def fixtures() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("NCT*.json"))]


PLAN = Plan(
    legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))], group_by="phases"
)


def deps(**overrides: Any) -> Deps:
    llm = overrides.pop("llm", None) or FakeLLM(
        RouterVerdict=RouterVerdict(intent=Intent.QUESTION),
        Plan=PLAN,
        JudgeVerdict=JudgeVerdict(verdict="ok"),
        ChartChoice=ChartChoice(chart="bar"),
    )
    return Deps(llm=llm, ctgov=overrides.pop("ctgov", None) or registry(fixtures()))


# --------------------------------------------------------------------------------------
# The four response shapes
# --------------------------------------------------------------------------------------


async def test_a_question_produces_a_visualization() -> None:
    response = await analyze(deps(), AnalyzeRequest(query="phases for melanoma"))

    assert response.response_type is ResponseType.VISUALIZATION
    assert response.visualization is not None
    assert response.visualization.data
    assert any(d.citations for d in response.visualization.data)


async def test_chit_chat_costs_nothing_and_is_the_only_null_visualization() -> None:
    """The router is the only stage that can end a request before any API call."""
    llm = FakeLLM(RouterVerdict=RouterVerdict(intent=Intent.CONVERSATIONAL, reply="Hello!"))
    response = await analyze(deps(llm=llm), AnalyzeRequest(query="hi"))

    assert response.response_type is ResponseType.CONVERSATIONAL
    assert response.visualization is None
    assert response.answer == "Hello!"
    assert llm.calls == ["RouterVerdict"], "no planner, no judge, no selector"


async def test_an_unsupported_question_is_a_redirect_not_a_refusal() -> None:
    """Naming the obstruction and offering a postable alternative is the difference
    between explaining a limit and just saying no."""
    llm = FakeLLM(
        RouterVerdict=RouterVerdict(
            intent=Intent.UNSUPPORTED,
            reason="Trials report incommensurable outcome measures.",
            suggestions=["How many pembrolizumab trials are there by phase?"],
        )
    )
    response = await analyze(deps(llm=llm), AnalyzeRequest(query="which drug works better?"))

    assert response.response_type is ResponseType.UNSUPPORTED
    assert "incommensurable" in response.answer
    assert response.meta.suggested_requests[0]["query"].startswith("How many pembrolizumab")


async def test_a_suggested_request_is_postable_as_is() -> None:
    """A suggestion the caller has to reformat is a hint, not a redirect."""
    llm = FakeLLM(
        RouterVerdict=RouterVerdict(
            intent=Intent.UNSUPPORTED,
            reason="no efficacy field",
            suggestions=["How many pembrolizumab trials are there?"],
        )
    )
    response = await analyze(
        deps(llm=llm), AnalyzeRequest(query="which is better?", condition="Melanoma")
    )

    body = response.meta.suggested_requests[0]
    rebuilt = AnalyzeRequest.model_validate(body)
    assert rebuilt.query == "How many pembrolizumab trials are there?"
    assert rebuilt.condition == "Melanoma", "the caller's structured fields are carried over"


async def test_every_non_conversational_type_carries_a_visualization_block() -> None:
    """So a frontend has one render path rather than a special case it rarely exercises."""
    llm = FakeLLM(
        RouterVerdict=RouterVerdict(intent=Intent.UNSUPPORTED, reason="out of scope")
    )
    response = await analyze(deps(llm=llm), AnalyzeRequest(query="x"))

    assert response.visualization is not None
    assert response.visualization.data == []
    assert response.visualization.type is VizType.KPI


async def test_no_matching_trials_is_reported_not_faked() -> None:
    response = await analyze(deps(ctgov=registry([])), AnalyzeRequest(query="phases"))

    assert response.response_type is ResponseType.NO_RESULTS
    assert response.visualization is not None
    assert response.visualization.data == []
    assert any("No trials matched" in w for w in response.meta.warnings)


async def test_a_registry_failure_explains_itself() -> None:
    response = await analyze(
        deps(ctgov=registry(status=503)), AnalyzeRequest(query="phases")
    )
    assert response.response_type is ResponseType.NO_RESULTS
    assert any("503" in w for w in response.meta.warnings)


async def test_a_planning_failure_becomes_unsupported() -> None:
    """Distinct from a contested plan: there is nothing to fall back to, so no chart."""
    from cheiron.llm.client import LLMError

    llm = FakeLLM(
        RouterVerdict=RouterVerdict(intent=Intent.QUESTION),
        Plan=LLMError("provider unreachable"),
    )
    response = await analyze(deps(llm=llm), AnalyzeRequest(query="phases"))

    assert response.response_type is ResponseType.UNSUPPORTED
    assert response.visualization is not None
    assert response.visualization.data == []


# --------------------------------------------------------------------------------------
# The trace
# --------------------------------------------------------------------------------------


async def test_the_response_records_how_the_plan_was_reached() -> None:
    """None of this changes a value. A reader who cannot see it has to take the chart on
    faith, which is the opposite of what the citations exist for."""
    response = await analyze(deps(), AnalyzeRequest(query="phases for melanoma"))

    assert response.meta.plan is not None
    assert response.meta.record_counts is not None
    assert response.meta.api_requests
    assert response.meta.llm_provider == "openai"
    assert response.meta.elapsed_ms is not None


async def test_a_reviewers_objection_is_surfaced_even_after_a_replan() -> None:
    llm = FakeLLM(
        RouterVerdict=RouterVerdict(intent=Intent.QUESTION),
        Plan=PLAN,
        JudgeVerdict=JudgeVerdict(verdict="concern", concerns=["comparison collapsed"]),
        ChartChoice=ChartChoice(chart="bar"),
    )
    response = await analyze(deps(llm=llm), AnalyzeRequest(query="compare A vs B"))
    assert any("comparison collapsed" in w for w in response.meta.warnings)


async def test_citations_can_be_switched_off() -> None:
    response = await analyze(
        deps(), AnalyzeRequest(query="phases", include_citations=False)
    )
    assert response.visualization is not None, "the chart itself is unaffected"
    assert response.visualization.data, "the datums stay; only their evidence is dropped"
    assert not any(d.citations for d in response.visualization.data)


async def test_the_planning_trace_can_be_switched_off() -> None:
    response = await analyze(
        deps(), AnalyzeRequest(query="phases", include_planning_trace=False)
    )
    assert response.meta.planning_trace == []


# --------------------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch) -> Any:
    """The app with both dependencies faked, so endpoints are testable offline."""
    from fastapi.testclient import TestClient

    from cheiron.api import app as app_module

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    test_client = TestClient(app_module.app)
    with test_client:
        app_module.app.state.deps = deps()
        yield test_client


def test_capabilities_is_generated_from_the_field_registry(client) -> None:
    """A hand-written capability list is wrong the first time someone adds a field."""
    from cheiron.schemas.fields import FIELDS

    body = client.get("/capabilities").json()
    assert {f["key"] for f in body["fields"]} == set(FIELDS)
    assert body["metrics"] == ["count", "distinct_count", "sum", "median"]
    assert any("Comparative efficacy" in limit for limit in body["limitations"])


def test_schema_publishes_what_the_service_actually_validates(client) -> None:
    body = client.get("/schema").json()
    assert "query" in body["request"]["properties"]
    assert "legs" in body["plan"]["properties"]
    assert "separators" in body["citation_offsets"], "offsets must be reproducible by hand"


def test_analyze_returns_the_envelope_over_http(client) -> None:
    body = client.post("/analyze", json={"query": "phases for melanoma"}).json()
    assert body["response_type"] == "visualization"
    assert body["visualization"]["data"]
    assert body["meta"]["record_counts"]["used"] > 0


def test_a_typo_in_a_field_name_is_rejected_at_the_edge(client) -> None:
    """`extra="forbid"` on the request means a misspelt field is an error rather than a
    silently ignored key that leaves the caller wondering why nothing changed."""
    response = client.post("/analyze", json={"query": "x", "conditon": "melanoma"})
    assert response.status_code == 422


def test_plan_runs_the_agent_layer_without_retrieving(client) -> None:
    body = client.post("/plan", json={"query": "phases for melanoma"}).json()
    assert body["plan"]["group_by"] == "phases"
    assert body["review"]["verdict"] == "ok"
    assert "attempts" in body and "probes" in body


def test_an_invariant_failure_returns_no_chart_at_all(client, monkeypatch) -> None:
    """A reconciliation failure means the chart would be wrong, so nothing is returned.
    Failing loudly has to hold at the HTTP boundary too, not just in the aggregator."""
    from cheiron import pipeline

    async def explode(*args, **kwargs):
        raise InvariantError("used=5 + excluded=2 != retrieved=9")

    monkeypatch.setattr(pipeline, "analyze", explode)
    from cheiron.api import app as app_module

    monkeypatch.setattr(app_module, "analyze", explode)

    response = client.post("/analyze", json={"query": "phases"})
    assert response.status_code == 500
    assert response.json()["error"] == "invariant_failure"


async def test_an_approval_is_recorded_not_just_a_concern() -> None:
    """A silent reviewer and an approving one must not look the same.

    Only unactioned concerns used to leave a trace, so `meta` could not distinguish "the
    judge approved this plan" from "the judge never ran" — which in a system whose case
    rests on its audit trail is the wrong default.
    """
    response = await analyze(deps(), AnalyzeRequest(query="phases"))
    assert response.meta.review is not None
    assert response.meta.review.verdict
    assert response.meta.review.revised is False
