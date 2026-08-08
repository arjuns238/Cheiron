"""Router, judge and chart selector — the three remaining LLM touchpoints.

What is tested here is the *containment*: what each stage does when the model is wrong,
unreachable, or answering outside the permitted set. Whether the model gives good answers
is a separate question, measured against a live model by `tests/adversarial_judge.py`.

Every stage here fails toward the safe direction, and the directions differ:

* router → in-domain (a wrong refusal looks broken; a wrongly-analysed greeting returns
  nothing)
* judge → approval (it is advisory, so an outage should cost nothing)
* selector → the rules' default (the model can only ever downgrade)

No network, no API key.
"""

from __future__ import annotations

import pytest

from cheiron.llm.client import LLMError, LLMSettings, Provider
from cheiron.llm.judge import (
    JudgeVerdict,
    build_review_prompt,
    concern_feedback,
    review,
)
from cheiron.llm.planner import plan_and_review
from cheiron.llm.probes import ProbeCall
from cheiron.llm.router import RouterVerdict, route
from cheiron.llm.selector import ChartChoice, build_prompt, select
from cheiron.schemas.fields import FieldKind
from cheiron.schemas.plan import Filters, Layout, Leg, Metric, Plan
from cheiron.schemas.request import AnalyzeRequest
from cheiron.schemas.response import VizType
from cheiron.viz.rules import Shape, legal_charts


class Scripted:
    """Returns a scripted value per schema, or raises a scripted error."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.tiers: list[object] = []
        self.settings = LLMSettings(
            provider=Provider.OPENAI, api_key="test", model_small="s", model_large="l"
        )

    async def complete(self, *, system, user, schema, tier, max_tokens=2048, **kwargs):
        self.prompts.append(user)
        self.tiers.append(tier)
        step = self.responses.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


# --------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------


async def test_a_question_is_routed_in_domain() -> None:
    client = Scripted(RouterVerdict(in_domain=True))
    verdict = await route(client, "How many melanoma trials are there?")
    assert verdict.in_domain is True


async def test_chit_chat_is_routed_out_with_a_reply() -> None:
    client = Scripted(RouterVerdict(in_domain=False, reply="Hello! I chart trials data."))
    verdict = await route(client, "hi")
    assert verdict.in_domain is False
    assert verdict.reply


async def test_the_router_fails_open() -> None:
    """A wrong refusal makes the system look broken for a question it could have answered;
    a wrongly-analysed greeting merely returns nothing."""
    client = Scripted(LLMError("provider unreachable"))
    assert (await route(client, "How many melanoma trials?")).in_domain is True


async def test_the_router_uses_the_cheap_model() -> None:
    """One closed-set classification, and the only gate before any API call."""
    client = Scripted(RouterVerdict(in_domain=True))
    await route(client, "hi")
    assert client.tiers[0].value == "small"


# --------------------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------------------


LEGAL = Plan(
    legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))], group_by="phases"
)


async def test_an_approval_is_a_positive_verdict_not_an_absent_objection() -> None:
    client = Scripted(JudgeVerdict(verdict="ok"))
    verdict = await review(client, "phases for melanoma", LEGAL)
    assert verdict.verdict == "ok"
    assert verdict.is_concerned is False


async def test_concerns_are_surfaced() -> None:
    client = Scripted(JudgeVerdict(verdict="concern", concerns=["metric mismatch"]))
    assert (await review(client, "median enrolment?", LEGAL)).is_concerned is True


async def test_a_concern_verdict_with_no_concerns_is_not_treated_as_one() -> None:
    """Otherwise a model can block a plan without saying what is wrong, and the re-plan
    would carry no feedback to act on."""
    assert JudgeVerdict(verdict="concern", concerns=[]).is_concerned is False


async def test_a_malformed_verdict_fails_toward_review() -> None:
    """Anything that is not an explicit 'ok' is a concern, so a garbled token cannot be
    read as silent approval."""
    assert JudgeVerdict(verdict="LGTM", concerns=["something"]).is_concerned is True


async def test_the_judge_fails_toward_approval() -> None:
    """It is advisory, so an outage should cost nothing. Failing toward concern would
    spend a re-plan on no evidence and make an outage look like a quality problem."""
    client = Scripted(LLMError("unreachable"))
    verdict = await review(client, "phases for melanoma", LEGAL)
    assert verdict.is_concerned is False


def test_the_review_prompt_carries_the_question_the_plan_and_the_probes() -> None:
    probes = [
        ProbeCall(
            tool="fill_rate", args={"field_name": "enrollment"}, result={"fill_rate": 0.2}
        )
    ]
    prompt = build_review_prompt("median enrolment for melanoma?", LEGAL, probes)

    assert "median enrolment for melanoma?" in prompt
    assert "phases" in prompt
    assert "fill_rate" in prompt, "sparse-grouping is invisible without probe results"


def test_the_review_prompt_names_the_failure_classes() -> None:
    """"Assess quality" invites agreement; a checklist gives something to check."""
    from cheiron.llm.judge import SYSTEM_PROMPT

    for failure in ("METRIC MISMATCH", "COLLAPSED COMPARISON", "WRONG FIELD", "TIME BASIS"):
        assert failure in SYSTEM_PROMPT
    assert "Approving is a real" in SYSTEM_PROMPT, "a judge must be told approval is valid"


def test_concern_feedback_is_actionable() -> None:
    feedback = concern_feedback(
        JudgeVerdict(verdict="concern", concerns=["asked about enrolment, plan counts trials"])
    )
    assert "asked about enrolment" in feedback
    assert "corrected Plan" in feedback


# --------------------------------------------------------------------------------------
# The judge's authority is bounded
# --------------------------------------------------------------------------------------


async def test_a_concern_triggers_exactly_one_replan() -> None:
    """A reviewer that can trigger unlimited revisions turns one disagreement into a loop."""
    revised = Plan(
        legs=[
            Leg(label="Pembrolizumab", filters=Filters(intervention="pembrolizumab")),
            Leg(label="Nivolumab", filters=Filters(intervention="nivolumab")),
        ],
        group_by="phases",
    )
    client = Scripted(
        LEGAL,
        JudgeVerdict(verdict="concern", concerns=["comparison collapsed into one leg"]),
        revised,
    )
    result = await plan_and_review(client, AnalyzeRequest(query="compare A vs B"))

    assert result.plan == revised
    assert result.revised_after_review is True
    assert not client.responses, "planned, reviewed, re-planned — and stopped"


async def test_the_replan_is_committed_without_a_second_review() -> None:
    """Asking again would be the start of the loop the bound exists to prevent."""
    client = Scripted(
        LEGAL, JudgeVerdict(verdict="concern", concerns=["wrong field"]), LEGAL
    )
    result = await plan_and_review(client, AnalyzeRequest(query="x"))
    assert result.revised_after_review is True
    assert len(client.prompts) == 3, "plan, review, re-plan — no second review"


async def test_the_concern_reaches_the_replanning_prompt() -> None:
    client = Scripted(
        LEGAL, JudgeVerdict(verdict="concern", concerns=["a disease in the drug filter"]), LEGAL
    )
    await plan_and_review(client, AnalyzeRequest(query="x"))
    assert "a disease in the drug filter" in client.prompts[2]


async def test_an_objection_is_recorded_even_when_it_did_not_resolve() -> None:
    """A reader should be able to see the objection whether or not it changed the plan."""
    client = Scripted(
        LEGAL, JudgeVerdict(verdict="concern", concerns=["time basis"]), LEGAL
    )
    result = await plan_and_review(client, AnalyzeRequest(query="x"))
    assert result.review.concerns == ["time basis"]


async def test_an_approved_plan_is_not_replanned() -> None:
    client = Scripted(LEGAL, JudgeVerdict(verdict="ok"))
    result = await plan_and_review(client, AnalyzeRequest(query="x"))
    assert result.revised_after_review is False
    assert not client.responses


async def test_review_can_be_switched_off() -> None:
    client = Scripted(LEGAL)
    result = await plan_and_review(client, AnalyzeRequest(query="x"), judge=False)
    assert result.review is None


# --------------------------------------------------------------------------------------
# Chart selector
# --------------------------------------------------------------------------------------


def temporal_shape() -> Shape:
    return Shape(
        group_kind=FieldKind.TEMPORAL,
        series_kind=None,
        metric=Metric.COUNT,
        layout=Layout.AGGREGATE,
        binned=False,
        bucket_count=10,
        series_count=0,
        has_other=False,
        sample_labels=("2015", "2016"),
        group_field="start_date",
    )


async def test_a_legal_preference_is_honoured() -> None:
    """The selector earns its place precisely where several charts are defensible: the
    aggregation cannot tell "how has X changed" from "which year had the most"."""
    shape = temporal_shape()
    assert legal_charts(shape) == (VizType.LINE, VizType.BAR)

    client = Scripted(ChartChoice(chart="bar", reason="ranking question"))
    assert await select(client, "Which year had the most trials?", shape) is VizType.BAR


@pytest.mark.parametrize("answer", ["pie", "network", "not-a-chart", ""])
async def test_an_illegal_choice_downgrades_to_the_rules_default(answer: str) -> None:
    """The safety property: the model can only ever fail to be heeded."""
    client = Scripted(ChartChoice(chart=answer))
    assert await select(client, "How has this changed?", temporal_shape()) is VizType.LINE


async def test_the_selector_fails_to_the_rules_default() -> None:
    client = Scripted(LLMError("unreachable"))
    assert await select(client, "anything", temporal_shape()) is VizType.LINE


async def test_a_single_legal_chart_skips_the_model_entirely() -> None:
    """Nothing to choose, so nothing to pay for."""
    shape = Shape(
        group_kind=FieldKind.NUMERIC,
        series_kind=None,
        metric=Metric.COUNT,
        layout=Layout.AGGREGATE,
        binned=True,
        bucket_count=8,
        series_count=0,
        has_other=False,
        sample_labels=("0", "1–10"),
        group_field="enrollment",
    )
    client = Scripted()  # no scripted response: calling the model would raise IndexError
    assert await select(client, "distribution?", shape) is VizType.HISTOGRAM


def test_the_selector_is_shown_shape_and_never_values() -> None:
    """A model that cannot read a number cannot write one into the output."""
    shape = temporal_shape()
    prompt = build_prompt("How has this changed?", shape, legal_charts(shape))

    assert "line, bar" in prompt
    assert "buckets: 10" in prompt
    for token in ("value", "count:", "trials:"):
        assert token not in prompt.lower().replace("chart types", "")


async def test_the_selector_uses_the_cheap_model() -> None:
    client = Scripted(ChartChoice(chart="line"))
    await select(client, "trend?", temporal_shape())
    assert client.tiers[0].value == "small"


def test_the_judge_can_see_the_metric_it_is_asked_to_check() -> None:
    """`exclude_defaults` drops `metric: "count"` *because* count is the default, leaving
    the judge asked whether the metric matches the question's noun while never being shown
    the metric. That reproducibly broke METRIC MISMATCH on one provider."""
    plan = Plan(
        legs=[Leg(label="M", filters=Filters(condition="melanoma"))],
        group_by="sponsor_class",
        metric=Metric.COUNT,
    )
    prompt = build_review_prompt("What is the median enrolment?", plan)

    assert '"metric": "count"' in prompt
    assert '"layout": "aggregate"' in prompt


def test_caller_pinned_filters_are_shown_as_deliberate() -> None:
    """A structured override is a hard constraint the planner had to honour. Unshown, the
    resulting filter looks like a field the planner invented."""
    plan = Plan(
        legs=[Leg(label="M", filters=Filters(condition="melanoma"))], group_by="phases"
    )
    prompt = build_review_prompt("How many trials?", plan, None, {"condition": "Melanoma"})

    assert "Melanoma" in prompt
    assert "not\na field error" in prompt or "not a field error" in prompt


async def test_overrides_reach_the_judge_from_the_request() -> None:
    client = Scripted(LEGAL, JudgeVerdict(verdict="ok"))
    await plan_and_review(
        client, AnalyzeRequest(query="How many trials per year?", condition="Melanoma")
    )
    assert "Melanoma" in client.prompts[1], "the review prompt carries the override"


def test_the_selector_prompt_covers_every_chart_the_rules_can_offer() -> None:
    """A chart type with no guidance is one the model will not choose. Geography was
    missing, and both providers picked `bar` over the `choropleth` default for "Where are
    trials running?" — making the selector worse than not running it at all."""
    from cheiron.llm.selector import SYSTEM_PROMPT

    for chart in ("line", "bar", "choropleth", "pie", "stacked_area", "grouped_bar"):
        assert chart in SYSTEM_PROMPT, f"{chart} is offerable but has no guidance"
