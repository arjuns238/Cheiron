"""The planner: natural-language question → validated `Plan`.

This is the system's only unconstrained input, and the `Plan` schema is the wall it has to
come through. The model may choose *what to compute*, expressed solely in terms of a closed
vocabulary the deterministic core defines; it never sees a trial record and never produces
a number that reaches the output.

Two properties do the work:

1. **The legal vocabulary is generated, not written.** `LEGAL_FIELDS` in the prompt comes
   from the same field registry the validator checks against and the normalizer emits. A
   field cannot exist in the prompt but not the validator, or vice versa, because there is
   one table. Hand-maintaining that list is how a planner ends up confidently proposing a
   field the aggregator has never heard of.

2. **Rejection is feedback, not failure.** A plan that fails validation goes back to the
   model with the validator's own error strings, which are written to be actionable. The
   loop is bounded, previously-rejected plans cannot be re-proposed, and on exhaustion the
   best attempt ships rather than the last — later attempts drift as a model over-corrects.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from cheiron.llm.client import LLMClient, LLMError, Provider, Tier
from cheiron.llm.probes import PROBE_BUDGET, ProbeCall, ProbeRunner, probe_tool_specs
from cheiron.schemas.fields import FIELDS, FieldKind
from cheiron.schemas.plan import BinScale, Plan, validate_plan
from cheiron.schemas.request import AnalyzeRequest, OverrideConflict

log = logging.getLogger(__name__)

#: `plan.md` §3: at most three revisions after the first attempt.
MAX_REVISIONS = 3

#: Plan fields withheld from the planner when the provider cannot accept the full schema.
#:
#: Anthropic caps a structured-output schema at 24 optional parameters and `Plan` plus
#: `Filters` expose 31, so seven have to go. These seven were chosen because losing the
#: model's judgement on them costs nothing:
#:
#: * `viz_hint` is advisory — the viz rules decide chart legality regardless.
#: * `bin_scale` is derived deterministically from the field registry's `skewed` flag,
#:   which is strictly better than a model guess: see `_apply_derived_defaults`.
#: * the five filters are all reachable as structured request fields, which override the
#:   planner anyway, so a caller who needs them still has them.
#:
#: Nothing here is silently defaulted without a reason. A field whose default would be
#: *wrong* must not be added to this set.
NARROW_SCHEMA_FIELDS = frozenset(
    {
        "viz_hint",
        "bin_scale",
        "site_status",
        "date_certainty",
        "has_results",
        "enrollment_min",
        "enrollment_max",
    }
)


@dataclass
class PlanAttempt:
    """One proposal and the validator's verdict on it."""

    plan: Plan | None
    errors: list[str]
    #: Set when the model failed to produce a schema-valid plan at all.
    failure: str | None = None

    @property
    def is_legal(self) -> bool:
        return self.plan is not None and not self.errors

    @property
    def score(self) -> tuple[int, int]:
        """Lower is better: unusable attempts first, then by error count.

        `plan.md` says to ship the best-scoring attempt on exhaustion rather than the last
        one, but does not define the score. Error count is the honest choice — it is the
        only quality signal the deterministic layer actually has, and ties break toward the
        earliest attempt because later ones drift.
        """
        return (0 if self.plan is not None else 1, len(self.errors))


@dataclass
class PlanningResult:
    """The committed plan and the trace of how it was reached."""

    plan: Plan
    attempts: list[PlanAttempt] = field(default_factory=list)
    #: True when no attempt was fully legal and the least-bad one was committed anyway.
    contested: bool = False
    #: Every probe the planner ran, in order, for `meta.planning_trace`. Recorded so a
    #: reader can see exactly which aggregate facts the model was shown before it chose.
    probes: list[ProbeCall] = field(default_factory=list)
    #: The judge's verdict, kept whether or not it was acted on. An objection that did not
    #: change the plan is still something a reader should be able to see.
    review: Any = None
    #: True when the judge's concerns triggered the one permitted re-plan.
    revised_after_review: bool = False

    @property
    def warnings(self) -> list[str]:
        if not self.contested:
            return []
        errors = self.attempts[-1].errors if self.attempts else []
        return [
            "The plan was contested and not fully resolved after "
            f"{len(self.attempts)} attempt(s); the closest-fitting plan was used. "
            f"Unresolved: {'; '.join(errors[:3])}"
        ]


class PlanningError(RuntimeError):
    """No usable plan could be produced.

    Distinct from a contested plan: this means every attempt failed to produce even a
    schema-valid `Plan`, so there is nothing to fall back to. The caller turns this into an
    `unsupported` response rather than drawing a chart from nothing.
    """


# --------------------------------------------------------------------------------------
# Prompt
#
# Generated from the field registry at import time. Adding a field to `schemas.fields`
# changes the prompt, the validator and the aggregator together.
# --------------------------------------------------------------------------------------


def _field_lines() -> str:
    """One line per legal field: name, kind, and the caveat that applies to it."""
    lines = []
    for key, spec in FIELDS.items():
        flags = []
        if spec.multi:
            flags.append("multi-valued")
        if spec.measurable:
            flags.append("measurable")
        if not spec.groupable:
            flags.append("not groupable")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {key} ({spec.kind.value}){suffix} — {spec.label}")
    return "\n".join(lines)


def _kind_list(kind: FieldKind) -> str:
    return ", ".join(k for k, f in FIELDS.items() if f.kind is kind) or "none"


def _trace(probes: ProbeRunner | None) -> list[ProbeCall]:
    return list(probes.calls) if probes is not None else []


def _apply_derived_defaults(plan: Plan) -> Plan:
    """Fill in choices the planner was not offered, from field metadata.

    Only `bin_scale` needs this. Leaving it at its `linear` default would resurrect the
    exact failure the log scale exists to prevent: enrollment spans 0 to over a million,
    so equal-width bins put nearly every trial in the first bar and produce a chart that
    is arithmetically correct and useless.

    The registry already knows which fields are heavy-tailed, so the answer is read from
    `FieldSpec.skewed` rather than guessed — a new skewed numeric field gets the right
    binning without anyone remembering to come back here.
    """
    if plan.bins is None or plan.group_by is None:
        return plan
    spec = FIELDS.get(plan.group_by)
    if spec is not None and spec.skewed and plan.bin_scale is BinScale.LINEAR:
        return plan.model_copy(update={"bin_scale": BinScale.LOG})
    return plan


def build_system_prompt() -> str:
    """The planner's static context, derived from the field registry."""
    return f"""\
You turn a question about clinical trials into an analysis Plan. You never see trial
records and you never state a number: deterministic code computes every value from
ClinicalTrials.gov data after you have chosen what to compute.

Produce a Plan using ONLY the fields below. A field not in this list does not exist.

LEGAL FIELDS
{_field_lines()}

Temporal fields: {_kind_list(FieldKind.TEMPORAL)}
Numeric fields: {_kind_list(FieldKind.NUMERIC)}
Entity fields (open vocabulary, high cardinality): {_kind_list(FieldKind.ENTITY)}
Categorical fields (small closed vocabulary): {_kind_list(FieldKind.CATEGORICAL)}

RULES
- legs: one per population being compared. "Compare A vs B" is TWO legs with distinct
  labels, one shared group_by. Never use series_by together with multiple legs.
- group_by: the chart's x axis. Null means a single headline number.
- series_by: a second dimension. Mutually exclusive with multiple legs.
- metric: count | distinct_count | sum | median. sum/median require a numeric
  metric_field; distinct_count requires distinct_of. Prefer median over sum for
  enrollment, which is heavily skewed.
- granularity: required when group_by is temporal; use "year" unless the question is
  clearly about quarters.
- top_n: required in practice for entity group_by, which can have tens of thousands of
  values. 10 is a sensible default.
- bins (2-50) with bin_scale "log": required when group_by is numeric. Use log for
  enrollment.
- layout "point": a scatter of one dot per trial; group_by is the x measure and
  metric_field the y measure, both numeric.
- filters: apply the user's constraints. Prefer intervention for drugs, condition for
  diseases, sponsor for organisations. Use start_year_min/max for date ranges.
  If the question points at something without naming it — "this drug", "the sponsor",
  "that condition" — leave that filter **null**. Never copy the phrase in as a value: the
  registry would be searched for the literal string "this drug" and match nothing. Where
  the question *does* name a value, always record it: your plan is the only statement of
  what the question asked for.
- assumptions: state any interpretation you made that a careful reader would want to
  check — which field you read a term as, a date range you inferred, a default you chose.

PROBES
You may call up to {PROBE_BUDGET} probe tools before committing, and they are the only way
you learn anything about the data. Probes return counts, never trial records. Use them when
the answer changes your plan:
- an entity you are unsure of: probe_count with it as intervention, then as condition.
  Zero means it does not resolve there.
- an entity group_by: field_values, to see whether it needs top_n and how large a tail.
- a field that may be sparsely reported in this slice: fill_rate before grouping on it.
- a slice that may be huge: probe_count, since above 100,000 the chart becomes a sample.
Do not probe to confirm something the schema already tells you, and do not copy a probe
result into the Plan as if it were an answer — probes shape the plan, they are not the
output.

Return only the Plan."""


def build_user_prompt(request: AnalyzeRequest) -> str:
    """The question plus the caller's structured parameters.

    The planner is told them so it plans against the slice that will actually be fetched:
    its probes run on the plan's own filters, so a planner ignorant of `drug_name` probes
    the whole corpus and calibrates granularity, bins and top_n to a population nobody
    asked about.

    It is not *trusted* with them. `apply_overrides` pins them onto every leg afterwards,
    deterministically — the earlier design put them here and nowhere else, so a model that
    ignored them produced a chart without them while the response still reported them as
    applied.

    Detecting a **contradiction** between the question and a parameter is deliberately not
    done here. Told the value, the planner adopts it and the disagreement vanishes from the
    plan; the judge sees the question and the plan together and is the stage that catches
    it — see `judge.SYSTEM_PROMPT` class 7.
    """
    overrides = request.overrides()
    parts = [f"QUESTION: {request.query}"]
    if overrides:
        parts.append(
            "STRUCTURED PARAMETERS the caller set explicitly. Plan against them — they are "
            "applied to every leg after you finish, so a plan built on a different slice "
            f"will be calibrated to the wrong population:\n"
            f"{json.dumps(overrides, indent=2, default=str)}"
        )
    return "\n\n".join(parts)


def build_repair_prompt(attempt: PlanAttempt, rejected: list[Plan]) -> str:
    """Feedback for a rejected plan: the errors verbatim, plus what not to repropose."""
    if attempt.plan is None:
        body = f"Your response could not be read as a Plan: {attempt.failure}"
    else:
        body = (
            "That plan was rejected by the validator:\n"
            + "\n".join(f"  - {e}" for e in attempt.errors)
        )
    already = "\n".join(
        f"  - {p.model_dump_json(exclude_defaults=True)}" for p in rejected
    )
    return (
        f"{body}\n\nDo not repropose any of these:\n{already}\n\n"
        f"Return a corrected Plan."
    )


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


async def plan_query(
    client: LLMClient,
    request: AnalyzeRequest,
    *,
    max_revisions: int = MAX_REVISIONS,
    probes: ProbeRunner | None = None,
    extra_context: str = "",
) -> PlanningResult:
    """Produce a validated plan for a request.

    Args:
        client: The provider client. The planner uses the large tier — this is the one
            decision in the system where model quality changes what gets computed.
        request: The caller's question and any structured overrides.
        max_revisions: Revisions after the first attempt.
        probes: Gives the planner read access to aggregate counts. Optional: without it
            the planner works from the schema alone, which is legal but blind — it cannot
            tell whether a drug name resolves or how many buckets a grouping produces.
        extra_context: Appended to the first prompt. Carries the judge's concerns on a
            re-plan, so the second attempt starts from the objection rather than
            rediscovering it.

    Returns:
        A `PlanningResult` whose `plan` has passed `validate_plan`, or whose `contested`
        flag is set when the loop was exhausted and the least-bad plan was committed.

    Raises:
        PlanningError: if no attempt produced even a schema-valid plan.
    """
    system = build_system_prompt()
    user = build_user_prompt(request)
    if extra_context:
        user = f"{user}\n\n{extra_context}"
    attempts: list[PlanAttempt] = []
    rejected: list[Plan] = []

    # Anthropic's schema-size limits force a narrower planner vocabulary; see
    # NARROW_SCHEMA_FIELDS for what is withheld and why nothing is lost by it.
    drop = (
        NARROW_SCHEMA_FIELDS
        if client.settings.provider is Provider.ANTHROPIC
        else frozenset()
    )
    tools = probe_tool_specs() if probes is not None else None
    executor = probes.run if probes is not None else None

    for revision in range(max_revisions + 1):
        prompt = user
        if revision:
            prompt = f"{user}\n\n{build_repair_prompt(attempts[-1], rejected)}"

        try:
            plan = await client.complete(
                system=system,
                user=prompt,
                schema=Plan,
                tier=Tier.LARGE,
                max_tokens=2048,
                drop=drop,
                tools=tools,
                executor=executor,
            )
        except LLMError as exc:
            # A schema-invalid response is a normal, recoverable step in this loop: the
            # error becomes feedback like any validator error would.
            log.warning("planner attempt %d unusable: %s", revision + 1, exc)
            attempts.append(PlanAttempt(plan=None, errors=[], failure=str(exc)))
            continue

        plan = _apply_derived_defaults(plan)
        errors = validate_plan(plan)
        attempts.append(PlanAttempt(plan=plan, errors=errors))
        if not errors:
            return PlanningResult(plan=plan, attempts=attempts, probes=_trace(probes))

        log.info("planner attempt %d rejected: %s", revision + 1, "; ".join(errors))
        rejected.append(plan)

    usable = [a for a in attempts if a.plan is not None]
    if not usable:
        raise PlanningError(
            f"the planner produced no usable plan in {len(attempts)} attempt(s): "
            f"{attempts[-1].failure if attempts else 'no attempts made'}"
        )

    # Exhausted, so commit the closest fit and say so. Failing closed here would turn a
    # nearly-right chart into no answer at all, which serves nobody.
    best = min(usable, key=lambda a: a.score)
    assert best.plan is not None
    return PlanningResult(
        plan=best.plan, attempts=attempts, contested=True, probes=_trace(probes)
    )


__all__ = [
    "MAX_REVISIONS",
    "NARROW_SCHEMA_FIELDS",
    "plan_and_review",
    "PlanAttempt",
    "PlanningError",
    "PlanningResult",
    "build_repair_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "plan_query",
]


async def plan_and_review(
    client: LLMClient,
    request: AnalyzeRequest,
    *,
    judge: bool = True,
    max_revisions: int = MAX_REVISIONS,
    probes: ProbeRunner | None = None,
) -> PlanningResult:
    """Plan, then have the plan reviewed, then re-plan once if the reviewer objects.

    The judge is advisory and bounded to a single re-plan, per `plan.md` §3. A reviewer
    that can veto is a second planner with no repair loop of its own; a reviewer that can
    trigger unlimited revisions turns one disagreement into a loop.

    The re-planned result is committed **whether or not** the reviewer would still object,
    because asking again would be the start of that loop. What the reviewer said is kept on
    the result either way, so a reader can see the objection even when it was not resolved.
    """
    from cheiron.llm.judge import concern_feedback, review

    result = await plan_query(
        client, request, max_revisions=max_revisions, probes=probes
    )
    if not judge:
        return result

    verdict = await review(
        client, request.query, result.plan, result.probes, request.overrides()
    )
    result.review = verdict
    if verdict.is_contradiction:
        # Fatal, and raised here rather than carried forward: the caller's question and the
        # caller's parameters state different things, so no plan satisfies both and a
        # re-plan would spend a revision arriving at the same place. See judge class 7.
        raise OverrideConflict(list(verdict.concerns))
    if not verdict.is_concerned:
        return result

    log.info("judge raised concerns: %s", "; ".join(verdict.concerns))
    revised = await plan_query(
        client,
        request,
        max_revisions=max_revisions,
        probes=probes,
        extra_context=concern_feedback(verdict),
    )
    revised.attempts = result.attempts + revised.attempts
    revised.review = verdict
    revised.revised_after_review = True
    return revised
