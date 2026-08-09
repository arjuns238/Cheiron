"""The HTTP surface.

Five endpoints, per `plan.md` §1. Two of them exist to make the system inspectable without
spending anything:

* `POST /plan` runs the agent layer and stops — no retrieval, no chart. It shows what the
  planner decided and which probes it ran, which is the cheapest way to see the reasoning.
* `GET /capabilities` and `GET /schema` are **generated from the field registry and the
  Pydantic models**, never hand-written. A hand-maintained capability list drifts from the
  code the first time a field is added, and then documents a system that no longer exists.

The app owns two long-lived clients and closes them on shutdown. Constructing an
`httpx.AsyncClient` per request would discard connection reuse and, more importantly, the
rate limiter — whose whole job is to remember what it did a moment ago.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cheiron.agg.aggregator import InvariantError
from cheiron.ctgov.client import BASE_URL, MAX_PAGES, CtGovClient
from cheiron.llm.client import LLMError, LLMSettings, build_client
from cheiron.llm.planner import plan_and_review
from cheiron.llm.probes import PROBE_BUDGET, ProbeRunner
from cheiron.pipeline import Deps, analyze
from cheiron.schemas.fields import FIELDS
from cheiron.schemas.plan import Metric, Plan
from cheiron.schemas.request import AnalyzeRequest, OverrideConflict
from cheiron.schemas.response import (
    AnalyzeResponse,
    CapabilitiesResponse,
    HealthResponse,
    VizType,
)

log = logging.getLogger(__name__)

VERSION = "0.1.0"

STATIC_DIR = Path(__file__).parent / "static"
#: The captured runs live at the repo root, outside the package. Resolved rather than
#: packaged because they are demo material, not something the service depends on: if the
#: directory is absent the UI still works and simply offers no saved examples.
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"

#: Stated in `/capabilities` and the README. Copied from `plan.md` §4 — these are questions
#: the registry cannot answer, not chart types the system lacks.
#: Two different kinds of limit, kept apart on purpose. Conflating them once led this
#: system to refuse questions it could answer, for reasons that were not true.
LIMITATIONS = [
    # --- the registry does not hold it ---------------------------------------------
    "Comparative efficacy ('which drug works better') — posted results exist, but each "
    "sponsor defines its own endpoints, units and analysis windows. Measured: 25 melanoma "
    "trials with results carried 157 outcome measures under 144 distinct titles in 34 "
    "units. There is no comparable field meaning 'worked better'.",
    "Individual participants — records are aggregate, at trial level and (with posted "
    "results) arm level. Aggregate demographics are available; per-person data is not.",
    "Enrolment attributed to a place — enrolment is recorded once per trial, not per site, "
    "so a multi-country trial has no per-country figure.",
    "Sponsor and intervention names are free text and are not deduplicated; the same "
    "organisation or agent can appear under several spellings.",
    # --- this version does not read it ---------------------------------------------
    "Not implemented: outcome measures from resultsSection. Participant flow, adverse "
    "events and baseline demographics ARE read; outcome measures are excluded because they "
    "are not comparable across trials, not because they are unavailable.",
    "Not implemented: semantic search over eligibility criteria. The text is in the API; "
    "it is simply not indexed here.",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the clients once, and fail at startup if the provider is misconfigured.

    A missing API key surfacing on the first request looks like a broken query rather than
    a broken deployment, so it is checked here instead.

    `.env` is loaded here rather than left to the caller. Nothing in `uvicorn` reads it, so
    a `.env` sitting next to the code would be silently ignored and the service would refuse
    to start with a key that is right there — which reads as a broken build, not a missing
    export. Real environment variables still win: `load_dotenv` does not override them.
    """
    load_dotenv()
    settings = LLMSettings.from_env()
    app.state.http = httpx.AsyncClient(timeout=120)
    app.state.deps = Deps(llm=build_client(settings), ctgov=CtGovClient(app.state.http))
    log.info("cheiron ready — provider=%s", settings.provider.value)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Cheiron",
    version=VERSION,
    description=(
        "Turns a natural-language question about clinical trials into a visualization "
        "specification backed by live ClinicalTrials.gov data. Every charted value is "
        "computed by deterministic code folding over source records; the language models "
        "choose what to compute and how to display it, never what a value is."
    ),
    lifespan=lifespan,
)


@app.exception_handler(InvariantError)
async def _invariant_failed(request: Any, exc: InvariantError) -> JSONResponse:
    """A reconciliation failure means the chart would be wrong.

    Returned as a 500 rather than a chart with a caveat. `plan.md`'s core invariant is that
    this system fails loudly rather than shipping a plausible number, and that has to hold
    at the HTTP boundary too.
    """
    log.error("invariant failure: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "invariant_failure",
            "detail": str(exc),
            "message": (
                "The record counts did not reconcile, so the chart would have been wrong. "
                "No result is returned rather than an unverified one."
            ),
        },
    )


@app.exception_handler(OverrideConflict)
async def _override_conflict(request: Any, exc: OverrideConflict) -> JSONResponse:
    """The question and the structured parameters disagree, so neither is guessed at.

    422 rather than a chart: honouring either side would answer a question the caller did
    not ask, and there is no basis for choosing between two things they stated themselves.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": "override_conflict",
            "detail": exc.conflicts,
            "message": str(exc),
        },
    )


@app.post("/analyze", response_model=AnalyzeResponse, response_model_exclude_none=False)
async def analyze_endpoint(request: AnalyzeRequest) -> AnalyzeResponse:
    """Answer a question with a visualization specification."""
    try:
        return await analyze(app.state.deps, request)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM provider unavailable: {exc}") from exc


@app.post("/plan")
async def plan_endpoint(request: AnalyzeRequest) -> dict[str, Any]:
    """Return the committed plan without retrieving anything.

    Costs the planner, the probes and the judge — no chart, and no page fetches. Useful for
    seeing what the agent layer decided, and for testing planning changes cheaply.
    """
    probes = ProbeRunner(app.state.deps.ctgov)
    try:
        planned = await plan_and_review(app.state.deps.llm, request, probes=probes)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM provider unavailable: {exc}") from exc

    review = planned.review
    return {
        "plan": planned.plan.model_dump(exclude_none=True),
        "attempts": len(planned.attempts),
        "contested": planned.contested,
        "revised_after_review": planned.revised_after_review,
        "review": {
            "verdict": getattr(review, "verdict", None),
            "concerns": getattr(review, "concerns", []),
        },
        "probes": [{"tool": c.tool, "args": c.args, "result": c.result} for c in planned.probes],
        "warnings": planned.warnings,
    }


@app.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    """What this system can express — generated from the field registry.

    A hand-written capability list is wrong the first time someone adds a field. Deriving
    it means the answer cannot drift from what the planner is actually allowed to do.
    """
    return CapabilitiesResponse(
        fields=[
            {
                "key": key,
                "kind": spec.kind.value,
                "label": spec.label,
                "multi": spec.multi,
                "groupable": spec.groupable,
                "measurable": spec.measurable,
                "filterable": spec.filterable,
                "note": spec.note,
            }
            for key, spec in FIELDS.items()
        ],
        metrics=[m.value for m in Metric],
        viz_types=[v.value for v in VizType],
        max_legs=6,
        limitations=[
            *LIMITATIONS,
            f"Retrieval stops after {MAX_PAGES * 1000:,} records per leg; beyond that a "
            f"chart is a sample and `meta.record_counts.truncated` says so.",
            f"The planner may run at most {PROBE_BUDGET} probes per attempt.",
        ],
    )


@app.get("/schema")
async def schema() -> dict[str, Any]:
    """JSON Schema for the request, the plan and the response.

    Generated from the Pydantic models, so a frontend engineer implements against what the
    service actually validates rather than against prose describing it.
    """
    return {
        "request": AnalyzeRequest.model_json_schema(),
        "plan": Plan.model_json_schema(),
        "response": AnalyzeResponse.model_json_schema(),
        "citation_offsets": (
            "Citation offsets index into the source record serialized as "
            'json.dumps(record, separators=(",", ":"), ensure_ascii=False). '
            "Rebuild that string to verify any excerpt by hand."
        ),
    }


# --------------------------------------------------------------------------------------
# Demo UI
#
# A mock frontend, and the assignment's stated bonus. It is deliberately a *client* of the
# documented response envelope and nothing else: it reads `visualization.encoding` to learn
# which key holds the dimension, `visualization.type` to pick a renderer, and `citations`
# to show provenance. It never reaches into the pipeline, so if it can render a chart, a
# real frontend engineer can too — which is the claim `/schema` is making.
# --------------------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    """Serve the demo frontend."""
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="UI assets are not installed.")
    return FileResponse(index)


@app.get("/examples", include_in_schema=False)
async def examples_index() -> list[dict[str, Any]]:
    """List the captured runs, so the UI is demoable with no API key and no spend."""
    index = EXAMPLES_DIR / "index.json"
    if not index.is_file():
        return []
    return json.loads(index.read_text())


@app.get("/examples/{slug}", include_in_schema=False)
async def example(slug: str) -> dict[str, Any]:
    """Return one captured run, verbatim as `/analyze` produced it."""
    # `slug` indexes a fixed directory listing rather than being joined into a path, so a
    # traversal attempt finds nothing to match rather than escaping the directory.
    known = {p.stem: p for p in EXAMPLES_DIR.glob("*.json") if p.name != "index.json"}
    path = known.get(slug)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No captured example named {slug!r}.")
    return json.loads(path.read_text())


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness, plus whether the two dependencies are actually reachable."""
    reachable = False
    try:
        response = await app.state.http.get(f"{BASE_URL}/version", timeout=10)
        reachable = response.status_code == 200
    except httpx.HTTPError:
        reachable = False

    configured = bool(getattr(app.state, "deps", None))
    return HealthResponse(
        status="ok" if (reachable and configured) else "degraded",
        ctgov_reachable=reachable,
        llm_configured=configured,
        version=VERSION,
    )


__all__ = ["LIMITATIONS", "VERSION", "app"]
