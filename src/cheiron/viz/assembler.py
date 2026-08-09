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
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from cheiron.agg.aggregator import OTHER, AggregationResult, Bucket
from cheiron.ctgov.normalizer import COMBINATION_SEPARATOR
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
from cheiron.viz.citations import locate, locate_endpoint, locate_term, verify
from cheiron.viz.rules import Shape, choose, describe_shape

#: How many contributing trials each datum names and cites. A sample, not the whole
#: population: a 2,900-trial bar does not need 2,900 excerpts in a JSON response, and the
#: API request URLs in `meta.api_requests` are the complete, reproducible record. The cap
#: is **per datum** rather than per response, so every bar carries its own evidence instead
#: of the largest one consuming the budget.
INLINE_CITATIONS = 5

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


def build_data(
    plan: Plan,
    result: AggregationResult,
    viz: VizType,
    records: dict[str, Any] | None = None,
) -> tuple[list[Datum] | NetworkData, int, int]:
    """Turn buckets into the payload the chosen chart type renders, evidence attached.

    Citations are built here rather than in a separate pass because a citation belongs to
    a datum, and building them together is what makes it impossible for the two to drift
    apart. Returns the payload plus the unquotable and unverified tallies.
    """
    records = records or {}
    if viz is VizType.NETWORK:
        return _build_network(plan, result, records)

    group_key = plan.group_by or DIMENSION_KEY
    series_key = plan.series_by or SERIES_KEY
    data: list[Datum] = []
    unquotable = unverified = 0

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

        cites, missing, bad = bucket_citations(bucket, records, series_term=bucket.series)
        unquotable += missing
        unverified += bad

        data.append(
            Datum(
                value=bucket.value,
                nct_ids=bucket.nct_ids[:INLINE_CITATIONS],
                nct_id_total=len(bucket.nct_ids),
                citations=cites,
                **extra,
            )
        )
    return data, unquotable, unverified


def _build_network(
    plan: Plan, result: AggregationResult, records: dict[str, Any]
) -> tuple[NetworkData, int, int]:
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
    unquotable = unverified = 0

    for bucket in result.buckets:
        if bucket.series is None:
            continue
        if bucket.dimension == OTHER:
            # "Other" is a residue, not an entity, so it cannot be a node — but the trials
            # inside it are real and their absence from the graph has to be stated. See
            # `network_omissions`, which turns this into a warning rather than a gap.
            continue
        ids = bucket.nct_ids
        # An edge asserts that both endpoints shared an arm group, and the contribution
        # already carries that composite as its field value — so the edge's own citation
        # states the pairing rather than either drug alone. Passing no `series_term` is
        # deliberate: the second endpoint is inside that composite, not a separate leg.
        cites, missing, bad = bucket_citations(bucket, records)
        unquotable += missing
        unverified += bad
        edges.append(
            Edge(
                source=f"{source_kind}:{bucket.dimension}",
                target=f"{target_kind}:{bucket.series}",
                weight=len(ids),
                nct_ids=ids[:INLINE_CITATIONS],
                nct_id_total=len(ids),
                citations=cites,
            )
        )
        node_trials.setdefault((source_kind, bucket.dimension), set()).update(ids)
        node_trials.setdefault((target_kind, bucket.series), set()).update(ids)

    nodes = [
        Node(id=f"{kind}:{label}", label=label, kind=kind, weight=len(ids))
        for (kind, label), ids in node_trials.items()
    ]
    _add_association_strength(edges)
    nodes.sort(key=lambda n: (-n.weight, n.id))
    edges.sort(key=lambda e: (-e.weight, e.source, e.target))
    return NetworkData(nodes=nodes, edges=edges), unquotable, unverified


def _add_association_strength(edges: list[Edge]) -> None:
    """Annotate each edge with `2m·w / (k_source · k_target)`.

    Raw co-occurrence counts rank by ubiquity, not by affinity. On myeloma the five
    heaviest edges all contain dexamethasone — not because those pairings are distinctive
    but because dexamethasone is in nearly every regimen. Association strength divides out
    each endpoint's total degree, which is the standard correction in bibliometric
    co-occurrence analysis (VOSviewer applies it by default).

    **Derived, and labelled as such.** Unlike `weight` this is arithmetic over the graph
    rather than a fold over trials, so no citation stands behind it. It ranks; it never
    replaces the countable value.

    A caveat worth knowing before trusting the ordering: on its own this metric favours
    pairs that occur only with each other, which score maximally on a single trial. It is
    informative in combination with an occurrence threshold — hence
    `config.suggested_min_occurrences` — and misleading without one.
    """
    degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        degree[edge.source] += edge.weight
        degree[edge.target] += edge.weight
    total = sum(edge.weight for edge in edges)
    if not total:
        return
    for edge in edges:
        divisor = degree[edge.source] * degree[edge.target]
        if divisor:
            edge.strength = round((2 * total * edge.weight) / divisor, 4)


def network_omissions(result: AggregationResult) -> int:
    """Distinct trials that contributed an edge but appear in no node the graph kept.

    Only non-zero when a plan asked for an explicit `top_n`: networks are otherwise
    returned complete. A trimmed network cannot show the removed entities, because "Other"
    is not a thing that co-occurs with anything, so those trials would otherwise vanish
    between the record counts and the graph — visible in neither, contradicting both.
    """
    return result.omitted_trials


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
        suggested_min_occurrences=result.suggested_min_occurrences,
    )


# --------------------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------------------


def bucket_citations(
    bucket: Bucket,
    records: dict[str, Any],
    *,
    series_term: str | None = None,
    limit: int = INLINE_CITATIONS,
) -> tuple[list[Citation], int, int]:
    """Evidence for one datum, attached to that datum.

    Citations belong to the datum rather than to a response-level map keyed by NCT ID.
    That is not a stylistic choice: on a multi-valued dimension one trial contributes to
    several datums, so a per-trial map can hold only one of its excerpts, and every other
    datum reading that map gets a citation stating a different datum's value. Measured on
    the geographic example, 32 of 55 lookups were wrong that way — each excerpt verified
    perfectly at its offsets while supporting the wrong bucket, which is the exact failure
    this module exists to prevent.

    A grouped datum has two coordinates, and they are cited separately: `supports="value"`
    for the bucket, `supports="series"` for the leg. The series citation is omitted when
    the record does not state the leg's term anywhere quotable — a leg is a search
    expression, and the registry expands it through synonyms the record never repeats.

    Returns:
        The citations, plus counts of the two distinct reasons one was not emitted.
    """
    citations: list[Citation] = []
    unquotable = 0
    unverified = 0

    for contribution in bucket.contributions[:limit]:
        record = records.get(contribution.nct_id)
        if record is None or not record.raw:
            unquotable += 1
            continue

        # Two different failures, kept apart because they mean opposite things. A value
        # the record never states — "NOT_REPORTED" is our label for an absent `phases`
        # key, not something the registry says — has nothing to quote, and no citation is
        # possible or honest. A located excerpt that fails re-slicing is a defect in this
        # code, and must be loud rather than folded in with the former.
        endpoints = contribution.field_value.split(COMBINATION_SEPARATOR)
        if len(endpoints) > 1:
            # A co-occurrence edge claims two agents shared an arm group, and no single
            # span shows that: the smallest one containing both drugs is usually the whole
            # `interventions` array. So each endpoint is cited on its own intervention
            # entry, and the shared `armGroupLabels` value is visible in both — which is
            # the co-administration claim, readable side by side.
            #
            # Only `COMBINATION_SEPARATOR` splits. Phases are the other composite kind and
            # must NOT: `"phases":["PHASE1","PHASE2"]` already states both in one span, so
            # splitting them would replace one correct citation with two weaker ones.
            found = 0
            for endpoint in endpoints:
                located = locate_endpoint(record.raw, endpoint)
                if located is None:
                    continue
                payload, path, excerpt = located
                if not verify(payload, excerpt):
                    unverified += 1
                    continue
                found += 1
                citations.append(
                    Citation(
                        nct_id=contribution.nct_id,
                        url=STUDY_URL.format(contribution.nct_id),
                        brief_title=record.get("brief_title") or "",
                        field_path=path,
                        field_value=endpoint,
                        excerpt=excerpt.text,
                        offset=(excerpt.start, excerpt.end),
                        supports="value",
                    )
                )
            # Half a pairing is not evidence of a pairing. If either endpoint could not be
            # quoted, drop what was found rather than let one drug stand for two.
            if found < len(endpoints):
                del citations[len(citations) - found :]
                unquotable += 1
            continue

        located = locate(record.raw, contribution.field_path, contribution.field_value)
        if located is None:
            unquotable += 1
            continue
        payload, excerpt = located
        if not verify(payload, excerpt):
            unverified += 1
            continue

        citations.append(
            Citation(
                nct_id=contribution.nct_id,
                url=STUDY_URL.format(contribution.nct_id),
                brief_title=record.get("brief_title") or "",
                field_path=contribution.field_path,
                field_value=contribution.field_value,
                excerpt=excerpt.text,
                offset=(excerpt.start, excerpt.end),
                supports="value",
            )
        )

        if series_term:
            found = locate_term(record.raw, series_term)
            if found is None:
                unquotable += 1
                continue
            series_payload, path, series_excerpt = found
            if not verify(series_payload, series_excerpt):
                unverified += 1
                continue
            citations.append(
                Citation(
                    nct_id=contribution.nct_id,
                    url=STUDY_URL.format(contribution.nct_id),
                    brief_title=record.get("brief_title") or "",
                    field_path=path,
                    field_value=series_term,
                    excerpt=series_excerpt.text,
                    offset=(series_excerpt.start, series_excerpt.end),
                    supports="series",
                )
            )

    return citations, unquotable, unverified


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

    records = {
        record.nct_id: record
        for leg_records in retrieval.records_by_leg.values()
        for record in leg_records
    }

    data, unquotable, unverified = build_data(plan, result, viz, records)

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

    if unquotable:
        warnings.append(
            f"{unquotable} contribution(s) carry no citation because the record does not "
            f"state the value anywhere quotable — an absent field reported as its own "
            f"bucket has nothing to quote, and a leg whose term the registry matched "
            f"through a synonym is not repeated in the record. Nothing is cited that the "
            f"record does not say."
        )
    if unverified:
        warnings.append(
            f"{unverified} citation(s) were dropped because their excerpt did not survive "
            f"re-verification against the fetched record. An unverifiable citation is "
            f"never emitted."
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
    "assemble",
    "build_answer",
    "bucket_citations",
    "build_config",
    "build_data",
    "build_encoding",
    "build_title",
]
