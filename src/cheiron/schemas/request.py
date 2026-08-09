"""The request schema.

The assignment requires `query` and permits candidate-defined structured fields. The
structured fields here are deliberately a *narrow* set: each one maps to a filter the
ClinicalTrials.gov API can push down server-side, or to a documented local filter. They
exist to remove ambiguity from the natural-language query, not to become a second,
parallel query language.

Override precedence, and conflict
---------------------------------
A structured field **supplies** what the query leaves open, and is applied to every leg
deterministically after planning — not by asking the model nicely. The assignment's own
example is the intended shape: `{"query": "How has the number of trials for this drug
changed over time?", "drug_name": "Pembrolizumab"}`, where the query names no drug.

When the query and a structured field say **different** things on the same dimension, the
request is rejected with 422 rather than resolved. "melanoma trials" with
`condition="glioblastoma"` is not a precedence question, it is a contradiction, and
silently honouring one of the two would produce a chart the caller did not ask for.

Three stages touch a parameter, and the split is deliberate:

* The **planner is told** it, so its probes run on the slice that will actually be fetched.
  A planner ignorant of `drug_name` probes the whole corpus and calibrates granularity,
  bin count and `top_n` to a population nobody asked about.
* `apply_overrides` **applies** it, because telling is not enforcing. See that function.
* The **judge** adjudicates contradiction (failure class 7, the one fatal verdict), because
  it is the only stage that reads the question and the plan together.

Withholding the parameters from the planner was tried and reverted: it made contradictions
detectable without a judge, at the cost of planning against a slice nobody asked about.
Recorded in `docs/decisions.md` under "Parameters in the planner prompt".

Nothing here is required except `query`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from cheiron.schemas.plan import Plan

# These mirror `/studies/enums` exactly. They are duplicated as Python enums so that
# FastAPI can generate a real JSON Schema for `/schema` and reject bad input at the edge
# with a 422 rather than passing it to the planner. `tests/test_enums_current.py` asserts
# they still match the live endpoint.


class Phase(StrEnum):
    NA = "NA"
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"


class Status(StrEnum):
    """Trial-level overall status.

    Distinct from *site*-level recruiting status, which is a different question and a
    different clause: measured on NSCLC, `AREA[OverallStatus]RECRUITING` matches 1,295
    trials where the nested site-level form matches 2,107. The gap is trials whose overall
    status is not RECRUITING but which still carry a recruiting site.
    """

    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class StudyType(StrEnum):
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPANDED_ACCESS = "EXPANDED_ACCESS"


class SponsorClass(StrEnum):
    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    INDIV = "INDIV"
    INDUSTRY = "INDUSTRY"
    NETWORK = "NETWORK"
    AMBIG = "AMBIG"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class InterventionType(StrEnum):
    BEHAVIORAL = "BEHAVIORAL"
    BIOLOGICAL = "BIOLOGICAL"
    COMBINATION_PRODUCT = "COMBINATION_PRODUCT"
    DEVICE = "DEVICE"
    DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"
    DIETARY_SUPPLEMENT = "DIETARY_SUPPLEMENT"
    DRUG = "DRUG"
    GENETIC = "GENETIC"
    PROCEDURE = "PROCEDURE"
    RADIATION = "RADIATION"
    OTHER = "OTHER"


#: Lower bound on any year the caller may supply. The registry's first records predate
#: this, but dates before it are almost always data-entry errors.
MIN_YEAR = 1900
MAX_YEAR = 2100


#: Request fields that are execution options rather than filters.
_NON_FILTER_FIELDS = frozenset({"query", "include_citations", "include_planning_trace"})

#: Request field -> the `Filters` field it pins. Two request fields deliberately carry
#: names from the assignment's own example rather than the internal vocabulary:
#: `drug_name` is an intervention search, and `start_year`/`end_year` bound the *start*
#: date on both sides (there is no separate completion-year filter here).
OVERRIDE_TO_FILTER: dict[str, str] = {
    "drug_name": "intervention",
    "condition": "condition",
    "sponsor": "sponsor",
    "country": "country",
    "phase": "phase",
    "status": "status",
    "study_type": "study_type",
    "sponsor_class": "sponsor_class",
    "intervention_type": "intervention_type",
    "start_year": "start_year_min",
    "end_year": "start_year_max",
    "enrollment_min": "enrollment_min",
    "enrollment_max": "enrollment_max",
}


class OverrideConflict(ValueError):
    """The query and a structured field disagree on the same dimension.

    Raised rather than resolved. A caller who asks about melanoma while pinning
    `condition="glioblastoma"` has contradicted themselves, and either answer would be a
    chart they did not ask for — so neither is produced. Surfaces as HTTP 422.
    """

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__(
            "The question and the structured parameters disagree: "
            + "; ".join(conflicts)
            + ". Remove one side, or make them agree."
        )


class AnalyzeRequest(BaseModel):
    """Body of `POST /analyze` and `POST /plan`."""

    model_config = ConfigDict(
        extra="forbid",  # a typo'd field name is an error, not a silently ignored key
        json_schema_extra={
            "examples": [
                {
                    "query": "How has the number of trials for this drug changed per "
                    "year since 2015?",
                    "drug_name": "Pembrolizumab",
                    "start_year": 2015,
                },
                {
                    "query": "Compare phases for melanoma trials run by industry vs academia",
                },
                {
                    "query": "Show a network of sponsors and drugs for glioblastoma trials",
                    "condition": "Glioblastoma",
                },
            ]
        },
    )

    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description="Natural-language question about clinical trials. Required.",
        ),
    ]

    # --- entity overrides: each maps to a server-side ct.gov search parameter ---------
    drug_name: str | None = Field(
        None,
        max_length=200,
        description="Intervention/drug name. Maps to `query.intr`. Matching is fuzzy and "
        "synonym-expanded by ClinicalTrials.gov, so related agents may be included.",
    )
    condition: str | None = Field(
        None,
        max_length=200,
        description="Condition or disease. Maps to `query.cond`. Also synonym-expanded.",
    )
    sponsor: str | None = Field(
        None,
        max_length=200,
        description="Sponsor or collaborator organisation name. Maps to `query.spons`.",
    )
    country: str | None = Field(
        None,
        max_length=100,
        description="Country name as ClinicalTrials.gov spells it (e.g. 'United States', "
        "'Korea, Republic of'). Maps to a nested location filter so that site-level "
        "status is respected.",
    )

    # --- categorical overrides: validated against the live enum vocabulary ------------
    phase: list[Phase] | None = Field(
        None,
        description="Restrict to these phases. Multiple values are OR-ed.",
    )
    status: list[Status] | None = Field(
        None,
        description="Restrict to these overall statuses. Multiple values are OR-ed.",
    )
    study_type: StudyType | None = Field(
        None, description="Restrict to interventional, observational, or expanded access."
    )
    sponsor_class: SponsorClass | None = Field(
        None, description="Restrict by lead sponsor class (e.g. INDUSTRY, NIH)."
    )
    intervention_type: InterventionType | None = Field(
        None, description="Restrict by intervention type (e.g. DRUG, DEVICE)."
    )

    # --- range overrides --------------------------------------------------------------
    start_year: int | None = Field(
        None,
        ge=MIN_YEAR,
        le=MAX_YEAR,
        description="Earliest trial start year, inclusive. Pushed down as a date range.",
    )
    end_year: int | None = Field(
        None,
        ge=MIN_YEAR,
        le=MAX_YEAR,
        description="Latest trial start year, inclusive.",
    )
    enrollment_min: int | None = Field(
        None, ge=0, description="Minimum enrollment count. Applied locally after fetch."
    )
    enrollment_max: int | None = Field(
        None, ge=0, description="Maximum enrollment count. Applied locally after fetch."
    )

    # --- execution options ------------------------------------------------------------
    # `max_records` was removed rather than shipped inert. It was documented as an upper
    # bound on records fetched, and the client never read it: retrieval is governed by a
    # fixed 20-page cap, so a caller asking for 500 got 20,000 and no indication their
    # parameter was ignored. A knob that looks effective and is not is worse than no knob.
    include_citations: bool = Field(
        True,
        description="Emit each datum's `citations`. Disabling it reduces payload size but "
        "does not change any chart value — the datums themselves are unaffected.",
    )
    include_planning_trace: bool = Field(
        True,
        description="Include the planner's probe calls and results in `meta.planning_trace`.",
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> AnalyzeRequest:
        """Reject range pairs and empty lists at the edge, before the planner sees them.

        An inverted range or an empty `phase: []` would compile to a clause matching
        nothing, and the response would report the filter as applied — a chart of zero
        trials that looks like an answer rather than a bad request.
        """
        if self.start_year is not None and self.end_year is not None:
            if self.start_year > self.end_year:
                raise ValueError(
                    f"start_year ({self.start_year}) must not exceed end_year ({self.end_year})"
                )
        if self.enrollment_min is not None and self.enrollment_max is not None:
            if self.enrollment_min > self.enrollment_max:
                raise ValueError(
                    f"enrollment_min ({self.enrollment_min}) must not exceed "
                    f"enrollment_max ({self.enrollment_max})"
                )
        for name in ("phase", "status"):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"{name} must be omitted or non-empty, not an empty list")
        return self

    def overrides(self) -> dict[str, object]:
        """The structured fields the caller actually supplied, by request field name."""
        return {
            k: v
            for k, v in self.model_dump(exclude_none=True).items()
            if k not in _NON_FILTER_FIELDS
        }


def apply_overrides(plan: Plan, request: AnalyzeRequest) -> tuple[Plan, list[str]]:
    """Pin the caller's structured parameters onto every leg that is silent about them.

    Deterministic on purpose. The planner *is* told the parameters — it must plan against
    the slice that will actually be fetched — but it is not trusted to apply them. The
    earlier design put them in the prompt and nowhere else, so a model that ignored them
    produced a chart without them while `meta.filters_applied` still listed them as
    applied: the response claiming a filter it had not used.

    Rules, per dimension:

    * the leg says nothing → the parameter is applied, and reported as an assumption.
    * the leg agrees (case- and order-insensitively) → nothing to do.
    * the leg says something else → **left alone**, and noted. The planner saw the
      parameter and still chose differently, which is right for a comparison: "pembrolizumab
      vs nivolumab" with `drug_name="Pembrolizumab"` plans two legs, and overwriting the
      second would collapse the comparison into the same population twice.

    Contradiction between the question and a parameter is *not* judged here. It needs the
    question, which this function does not have — the judge reads both and raises
    `OverrideConflict` itself (class 7).
    """
    supplied = request.overrides()
    if not supplied:
        return plan, []

    notes: list[str] = []
    legs = [leg.model_copy(deep=True) for leg in plan.legs]

    for field, value in supplied.items():
        target = OVERRIDE_TO_FILTER[field]
        kept: list[str] = []
        for leg in legs:
            current = getattr(leg.filters, target)
            if current is None:
                setattr(leg.filters, target, value)
            elif not _same(current, value):
                kept.append(leg.label)
        notes.append(f"{target} pinned to {_render(value)} by the {field} parameter.")
        if kept:
            notes.append(
                f"{target} was left as the plan set it on leg(s) {', '.join(kept)}: the "
                f"planner distinguished them on that field, and forcing the {field} "
                f"parameter there would merge them into one population."
            )
    return plan.model_copy(update={"legs": legs}), notes


def _same(current: object, value: object) -> bool:
    """Whether a planner-derived filter and an override mean the same thing.

    Case- and order-insensitive: a caller typing `condition="Melanoma"` against a planner
    that wrote `"melanoma"` has not contradicted anything, and refusing that would make
    the parameters unusable in practice.
    """
    if isinstance(current, list) or isinstance(value, list):
        return _as_set(current) == _as_set(value)
    return str(getattr(current, "value", current)).casefold() == str(
        getattr(value, "value", value)
    ).casefold()


def _as_set(value: object) -> set[str]:
    items = value if isinstance(value, list) else [value]
    return {str(getattr(x, "value", x)).casefold() for x in items}


def _render(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(getattr(v, "value", v)) for v in value)
    return str(getattr(value, "value", value))
