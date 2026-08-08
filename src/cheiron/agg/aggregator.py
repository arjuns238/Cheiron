"""Fold normalized records into chart-ready buckets.

This module is where the core invariant is actually enforced. Everything above it — the
router, the planner, the judge — decides *what* to compute; this decides *what the value
is*, and it does so by folding over a list of source records that it keeps.

The one structure everything hangs off is:

    bucket -> [ Contribution(nct_id, value, field_path, field_value), ... ]

The chart value is a fold over that list, and the citations for the datum are the first
few elements of the same list. They cannot disagree, because they are one object. A
post-hoc "now go find some trials that support this bar" lookup is exactly the failure
mode this shape exists to make impossible.

Three rules govern the accounting, and the invariant check at the bottom of the module is
what stops them from being aspirational:

1. **Every retrieved record is accounted for.** It either contributes to a bucket or it is
   counted into `excluded_by_reason` under a reason naming the field that was missing.
   `used + sum(excluded) == retrieved`, checked, raised on.

2. **Absence is excluded, not invented.** A record with no value for the grouping dimension
   does not become a zero, a blank bucket, or an "Unknown" bar; it leaves the chart and is
   counted. The exception is `phases`, where the registry deliberately records "does not
   apply" and the normalizer has already turned that into a real value.

3. **Legs are populations, not partitions.** A trial matching two legs of a comparison
   contributes to both, because each bar means "trials involving X" and a combination
   study genuinely involves both. The overlap is detected and reported rather than being
   silently resolved by leg order.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from cheiron.ctgov.normalizer import (
    COMBINATION_SEPARATOR,
    NormalizedRecord,
    date_quarter,
    date_year,
)
from cheiron.schemas.fields import FIELDS, FieldSpec, spec
from cheiron.schemas.plan import BinScale, Granularity, Layout, Metric, Plan, Sort

#: Label for the bucket that absorbs everything past `top_n`. Held as a constant because
#: the spec assembler, the warnings, and the sort all need to special-case it.
OTHER = "Other"


def missing_reason(field_key: str) -> str:
    """The `excluded_by_reason` key for a record lacking `field_key`.

    Built from the field name rather than drawn from a closed enum, because the reasons are
    per-field by construction and a fixed enum would have to be edited every time the field
    registry grows. `plan.md`'s own example (`missing_start_date`) uses this shape.
    """
    return f"missing_{field_key}"


#: A year-only date cannot be placed in a quarter, and guessing Q1 would put a fabricated
#: spike at the start of every year. Such records are excluded from quarterly charts only.
IMPRECISE_FOR_QUARTER = "imprecise_date_for_quarter"

#: A trial with fewer than two values in the paired field contributes no edge. Counted
#: rather than dropped, because "how many trials had nothing to pair" is exactly the
#: question a sparse-looking network raises.
NO_COOCCURRING_VALUES = "no_cooccurring_values"

#: Fields whose pairing is restricted to agents sharing an arm group, rather than anything
#: co-listed in the trial. See `_cooccurrence_pairs`.
_ARM_SCOPED: dict[str, str] = {"intervention_names": "combination_groups"}

#: Node count a *suggested* threshold aims for. Networks are returned complete — see
#: `suggest_min_occurrences` — and this only calibrates the advice offered to a frontend.
LEGIBLE_NETWORK_NODES = 40

#: Ceiling on the suggested threshold. Past this the advice would be demanding so much
#: evidence per node that almost nothing survives, which is not useful advice.
MAX_OCCURRENCE_THRESHOLD = 50


@dataclass(frozen=True)
class Contribution:
    """One record's contribution to one bucket.

    Attributes:
        nct_id: The contributing trial.
        value: What the fold consumes. `None` for `count`, the distinct key for
            `distinct_count`, the numeric measure for `sum` and `median`.
        field_path: Dotted path of the value that put this trial in this bucket, recorded
            for the citation. Indexed (`...phases[0]`) when the value came from an array.
        field_value: The value at that path, as a string.
    """

    nct_id: str
    value: Any
    field_path: str
    field_value: str


@dataclass
class Bucket:
    """One datum of the eventual chart.

    `value` is filled by the fold. `contributions` is retained afterwards because the spec
    assembler reads it to build citations and the invariant check reads it to reconcile
    counts.
    """

    dimension: str
    series: str | None
    contributions: list[Contribution] = field(default_factory=list)
    value: float = 0.0
    #: Sort position for dimensions with an inherent order that their labels do not carry.
    #: A histogram bin labelled "100–999" must sort after "10–99", which no string
    #: comparison achieves. None means the label itself is the ordering.
    order: float | None = None
    #: The x coordinate, set only in point layout, where a datum is a trial rather than a
    #: bucket and therefore needs two numbers rather than one.
    point_x: float | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.dimension, self.series)

    @property
    def nct_ids(self) -> list[str]:
        """Distinct contributing trials, in first-seen order."""
        return list(dict.fromkeys(c.nct_id for c in self.contributions))


@dataclass
class AggregationResult:
    """Buckets plus the accounting that proves they are right."""

    buckets: list[Bucket] = field(default_factory=list)
    retrieved: int = 0
    used: int = 0
    excluded_by_reason: dict[str, int] = field(default_factory=dict)
    counting_semantics: str = ""
    warnings: list[str] = field(default_factory=list)
    #: Dimension labels collapsed into `Other`, so the warning can say how many.
    collapsed_dimensions: int = 0
    #: Trials matching more than one leg, counted once per extra membership.
    overlapping_trials: int = 0
    #: Networks only, and **advisory**: the smallest occurrence threshold that would yield
    #: a legible graph. Nothing is removed on account of it — see `suggest_min_occurrences`.
    suggested_min_occurrences: int | None = None
    #: Trials that contributed an edge but whose nodes were trimmed away by an explicit
    #: `top_n`. They remain in `record_counts` and appear in no node or edge, so the gap
    #: has to be stated rather than left for the reader to find.
    omitted_trials: int = 0

    @property
    def dimensions(self) -> list[str]:
        return list(dict.fromkeys(b.dimension for b in self.buckets))

    @property
    def series(self) -> list[str]:
        return list(dict.fromkeys(b.series for b in self.buckets if b.series is not None))


class InvariantError(AssertionError):
    """Raised when the accounting does not reconcile.

    This is deliberately an error and never a warning. Anything caught here means the
    chart about to be returned is wrong, and `plan.md` is explicit that the system fails
    loudly rather than shipping a plausible chart built on a bad aggregation.
    """


# --------------------------------------------------------------------------------------
# Co-occurrence
# --------------------------------------------------------------------------------------


def _cooccurrence_pairs(
    record: NormalizedRecord, field_key: str
) -> tuple[list[tuple[str, str, str]], str | None]:
    """Every unordered pair of values this trial contributes, and where they came from.

    **The pairing rule depends on the field, and the difference is a correctness one.**

    For `intervention_names` the pairs come from `combination_groups`, which pairs only
    agents sharing an arm group. Two drugs in the same *trial* are frequently the two sides
    of a comparison rather than a combination: over 500 melanoma trials, 217 co-list two or
    more agents but only 157 share an arm, so trial-level pairing would assert roughly a
    third more combinations than exist — including drug-versus-its-own-placebo edges.

    For every other field — `conditions`, `intervention_mesh` — co-occurrence within the
    trial *is* the relationship being asked about, and there is no arm structure to use.
    Those pair the whole list, and the assembler warns that co-listing is what was measured.

    Pairs are ordered within themselves so that (A, B) and (B, A) are one bucket, and
    deduplicated so a trial listing the same combination in two arms contributes one edge.
    """
    if source_key := _ARM_SCOPED.get(field_key):
        groups = [g.split(COMBINATION_SEPARATOR) for g in record.get(source_key) or []]
        path = FIELDS[source_key].source
    else:
        groups = [record.get(field_key) or []]
        path = FIELDS[field_key].source

    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for members in groups:
        distinct = sorted({m.strip() for m in members if m and m.strip()})
        for index, first in enumerate(distinct):
            for second in distinct[index + 1 :]:
                if (first, second) in seen:
                    continue
                seen.add((first, second))
                pairs.append((first, second, path))

    return (pairs, None) if pairs else ([], NO_COOCCURRING_VALUES)


def _is_network(plan: Plan) -> bool:
    """Whether this plan renders as a graph, by either route.

    Co-occurrence pairs one field with itself; a crossed pair of entity fields counted by
    trial is bipartite. Both produce edges, and both explode without bounding — 51,497
    distinct lead sponsors corpus-wide — so the same trimming applies to each.
    """
    if plan.layout is Layout.COOCCURRENCE:
        return True
    dimensions = (FIELDS.get(plan.group_by or ""), FIELDS.get(plan.series_by or ""))
    return (
        plan.metric is Metric.COUNT
        and all(d is not None and d.is_entity for d in dimensions)
    )


def _node_trials(result: AggregationResult) -> dict[str, set[str]]:
    """Distinct trials per node, across both endpoints of every edge."""
    trials: dict[str, set[str]] = defaultdict(set)
    for bucket in result.buckets:
        ids = set(bucket.nct_ids)
        trials[bucket.dimension] |= ids
        if bucket.series is not None:
            trials[bucket.series] |= ids
    return trials


def suggest_min_occurrences(result: AggregationResult) -> int | None:
    """The smallest "appears in at least N trials" threshold that would render legibly.

    **Advice, not policy — nothing is removed on account of this.** The graph is returned
    complete because thresholding is a presentation decision, and the most useful thing a
    client can do with a co-occurrence network is move the threshold interactively. That
    is how VOSviewer works, and it requires the client to hold the whole network.

    The advice is worth computing because the right threshold is not obvious and the
    default guess is wrong: roughly 80% of agents appear in exactly one trial (melanoma
    yields 1,425 nodes at ≥1 and 288 at ≥2), so an unfiltered graph is mostly nodes that
    say nothing about what co-occurs *frequently*. A client that starts here gets a
    principled filter instead of inventing one.

    Returns None when there is no useful advice to give: either the graph already renders
    whole, or every node occurs equally often so no threshold separates them. Suggesting
    "appears in at least 1 trial" would be advice that filters nothing.
    """
    trials = _node_trials(result)
    if not trials or len(trials) <= LEGIBLE_NETWORK_NODES:
        return None

    ceiling = min(MAX_OCCURRENCE_THRESHOLD, max(len(ids) for ids in trials.values()))
    if ceiling < 2:
        return None
    for threshold in range(2, ceiling + 1):
        if sum(1 for ids in trials.values() if len(ids) >= threshold) <= LEGIBLE_NETWORK_NODES:
            return threshold
    return ceiling


def _apply_network_top_n(result: AggregationResult, plan: Plan) -> None:
    """Keep the busiest `top_n` nodes and every edge between them.

    Applied only when a plan asks for it explicitly. `Other` is not available here: it is
    not an entity, so it has nothing to co-occur with. Trimming therefore removes nodes and
    every edge touching them — a half-edge would point at a node that is not in the graph.
    """
    assert plan.top_n is not None
    trials = _node_trials(result)
    if len(trials) <= plan.top_n:
        return

    keep = {
        node
        for node, _ in sorted(trials.items(), key=lambda kv: (-len(kv[1]), kv[0]))[: plan.top_n]
    }
    before = {c.nct_id for b in result.buckets for c in b.contributions}
    result.collapsed_dimensions = len(trials) - len(keep)
    result.buckets = [
        b for b in result.buckets if b.dimension in keep and (b.series or "") in keep
    ]
    after = {c.nct_id for b in result.buckets for c in b.contributions}
    result.omitted_trials = len(before - after)


# --------------------------------------------------------------------------------------
# Histogram binning
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bins:
    """Bin edges for a numeric histogram, derived from the observed values.

    Edges come from the data rather than from the plan because the planner has never seen
    the data — it knows how many bins are wanted, not where a sensible boundary lies.

    Zero is held out into its own bin on a log scale. It is not a rounding artefact: a
    withdrawn trial genuinely enrolled nobody (NCT04193930 in the fixtures), and folding it
    into the lowest positive bin would misreport a real and meaningful population.
    """

    edges: tuple[float, ...]
    scale: BinScale
    zero_bucket: bool = False

    def label_for(self, value: float) -> tuple[str, float]:
        """The bin label and its sort position for one value."""
        if self.zero_bucket and value <= 0:
            return "0", -1.0
        for index in range(len(self.edges) - 1):
            low, high = self.edges[index], self.edges[index + 1]
            # The last bin is closed so the maximum value does not fall off the end.
            last = index == len(self.edges) - 2
            if low <= value < high or (last and value <= high):
                return f"{_num(low)}–{_num(high)}", float(index)
        return f"{_num(self.edges[-1])}+", float(len(self.edges))


def _num(value: float) -> str:
    """Render a bin edge without trailing zeros: 430, 4.6, 0.05."""
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _nice(value: float, mode: str = "round") -> float:
    """Round a bin edge to two significant figures.

    Log-spaced edges land on values like 94.87 and 432.67, which are arithmetically
    correct and unreadable on an axis. The edges themselves are rounded rather than only
    their labels, so that a trial enrolling 433 participants falls in the bin whose label
    says it does — rounding the label alone would put values outside the range they claim.

    The outermost edges round *outward* (`mode` of `floor` and `ceil`). Rounding the top
    edge to nearest would move it below the largest observed value, pushing that trial out
    of every bin — a silent loss, which is the one thing this system does not do.
    """
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    step = 10 ** (magnitude - 1)
    scaled = value / step
    if mode == "floor":
        return math.floor(scaled) * step
    if mode == "ceil":
        return math.ceil(scaled) * step
    return round(scaled) * step


def build_bins(values: list[float], count: int, scale: BinScale) -> Bins | None:
    """Compute bin edges over the observed values, or None if there is nothing to bin."""
    if not values:
        return None
    low, high = min(values), max(values)

    if scale is BinScale.LOG:
        positive = [v for v in values if v > 0]
        if not positive:
            return Bins(edges=(0.0, 0.0), scale=scale, zero_bucket=True)
        start, stop = math.log10(min(positive)), math.log10(max(positive))
        if stop == start:
            stop = start + 1
        step = (stop - start) / count
        edges = _rounded_edges([10 ** (start + step * i) for i in range(count + 1)])
        return Bins(edges=edges, scale=scale, zero_bucket=any(v <= 0 for v in values))

    if high == low:
        high = low + 1
    step = (high - low) / count
    return Bins(edges=_rounded_edges([low + step * i for i in range(count + 1)]), scale=scale)


def _rounded_edges(edges: list[float]) -> tuple[float, ...]:
    """Round edges for readability, keeping them strictly increasing.

    Rounding can collapse two adjacent edges into one — on a narrow range, 1.02 and 1.04
    both become 1.0 — which would silently delete a bin. When that happens the raw edges
    are kept: an ugly axis is better than a missing bucket.
    """
    rounded = [_nice(e) for e in edges]
    rounded[0] = _nice(edges[0], "floor")
    rounded[-1] = _nice(edges[-1], "ceil")
    if len(set(rounded)) != len(rounded) or any(
        b <= a for a, b in zip(rounded, rounded[1:], strict=False)
    ):
        return tuple(edges)
    return tuple(rounded)


# --------------------------------------------------------------------------------------
# Dimension extraction
# --------------------------------------------------------------------------------------


def _dimension_values(
    record: NormalizedRecord,
    field_key: str,
    granularity: Granularity | None,
    bins: Bins | None = None,
) -> tuple[list[tuple[str, str, str, float | None]], str | None]:
    """Resolve a record's bucket label(s) for one dimension.

    Returns `(values, exclusion_reason)`, where `values` is a list of
    `(label, field_path, field_value, order)` tuples — more than one only for a
    multi-valued field, where the trial genuinely belongs in several buckets. An empty list
    is always accompanied by a reason, so a record can never vanish without being counted.
    """
    field_spec = spec(field_key)
    raw = record.get(field_key)

    if field_spec.is_temporal:
        return _temporal_values(raw, field_spec, granularity)

    if bins is not None:
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return [], missing_reason(field_key)
        label, order = bins.label_for(float(raw))
        return [(label, field_spec.source, str(raw), order)], None

    if field_spec.multi:
        values = [v for v in (raw or []) if v]
        if not values:
            return [], missing_reason(field_key)
        return [
            (str(v), f"{field_spec.source}[{i}]", str(v), None) for i, v in enumerate(values)
        ], None

    if raw is None or raw == "":
        return [], missing_reason(field_key)
    return [(str(raw), field_spec.source, str(raw), None)], None


def _temporal_values(
    raw: Any,
    field_spec: FieldSpec,
    granularity: Granularity | None,
) -> tuple[list[tuple[str, str, str, float | None]], str | None]:
    """Bucket a partial date into a year or a quarter.

    Quarterly bucketing needs at least month precision. A year-only date is excluded under
    its own reason rather than the generic missing-field one, because it is a different
    problem with a different fix (ask for yearly granularity) and the distinction is
    visible to the user in `excluded_by_reason`.
    """
    if not raw:
        return [], missing_reason(field_spec.key)

    if granularity is Granularity.QUARTER:
        quarter = date_quarter(raw)
        if quarter is None:
            return [], IMPRECISE_FOR_QUARTER
        return [(quarter, field_spec.source, str(raw), None)], None

    year = date_year(raw)
    if year is None:
        return [], missing_reason(field_spec.key)
    return [(str(year), field_spec.source, str(raw), None)], None


def _measure(record: NormalizedRecord, plan: Plan) -> tuple[Any, str | None]:
    """The value this record contributes to the fold, or a reason it cannot contribute."""
    if plan.layout is Layout.POINT:
        # `metric_field` is the y coordinate rather than something to fold, so it is
        # required here whatever `metric` says.
        assert plan.metric_field is not None, "validator guarantees a y measure for points"
        raw = record.get(plan.metric_field)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, missing_reason(plan.metric_field)
        return float(raw), None

    if plan.metric is Metric.COUNT:
        return None, None

    field_key = plan.distinct_of if plan.metric is Metric.DISTINCT_COUNT else plan.metric_field
    assert field_key is not None, "validator guarantees the field is set for this metric"
    raw = record.get(field_key)

    if plan.metric is Metric.DISTINCT_COUNT:
        # A multi-valued distinct_of contributes every one of its values; "how many
        # distinct countries" means the union across trials, not one country per trial.
        values = [v for v in (raw or [])] if spec(field_key).multi else ([raw] if raw else [])
        if not values:
            return None, missing_reason(field_key)
        return values, None

    if raw is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None, missing_reason(field_key)
    return float(raw), None


# --------------------------------------------------------------------------------------
# Folds
#
# Each is a pure function of a bucket's contributions. This is the complete set of
# operations the system can perform on data; there is no expression evaluator, because an
# expression evaluator is where an LLM would start influencing numbers.
# --------------------------------------------------------------------------------------


def _fold(metric: Metric, contributions: list[Contribution]) -> float:
    match metric:
        case Metric.COUNT:
            # Distinct trials, not contributions. A trial that reached this bucket by two
            # routes is still one trial.
            return float(len({c.nct_id for c in contributions}))
        case Metric.DISTINCT_COUNT:
            distinct: set[Any] = set()
            for c in contributions:
                distinct.update(c.value or [])
            return float(len(distinct))
        case Metric.SUM:
            return float(sum(c.value for c in contributions if c.value is not None))
        case Metric.MEDIAN:
            values = [c.value for c in contributions if c.value is not None]
            return float(statistics.median(values)) if values else 0.0
    raise InvariantError(f"unhandled metric {metric!r}")


# --------------------------------------------------------------------------------------
# The aggregator
# --------------------------------------------------------------------------------------


def aggregate(
    plan: Plan,
    records_by_leg: dict[str, list[NormalizedRecord]],
    *,
    prior_exclusions: dict[str, int] | None = None,
) -> AggregationResult:
    """Fold per-leg normalized records into buckets according to `plan`.

    Args:
        plan: The committed plan. Assumed to have passed `validate_plan`; this function
            relies on that for invariants like "sum implies a numeric metric_field".
        records_by_leg: Records that survived normalization and local filtering, keyed by
            the leg label they were fetched for. A trial matching two legs appears under
            both, and is counted under both.
        prior_exclusions: Exclusion counts accumulated upstream (normalizer failures,
            local filters). Folded into the result so that `retrieved` reconciles against
            what the API actually returned rather than against what survived to here.

    Returns:
        An `AggregationResult` whose accounting has been checked by `check_invariants`.

    Raises:
        InvariantError: if the counts do not reconcile, which means the chart is wrong.
    """
    result = AggregationResult(excluded_by_reason=dict(prior_exclusions or {}))
    # `retrieved` means what the API returned, not what survived to here, so records the
    # normalizer or a local filter already dropped are seeded into the baseline. Without
    # this the reconciliation would be against a total that had quietly shrunk.
    result.retrieved = sum(result.excluded_by_reason.values())
    buckets: dict[tuple[str, str | None], Bucket] = {}
    multi_leg = len(records_by_leg) > 1

    def exclude(reason: str) -> None:
        result.excluded_by_reason[reason] = result.excluded_by_reason.get(reason, 0) + 1

    # Bin edges need the whole value distribution, so they are computed in a pre-pass over
    # every leg before any record is bucketed. Deriving them per leg would give the legs of
    # a comparison different x axes, which is not a comparison.
    bins: Bins | None = None
    if plan.bins is not None and plan.group_by is not None:
        observed = [
            float(v)
            for records in records_by_leg.values()
            for r in records
            if isinstance(v := r.get(plan.group_by), (int, float)) and not isinstance(v, bool)
        ]
        bins = build_bins(observed, plan.bins, plan.bin_scale)

    for leg_label, records in records_by_leg.items():
        # Legs become the series dimension; the validator guarantees `series_by` and
        # multiple legs are mutually exclusive, so at most one of these is in play.
        for record in records:
            result.retrieved += 1

            if plan.layout is Layout.COOCCURRENCE:
                assert plan.group_by is not None
                pairs, pair_missing = _cooccurrence_pairs(record, plan.group_by)
                if pair_missing:
                    exclude(pair_missing)
                    continue
                result.used += 1
                for first, second, path in pairs:
                    bucket = buckets.setdefault((first, second), Bucket(first, second))
                    bucket.contributions.append(
                        Contribution(
                            nct_id=record.nct_id,
                            value=None,
                            field_path=path,
                            field_value=f"{first}{COMBINATION_SEPARATOR}{second}",
                        )
                    )
                continue

            measure, measure_missing = _measure(record, plan)
            if measure_missing:
                exclude(measure_missing)
                continue

            if plan.group_by is None:
                dimension_values: list[tuple[str, str, str, float | None]] = [
                    ("All", "", "", None)
                ]
                dimension_missing = None
            else:
                dimension_values, dimension_missing = _dimension_values(
                    record, plan.group_by, plan.granularity, bins
                )
            if dimension_missing:
                exclude(dimension_missing)
                continue

            if plan.series_by is not None:
                series_values, series_missing = _dimension_values(
                    record, plan.series_by, None
                )
                if series_missing:
                    exclude(series_missing)
                    continue
                series_labels: list[str | None] = [label for label, _, _, _ in series_values]
            else:
                series_labels = [leg_label if multi_leg else None]

            result.used += 1

            if plan.layout is Layout.POINT:
                # One trial, one datum. The bucket still exists and still holds the
                # contribution that justifies the point, so a scatter point is as
                # traceable as a bar; it simply has an audience of one.
                x_label, x_path, x_value, _ = dimension_values[0]
                for series in series_labels:
                    bucket = buckets.setdefault(
                        (record.nct_id, series), Bucket(record.nct_id, series)
                    )
                    bucket.point_x = float(x_value)
                    bucket.contributions.append(
                        Contribution(
                            nct_id=record.nct_id,
                            value=measure,
                            field_path=x_path,
                            field_value=x_value,
                        )
                    )
                continue

            for label, path, value, order in dimension_values:
                for series in series_labels:
                    bucket = buckets.setdefault((label, series), Bucket(label, series))
                    if order is not None:
                        bucket.order = order
                    bucket.contributions.append(
                        Contribution(
                            nct_id=record.nct_id,
                            value=measure,
                            field_path=path,
                            field_value=value,
                        )
                    )

    result.buckets = list(buckets.values())
    for bucket in result.buckets:
        if plan.layout is Layout.POINT:
            # A one-trial bucket folds to that trial's y value. Still a fold, still
            # deterministic, still carrying its own citation.
            bucket.value = float(bucket.contributions[0].value)
        else:
            bucket.value = _fold(plan.metric, bucket.contributions)

    # Invariants are checked on the *aggregation*, before anything is trimmed for display.
    # Trimming deliberately drops buckets, so running the checks afterwards would flag
    # intentional presentation choices as lost records and mask the real thing they exist
    # to catch.
    check_invariants(plan, result)

    # Networks are returned complete. Thresholding a graph is a presentation decision, and
    # a client that holds the whole network can move the threshold interactively; one that
    # receives a pre-trimmed graph cannot get the rest back without another request. An
    # explicit `top_n` is still honoured, because that is a request rather than a default.
    if _is_network(plan):
        result.suggested_min_occurrences = suggest_min_occurrences(result)
        if plan.top_n is not None:
            _apply_network_top_n(result, plan)
    elif plan.top_n is not None:
        _apply_top_n(result, plan)
    _sort(result, plan)

    result.overlapping_trials = _count_overlap(records_by_leg)
    result.counting_semantics = _counting_semantics(plan, result)
    result.warnings = _warnings(plan, result)
    return result


def _apply_top_n(result: AggregationResult, plan: Plan) -> None:
    """Keep the top `top_n` dimensions and merge the rest into a single `Other` bucket.

    Ranking is on the dimension's total across all series, not per series. Ranking within
    each series independently would give the series different x axes, and a grouped bar
    chart whose categories differ per group is not a chart.

    The collapsed records are merged rather than dropped: `Other` holds their contributions,
    so it carries citations like any other bar and the count reconciliation still holds.
    """
    assert plan.top_n is not None
    totals: Counter[str] = Counter()
    for bucket in result.buckets:
        totals[bucket.dimension] += bucket.value

    if len(totals) <= plan.top_n:
        return

    keep = {dimension for dimension, _ in totals.most_common(plan.top_n)}
    result.collapsed_dimensions = len(totals) - len(keep)

    kept: list[Bucket] = []
    other: dict[str | None, Bucket] = {}
    for bucket in result.buckets:
        if bucket.dimension in keep:
            kept.append(bucket)
            continue
        merged = other.setdefault(bucket.series, Bucket(OTHER, bucket.series))
        merged.contributions.extend(bucket.contributions)

    for bucket in other.values():
        bucket.value = _fold(plan.metric, bucket.contributions)
    result.buckets = kept + list(other.values())


def _sort(result: AggregationResult, plan: Plan) -> None:
    """Order buckets for display, always leaving `Other` last.

    Temporal dimensions sort lexically, which is chronological for both `YYYY` and
    `YYYY-Qn`. `Other` is pinned to the end regardless of the requested sort, because it
    is a residue rather than a category and a reader expects it there.
    """
    totals: dict[str, float] = defaultdict(float)
    for bucket in result.buckets:
        totals[bucket.dimension] += bucket.value

    def sort_key(bucket: Bucket) -> tuple[Any, ...]:
        residue = bucket.dimension == OTHER
        if bucket.order is not None:
            # Histogram bins carry their own order because their labels do not: "100–999"
            # sorts before "10–99" under any string comparison, and reordering a
            # histogram's bins destroys the distribution it exists to show.
            return (residue, bucket.order, bucket.dimension, bucket.series or "")
        if plan.layout is Layout.POINT:
            # Points have no meaningful order; sorting by value would suggest a ranking
            # that a scatter plot is specifically not making.
            return (residue, bucket.point_x or 0.0, bucket.dimension, bucket.series or "")
        if plan.sort is Sort.DIMENSION_ASC:
            primary: Any = bucket.dimension
        elif plan.sort is Sort.VALUE_ASC:
            primary = totals[bucket.dimension]
        else:
            primary = -totals[bucket.dimension]
        return (residue, primary, bucket.dimension, bucket.series or "")

    result.buckets.sort(key=sort_key)


def _count_overlap(records_by_leg: dict[str, list[NormalizedRecord]]) -> int:
    """How many trials matched more than one leg, counted once per extra membership."""
    if len(records_by_leg) < 2:
        return 0
    memberships: Counter[str] = Counter()
    for records in records_by_leg.values():
        memberships.update({r.nct_id for r in records})
    return sum(count - 1 for count in memberships.values() if count > 1)


# --------------------------------------------------------------------------------------
# Derived prose
#
# Both of these are generated from field metadata rather than hand-written per query. That
# is the whole reason `FieldSpec` carries `multi` and `note`: adding a field to the registry
# has to bring its caveats with it, or the caveats rot.
# --------------------------------------------------------------------------------------


def _counting_semantics(plan: Plan, result: AggregationResult) -> str:
    """One sentence saying what a single unit of the chart's value means."""
    if plan.metric is Metric.COUNT:
        base = "Each trial is counted once per bucket."
        if plan.group_by and FIELDS[plan.group_by].multi:
            return (
                f"Trials are counted once per distinct {FIELDS[plan.group_by].label.lower()}, "
                f"so a trial with several contributes to several buckets and the column "
                f"totals exceed the distinct trial count."
            )
        if result.overlapping_trials:
            return (
                f"{base} Legs are overlapping populations: {result.overlapping_trials} "
                f"trial memberships are shared between legs, so series totals overlap."
            )
        return base
    if plan.metric is Metric.DISTINCT_COUNT:
        assert plan.distinct_of is not None
        return (
            f"Each value is the number of distinct {FIELDS[plan.distinct_of].label.lower()} "
            f"values across the trials in that bucket, not a trial count."
        )
    assert plan.metric_field is not None
    label = FIELDS[plan.metric_field].label.lower()
    verb = "sum" if plan.metric is Metric.SUM else "median"
    return f"Each value is the {verb} of {label} over the trials in that bucket."


def _warnings(plan: Plan, result: AggregationResult) -> list[str]:
    """Data-quality warnings implied by the plan and the field registry."""
    warnings: list[str] = []

    if plan.group_by:
        field_spec = FIELDS[plan.group_by]
        if field_spec.multi:
            warnings.append(
                f"{field_spec.label} is multi-valued: bucket totals sum to more than the "
                f"number of distinct trials."
            )
        if field_spec.is_temporal:
            warnings.append(
                "Registry lag undercounts the most recent periods: sponsors register and "
                "update records on their own schedule."
            )
        if field_spec.note:
            warnings.append(field_spec.note)

    for reason, count in sorted(result.excluded_by_reason.items()):
        warnings.append(f"{count} record(s) excluded: {reason.replace('_', ' ')}.")

    if result.collapsed_dimensions:
        warnings.append(
            f"{result.collapsed_dimensions} value(s) beyond the top {plan.top_n} were "
            f"collapsed into '{OTHER}'."
        )
    if result.overlapping_trials:
        warnings.append(
            f"{result.overlapping_trials} trial membership(s) are shared between legs; the "
            f"compared populations overlap and do not sum to a distinct total."
        )
    if plan.metric is Metric.MEDIAN:
        warnings.append(
            "Median is reported rather than mean because the underlying distribution is "
            "heavily right-skewed."
        )
    return warnings


# --------------------------------------------------------------------------------------
# Invariants — these raise, they never warn
# --------------------------------------------------------------------------------------


def check_invariants(plan: Plan, result: AggregationResult) -> None:
    """Reconcile the aggregation against its own record counts.

    These are the checks from `plan.md` §3 that concern the aggregator. They run in
    production, not only in tests, because their whole purpose is to stop a wrong chart
    from being returned, and a wrong chart is produced by real data rather than by fixtures.

    Raises:
        InvariantError: on any failure, with the numbers that did not reconcile.
    """
    excluded = sum(result.excluded_by_reason.values())
    if result.used + excluded != result.retrieved:
        raise InvariantError(
            f"record counts do not reconcile: used={result.used} + "
            f"excluded={excluded} != retrieved={result.retrieved}"
        )

    # Counts, distinct counts and sums over enrollment are all non-negative. A point's
    # value is a raw measurement rather than a fold over many, so it is exempt.
    if plan.layout is not Layout.POINT and any(b.value < 0 for b in result.buckets):
        raise InvariantError("a bucket folded to a negative value, which no metric admits")

    if result.used and not result.buckets:
        raise InvariantError(
            f"{result.used} record(s) were counted as used but produced no buckets"
        )

    # A record can only land in more than one bucket via a multi-valued dimension. When
    # neither dimension is multi-valued, bucket membership is a partition of the used
    # records and must add up exactly. This is the check that catches a record being
    # dropped between the exclusion accounting and the fold.
    dimensions = [d for d in (plan.group_by, plan.series_by) if d is not None]
    if not any(FIELDS[d].multi for d in dimensions):
        total = sum(len(b.contributions) for b in result.buckets)
        if total != result.used:
            raise InvariantError(
                f"bucket membership does not partition the used records: "
                f"sum(bucket sizes)={total} != used={result.used}"
            )


def check_citations(result: AggregationResult, fetched_ids: set[str]) -> None:
    """Every cited trial must be one that was actually fetched.

    Separate from `check_invariants` because it needs the API client's record of what came
    back, which the aggregator does not otherwise see. A cited ID that was never fetched
    would mean an ID was synthesized somewhere, which is the single worst failure this
    system can have.
    """
    cited = {c.nct_id for b in result.buckets for c in b.contributions}
    unknown = cited - fetched_ids
    if unknown:
        raise InvariantError(
            f"{len(unknown)} cited trial(s) were never fetched: {sorted(unknown)[:5]}"
        )


__all__ = [
    "OTHER",
    "AggregationResult",
    "Bucket",
    "Contribution",
    "InvariantError",
    "aggregate",
    "check_citations",
    "check_invariants",
    "missing_reason",
]
