"""Rules deciding which chart types are *legal* for a result.

The division of labour here is the point, and it is what keeps an LLM from producing a
misleading chart. Rules decide legality; the model decides preference within whatever the
rules allow. A model asked to pick from `[line, bar]` can be wrong about taste. It cannot
put a time series on a pie chart, because the pie was never in the set.

Legality is computed **after** aggregation, never before, because it depends on facts that
do not exist until the data comes back: how many buckets there are, whether the dimension
turned out to be high-cardinality, whether any series survived. A plan that looked like a
tidy six-bar chart can return four hundred buckets.

Every rule below is a row of the table in `plan.md` §3. The one deviation: `plan.md` §3
lists a plain `area` chart for single-series temporal data, but the response envelope in
§1 — and the frozen `VizType` enum — has only `stacked_area`. The envelope wins, since it
is what a frontend implements against, so single-series temporal offers `[line, bar]`.
"""

from __future__ import annotations

from dataclasses import dataclass

from cheiron.agg.aggregator import OTHER, AggregationResult
from cheiron.schemas.fields import FIELDS, FieldKind
from cheiron.schemas.plan import Layout, Metric, Plan
from cheiron.schemas.response import VizType

#: Above this many buckets a categorical axis stops being readable and the chart is
#: treated as high-cardinality: bar only, sorted, and normally topped-N. Twelve is the
#: figure in `plan.md` §3 and is roughly where a pie chart's slices become unreadable.
MAX_CATEGORICAL_BUCKETS = 12


@dataclass(frozen=True)
class Shape:
    """What the aggregation actually produced.

    This is also exactly what the chart-selector LLM is shown. It is deliberately a
    description of the result's *shape* and never its values: bucket counts, dimension
    kinds, label samples. The model choosing the chart has no way to read a number off the
    data, so it has no way to put one in the output.
    """

    group_kind: FieldKind | None
    series_kind: FieldKind | None
    metric: Metric
    layout: Layout
    binned: bool
    bucket_count: int
    series_count: int
    has_other: bool
    sample_labels: tuple[str, ...]
    group_field: str | None = None
    series_field: str | None = None

    @property
    def has_series(self) -> bool:
        return self.series_count > 1 or self.series_kind is not None


def describe_shape(plan: Plan, result: AggregationResult) -> Shape:
    """Summarize an aggregation for the rules and for the chart selector."""
    dimensions = result.dimensions
    return Shape(
        group_kind=FIELDS[plan.group_by].kind if plan.group_by else None,
        series_kind=FIELDS[plan.series_by].kind if plan.series_by else None,
        metric=plan.metric,
        layout=plan.layout,
        binned=plan.bins is not None,
        bucket_count=len(dimensions),
        series_count=len(result.series),
        has_other=OTHER in dimensions,
        sample_labels=tuple(dimensions[:8]),
        group_field=plan.group_by,
        series_field=plan.series_by,
    )


def legal_charts(shape: Shape) -> tuple[VizType, ...]:
    """The chart types that would honestly render this result.

    The first element is the default: what is returned when no chart selector runs, and
    what a failed or out-of-set selection falls back to. Order is therefore meaningful —
    it encodes which chart is the safest reading of the shape, not merely which is allowed.
    """
    # A scatter is not an aggregation, so it short-circuits everything below.
    if shape.layout is Layout.POINT:
        return (VizType.SCATTER,)

    # A co-occurrence result is already a set of edges; nothing else renders it honestly.
    if shape.layout is Layout.COOCCURRENCE:
        return (VizType.NETWORK,)

    # Binned numeric data is a distribution. A bar chart of bins is a histogram; calling
    # it anything else would invite the frontend to reorder the bins.
    if shape.binned:
        return (VizType.HISTOGRAM,)

    if shape.group_kind is None:
        return (VizType.KPI,)

    if shape.group_kind is FieldKind.TEMPORAL:
        if shape.has_series:
            return (VizType.STACKED_AREA, VizType.GROUPED_BAR, VizType.LINE)
        return (VizType.LINE, VizType.BAR)

    # Two entity dimensions crossed by a count is a co-occurrence matrix, which is a
    # network. This is the highest-value visualization in the assignment and it falls
    # straight out of the bucket structure: an edge's weight is the bucket's own count.
    if (
        shape.group_kind is FieldKind.ENTITY
        and shape.series_kind is FieldKind.ENTITY
        and shape.metric is Metric.COUNT
    ):
        return (VizType.NETWORK, VizType.GROUPED_BAR)

    # Geography gets a map, but always with a bar chart behind it: country coverage in the
    # registry is uneven enough that a choropleth alone can mislead.
    if shape.group_field == "countries" and not shape.has_series:
        return (VizType.CHOROPLETH, VizType.BAR)

    if shape.has_series:
        return (VizType.GROUPED_BAR, VizType.STACKED_BAR)

    if shape.group_kind is FieldKind.CATEGORICAL and shape.bucket_count <= MAX_CATEGORICAL_BUCKETS:
        # A pie is only honest for a partition of a whole. An "Other" bucket is fine, but a
        # multi-valued dimension whose slices sum past 100% is not.
        if shape.group_field and FIELDS[shape.group_field].multi:
            return (VizType.BAR,)
        return (VizType.BAR, VizType.PIE)

    # Entity dimensions and long categorical tails: a sorted bar chart and nothing else.
    return (VizType.BAR,)


def is_legal(chart: VizType, shape: Shape) -> bool:
    return chart in legal_charts(shape)


def choose(shape: Shape, preference: VizType | str | None = None) -> VizType:
    """Resolve a preference against the legal set, falling back to the default.

    The chart selector cannot produce an illegal chart — the worst it can do is fail to be
    heeded and get the rule's own default. That property is what makes it safe to let a
    model choose at all, so the check lives here rather than in the caller.
    """
    legal = legal_charts(shape)
    if preference is None:
        return legal[0]
    try:
        candidate = VizType(preference)
    except ValueError:
        return legal[0]
    return candidate if candidate in legal else legal[0]


__all__ = [
    "MAX_CATEGORICAL_BUCKETS",
    "Shape",
    "choose",
    "describe_shape",
    "is_legal",
    "legal_charts",
]
