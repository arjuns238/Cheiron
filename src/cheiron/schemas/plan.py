"""The `Plan` schema and its deterministic validator.

A `Plan` is the entire contract between the LLM layer and the deterministic core. It is
the constrained vocabulary that makes the core invariant enforceable: the planner may
choose *what to compute*, expressed only in terms of this schema, and the deterministic
code below the plan decides *what the values are*.

Consequences of that framing, which explain some choices that would otherwise look odd:

* There is no free-text escape hatch. No raw Essie expression, no arbitrary field path.
  A plan that cannot be expressed here is a plan the system refuses, loudly.
* `metric` is a closed set of four folds, not an expression language.
* Comparisons are `legs`, not a separate planning path. "Compare A vs B" is two legs with
  one shared `group_by`, merged into a series dimension. Every comparison example in the
  assignment appendix falls out of this without a sub-planner.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from cheiron.schemas.fields import FIELDS, FieldKind
from cheiron.schemas.request import (
    InterventionType,
    Phase,
    SponsorClass,
    Status,
    StudyType,
)


class Metric(StrEnum):
    """The four folds the aggregator can perform over a bucket.

    Each is a pure function of a list of `(nct_id, value)` pairs. Nothing else is allowed,
    because anything else would need an expression evaluator, and an expression evaluator
    is where an LLM would start influencing numbers.
    """

    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    SUM = "sum"
    MEDIAN = "median"


class Granularity(StrEnum):
    YEAR = "year"
    QUARTER = "quarter"


class Layout(StrEnum):
    """Whether a datum is a bucket of trials or a single trial.

    Everything in this system is an aggregation except one chart: a scatter plot asks
    "how do these two measures relate across trials", and collapsing trials into buckets
    would destroy exactly the relationship being asked about.

    `POINT` does not weaken the core invariant. A point is still a fold over a bucket —
    the bucket simply contains one trial — so the value is still produced by deterministic
    code and still carries the citation that justifies it.

    `COOCCURRENCE` builds a network from a single multi-valued field by pairing the values
    a trial carries: drug↔drug, condition↔condition. Each pair is a bucket whose value is
    the number of trials containing both, so an edge weight is a fold over its own trial
    list exactly like a bar. This is a new enum member rather than a new `Plan` field on
    purpose — Anthropic's structured-output schema is at exactly its 24-optional-parameter
    ceiling, and an added field would break planning on that provider.
    """

    AGGREGATE = "aggregate"
    POINT = "point"
    COOCCURRENCE = "cooccurrence"


class BinScale(StrEnum):
    """How a numeric range is divided for a histogram.

    `LOG` exists because the registry's numeric fields are not merely skewed, they are
    pathologically so: enrollment runs from 0 to over 1.1 million with a median in the
    low hundreds. Equal-width bins over that range put essentially every trial in the
    first bin and render a chart that is technically correct and useless.
    """

    LINEAR = "linear"
    LOG = "log"


class Sort(StrEnum):
    VALUE_DESC = "value_desc"
    VALUE_ASC = "value_asc"
    DIMENSION_ASC = "dimension_asc"


class DateCertainty(StrEnum):
    """Which date records to keep.

    `ACTUAL` excludes both estimated *and* unrecorded certainty; `ANY` keeps everything.
    The three-way split exists because `startDateStruct.type` is absent on many older
    records, so "not actual" and "estimated" are genuinely different populations.
    """

    ANY = "any"
    ACTUAL_ONLY = "actual_only"
    EXCLUDE_ESTIMATED = "exclude_estimated"


class Filters(BaseModel):
    """One leg's filter set.

    Split into pushdown filters (translated into the ClinicalTrials.gov request) and local
    filters (applied after fetch because the API has no equivalent). The split is not
    cosmetic: local filters make `retrieved` and `used` diverge, and both numbers are
    reported in `meta.record_counts`.
    """

    model_config = ConfigDict(extra="forbid")

    # --- pushdown -------------------------------------------------------------------
    condition: str | None = Field(None, description="→ query.cond")
    intervention: str | None = Field(None, description="→ query.intr")
    sponsor: str | None = Field(None, description="→ query.spons")
    free_text: str | None = Field(None, description="→ query.term")
    country: str | None = Field(
        None,
        description="→ nested SEARCH[Location] filter, so that site-level status is "
        "respected rather than trial-level status.",
    )
    site_status: list[Status] | None = Field(
        None,
        description="Status of the site itself, only meaningful together with `country`. "
        "Distinct from `status`, which is the trial's overall status.",
    )
    status: list[Status] | None = Field(None, description="→ filter.overallStatus")
    phase: list[Phase] | None = Field(None, description="→ AREA[Phase]")
    start_year_min: int | None = Field(None, description="→ AREA[StartDate]RANGE lower bound")
    start_year_max: int | None = Field(None, description="→ AREA[StartDate]RANGE upper bound")

    # --- local ----------------------------------------------------------------------
    study_type: StudyType | None = None
    sponsor_class: SponsorClass | None = None
    intervention_type: InterventionType | None = None
    enrollment_min: int | None = None
    enrollment_max: int | None = None
    has_results: bool | None = None
    date_certainty: DateCertainty = DateCertainty.ANY

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True, exclude_defaults=True)


class Leg(BaseModel):
    """One arm of a comparison, or the sole population of a simple query."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=80)] = Field(
        description="Series label shown to the user, e.g. 'Pembrolizumab'. Must be "
        "distinct across legs; it becomes the series dimension."
    )
    filters: Filters = Field(default_factory=Filters)


class Plan(BaseModel):
    """The committed analysis plan."""

    model_config = ConfigDict(extra="forbid")

    legs: Annotated[list[Leg], Field(min_length=1, max_length=6)]
    group_by: str | None = Field(
        None, description="A flattener output key. Null means a single aggregate (a KPI)."
    )
    series_by: str | None = Field(
        None,
        description="A second dimension. Mutually exclusive with multiple legs — legs "
        "*become* the series.",
    )
    metric: Metric = Metric.COUNT
    metric_field: str | None = Field(None, description="Required for sum and median.")
    distinct_of: str | None = Field(None, description="Required for distinct_count.")
    granularity: Granularity | None = None
    layout: Layout = Field(
        Layout.AGGREGATE,
        description="'point' emits one datum per trial for a scatter plot; group_by is "
        "the x measure and metric_field the y measure. Everything else aggregates.",
    )
    bins: int | None = Field(
        None,
        ge=2,
        le=50,
        description="Number of histogram bins. Requires a numeric group_by.",
    )
    bin_scale: BinScale = Field(
        BinScale.LINEAR,
        description="Bin edge spacing. Use 'log' for enrollment and other heavily "
        "right-skewed measures, where linear bins collapse into one bar.",
    )
    top_n: int | None = Field(None, ge=1, le=100)
    sort: Sort = Sort.VALUE_DESC
    viz_hint: str | None = Field(
        None, description="Advisory only. The viz rules decide legality regardless."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Plain-language notes on how the question was interpreted. Echoed "
        "into meta.assumptions verbatim.",
    )


# --------------------------------------------------------------------------------------
# Validator
#
# Returns a list of human-readable error strings, handed back to the planner *verbatim* as
# feedback on a rejected plan. They are therefore written to be actionable by a model:
# each says what was wrong and what would be acceptable instead.
# --------------------------------------------------------------------------------------

_NUMERIC_KINDS = {FieldKind.NUMERIC}
_ENTITYISH_KINDS = {FieldKind.ENTITY, FieldKind.CATEGORICAL}


def validate_plan(plan: Plan) -> list[str]:
    """Check a plan against the rules in `plan.md` §3. Empty list means the plan is legal."""
    errors: list[str] = []

    def check_field(value: str | None, slot: str, *, allow: set[FieldKind] | None = None) -> None:
        if value is None:
            return
        field = FIELDS.get(value)
        if field is None:
            errors.append(
                f"{slot}={value!r} is not a known field. Legal fields: {', '.join(FIELDS)}"
            )
            return
        if allow is not None and field.kind not in allow:
            kinds = ", ".join(sorted(k.value for k in allow))
            errors.append(
                f"{slot}={value!r} is a {field.kind.value} field, but {slot} requires one "
                f"of: {kinds}"
            )

    # 1. every field reference resolves
    check_field(plan.group_by, "group_by")
    check_field(plan.series_by, "series_by")
    check_field(plan.distinct_of, "distinct_of")

    # 2. sum / median require a numeric metric_field
    is_point = plan.layout is Layout.POINT
    if plan.metric in (Metric.SUM, Metric.MEDIAN):
        if plan.metric_field is None:
            errors.append(
                f"metric={plan.metric.value!r} requires metric_field to be set to a numeric "
                f"field, e.g. 'enrollment'"
            )
        else:
            check_field(plan.metric_field, "metric_field", allow=_NUMERIC_KINDS)
    elif is_point:
        # In point layout metric_field is the y measure rather than something to fold, so
        # it is required regardless of `metric`.
        pass
    elif plan.metric_field is not None:
        errors.append(
            f"metric_field={plan.metric_field!r} is only meaningful for metric 'sum' or "
            f"'median', not {plan.metric.value!r}"
        )

    # 3. distinct_count requires distinct_of
    if plan.metric is Metric.DISTINCT_COUNT and plan.distinct_of is None:
        errors.append("metric='distinct_count' requires distinct_of to name the field counted")
    if plan.metric is not Metric.DISTINCT_COUNT and plan.distinct_of is not None:
        errors.append(
            f"distinct_of={plan.distinct_of!r} is only meaningful for metric "
            f"'distinct_count', not {plan.metric.value!r}"
        )

    # 4. granularity requires a temporal group_by
    if plan.granularity is not None:
        field = FIELDS.get(plan.group_by or "")
        if field is None or not field.is_temporal:
            errors.append(
                f"granularity={plan.granularity.value!r} requires a temporal group_by "
                f"(one of: {', '.join(k for k, f in FIELDS.items() if f.is_temporal)}), "
                f"got group_by={plan.group_by!r}"
            )
    elif plan.group_by and FIELDS.get(plan.group_by, None) and FIELDS[plan.group_by].is_temporal:
        errors.append(
            f"group_by={plan.group_by!r} is temporal and requires an explicit granularity "
            f"of 'year' or 'quarter'"
        )

    # 5. top_n requires a high-cardinality (entity or categorical) group_by
    if plan.top_n is not None:
        field = FIELDS.get(plan.group_by or "")
        if field is None or field.kind not in _ENTITYISH_KINDS:
            errors.append(
                f"top_n={plan.top_n} requires group_by to be an entity or categorical "
                f"field, got group_by={plan.group_by!r}"
            )

    # 6. the two dimensions must differ
    if plan.group_by is not None and plan.group_by == plan.series_by:
        errors.append(
            f"group_by and series_by are both {plan.group_by!r}; they must be different "
            f"dimensions"
        )

    # 7. series_by and multiple legs are mutually exclusive
    if plan.series_by is not None and len(plan.legs) > 1:
        errors.append(
            f"series_by={plan.series_by!r} conflicts with {len(plan.legs)} legs. Legs "
            f"become the series dimension; use one or the other, not both."
        )

    # 8. leg labels must be distinct and usable as series labels
    labels = [leg.label for leg in plan.legs]
    if len(set(labels)) != len(labels):
        errors.append(f"leg labels must be distinct, got {labels}")

    # 9. a network needs two entity dimensions to form nodes on
    if plan.viz_hint == "network":
        dims = [d for d in (plan.group_by, plan.series_by) if d is not None]
        non_entity = [d for d in dims if d not in FIELDS or not FIELDS[d].is_entity]
        if len(dims) != 2 or non_entity:
            errors.append(
                "viz_hint='network' requires group_by and series_by to both be entity "
                f"fields (one of: {', '.join(k for k, f in FIELDS.items() if f.is_entity)}), "
                f"got group_by={plan.group_by!r}, series_by={plan.series_by!r}"
            )

    # 10. histogram binning applies to numeric measures only
    if plan.bins is not None:
        if is_point:
            errors.append("bins is a histogram setting and does not apply to layout='point'")
        else:
            check_field(plan.group_by, "group_by", allow=_NUMERIC_KINDS)
            if plan.group_by is None:
                errors.append(
                    f"bins={plan.bins} requires a numeric group_by to divide, e.g. 'enrollment'"
                )
    elif plan.group_by and FIELDS.get(plan.group_by) and FIELDS[plan.group_by].is_numeric:
        # Without bins a numeric grouping would emit one bucket per distinct value —
        # thousands of single-trial bars, which is not a chart.
        if not is_point:
            errors.append(
                f"group_by={plan.group_by!r} is numeric and requires bins to be set (2-50), "
                f"otherwise every distinct value becomes its own bucket"
            )

    # 11. a scatter needs two numeric measures, one per axis
    if is_point:
        check_field(plan.group_by, "group_by", allow=_NUMERIC_KINDS)
        check_field(plan.metric_field, "metric_field", allow=_NUMERIC_KINDS)
        if plan.group_by is None or plan.metric_field is None:
            errors.append(
                "layout='point' requires group_by (the x measure) and metric_field (the y "
                f"measure) to both be numeric fields, one of: "
                f"{', '.join(k for k, f in FIELDS.items() if f.is_numeric)}"
            )
        if plan.granularity is not None:
            errors.append("layout='point' has no time axis, so granularity does not apply")
        if plan.top_n is not None:
            errors.append("layout='point' plots trials individually, so top_n does not apply")
        if plan.series_by is not None and FIELDS.get(plan.series_by, None) is not None:
            if FIELDS[plan.series_by].kind not in _ENTITYISH_KINDS:
                errors.append(
                    f"series_by={plan.series_by!r} colours the points and must therefore be "
                    f"a categorical or entity field"
                )

    # 12. a co-occurrence network pairs one field's values with each other
    if plan.layout is Layout.COOCCURRENCE:
        field = FIELDS.get(plan.group_by or "")
        if field is None or not field.is_entity or not field.multi:
            multi_entities = ", ".join(
                k for k, f in FIELDS.items() if f.is_entity and f.multi and f.groupable
            )
            errors.append(
                f"layout='cooccurrence' pairs a field's values with each other, so "
                f"group_by must be a multi-valued entity field (one of: {multi_entities}), "
                f"got group_by={plan.group_by!r}"
            )
        if plan.series_by is not None:
            errors.append(
                "layout='cooccurrence' derives both endpoints from group_by, so series_by "
                "does not apply"
            )
        if plan.metric is not Metric.COUNT:
            errors.append(
                f"layout='cooccurrence' weighs an edge by how many trials contain both "
                f"endpoints, so metric must be 'count', not {plan.metric.value!r}"
            )
        if plan.granularity is not None or plan.bins is not None:
            errors.append("layout='cooccurrence' has no axis, so granularity and bins do not apply")

    # 13. the plan must actually narrow or split something
    if plan.group_by is None and all(leg.filters.is_empty() for leg in plan.legs):
        errors.append(
            "plan has neither a group_by nor any filters, so it would aggregate the entire "
            "registry into one number; add a filter or a grouping dimension"
        )

    return errors
