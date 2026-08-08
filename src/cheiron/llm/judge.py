"""③ Judge: does this plan actually answer the question that was asked?

The plan validator already proves a plan is *legal* — every field exists, every metric has
what it needs. Legality is not correctness. A plan that counts trials when the question
asked about enrolment, or that collapses "A versus B" into a single population, passes
every validator rule and answers the wrong question. That gap is what this stage looks at,
and it is the only stage that reads the question and the plan together.

**Advisory, and bounded.** A concern triggers at most one re-plan and then the pipeline
proceeds regardless. `plan.md` is explicit that the judge cannot block: an advisory reviewer
that can veto becomes a second planner with no repair loop of its own.

**Two guards against a rubber stamp**, both from `plan.md` §3:

1. *The verdict token is always required.* `{"verdict": "ok"}` is a decision; a failed call
   is not. Without a required token the two are indistinguishable, and a judge that is
   quietly erroring looks exactly like a judge that approves everything. It also makes the
   approval rate loggable, which is the only way to notice a rubber stamp.
2. *The prompt names the failure classes.* "Assess quality" invites agreement. A checklist
   of specific, recognisable errors gives the model something to check rather than a
   sentiment to express.

Whether it actually catches those errors is an empirical question, answered by the
adversarial set in `tests/test_judge_adversarial.py` and reported honestly in the README —
including if it does not.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field

from cheiron.llm.client import LLMClient, LLMError, Tier
from cheiron.llm.probes import ProbeCall
from cheiron.schemas.fields import FIELDS
from cheiron.schemas.plan import Plan

log = logging.getLogger(__name__)


class JudgeVerdict(BaseModel):
    """The judge's decision.

    `verdict` is required precisely so that approval is a positive act rather than the
    absence of an objection.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description="Exactly 'ok' or 'concern'.")
    concerns: list[str] = Field(
        default_factory=list,
        description="What the plan gets wrong about the question, one per entry. Each must "
        "name the specific mismatch and what would fix it. Empty when the verdict is ok.",
    )

    @property
    def is_concerned(self) -> bool:
        # Anything that is not an explicit "ok" is treated as a concern, so a malformed
        # verdict fails toward review rather than toward silent approval.
        return self.verdict.strip().lower() != "ok" and bool(self.concerns)


SYSTEM_PROMPT = """\
You review an analysis plan against the question it is meant to answer. The plan is already
known to be structurally legal; you are checking whether it answers the *right* question.

Report a concern only when one of these is true. This is the whole list — do not invent
other grounds, and do not comment on style or completeness:

1. METRIC MISMATCH — the metric measures a different noun than the question asked about.
   The question names the quantity it wants; the plan's `metric` must be that quantity.
   "how many trials" → count. "median enrolment", "average enrolment", "typical size" →
   median with metric_field=enrolment. "total participants", "how many people" → sum with
   metric_field=enrolment. "how many distinct sponsors" → distinct_count.
   A plan with metric=count answers "how many trials" and nothing else, so it mismatches
   any question whose noun is participants, enrolment, size, or distinct values — even
   when the plan is otherwise sensible and even when it groups the trials usefully.
2. COLLAPSED COMPARISON — the question compares two or more named things, but the plan has
   a single leg, so the comparison disappears into one population.
3. WRONG FIELD — a filter value is in the wrong slot: a drug name in `condition`, a disease
   in `intervention`, a sponsor in `free_text` when `sponsor` exists.
4. TIME BASIS — the question is about when trials *started* but the plan groups on a
   completion date, or the reverse.
5. SPARSE GROUPING — the probe results show the grouping field is mostly empty in this
   slice, so the chart would describe reporting practice rather than trials.

If none applies, answer exactly {"verdict": "ok", "concerns": []}. Approving is a real
decision and is frequently the right one — most plans are correct, and inventing a concern
to seem useful costs a re-plan and makes the review worthless.

If one applies, answer {"verdict": "concern", "concerns": ["..."]} where each entry names
the mismatch and says what would fix it, in one sentence."""


def build_review_prompt(
    query: str,
    plan: Plan,
    probes: list[ProbeCall] | None = None,
    overrides: dict[str, object] | None = None,
) -> str:
    """The question, the plan, and whatever aggregate facts the planner saw.

    **Defaulted fields are shown, not omitted.** `exclude_defaults` would drop
    `metric: "count"` precisely because count is the default — leaving the judge asked
    whether the metric matches the question's noun while never being shown the metric. The
    same applies to `layout` and `sort`. Only nulls are dropped.

    **Structured overrides are shown too.** A caller who sets `condition` on the request
    hands the planner a hard constraint; without it here, the judge sees a filter in the
    plan with no visible justification and can read it as a field error.

    Probe results are included because one failure class — a grouping field that is empty
    in this slice — is invisible without them. They are counts, never records.
    """
    parts = [
        f"QUESTION: {query}",
        f"PLAN:\n{plan.model_dump_json(indent=2, exclude_none=True)}",
    ]
    if overrides:
        parts.append(
            "STRUCTURED FIELDS the caller set explicitly. The plan is required to honour "
            "these, so a filter matching one of them is correct by construction and is not "
            f"a field error:\n{json.dumps(overrides, indent=2, default=str)}"
        )
    if plan.group_by:
        spec = FIELDS[plan.group_by]
        parts.append(f"GROUPING FIELD: {plan.group_by} — {spec.label} ({spec.kind.value})")
    if probes:
        trace = [{"tool": p.tool, "args": p.args, "result": p.result} for p in probes]
        parts.append(
            "PROBE RESULTS the planner saw (aggregate counts, never trial records):\n"
            + json.dumps(trace, indent=2, default=str)[:2000]
        )
    return "\n\n".join(parts)


async def review(
    client: LLMClient,
    query: str,
    plan: Plan,
    probes: list[ProbeCall] | None = None,
    overrides: dict[str, object] | None = None,
) -> JudgeVerdict:
    """Review one plan. Never raises — an unusable answer is treated as approval.

    Failing toward approval is deliberate. The judge is advisory, so an unreachable model
    should cost nothing; failing toward *concern* would spend a re-plan on no evidence and
    make an outage look like a quality problem.
    """
    try:
        return await client.complete(
            system=SYSTEM_PROMPT,
            user=build_review_prompt(query, plan, probes, overrides),
            schema=JudgeVerdict,
            tier=Tier.LARGE,
            max_tokens=1024,
        )
    except LLMError as exc:
        log.warning("judge unavailable, proceeding without review: %s", exc)
        return JudgeVerdict(verdict="ok")


def concern_feedback(verdict: JudgeVerdict) -> str:
    """Turn concerns into planner-facing repair feedback."""
    listed = "\n".join(f"  - {c}" for c in verdict.concerns)
    return (
        f"A reviewer raised concerns that this plan does not answer the question asked:\n"
        f"{listed}\n\nReturn a corrected Plan that addresses them."
    )


__all__: list[str] = [
    "SYSTEM_PROMPT",
    "JudgeVerdict",
    "build_review_prompt",
    "concern_feedback",
    "review",
]
