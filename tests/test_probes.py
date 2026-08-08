"""Probe tool tests.

Probes are the one place the planner learns anything about the data, so the properties
that matter are about what they *can't* do as much as what they can: they return counts
and never records, they are bounded by a budget, and they say plainly whether an answer is
exact or sampled.

Driven by a mock transport, so the Essie clauses each probe compiles are asserted exactly.
Those clauses were verified against the live API first — `AREA[EnrollmentCount]RANGE[MIN,MAX]`
returned 3,625 of 3,743 melanoma trials — and the assertions here stop them drifting.

No network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cheiron.ctgov.client import CtGovClient
from cheiron.llm.probes import (
    PROBE_BUDGET,
    FieldValuesArgs,
    FillRateArgs,
    ProbeCountArgs,
    ProbeFilters,
    ProbeRunner,
    probe_tool_specs,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


class Registry:
    """A mock transport that answers counts and records what it was asked."""

    def __init__(self, total: int = 100, per_request: dict[str, int] | None = None) -> None:
        self.total = total
        self.per_request = per_request or {}
        self.requests: list[httpx.URL] = []
        self.studies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        advanced = request.url.params.get("filter.advanced", "")
        total = next(
            (n for clause, n in self.per_request.items() if clause in advanced), self.total
        )
        return httpx.Response(200, json={"totalCount": total, "studies": self.studies})

    @property
    def clauses(self) -> list[str]:
        return [r.params.get("filter.advanced", "") for r in self.requests]


def runner(registry: Registry, budget: int = PROBE_BUDGET) -> ProbeRunner:
    transport = httpx.MockTransport(registry)
    return ProbeRunner(CtGovClient(httpx.AsyncClient(transport=transport)), budget=budget)


# --------------------------------------------------------------------------------------
# probe_count
# --------------------------------------------------------------------------------------


async def test_probe_count_returns_an_exact_total() -> None:
    registry = Registry(total=3743)
    result = await runner(registry).run("probe_count", {"condition": "melanoma"})

    assert result["total"] == 3743
    assert result["exact"] is True
    assert registry.requests[0].params["query.cond"] == "melanoma"


async def test_a_term_that_does_not_resolve_says_so() -> None:
    """Zero is the planner's signal that a name is wrong or in the wrong field."""
    result = await runner(Registry(total=0)).run("probe_count", {"intervention": "zzqqxx"})
    assert result["total"] == 0
    assert "may be misspelled" in result["note"]


async def test_a_slice_beyond_the_page_cap_is_flagged() -> None:
    """Above 20,000 the chart becomes a sample, and the planner can still choose to narrow."""
    result = await runner(Registry(total=42_724)).run("probe_count", {"country": "France"})
    assert "page cap" in result["note"]


async def test_probe_count_asks_for_one_record_not_a_page() -> None:
    """A probe wants the count, not the data. Fetching 1,000 records to read totalCount
    would make the cheapest probe the most expensive request in the system."""
    registry = Registry()
    await runner(registry).run("probe_count", {"condition": "melanoma"})
    assert registry.requests[0].params["pageSize"] == "1"
    assert registry.requests[0].params["countTotal"] == "true"


# --------------------------------------------------------------------------------------
# fill_rate
# --------------------------------------------------------------------------------------


async def test_fill_rate_is_exact_and_uses_the_presence_clause() -> None:
    """`RANGE[MIN,MAX]` matches records that have the field — verified live at 3,625/3,743."""
    registry = Registry(total=3743, per_request={"AREA[EnrollmentCount]RANGE[MIN,MAX]": 3625})
    result = await runner(registry).run(
        "fill_rate", {"condition": "melanoma", "field_name": "enrollment"}
    )

    assert result == {
        "present": 3625,
        "total": 3743,
        "fill_rate": 0.9685,
        "exact": True,
        "note": "",
    }
    assert any("AREA[EnrollmentCount]RANGE[MIN,MAX]" in c for c in registry.clauses)


async def test_a_sparsely_reported_field_gets_a_warning() -> None:
    """The failure this probe exists to prevent: a chart about reporting, not about trials."""
    registry = Registry(total=1000, per_request={"RANGE[MIN,MAX]": 200})
    result = await runner(registry).run(
        "fill_rate", {"condition": "melanoma", "field_name": "enrollment"}
    )

    assert result["fill_rate"] == 0.2
    assert "reporting practice" in result["note"]


async def test_fill_rate_refuses_fields_the_registry_cannot_answer_for() -> None:
    result = await runner(Registry()).run(
        "fill_rate", {"condition": "melanoma", "field_name": "brief_title"}
    )
    assert "not measurable" in result["error"]


async def test_fill_rate_preserves_the_slice_filters_on_both_counts() -> None:
    """A rate measured against the whole registry would answer a different question."""
    registry = Registry()
    await runner(registry).run(
        "fill_rate", {"condition": "melanoma", "field_name": "enrollment"}
    )
    assert len(registry.requests) == 2
    assert all(r.params["query.cond"] == "melanoma" for r in registry.requests)


# --------------------------------------------------------------------------------------
# field_values
# --------------------------------------------------------------------------------------


async def test_categorical_values_are_counted_exactly_per_member() -> None:
    """A closed vocabulary is small enough to count member by member."""
    registry = Registry(total=100, per_request={"AREA[Phase]PHASE3": 219})
    result = await runner(registry).run(
        "field_values", {"condition": "melanoma", "field_name": "phases"}
    )

    assert result["exact"] is True
    assert result["values"]["PHASE3"] == 219
    assert len(registry.requests) == 6, "one count per Phase enum member"


async def test_categorical_counts_declare_the_registry_double_counting() -> None:
    """Registry facets return a multi-phase trial under both phases; our charts do not.

    Without this note a reader comparing `meta.planning_trace` to the chart would find a
    mismatch and reasonably conclude one of them is wrong.
    """
    result = await runner(Registry()).run(
        "field_values", {"condition": "melanoma", "field_name": "phases"}
    )
    assert "own faceting" in result["note"]
    assert "chart values" in result["note"]


async def test_entity_values_are_sampled_and_say_so() -> None:
    """51,497 distinct lead sponsors corpus-wide: this vocabulary cannot be enumerated."""
    registry = Registry(total=5000)
    registry.studies = [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("NCT*.json"))]

    result = await runner(registry).run(
        "field_values", {"condition": "melanoma", "field_name": "sponsor_name"}
    )

    assert result["exact"] is False
    assert result["matching_total"] == 5000
    assert result["sample_size"] == len(registry.studies)
    assert "not chart values" in result["note"]
    assert result["distinct_values_in_sample"] > 0


async def test_a_sampled_probe_pulls_only_the_projected_field() -> None:
    """A sample is still a fetch, so it fetches the narrowest record it can."""
    registry = Registry()
    await runner(registry).run(
        "field_values", {"condition": "melanoma", "field_name": "sponsor_name"}
    )
    fields = registry.requests[0].params["fields"]
    assert "LeadSponsorName" in fields
    assert "EnrollmentCount" not in fields


async def test_field_values_refuses_unknown_and_ungroupable_fields() -> None:
    unknown = await runner(Registry()).run("field_values", {"field_name": "nonsense"})
    assert "not a legal field" in unknown["error"]

    ungroupable = await runner(Registry()).run("field_values", {"field_name": "brief_title"})
    assert "cannot be grouped" in ungroupable["error"]


# --------------------------------------------------------------------------------------
# Budget and failure handling
# --------------------------------------------------------------------------------------


async def test_the_budget_is_enforced_in_code_not_only_in_the_prompt() -> None:
    """A model that decides to probe eight times should be stopped, not asked nicely."""
    probes = runner(Registry(), budget=2)
    for _ in range(2):
        await probes.run("probe_count", {"condition": "melanoma"})

    assert probes.remaining == 0
    refusal = await probes.run("probe_count", {"condition": "melanoma"})
    assert "budget exhausted" in refusal["error"]
    assert len(probes.calls) == 2, "a refused probe is not recorded as a call"


async def test_an_exhausted_budget_tells_the_model_what_to_do_next() -> None:
    """An exception would abort planning over something entirely recoverable."""
    probes = runner(Registry(), budget=0)
    result = await probes.run("probe_count", {"condition": "melanoma"})
    assert "Commit to a plan" in result["error"]


async def test_a_registry_error_becomes_a_result_not_an_exception() -> None:
    """A failed probe must never abort planning: the planner can proceed without it."""

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Unknown area name: `NotAField`")

    probes = ProbeRunner(CtGovClient(httpx.AsyncClient(transport=httpx.MockTransport(failing))))
    result = await probes.run("probe_count", {"condition": "melanoma"})

    assert "Unknown area name" in result["error"], "the registry's own message is kept"
    assert len(probes.calls) == 1


async def test_an_unknown_tool_is_reported_rather_than_raised() -> None:
    result = await runner(Registry()).run("probe_everything", {})
    assert "unknown probe" in result["error"]


async def test_every_probe_is_recorded_for_the_planning_trace() -> None:
    """`meta.planning_trace` exists so a reader sees which facts the model was shown."""
    probes = runner(Registry())
    await probes.run("probe_count", {"condition": "melanoma"})
    await probes.run("fill_rate", {"condition": "melanoma", "field_name": "enrollment"})

    assert [c.tool for c in probes.calls] == ["probe_count", "fill_rate"]
    assert probes.calls[0].args == {"condition": "melanoma"}
    assert probes.calls[0].result["total"] is not None


# --------------------------------------------------------------------------------------
# The invariant: probes carry no trial data
# --------------------------------------------------------------------------------------


async def test_no_probe_result_contains_a_trial_identifier() -> None:
    """The core invariant at this layer: the planner sees aggregates, never records.

    A sampled probe fetches real records to count them, which makes this the one place a
    trial could leak into the model's context. It counts locally and returns only labels
    and totals.
    """
    registry = Registry(total=5000)
    registry.studies = [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("NCT*.json"))]
    probes = runner(registry)

    await probes.run("field_values", {"condition": "melanoma", "field_name": "sponsor_name"})
    await probes.run("probe_count", {"condition": "melanoma"})

    serialized = json.dumps([c.result for c in probes.calls])
    assert "NCT" not in serialized
    assert "briefTitle" not in serialized and "brief_title" not in serialized


# --------------------------------------------------------------------------------------
# Tool specs
# --------------------------------------------------------------------------------------


def test_probe_arguments_never_leak_into_the_filter_model() -> None:
    """`field_name` names the field being measured; it is not a filter.

    A blanket `model_dump` into `Filters` fails on every `field_values` and `fill_rate`
    call, which is exactly what happened the first time this ran live.
    """
    args = FieldValuesArgs(condition="melanoma", field_name="sponsor_name")
    filters = args.to_filters()
    assert filters.condition == "melanoma"
    assert not hasattr(filters, "field_name")

    assert FillRateArgs(field_name="enrollment").to_filters().condition is None


def test_probe_filters_are_narrower_than_the_full_filter_set() -> None:
    """Carrying all sixteen filters would inflate every tool schema for no gain."""
    from cheiron.schemas.plan import Filters

    assert set(ProbeFilters.model_fields) < set(Filters.model_fields)


@pytest.mark.parametrize("spec", probe_tool_specs())
def test_every_tool_spec_is_complete_and_says_whether_it_is_exact(spec: dict) -> None:
    assert spec["name"] and spec["description"]
    assert spec["input_schema"]["type"] == "object"
    assert "xact" in spec["description"] or "ampled" in spec["description"]


def test_the_tool_set_matches_what_the_runner_dispatches() -> None:
    assert {s["name"] for s in probe_tool_specs()} == {
        "probe_count",
        "field_values",
        "fill_rate",
    }


def test_tool_schemas_stay_inside_anthropics_optional_parameter_limit() -> None:
    """Tool schemas are compiled by the same grammar that caps `Plan`; keep them small."""
    from tests.test_llm import ANTHROPIC_MAX_OPTIONAL, count_optional

    for spec in probe_tool_specs():
        assert count_optional(spec["input_schema"]) <= ANTHROPIC_MAX_OPTIONAL


def test_probe_args_carry_no_defaults_that_would_silently_widen_a_slice() -> None:
    """Every filter defaults to None, so an omitted filter means "unfiltered", not a guess."""
    args = ProbeCountArgs()
    assert all(value is None for value in args.model_dump().values())
