"""The request schema.

The assignment requires `query` and permits candidate-defined structured fields. The
structured fields here are deliberately a *narrow* set: each one maps to a filter the
ClinicalTrials.gov API can push down server-side, or to a documented local filter. They
exist to remove ambiguity from the natural-language query, not to become a second,
parallel query language.

Override precedence
-------------------
A structured field, when present, **overrides** whatever the planner infers from the
natural-language query for that same dimension, and the override is reported in
`meta.assumptions`. The planner is told the overrides up front so it can plan around
them rather than fight them. Rationale: the caller typed the structured field
deliberately and it is unambiguous; the natural-language phrasing is neither.

Nothing here is required except `query`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    max_records: int = Field(
        5000,
        ge=1,
        le=20000,
        description="Upper bound on records fetched. If the matching set is larger the "
        "result is a sample and `meta.record_counts.truncated` is true.",
    )
    include_citations: bool = Field(
        True,
        description="Emit the top-level citations map. Disabling it reduces payload size "
        "but does not change any chart value.",
    )
    include_planning_trace: bool = Field(
        True,
        description="Include the planner's probe calls and results in `meta.planning_trace`.",
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> AnalyzeRequest:
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
        """The structured fields the caller actually supplied.

        Returned to the planner as hard constraints and echoed into
        `meta.filters_applied` so the response states exactly what was pinned.
        """
        return {
            k: v
            for k, v in self.model_dump(exclude_none=True).items()
            if k not in {"query", "max_records", "include_citations", "include_planning_trace"}
        }
