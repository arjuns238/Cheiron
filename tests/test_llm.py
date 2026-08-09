"""LLM layer tests: schema translation, and the planner's repair loop.

The provider clients are exercised against their *schema* requirements rather than against
the network. Both providers' limits were discovered by 400s from the live APIs, and those
limits are now asserted here so a future schema change that would break a provider fails in
the test suite instead of in production:

    Anthropic: no `$ref`, no `maxItems`, at most 16 union-typed and 24 optional parameters
    OpenAI:    no `$ref` with sibling keywords, every property in `required`

The planner is driven by a fake client, so the loop's behaviour — what feedback a rejected
plan gets, what happens on exhaustion — is tested deterministically rather than by hoping a
real model misbehaves on cue.

No network, no API key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from cheiron.llm.client import (
    LLMError,
    LLMSettings,
    Optionality,
    Provider,
    Tier,
    json_schema_for,
)
from cheiron.llm.planner import (
    NARROW_SCHEMA_FIELDS,
    PlanningError,
    _apply_derived_defaults,
    build_repair_prompt,
    build_system_prompt,
    build_user_prompt,
    plan_query,
)
from cheiron.schemas.fields import FIELDS, SKEWED_FIELDS
from cheiron.schemas.plan import BinScale, Filters, Granularity, Leg, Plan
from cheiron.schemas.request import AnalyzeRequest

#: Anthropic's documented ceilings, both learned from live 400 responses.
ANTHROPIC_MAX_UNIONS = 16
ANTHROPIC_MAX_OPTIONAL = 24


def walk(node: Any):
    """Every dict in a schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def count_unions(schema: dict[str, Any]) -> int:
    return sum(1 for node in walk(schema) if "anyOf" in node)


def count_optional(schema: dict[str, Any]) -> int:
    total = 0
    for node in walk(schema):
        if node.get("type") == "object" and "properties" in node:
            required = set(node.get("required", []))
            total += sum(1 for key in node["properties"] if key not in required)
    return total


# --------------------------------------------------------------------------------------
# Schema translation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("optionality", list(Optionality))
def test_no_references_survive_translation(optionality: Optionality) -> None:
    """Both providers reject `$ref`; Pydantic emits one per enum-typed field."""
    text = json.dumps(json_schema_for(Plan, optionality))
    assert "$ref" not in text
    assert "$defs" not in text


@pytest.mark.parametrize("optionality", list(Optionality))
def test_unsupported_keywords_are_stripped(optionality: Optionality) -> None:
    """Anthropic rejected `maxItems`, OpenAI rejected a `$ref` carrying `default`."""
    text = json.dumps(json_schema_for(Plan, optionality))
    for keyword in ("maxItems", "minItems", "\"default\"", "multipleOf", "minLength"):
        assert keyword not in text, f"{keyword} would be rejected by a provider"


def test_dropped_constraints_are_still_enforced_by_pydantic() -> None:
    """Stripping a constraint from the wire schema moves it, it does not remove it.

    `max_length=6` on legs is gone from the schema Anthropic sees, so a model *could*
    return seven. Pydantic still refuses it, and the refusal becomes repair-loop feedback.
    """
    assert "maxItems" not in json.dumps(json_schema_for(Plan))
    with pytest.raises(PydanticValidationError, match="at most 6"):
        Plan(legs=[Leg(label=f"L{i}") for i in range(7)], group_by="phases")


def test_openai_style_requires_every_property_and_uses_nullable_unions() -> None:
    """Strict mode demands required-all; optionality is then expressed as `anyOf [T, null]`."""
    schema = json_schema_for(Plan, Optionality.NULLABLE)
    assert set(schema["required"]) == set(schema["properties"])
    assert count_optional(schema) == 0
    assert count_unions(schema) > 0


def test_anthropic_style_expresses_optionality_as_absence() -> None:
    """The mirror image, because Anthropic caps unions at 16 and Plan would need 24."""
    schema = json_schema_for(Plan, Optionality.OMITTABLE)
    assert count_unions(schema) == 0
    assert schema["required"] == ["legs"], "only legs has no default"


def test_anthropic_schema_fits_both_documented_limits() -> None:
    """The regression guard: adding an optional Plan field must not silently break Anthropic."""
    schema = json_schema_for(Plan, Optionality.OMITTABLE, NARROW_SCHEMA_FIELDS)
    assert count_unions(schema) <= ANTHROPIC_MAX_UNIONS
    assert count_optional(schema) <= ANTHROPIC_MAX_OPTIONAL


def test_the_full_schema_does_not_fit_anthropic_which_is_why_trimming_exists() -> None:
    """Documents the reason for `NARROW_SCHEMA_FIELDS` rather than leaving it folklore."""
    assert count_optional(json_schema_for(Plan, Optionality.OMITTABLE)) > ANTHROPIC_MAX_OPTIONAL


def test_dropped_fields_disappear_at_every_nesting_level() -> None:
    """Five of the seven trimmed fields live on Filters, two levels down from Plan."""
    schema = json_schema_for(Plan, Optionality.OMITTABLE, NARROW_SCHEMA_FIELDS)
    text = json.dumps(schema)
    for name in NARROW_SCHEMA_FIELDS:
        assert f'"{name}"' not in text


def test_dropping_a_field_leaves_its_pydantic_default_intact() -> None:
    """A withheld field is a choice the model no longer makes, not one that vanishes."""
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="melanoma"))])
    assert plan.bin_scale is BinScale.LINEAR
    assert plan.viz_hint is None


def test_objects_are_closed_in_both_styles() -> None:
    for optionality in Optionality:
        for node in walk(json_schema_for(Plan, optionality)):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False


def test_a_recursive_schema_fails_loudly() -> None:
    """Silently truncating would constrain the model to something the code doesn't validate."""
    from pydantic import BaseModel

    class Node(BaseModel):
        child: Node | None = None  # noqa: UP037 — recursion is the point of this fixture

    Node.model_rebuild()
    with pytest.raises(LLMError, match="recursive"):
        json_schema_for(Node)


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def test_a_missing_key_fails_at_startup_not_at_first_query(monkeypatch) -> None:
    """There is no heuristic planner to degrade to, so this is a startup failure."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENAI_API_KEY is not set"):
        LLMSettings.from_env()


def test_an_unknown_provider_names_the_supported_ones(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(LLMError, match="anthropic, openai"):
        LLMSettings.from_env()


def test_tiers_resolve_to_their_configured_models(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_MODEL_SMALL", "small-model")
    monkeypatch.setenv("ANTHROPIC_MODEL_LARGE", "large-model")
    settings = LLMSettings.from_env()
    assert settings.model_for(Tier.SMALL) == "small-model"
    assert settings.model_for(Tier.LARGE) == "large-model"


# --------------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------------


def test_the_legal_field_list_is_generated_from_the_registry() -> None:
    """The prompt, the validator and the normalizer read one table.

    A hand-written field list is how a planner ends up proposing a field the aggregator
    has never heard of, so this asserts the prompt covers the registry exactly.
    """
    prompt = build_system_prompt()
    for key in FIELDS:
        assert key in prompt


def test_structured_parameters_are_given_to_the_planner() -> None:
    """The planner must plan against the slice that will actually be fetched.

    Its probes run on the plan's own filters, so a planner ignorant of `drug_name` probes
    the whole corpus and calibrates granularity, bins and top_n to a population nobody
    asked about. It is told them, but not trusted with them: `apply_overrides` pins them
    afterwards, and the judge — not the planner — catches a contradiction.
    """
    request = AnalyzeRequest(query="How many trials?", condition="Melanoma", start_year=2015)
    prompt = build_user_prompt(request)
    assert "How many trials?" in prompt
    assert "Melanoma" in prompt
    assert "2015" in prompt


def test_repair_feedback_carries_the_validator_errors_verbatim() -> None:
    """The validator's messages are written to be actionable; paraphrasing loses that."""
    from cheiron.llm.planner import PlanAttempt

    plan = Plan(legs=[Leg(label="All")], group_by="start_date")
    errors = ["group_by='start_date' is temporal and requires an explicit granularity"]
    prompt = build_repair_prompt(PlanAttempt(plan=plan, errors=errors), [plan])

    assert errors[0] in prompt
    assert "Do not repropose" in prompt
    assert "start_date" in prompt


# --------------------------------------------------------------------------------------
# Derived defaults
# --------------------------------------------------------------------------------------


def test_skewed_numeric_binning_defaults_to_log() -> None:
    """Withholding bin_scale from Anthropic must not resurrect the one-bar histogram."""
    assert "enrollment" in SKEWED_FIELDS
    plan = Plan(legs=[Leg(label="All")], group_by="enrollment", bins=10)
    assert plan.bin_scale is BinScale.LINEAR
    assert _apply_derived_defaults(plan).bin_scale is BinScale.LOG


def test_an_explicit_scale_is_not_overridden() -> None:
    """A provider that *can* express the choice keeps it."""
    plan = Plan(
        legs=[Leg(label="All")], group_by="enrollment", bins=10, bin_scale=BinScale.LOG
    )
    assert _apply_derived_defaults(plan).bin_scale is BinScale.LOG


def test_derivation_only_applies_to_binned_plans() -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    assert _apply_derived_defaults(plan) == plan


# --------------------------------------------------------------------------------------
# The repair loop
# --------------------------------------------------------------------------------------


class FakeClient:
    """Replays a scripted sequence of plans (or errors) and records the prompts it saw."""

    def __init__(
        self,
        script: list[Plan | Exception],
        provider: Provider = Provider.OPENAI,
        probe_script: list[list[tuple[str, dict]]] | None = None,
    ):
        self.script = list(script)
        self.probe_script = list(probe_script or [])
        self.prompts: list[str] = []
        self.drops: list[frozenset[str]] = []
        self.tools_offered: list[Any] = []
        self.settings = LLMSettings(
            provider=provider, api_key="test", model_small="small", model_large="large"
        )

    async def complete(
        self,
        *,
        system,
        user,
        schema,
        tier,
        max_tokens=2048,
        drop=frozenset(),
        tools=None,
        executor=None,
    ):
        self.prompts.append(user)
        self.drops.append(drop)
        self.tools_offered.append(tools)
        # Replay any scripted probe calls, so the planner's trace can be asserted without
        # a real model deciding to probe.
        for name, args in self.probe_script.pop(0) if self.probe_script else []:
            assert executor is not None, "probe scripted but no executor supplied"
            await executor(name, args)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


LEGAL = Plan(
    legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))], group_by="phases"
)
#: Temporal group_by with no granularity — rejected by validator rule 4.
ILLEGAL = Plan(legs=[Leg(label="Melanoma")], group_by="start_date")
#: Two errors: an unknown field and a top_n that cannot apply to it.
WORSE = Plan(legs=[Leg(label="Melanoma")], group_by="start_date", top_n=5)


async def test_a_legal_plan_commits_on_the_first_attempt() -> None:
    client = FakeClient([LEGAL])
    result = await plan_query(client, AnalyzeRequest(query="phases for melanoma"))

    assert result.plan == LEGAL
    assert len(result.attempts) == 1
    assert result.contested is False
    assert result.warnings == []


async def test_a_rejected_plan_is_repaired_and_committed() -> None:
    client = FakeClient([ILLEGAL, LEGAL])
    result = await plan_query(client, AnalyzeRequest(query="melanoma over time"))

    assert result.plan == LEGAL
    assert len(result.attempts) == 2
    assert result.contested is False
    assert "requires an explicit granularity" in client.prompts[1]


async def test_the_repair_prompt_forbids_reproposing_a_rejected_plan() -> None:
    client = FakeClient([ILLEGAL, LEGAL])
    await plan_query(client, AnalyzeRequest(query="melanoma over time"))
    assert "Do not repropose" in client.prompts[1]
    assert "start_date" in client.prompts[1]


async def test_an_unusable_response_becomes_feedback_rather_than_a_failure() -> None:
    """A schema-invalid response is a normal step in this loop, not the end of it."""
    client = FakeClient([LLMError("model output does not fit Plan: field required"), LEGAL])
    result = await plan_query(client, AnalyzeRequest(query="phases for melanoma"))

    assert result.plan == LEGAL
    assert result.attempts[0].plan is None
    assert "field required" in client.prompts[1]


async def test_exhaustion_commits_the_best_attempt_not_the_last() -> None:
    """`plan.md`: later attempts drift as the model over-corrects, so score, don't recency."""
    client = FakeClient([ILLEGAL, WORSE, WORSE, WORSE])
    result = await plan_query(
        client, AnalyzeRequest(query="melanoma over time"), max_revisions=3
    )

    assert result.contested is True
    assert result.plan == ILLEGAL, "the one-error attempt beats the two-error ones"
    assert len(result.attempts) == 4


async def test_a_contested_plan_says_so_in_its_warnings() -> None:
    client = FakeClient([ILLEGAL] * 4)
    result = await plan_query(
        client, AnalyzeRequest(query="melanoma over time"), max_revisions=3
    )

    assert result.contested is True
    warning = result.warnings[0]
    assert "contested and not fully resolved" in warning
    assert "granularity" in warning


async def test_never_failing_closed_beats_a_perfect_refusal() -> None:
    """A nearly-right chart with a stated caveat serves the reader; no answer does not."""
    client = FakeClient([ILLEGAL] * 4)
    result = await plan_query(
        client, AnalyzeRequest(query="melanoma over time"), max_revisions=3
    )
    assert result.plan is not None


async def test_no_usable_plan_at_all_raises() -> None:
    """Distinct from contested: there is nothing to fall back to, so do not draw a chart."""
    client = FakeClient([LLMError("provider unreachable")] * 4)
    with pytest.raises(PlanningError, match="no usable plan"):
        await plan_query(client, AnalyzeRequest(query="phases"), max_revisions=3)


async def test_the_revision_budget_is_respected() -> None:
    client = FakeClient([ILLEGAL] * 3)
    result = await plan_query(
        client, AnalyzeRequest(query="melanoma over time"), max_revisions=2
    )
    assert len(result.attempts) == 3


# --------------------------------------------------------------------------------------
# Provider-dependent vocabulary
# --------------------------------------------------------------------------------------


async def test_anthropic_gets_the_narrowed_schema_and_openai_does_not() -> None:
    """A documented capability difference, asserted so it cannot drift silently."""
    for provider, expected in (
        (Provider.ANTHROPIC, NARROW_SCHEMA_FIELDS),
        (Provider.OPENAI, frozenset()),
    ):
        client = FakeClient([LEGAL], provider=provider)
        await plan_query(client, AnalyzeRequest(query="phases for melanoma"))
        assert client.drops[0] == expected


async def test_a_withheld_bin_scale_is_derived_after_the_model_answers() -> None:
    """End-to-end proof that the Anthropic path still gets a log-scaled histogram."""
    binned = Plan(
        legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
        group_by="enrollment",
        bins=10,
    )
    client = FakeClient([binned], provider=Provider.ANTHROPIC)
    result = await plan_query(client, AnalyzeRequest(query="enrollment distribution"))

    assert result.plan.bin_scale is BinScale.LOG
    assert "bin_scale" in client.drops[0]


# --------------------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------------------


async def test_probe_tools_are_offered_only_when_a_runner_is_supplied() -> None:
    """Without a runner the planner works from the schema alone — legal, but blind."""
    bare = FakeClient([LEGAL])
    await plan_query(bare, AnalyzeRequest(query="phases for melanoma"))
    assert bare.tools_offered[0] is None

    import httpx

    from cheiron.ctgov.client import CtGovClient
    from cheiron.llm.probes import ProbeRunner

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"totalCount": 1}))
    probed = FakeClient([LEGAL])
    await plan_query(
        probed,
        AnalyzeRequest(query="phases for melanoma"),
        probes=ProbeRunner(CtGovClient(httpx.AsyncClient(transport=transport))),
    )
    assert {t["name"] for t in probed.tools_offered[0]} == {
        "probe_count",
        "field_values",
        "fill_rate",
    }


async def test_probe_calls_are_recorded_on_the_planning_result() -> None:
    """`meta.planning_trace` is built from this: what the model was shown, before it chose."""
    import httpx

    from cheiron.ctgov.client import CtGovClient
    from cheiron.llm.probes import ProbeRunner

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"totalCount": 2922}))
    runner = ProbeRunner(CtGovClient(httpx.AsyncClient(transport=transport)))
    client = FakeClient(
        [LEGAL], probe_script=[[("probe_count", {"intervention": "pembrolizumab"})]]
    )

    result = await plan_query(client, AnalyzeRequest(query="pembro phases"), probes=runner)

    assert [c.tool for c in result.probes] == ["probe_count"]
    assert result.probes[0].result["total"] == 2922
    assert result.probes[0].args == {"intervention": "pembrolizumab"}


async def test_the_probe_budget_spans_the_whole_planning_loop() -> None:
    """The budget is per planning attempt in the prompt, but the runner bounds the loop.

    A planner that probes on every revision would otherwise multiply its budget by the
    revision count.
    """
    import httpx

    from cheiron.ctgov.client import CtGovClient
    from cheiron.llm.probes import PROBE_BUDGET, ProbeRunner

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"totalCount": 1}))
    runner = ProbeRunner(CtGovClient(httpx.AsyncClient(transport=transport)))
    probe = ("probe_count", {"condition": "melanoma"})
    client = FakeClient([ILLEGAL, LEGAL], probe_script=[[probe] * 3, [probe] * 3])

    result = await plan_query(client, AnalyzeRequest(query="melanoma"), probes=runner)

    # Six probes were attempted across two attempts; only the budgeted four ran. Refused
    # probes are deliberately not recorded as calls, so the trace shows work done, not
    # work attempted.
    assert len(result.probes) == PROBE_BUDGET
    assert all("error" not in c.result for c in result.probes)


async def test_the_prompt_tells_the_planner_what_probes_are_for() -> None:
    prompt = build_system_prompt()
    for tool in ("probe_count", "field_values", "fill_rate"):
        assert tool in prompt
    assert "probes shape the plan" in prompt, (
        "the model must be told not to copy a probe result into the plan"
    )


async def test_a_plan_that_needs_no_repair_costs_one_call() -> None:
    client = FakeClient([Plan(
        legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
        group_by="start_date",
        granularity=Granularity.YEAR,
    )])
    result = await plan_query(client, AnalyzeRequest(query="melanoma per year"))
    assert len(result.attempts) == 1
    assert len(client.prompts) == 1
