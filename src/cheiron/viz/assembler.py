"""Build the response envelope from an aggregation.

Everything here is deterministic. The assembler is the last stage before the response goes
out, and it is the stage that would be easiest to let an LLM write — titles, summaries,
axis labels all read like language tasks. They are not written by a model, and the reason
is narrow: any of them could restate a number, and a restated number is a number the
invariant check never saw. `answer` is a template with slots filled from the aggregator,
so the sentence and the chart cannot disagree.

Citations are assembled here too, but they are not *found* here. They were born in the
aggregator as the same list the value was folded from, and this stage only formats them.
The excerpt-and-offset verification described in `plan.md` is a later milestone; until it
lands, a citation carries the field path and value that put the trial in its bucket, which
is already checked against the fetched record set by `check_citations`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from cheiron.agg.aggregator import OTHER, AggregationResult
from cheiron.ctgov.retrieval import Retrieval
from cheiron.schemas.fields import FIELDS, FieldKind
from cheiron.schemas.plan import Layout, Metric, Plan
from cheiron.schemas.response import (
    AnalyzeResponse,
    Channel,
    Citation,
    Datum,
    Edge,
    Encoding,
    Meta,
    NetworkData,
    Node,
    RecordCounts,
    ResponseType,
    Visualization,
    VizConfig,
    VizType,
)
from cheiron.viz.rules import Shape, choose, describe_shape

#: How many contributing trials each datum names inline. The full attribution lives in the
#: top-level citations map; this is a sample so a reader can spot-check a bar without
#: cross-referencing.
INLINE_CITATIONS = 5

#: Cap on the citations map. Every datum is represented, but a 2,900-trial chart does not
#: need 2,900 citation entries in a JSON response — the API request URLs in
#: `meta.api_requests` are the complete, reproducible record.
MAX_CITATIONS = 100

STUDY_URL = "https://clinicaltrials.gov/study/{}"

#: Axis keys used when a dimension has no field behind it.
DIMENSION_KEY = "dimension"
SERIES_KEY = "series"

#: Fields whose co-occurrence edges mean "given together in one arm" rather than merely
#: "listed in the same trial". The distinction changes what the chart is called, because
#: calling a co-listing graph a combination graph would overstate it.
_ARM_SCOPED_LABELS = frozenset({"intervention_names"})


def _channel_type(kind: FieldKind | None) -> str:
    """Map a field kind onto the frontend's scale types."""
    if kind is FieldKind.TEMPORAL:
        return "temporal"
    if kind is FieldKind.NUMERIC:
        return "quantitative"
    if kind is FieldKind.CATEGORICAL:
        return "ordinal"
    return "nominal"


def _metric_label(plan: Plan) -> tuple[str, str | None]:
    """Human-readable name and unit for the measured quantity."""
    if plan.layout is Layout.POINT:
        assert plan.metric_field is not None
        return FIELDS[plan.metric_field].label, "participants"
    match plan.metric:
        case Metric.COUNT:
            return "Trials", "trials"
        case Metric.DISTINCT_COUNT:
            assert plan.distinct_of is not None
            return f"Distinct {FIELDS[plan.distinct_of].label}", None
        case Metric.SUM:
            assert plan.metric_field is not None
            return f"Total {FIELDS[plan.metric_field].label}", "participants"
        case Metric.MEDIAN:
            assert plan.metric_field is not None
            return f"Median {FIELDS[plan.metric_field].label}", "participants"
    raise AssertionError(f"unhandled metric {plan.metric!r}")


def build_title(plan: Plan) -> str:
    """A title stating what was measured, over what, and filtered how.

    Assembled from the plan rather than written by a model, so it cannot describe a chart
    other than the one that was actually computed.
    """
    measure, _ = _metric_label(plan)
    if plan.layout is Layout.COOCCURRENCE:
        assert plan.group_by is not None
        # "Trials by Intervention" would describe a bar chart. The subject of this chart
        # is the pairing, so the title names it.
        label = FIELDS[plan.group_by].label
        pairing = (
            "Co-administered" if plan.group_by in _ARM_SCOPED_LABELS else "Co-occurring"
        )
        title = f"{pairing} {label} Network"
    elif plan.layout is Layout.POINT:
        assert plan.group_by is not None
        title = f"{measure} vs {FIELDS[plan.group_by].label}"
    elif plan.group_by:
        title = f"{measure} by {FIELDS[plan.group_by].label}"
    else:
        title = measure

    if plan.series_by:
        title += f", split by {FIELDS[plan.series_by].label}"

    # Legs name themselves; one unfiltered leg is not worth a subtitle.
    labels = [leg.label for leg in plan.legs]
    if len(labels) > 1:
        title += f" — {' vs '.join(labels)}"
    elif labels and not plan.legs[0].filters.is_empty():
        title += f" — {labels[0]}"
    return title


def build_answer(
    plan: Plan,
    result: AggregationResult,
    viz: VizType,
    network: NetworkData | None = None,
) -> str:
    """A one-sentence summary, templated with slots filled from the aggregator.

    Never LLM-written. Every number in this sentence is read directly off a bucket that
    the invariant check has already reconciled, so the prose cannot drift from the chart.
    """
    if not result.buckets:
        return "No trials matched this query."

    measure, unit = _metric_label(plan)
    trials = f"{result.used:,} trial{'s' if result.used != 1 else ''}"

    if viz is VizType.KPI:
        return f"{measure} across {trials}: {_format(result.buckets[0].value)}."

    if viz is VizType.NETWORK:
        # A network has no "highest bucket" — describing it as a ranked dimension would
        # misdescribe the chart. Its subject is the strongest link.
        assert plan.group_by is not None
        if network is None or not network.edges:
            between = (
                f"between {FIELDS[plan.group_by].label.lower()} and "
                f"{FIELDS[plan.series_by].label.lower()}"
                if plan.series_by
                else f"among {FIELDS[plan.group_by].label.lower()} values"
            )
            return f"No links found {between}."
        top = network.edges[0]
        return (
            f"{len(network.nodes):,} entities and {len(network.edges):,} links across "
            f"{trials}; the strongest is {top.source.split(':', 1)[1]} – "
            f"{top.target.split(':', 1)[1]} with {top.weight:,} shared trials."
        )

    if plan.layout is Layout.POINT:
        assert plan.group_by is not None
        return (
            f"{len(result.buckets):,} trials plotted by "
            f"{FIELDS[plan.group_by].label.lower()} against {measure.lower()}."
        )

    top = max(result.buckets, key=lambda b: b.value)
    dimension = FIELDS[plan.group_by].label.lower() if plan.group_by else "bucket"
    where = f" ({top.series})" if top.series else ""
    return (
        f"Across {trials}, the highest {dimension} is {top.dimension}{where} "
        f"at {_format(top.value)} {unit or ''}".strip()
        + "."
    )


def _format(value: float) -> str:
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.1f}"


# --------------------------------------------------------------------------------------
# Data payloads
# --------------------------------------------------------------------------------------


def build_data(plan: Plan, result: AggregationResult, viz: VizType) -> list[Datum] | NetworkData:
    """Turn buckets into the payload the chosen chart type renders."""
    if viz is VizType.NETWORK:
        return _build_network(plan, result)

    group_key = plan.group_by or DIMENSION_KEY
    series_key = plan.series_by or SERIES_KEY
    data: list[Datum] = []

    for bucket in result.buckets:
        extra: dict[str, object] = {}
        if plan.layout is Layout.POINT:
            # A point's identity is the trial, and its x is a measurement rather than a
            # label, so both are emitted as their own keys.
            extra["nct_id"] = bucket.dimension
            extra[group_key] = bucket.point_x
        else:
            extra[group_key] = bucket.dimension
        if bucket.series is not None:
            extra[series_key] = bucket.series

        data.append(
            Datum(
                value=bucket.value,
                nct_ids=bucket.nct_ids[:INLINE_CITATIONS],
                nct_id_total=len(bucket.nct_ids),
                **extra,
            )
        )
    return data


def _build_network(plan: Plan, result: AggregationResult) -> NetworkData:
    """Build a bipartite co-occurrence graph from crossed entity dimensions.

    An edge's weight is the number of trials in which both endpoints appear, which is
    exactly the length of the bucket's own trial list. The weight and the edge's citations
    are therefore the same object, and cannot disagree — the same property that makes the
    bar charts trustworthy, applied to edges.

    A node's weight is the number of *distinct* trials it appears in, not the sum of its
    edge weights. A sponsor running one trial that covers three conditions has three edges
    of weight one and a node weight of one; summing the edges would triple-count it.
    """
    assert plan.group_by is not None
    # In a co-occurrence graph both endpoints come from one field; in a bipartite graph
    # they come from two. The node id carries its field so a frontend can colour by kind
    # and so two entities that happen to share a label stay distinct.
    source_kind = plan.group_by
    target_kind = plan.series_by or plan.group_by

    edges: list[Edge] = []
    node_trials: dict[tuple[str, str], set[str]] = {}

    for bucket in result.buckets:
        if bucket.series is None:
            continue
        if bucket.dimension == OTHER:
            # "Other" is a residue, not an entity, so it cannot be a node — but the trials
            # inside it are real and their absence from the graph has to be stated. See
            # `network_omissions`, which turns this into a warning rather than a gap.
            continue
        ids = bucket.nct_ids
        edges.append(
            Edge(
                source=f"{source_kind}:{bucket.dimension}",
                target=f"{target_kind}:{bucket.series}",
                weight=len(ids),
                nct_ids=ids[:INLINE_CITATIONS],
                nct_id_total=len(ids),
            )
        )
        node_trials.setdefault((source_kind, bucket.dimension), set()).update(ids)
        node_trials.setdefault((target_kind, bucket.series), set()).update(ids)

    nodes = [
        Node(id=f"{kind}:{label}", label=label, kind=kind, weight=len(ids))
        for (kind, label), ids in node_trials.items()
    ]
    nodes.sort(key=lambda n: (-n.weight, n.id))
    edges.sort(key=lambda e: (-e.weight, e.source, e.target))
    return NetworkData(nodes=nodes, edges=edges)


def network_omissions(result: AggregationResult) -> int:
    """Distinct trials that sit in an `Other` bucket and so appear in no node or edge.

    A network built after a top-N collapse cannot show the collapsed entities, because
    "Other" is not a thing that co-occurs with anything. Those trials would otherwise
    vanish between the record counts and the graph — visible in neither, contradicting
    both. Counting them here is what lets the warning name a number.
    """
    return len(
        {
            contribution.nct_id
            for bucket in result.buckets
            if bucket.dimension == OTHER
            for contribution in bucket.contributions
        }
    )


def build_encoding(plan: Plan, shape: Shape, viz: VizType) -> Encoding:
    """Bind fields to visual channels.

    Every `field` named here is a key present on every datum, so a frontend can read the
    payload without inspecting it first.
    """
    measure, unit = _metric_label(plan)
    y = Channel(field="value", label=measure, type="quantitative", unit=unit)

    if viz is VizType.KPI:
        return Encoding(y=y)

    if viz is VizType.NETWORK:
        return Encoding(
            x=Channel(field="id", label="Entity", type="nominal", unit=None),
            y=Channel(field="weight", label="Trials in common", type="quantitative", unit="trials"),
        )

    group_key = plan.group_by or DIMENSION_KEY
    x_label = FIELDS[plan.group_by].label if plan.group_by else "Group"
    x_type = "quantitative" if plan.layout is Layout.POINT else _channel_type(shape.group_kind)
    if shape.binned:
        # Bins are ordered categories, not a continuous axis: the labels are ranges.
        x_type = "ordinal"

    encoding = Encoding(
        x=Channel(field=group_key, label=x_label, type=x_type, unit=None),
        y=y,
    )
    if shape.has_series:
        series_key = plan.series_by or SERIES_KEY
        label = FIELDS[plan.series_by].label if plan.series_by else "Series"
        encoding.series = Channel(
            field=series_key, label=label, type=_channel_type(shape.series_kind), unit=None
        )
    return encoding


def build_config(plan: Plan, result: AggregationResult, viz: VizType) -> VizConfig:
    return VizConfig(
        sort=plan.sort.value,
        granularity=plan.granularity.value if plan.granularity else None,
        top_n=plan.top_n,
        other_bucket=any(b.dimension == OTHER for b in result.buckets),
        stacked=viz in (VizType.STACKED_BAR, VizType.STACKED_AREA),
        y_starts_at_zero=plan.metric is not Metric.MEDIAN,
        value_format="integer" if plan.metric is Metric.COUNT else "decimal:1",
    )


# --------------------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------------------


def build_citations(
    result: AggregationResult, titles: dict[str, str], limit: int = MAX_CITATIONS
) -> dict[str, Citation]:
    """One citation per cited trial, deduplicated across datapoints.

    Taken in bucket order so that the sample is spread across the chart rather than
    concentrated in whichever bucket happens to be largest — a citation set that only
    covers the tallest bar is not traceability.
    """
    citations: dict[str, Citation] = {}
    for bucket in result.buckets:
        for contribution in bucket.contributions[:INLINE_CITATIONS]:
            if contribution.nct_id in citations:
                continue
            if len(citations) >= limit:
                return citations
            citations[contribution.nct_id] = Citation(
                nct_id=contribution.nct_id,
                url=STUDY_URL.format(contribution.nct_id),
                brief_title=titles.get(contribution.nct_id, ""),
                field_path=contribution.field_path,
                field_value=contribution.field_value,
                # Offset-verified excerpts are a later milestone. Until then the citation
                # carries the value that put the trial in the bucket, which is checked
                # against the fetched records rather than asserted.
                excerpt=contribution.field_value,
                offset=(0, 0),
            )
    return citations


# --------------------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------------------


def assemble(
    plan: Plan,
    result: AggregationResult,
    retrieval: Retrieval,
    *,
    query: str = "",
    preference: VizType | str | None = None,
    request_id: str | None = None,
    elapsed_ms: int | None = None,
) -> AnalyzeResponse:
    """Build the full response.

    Args:
        plan: The committed plan, echoed into `meta.plan`.
        result: The aggregation, already invariant-checked.
        retrieval: What the network stage produced, for the record counts and audit trail.
        query: The original question, used only for `meta.interpretation`.
        preference: A chart-selector preference. Validated against the legal set, so an
            out-of-set value silently becomes the rule's default rather than an error.
        request_id: Overridable for reproducible example runs.
        elapsed_ms: Wall time, if the caller measured it.
    """
    shape = describe_shape(plan, result)
    viz = choose(shape, preference)

    titles = {
        record.nct_id: record.get("brief_title") or ""
        for records in retrieval.records_by_leg.values()
        for record in records
    }

    data = build_data(plan, result, viz)

    warnings = list(result.warnings)
    if plan.layout is Layout.COOCCURRENCE:
        assert plan.group_by is not None
        warnings.insert(
            0,
            (
                "An edge means the two agents shared an arm group, which is the registry's "
                "own statement that they were given together. Agents merely listed in the "
                "same trial are excluded, because those are usually the two sides of a "
                "comparison rather than a combination."
                if plan.group_by in _ARM_SCOPED_LABELS
                else f"An edge means both values appear on the same trial. For "
                f"{FIELDS[plan.group_by].label.lower()} the registry records no arm "
                f"structure, so co-listing is what was measured — it does not by itself "
                f"mean the two were studied together."
            ),
        )
    if viz is VizType.NETWORK and (omitted := network_omissions(result)):
        warnings.insert(
            0,
            f"{omitted:,} trial(s) fall outside the top {plan.top_n} entities and are "
            f"counted in the record totals but drawn in no node or edge, because 'Other' "
            f"is a residue rather than an entity that can co-occur.",
        )
    if retrieval.truncated:
        warnings.insert(
            0,
            f"The page cap was reached: {result.used:,} of {retrieval.matched:,} matching "
            f"trials were analysed, so this chart is a sample rather than the whole slice.",
        )

    response_type = ResponseType.VISUALIZATION if result.buckets else ResponseType.NO_RESULTS
    if not result.buckets:
        warnings.append("No trials matched the filters in this plan.")

    return AnalyzeResponse(
        request_id=request_id or str(uuid.uuid4()),
        response_type=response_type,
        answer=build_answer(
            plan, result, viz, data if isinstance(data, NetworkData) else None
        ),
        visualization=Visualization(
            type=viz,
            title=build_title(plan),
            subtitle=result.counting_semantics or None,
            encoding=build_encoding(plan, shape, viz),
            data=data,
            config=build_config(plan, result, viz),
        ),
        citations=build_citations(result, titles),
        meta=Meta(
            interpretation=query or build_title(plan),
            plan=plan,
            filters_applied={
                leg.label: leg.filters.model_dump(exclude_none=True, exclude_defaults=True)
                for leg in plan.legs
            },
            counting_semantics=result.counting_semantics or None,
            record_counts=RecordCounts(
                matched=retrieval.matched,
                retrieved=result.retrieved,
                used=result.used,
                excluded_by_reason=result.excluded_by_reason,
                truncated=retrieval.truncated,
            ),
            assumptions=list(plan.assumptions),
            warnings=warnings,
            api_requests=list(retrieval.urls),
            generated_at=datetime.now(UTC).isoformat(),
            cache_hit=retrieval.cache_hit,
            elapsed_ms=elapsed_ms,
        ),
    )


__all__ = [
    "INLINE_CITATIONS",
    "MAX_CITATIONS",
    "assemble",
    "build_answer",
    "build_citations",
    "build_config",
    "build_data",
    "build_encoding",
    "build_title",
]
