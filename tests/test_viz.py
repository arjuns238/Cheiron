"""Viz rules and spec assembler tests.

Two properties matter more than any individual assertion here:

1. **The rules cannot be talked out of a chart type.** `choose` is given deliberately
   illegal preferences and must fall back to the rule's default every time. That is the
   whole safety argument for letting a model pick a chart at all.
2. **The envelope never restates a number the aggregator did not produce.** Titles,
   answers and axis labels are templated, and the tests read the numbers back out of the
   response to confirm they match the buckets.

No network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheiron.agg.aggregator import aggregate
from cheiron.ctgov.client import FetchResult
from cheiron.ctgov.normalizer import PHASE_NOT_REPORTED, NormalizedRecord, normalize_study
from cheiron.ctgov.retrieval import Retrieval
from cheiron.ctgov.retrieval import assemble as assemble_retrieval
from cheiron.schemas.plan import (
    BinScale,
    Filters,
    Granularity,
    Layout,
    Leg,
    Metric,
    Plan,
    Sort,
)
from cheiron.schemas.response import NetworkData, ResponseType, VizType
from cheiron.viz.assembler import assemble, build_answer, build_title
from cheiron.viz.rules import Shape, choose, describe_shape, legal_charts

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


@pytest.fixture
def records() -> list[NormalizedRecord]:
    out = []
    for path in sorted(FIXTURE_DIR.glob("NCT*.json")):
        record = normalize_study(json.loads(path.read_text()))
        assert isinstance(record, NormalizedRecord)
        out.append(record)
    return out


@pytest.fixture
def retrieval(records: list[NormalizedRecord]) -> Retrieval:
    raws = [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("NCT*.json"))]
    return assemble_retrieval(
        [FetchResult(leg_label="All trials", studies=raws, matched=len(raws), pages=1)]
    )


def run(plan: Plan, records: list[NormalizedRecord], retrieval: Retrieval, **kwargs: object):
    legs = {leg.label: records for leg in plan.legs}
    result = aggregate(plan, legs)
    retrieval.records_by_leg = legs
    return assemble(plan, result, retrieval, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Legality
# --------------------------------------------------------------------------------------


def shape_for(plan: Plan, records: list[NormalizedRecord]) -> Shape:
    return describe_shape(plan, aggregate(plan, {leg.label: records for leg in plan.legs}))


def test_no_grouping_is_a_kpi(records: list[NormalizedRecord]) -> None:
    shape = shape_for(Plan(legs=[Leg(label="All", filters=Filters(condition="x"))]), records)
    assert legal_charts(shape) == (VizType.KPI,)


def test_temporal_offers_line_first(records: list[NormalizedRecord]) -> None:
    """"How has X changed" wants a line; "which year had the most" wants a bar. Both legal."""
    plan = Plan(legs=[Leg(label="All")], group_by="start_date", granularity=Granularity.YEAR)
    assert legal_charts(shape_for(plan, records)) == (VizType.LINE, VizType.BAR)


def test_temporal_with_series_offers_stacked_area(records: list[NormalizedRecord]) -> None:
    plan = Plan(
        legs=[Leg(label="A"), Leg(label="B")],
        group_by="start_date",
        granularity=Granularity.YEAR,
    )
    charts = legal_charts(shape_for(plan, records))
    assert charts[0] is VizType.STACKED_AREA
    assert VizType.PIE not in charts


def test_small_categorical_allows_a_pie(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    assert legal_charts(shape_for(plan, records)) == (VizType.BAR, VizType.PIE)


def test_multi_valued_categorical_forbids_a_pie(records: list[NormalizedRecord]) -> None:
    """A pie asserts a partition of a whole. Slices summing past 100% are a lie."""
    plan = Plan(legs=[Leg(label="All")], group_by="intervention_types")
    assert VizType.PIE not in legal_charts(shape_for(plan, records))


def test_countries_gets_a_map_but_keeps_a_bar(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="countries")
    assert legal_charts(shape_for(plan, records)) == (VizType.CHOROPLETH, VizType.BAR)


def test_high_cardinality_entity_is_bar_only(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_name")
    assert legal_charts(shape_for(plan, records)) == (VizType.BAR,)


def test_crossed_entities_with_a_count_are_a_network(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_name", series_by="conditions")
    assert legal_charts(shape_for(plan, records))[0] is VizType.NETWORK


def test_binned_numeric_is_a_histogram(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="enrollment", bins=5)
    assert legal_charts(shape_for(plan, records)) == (VizType.HISTOGRAM,)


def test_point_layout_is_a_scatter(records: list[NormalizedRecord]) -> None:
    plan = Plan(
        legs=[Leg(label="All")],
        group_by="site_count",
        metric_field="enrollment",
        layout=Layout.POINT,
    )
    assert legal_charts(shape_for(plan, records)) == (VizType.SCATTER,)


# --------------------------------------------------------------------------------------
# The selector cannot escape the legal set
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preference", [VizType.PIE, VizType.NETWORK, VizType.SCATTER, "nonsense", None, ""]
)
def test_an_illegal_preference_falls_back_to_the_rules_default(
    preference: object, records: list[NormalizedRecord]
) -> None:
    """The strongest guarantee in the chart layer: the model can only ever downgrade."""
    plan = Plan(legs=[Leg(label="All")], group_by="start_date", granularity=Granularity.YEAR)
    shape = shape_for(plan, records)
    assert choose(shape, preference) is VizType.LINE  # type: ignore[arg-type]


def test_a_legal_preference_is_honoured(records: list[NormalizedRecord]) -> None:
    """The selector earns its place precisely when several charts are defensible."""
    plan = Plan(legs=[Leg(label="All")], group_by="start_date", granularity=Granularity.YEAR)
    assert choose(shape_for(plan, records), VizType.BAR) is VizType.BAR


def test_the_shape_shown_to_the_selector_carries_no_values(
    records: list[NormalizedRecord],
) -> None:
    """The chart selector sees shape, never data. If it cannot read a number, it cannot
    write one into the output.

    Asserted structurally rather than by scanning the serialized shape for digits: bucket
    *labels* legitimately contain digits ("PHASE3", "2019"), so a substring check would
    confuse a label with a value. What matters is that no attribute of `Shape` is derived
    from `bucket.value` at all, which is a property of the field list itself.
    """
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    shape = shape_for(plan, records)

    assert set(shape.__dict__) == {
        "group_kind",
        "series_kind",
        "metric",
        "layout",
        "binned",
        "bucket_count",
        "series_count",
        "has_other",
        "sample_labels",
        "group_field",
        "series_field",
    }, "a new Shape attribute must be reviewed for whether it leaks a computed value"

    # The only free-text it carries is dimension labels, which are keys rather than
    # measurements — they exist in the data before any fold happens.
    result = aggregate(plan, {"All": records})
    assert set(shape.sample_labels) <= {b.dimension for b in result.buckets}


# --------------------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------------------


def test_bar_chart_end_to_end(records: list[NormalizedRecord], retrieval: Retrieval) -> None:
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="melanoma"))], group_by="phases")
    response = run(plan, records, retrieval)

    assert response.response_type is ResponseType.VISUALIZATION
    assert response.visualization is not None
    assert response.visualization.type is VizType.BAR
    assert response.visualization.encoding.x is not None
    assert response.visualization.encoding.x.field == "phases"
    assert response.visualization.encoding.y is not None
    assert response.visualization.encoding.y.unit == "trials"

    # Every datum carries the key the encoding names, or a frontend would have to guess.
    for datum in response.visualization.data:
        assert "phases" in datum.model_dump()

    assert sum(d.value for d in response.visualization.data) == 11
    assert response.meta.record_counts is not None
    assert response.meta.record_counts.used == 11


def test_the_answer_restates_only_numbers_the_aggregator_produced(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": records})
    answer = build_answer(plan, result, VizType.BAR)

    top = max(result.buckets, key=lambda b: b.value)
    assert top.dimension in answer
    assert f"{int(top.value):,}" in answer
    assert "11 trials" in answer


def test_kpi_answer_reports_the_single_value(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="melanoma"))])
    response = run(plan, records, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.KPI
    assert response.visualization.encoding.x is None
    assert "11" in response.answer


def test_titles_name_the_measure_the_dimension_and_the_legs() -> None:
    plan = Plan(
        legs=[
            Leg(label="Pembrolizumab", filters=Filters(intervention="pembrolizumab")),
            Leg(label="Nivolumab", filters=Filters(intervention="nivolumab")),
        ],
        group_by="phases",
    )
    assert build_title(plan) == "Trials by Phase — Pembrolizumab vs Nivolumab"


def test_median_title_and_axis_say_median() -> None:
    plan = Plan(
        legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
        group_by="sponsor_class",
        metric=Metric.MEDIAN,
        metric_field="enrollment",
    )
    assert build_title(plan) == "Median Enrollment by Sponsor Class — Melanoma"


def test_grouped_bar_carries_the_series_key(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    industry = [r for r in records if r.get("sponsor_class") == "INDUSTRY"]
    other = [r for r in records if r.get("sponsor_class") != "INDUSTRY"]
    plan = Plan(legs=[Leg(label="Industry"), Leg(label="Other")], group_by="phases")

    result = aggregate(plan, {"Industry": industry, "Other": other})
    retrieval.records_by_leg = {"Industry": industry, "Other": other}
    response = assemble(plan, result, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.GROUPED_BAR
    assert response.visualization.encoding.series is not None
    assert response.visualization.encoding.series.field == "series"
    for datum in response.visualization.data:
        assert datum.model_dump()["series"] in {"Industry", "Other"}


def test_histogram_bins_are_ordered_and_labelled_as_ranges(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(
        legs=[Leg(label="All")], group_by="enrollment", bins=4, bin_scale=BinScale.LOG
    )
    response = run(plan, records, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.HISTOGRAM
    assert response.visualization.encoding.x is not None
    assert response.visualization.encoding.x.type == "ordinal"

    # The zero-enrolment trial is its own bin, not folded into the lowest positive one.
    labels = [d.model_dump()["enrollment"] for d in response.visualization.data]
    assert labels[0] == "0"
    assert all("–" in label for label in labels[1:])


def test_log_bins_spread_a_skewed_distribution(records: list[NormalizedRecord]) -> None:
    """Linear bins over 0–1.1M put every trial but two in the first bar."""
    linear = aggregate(
        Plan(legs=[Leg(label="All")], group_by="enrollment", bins=4), {"All": records}
    )
    log = aggregate(
        Plan(
            legs=[Leg(label="All")],
            group_by="enrollment",
            bins=4,
            bin_scale=BinScale.LOG,
        ),
        {"All": records},
    )
    assert max(b.value for b in linear.buckets) == 8  # everything in one bar
    assert max(b.value for b in log.buckets) < 8  # spread across bins


def test_scatter_emits_one_point_per_trial_with_both_measures(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(
        legs=[Leg(label="All")],
        group_by="site_count",
        metric_field="enrollment",
        layout=Layout.POINT,
    )
    response = run(plan, records, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.SCATTER

    # One datum per trial that reported enrolment; the one null-enrolment fixture drops.
    assert len(response.visualization.data) == 10
    for datum in response.visualization.data:
        payload = datum.model_dump()
        assert payload["nct_id"].startswith("NCT")
        assert isinstance(payload["site_count"], (int, float))
        assert payload["nct_id_total"] == 1


def test_network_edges_weigh_shared_trials_and_nodes_count_distinct_ones(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_name", series_by="conditions")
    response = run(plan, records, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.NETWORK
    data = response.visualization.data
    assert isinstance(data, NetworkData)

    # An edge exists only where a sponsor and a condition genuinely co-occur.
    for edge in data.edges:
        assert edge.weight == edge.nct_id_total >= 1
        sponsor = edge.source.split(":", 1)[1]
        condition = edge.target.split(":", 1)[1]
        for nct_id in edge.nct_ids:
            record = next(r for r in records if r.nct_id == nct_id)
            assert record.get("sponsor_name") == sponsor
            assert condition in record.get("conditions")

    # A node's weight is distinct trials, never the sum of its edges — a sponsor running
    # one trial across three conditions has three edges and a node weight of one.
    for node in data.nodes:
        incident = [e for e in data.edges if node.id in (e.source, e.target)]
        assert node.weight <= sum(e.weight for e in incident)


def test_bin_labels_are_round_numbers_that_still_contain_every_value(
    records: list[NormalizedRecord],
) -> None:
    """Log edges compute to 94.87–432.67. Readable edges must not lose the extremes.

    The outer edges round outward for exactly this reason: rounding the top edge to
    nearest would drop it below the largest observed enrolment, and that trial would fall
    into no bin at all.
    """
    plan = Plan(
        legs=[Leg(label="All")], group_by="enrollment", bins=5, bin_scale=BinScale.LOG
    )
    result = aggregate(plan, {"All": records})

    reported = [r for r in records if r.get("enrollment") is not None]
    assert sum(b.value for b in result.buckets) == len(reported)

    for bucket in result.buckets:
        if bucket.dimension == "0":
            continue
        low, high = bucket.dimension.split("–")
        # Two significant figures means at most two non-zero leading digits.
        assert len(str(int(float(low))).rstrip("0")) <= 2
        assert len(str(int(float(high))).rstrip("0")) <= 2


def test_the_largest_value_lands_inside_the_last_bin(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="enrollment", bins=4)
    result = aggregate(plan, {"All": records})

    largest = max(r.get("enrollment") for r in records if r.get("enrollment") is not None)
    holder = next(
        b for b in result.buckets for c in b.contributions if float(c.field_value) == largest
    )
    assert not holder.dimension.endswith("+"), "the maximum fell outside every bin"


def test_a_network_answer_describes_links_not_a_ranked_dimension(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_name", series_by="conditions")
    response = run(plan, records, retrieval)

    assert "entities" in response.answer and "links" in response.answer
    assert "highest lead sponsor" not in response.answer


def test_trials_collapsed_into_other_are_declared_missing_from_the_graph(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    """A network cannot draw an 'Other' node, so those trials appear in the record counts
    and in no edge. That gap is stated with a number rather than left for the reader."""
    plan = Plan(
        legs=[Leg(label="All")], group_by="sponsor_name", series_by="conditions", top_n=2
    )
    response = run(plan, records, retrieval)

    assert response.visualization is not None
    assert response.visualization.type is VizType.NETWORK
    data = response.visualization.data
    assert isinstance(data, NetworkData)

    assert not any("Other" in n.label for n in data.nodes)
    warning = next(w for w in response.meta.warnings if "drawn in no node or edge" in w)
    graphed = {i for e in data.edges for i in e.nct_ids}
    assert str(len({r.nct_id for r in records} - graphed)) in warning.replace(",", "")


def test_truncation_is_the_first_warning_a_reader_sees(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": records})
    retrieval.truncated = True
    retrieval.matched = 50_000
    response = assemble(plan, result, retrieval)

    assert "sample rather than the whole slice" in response.meta.warnings[0]
    assert response.meta.record_counts is not None
    assert response.meta.record_counts.truncated is True


def test_an_empty_result_still_returns_a_full_visualization_block(
    retrieval: Retrieval,
) -> None:
    """`no_results` renders the same shape as any other answer, so the frontend never
    branches into a separate empty-state code path."""
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="nothing"))], group_by="phases")
    result = aggregate(plan, {"All": []})
    retrieval.records_by_leg = {"All": []}
    response = assemble(plan, result, retrieval)

    assert response.response_type is ResponseType.NO_RESULTS
    assert response.visualization is not None
    assert response.visualization.data == []
    assert response.answer == "No trials matched this query."
    assert any("No trials matched" in w for w in response.meta.warnings)


def test_citations_are_spread_across_buckets_not_concentrated(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    response = run(plan, records, retrieval)

    cited = {c.nct_id for d in response.visualization.data for c in d.citations}
    assert cited, "a chart with data must carry citations"
    for datum in response.visualization.data:  # type: ignore[union-attr]
        if datum.model_dump()["phases"] == PHASE_NOT_REPORTED:
            # An absent `phases` key is reported as its own bucket, but the registry never
            # writes "NOT_REPORTED" — there is no text to quote, so no citation is honest.
            assert not set(datum.nct_ids) & cited
            continue
        assert set(datum.nct_ids) & cited, "every bar contributes at least one citation"

    for citation in [c for d in response.visualization.data for c in d.citations]:
        nct_id = citation.nct_id
        assert citation.nct_id == nct_id
        assert citation.url == f"https://clinicaltrials.gov/study/{nct_id}"
        assert citation.field_path.endswith("phases")
        record = next(r for r in records if r.nct_id == nct_id)
        assert citation.field_value == record.get("phases")
        assert citation.brief_title == record.get("brief_title")


def test_every_cited_trial_was_actually_fetched(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_class")
    response = run(plan, records, retrieval)
    cited = {c.nct_id for d in response.visualization.data for c in d.citations}
    assert cited <= {r.nct_id for r in records}


def test_meta_carries_the_reproducible_audit_trail(
    records: list[NormalizedRecord], retrieval: Retrieval
) -> None:
    plan = Plan(
        legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma", start_year_min=2015))],
        group_by="phases",
        sort=Sort.VALUE_DESC,
    )
    response = run(plan, records, retrieval, query="How are melanoma trials split by phase?")

    assert response.meta.interpretation == "How are melanoma trials split by phase?"
    assert response.meta.plan == plan
    assert response.meta.filters_applied["Melanoma"]["condition"] == "melanoma"
    assert response.meta.filters_applied["Melanoma"]["start_year_min"] == 2015
    assert response.meta.record_counts is not None
    assert response.meta.generated_at


def test_a_scatter_is_not_described_as_an_aggregation(
    records: list[NormalizedRecord],
) -> None:
    """A point layout folds nothing, and must not claim to.

    `metric` still has to be set for the plan to validate, so the generic wording produced
    "Each value is the median of enrollment over the trials in that bucket" above 3,625
    buckets of one trial each — every number correct, every reader misled about what it
    was. The same applied to the right-skew warning, which explains a fold that never ran.
    """
    plan = Plan(
        legs=[Leg(label="All")],
        group_by="site_count",
        metric=Metric.MEDIAN,
        metric_field="enrollment",
        layout=Layout.POINT,
    )
    result = aggregate(plan, {"All": records})

    assert "median" not in result.counting_semantics.lower()
    assert "one trial" in result.counting_semantics
    assert not any("Median is reported" in w for w in result.warnings)
    # And the claim it makes instead is true: one datum per contributing trial.
    assert all(len(b.nct_ids) == 1 for b in result.buckets)
