"""The pipeline: one question in, one response envelope out.

Everything before this module is a stage tested in isolation. This is where they run in
order, and it is deliberately thin — the sequence is the design, and the design is in
`plan.md` §2:

    route ─ chit-chat / unsupported ──► early response, zero API calls
      │ question
      ▼
    plan ↔ probes ─► validate ─► judge ─► one re-plan if concerned
      │
      ▼
    compile ─► fetch ─► normalize ─► aggregate ─► invariants
      │
      ▼
    viz rules ─► chart selector (within the legal set) ─► assemble
      │
      ▼
    response envelope

**Four response types, one shape.** `conversational` is the only one with a null
visualization. `unsupported` and `no_results` carry a full visualization block with empty
data and the reason in `meta.warnings`, so a frontend renders one shape always and never
branches into a separate empty state.

**Nothing here computes a number.** Every value in the response was folded by the
aggregator and reconciled by the invariant check before this module saw it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cheiron.agg.aggregator import aggregate
from cheiron.ctgov.client import CtGovClient
from cheiron.ctgov.retrieval import retrieve
from cheiron.llm.client import LLMClient
from cheiron.llm.planner import PlanningError, plan_and_review
from cheiron.llm.probes import ProbeRunner
from cheiron.llm.router import Intent, route
from cheiron.llm.selector import select
from cheiron.schemas.request import AnalyzeRequest, apply_overrides
from cheiron.schemas.response import (
    AnalyzeResponse,
    Encoding,
    Meta,
    NetworkData,
    ProbeCall,
    RecordCounts,
    ResponseType,
    Review,
    Visualization,
    VizConfig,
    VizType,
)
from cheiron.viz.assembler import assemble
from cheiron.viz.rules import describe_shape

log = logging.getLogger(__name__)


@dataclass
class Deps:
    """What the pipeline needs. Injected so a test can supply fakes for either half."""

    llm: LLMClient
    ctgov: CtGovClient


def _empty_visualization(title: str) -> Visualization:
    """The block `unsupported` and `no_results` still carry.

    A frontend that has to branch on "did I get a visualization" ends up with two render
    paths and one of them is always the less-tested one. Returning the same shape with
    empty data costs a few bytes and removes that branch.
    """
    return Visualization(
        type=VizType.KPI,
        title=title,
        encoding=Encoding(),
        data=[],
        config=VizConfig(),
    )


def _meta(
    interpretation: str,
    *,
    warnings: list[str] | None = None,
    suggested: list[dict[str, object]] | None = None,
    elapsed_ms: int | None = None,
    provider: str | None = None,
) -> Meta:
    """Build the `meta` block for a response that never reached retrieval.

    Conversational and unsupported answers carry the same envelope as any other, so the
    frontend has one shape to render. `record_counts` is left null rather than zeroed: no
    records were fetched, which is a different fact from having fetched none.
    """
    return Meta(
        interpretation=interpretation,
        warnings=warnings or [],
        suggested_requests=suggested or [],
        record_counts=RecordCounts(matched=0, retrieved=0, used=0),
        generated_at=datetime.now(UTC).isoformat(),
        elapsed_ms=elapsed_ms,
        llm_provider=provider,
    )


async def analyze(deps: Deps, request: AnalyzeRequest) -> AnalyzeResponse:
    """Answer one request.

    Raises nothing the caller must handle: every failure that can be explained becomes a
    response with a reason. The exception is a bug in the deterministic core, which raises
    `InvariantError` and is meant to — a reconciliation failure means the chart would be
    wrong, and `plan.md` is explicit that this system fails loudly rather than quietly.
    """
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    provider = deps.llm.settings.provider.value

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    # ① Router — the only stage that can end a request before any API call.
    verdict = await route(deps.llm, request.query)

    if verdict.intent == Intent.CONVERSATIONAL:
        return AnalyzeResponse(
            request_id=request_id,
            response_type=ResponseType.CONVERSATIONAL,
            answer=verdict.reply or "I chart clinical trials data from ClinicalTrials.gov.",
            visualization=None,  # the one and only case with no visualization
            meta=_meta(
                "Handled as conversation; no data was retrieved.",
                elapsed_ms=elapsed(),
                provider=provider,
            ),
        )

    if verdict.intent == Intent.UNSUPPORTED:
        # A refusal that names the obstruction and offers a postable alternative is a
        # redirect. One that just says no makes the reader guess what would work.
        return AnalyzeResponse(
            request_id=request_id,
            response_type=ResponseType.UNSUPPORTED,
            answer=verdict.reason or "The registry does not record what this question asks for.",
            visualization=_empty_visualization("Not answerable from registration data"),
            meta=_meta(
                verdict.reason or "Out of scope for ClinicalTrials.gov registration records.",
                warnings=[verdict.reason] if verdict.reason else [],
                suggested=[
                    request.model_copy(update={"query": s}).model_dump(exclude_defaults=True)
                    for s in verdict.suggestions[:3]
                ],
                elapsed_ms=elapsed(),
                provider=provider,
            ),
        )

    # ② Planner ↔ probes, ③ Judge, and the one permitted re-plan.
    probes = ProbeRunner(deps.ctgov)
    try:
        planned = await plan_and_review(deps.llm, request, probes=probes)
        # Applied here rather than asked of the planner: the plan above is the model's
        # reading of the *question*, so pinning the caller's parameters onto it is both
        # deterministic and the only point at which the two can be compared. A
        # contradiction raises and becomes a 422 — see `apply_overrides`.
        plan, override_notes = apply_overrides(planned.plan, request)
        planned.plan = plan
    except PlanningError as exc:
        return AnalyzeResponse(
            request_id=request_id,
            response_type=ResponseType.UNSUPPORTED,
            answer="This question could not be expressed as an analysis this system can run.",
            visualization=_empty_visualization("No plan could be produced"),
            meta=_meta(
                str(exc),
                warnings=[str(exc)],
                elapsed_ms=elapsed(),
                provider=provider,
            ),
        )

    # Query compiler → API client → normalizer.
    # An upstream failure is **not** an empty result, and used to be reported as one.
    # `no_results` is the machine-readable statement "the registry holds no trials matching
    # this", so a 502 came back as an answer: a frontend branching on `response_type` would
    # render "no diabetes trials found" for an outage. Observed live — five queries in one
    # sweep hit 502/429 and every one of them claimed an empty slice. The error propagates
    # instead, and the HTTP layer maps it to a 502.
    retrieval = await retrieve(deps.ctgov, planned.plan)

    # Aggregator + invariant check. An InvariantError here is deliberately not caught.
    result = aggregate(
        planned.plan, retrieval.records_by_leg, prior_exclusions=retrieval.exclusions
    )

    # ④ Chart selector, constrained to the set the rules already permit.
    shape = describe_shape(planned.plan, result)
    chart = await select(deps.llm, request.query, shape)

    response = assemble(
        planned.plan,
        result,
        retrieval,
        query=request.query,
        preference=chart,
        request_id=request_id,
        elapsed_ms=elapsed(),
    )
    _attach_agent_trace(response, planned, provider, request, override_notes)
    return response


def _attach_agent_trace(
    response: AnalyzeResponse,
    planned: object,
    provider: str,
    request: AnalyzeRequest,
    override_notes: list[str],
) -> None:
    """Record how the plan was reached, so the answer can be audited rather than trusted.

    The probe trace, the judge's verdict and the contested flag all describe *how* the
    system decided what to compute. None of them affect a value — but a reader who cannot
    see them has to take the chart on faith.
    """
    meta = response.meta
    meta.llm_provider = provider
    # Reported as assumptions, not merely echoed. The old behaviour listed the overrides in
    # `filters_applied` beside the filters that had actually been used, so a response could
    # show `condition: melanoma` and `overrides: {condition: glioblastoma}` side by side and
    # claim both. They are now the same thing by construction.
    meta.assumptions = [*override_notes, *meta.assumptions]
    if supplied := request.overrides():
        meta.filters_applied = {**meta.filters_applied, "parameters": supplied}

    if request.include_planning_trace:
        meta.planning_trace = [
            ProbeCall(tool=call.tool, args=call.args, result=call.result)
            for call in getattr(planned, "probes", [])
        ]

    if getattr(planned, "contested", False):
        meta.warnings.extend(getattr(planned, "warnings", []))

    review = getattr(planned, "review", None)
    if review is not None:
        # Recorded whether or not it objected. An approval that leaves no trace is
        # indistinguishable from a reviewer that never ran, which is the wrong default in
        # a system whose case rests on its audit trail.
        meta.review = Review(
            verdict=review.verdict,
            concerns=list(getattr(review, "concerns", []) or []),
            revised=bool(getattr(planned, "revised_after_review", False)),
        )
    if review is not None and getattr(review, "concerns", None):
        prefix = (
            "The plan was revised after review."
            if getattr(planned, "revised_after_review", False)
            else "A reviewer raised concerns that were not acted on."
        )
        meta.warnings.append(f"{prefix} {' '.join(review.concerns)}")

    if not request.include_citations:
        # Citations live on each datum now, so switching them off means clearing them
        # there. The datums themselves stay: the chart is unaffected by whether its
        # evidence was requested.
        data = response.visualization.data if response.visualization else None
        if isinstance(data, NetworkData):
            for edge in data.edges:
                edge.citations = []
        elif data:
            for datum in data:
                datum.citations = []


__all__ = ["Deps", "analyze"]
