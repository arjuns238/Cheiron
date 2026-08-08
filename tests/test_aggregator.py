"""Aggregator tests — the regression suite for the deterministic core.

These are the golden tests `plan.md` §7 calls for: a hardcoded plan plus the real fixture
records, asserting the exact numbers that come out. No network, no LLM, no API key.

The expected counts below were derived by hand from the 11 fixtures and are written as
literals on purpose. A test that recomputes the answer the same way the code does would
pass no matter what the code did.

Fixture phase distribution, counted by hand from the raw payloads:

    PHASE1|PHASE2   3   NCT00676871, NCT00874328, NCT02803307
    NA              4   NCT00987428, NCT04078230, NCT04193930, NCT07725679
    NOT_REPORTED    3   NCT02229435, NCT02248896, NCT05844436
    PHASE3          1   NCT06077760
                   --
                   11

Note that the three multi-phase trials form their own bucket rather than being counted
into both Phase 1 and Phase 2. That is the divergence from ClinicalTrials.gov's own facets
documented in `schemas.fields`, and it is why these totals sum to 11 rather than 14.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cheiron.agg.aggregator import (
    OTHER,
    AggregationResult,
    InvariantError,
    aggregate,
    check_citations,
    check_invariants,
    missing_reason,
)
from cheiron.ctgov.normalizer import NormalizedRecord, normalize_study
from cheiron.schemas.plan import Granularity, Leg, Metric, Plan, Sort

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


def _load_all() -> list[NormalizedRecord]:
    records = []
    for path in sorted(FIXTURE_DIR.glob("NCT*.json")):
        record = normalize_study(json.loads(path.read_text()))
        assert isinstance(record, NormalizedRecord), f"{path.name} failed to normalize"
        records.append(record)
    return records


@pytest.fixture
def records() -> list[NormalizedRecord]:
    return _load_all()


def one_leg(records: list[NormalizedRecord]) -> dict[str, list[NormalizedRecord]]:
    return {"All trials": records}


def values(result: AggregationResult) -> dict[str, float]:
    """Bucket values keyed by dimension, for single-series results."""
    return {b.dimension: b.value for b in result.buckets}


# --------------------------------------------------------------------------------------
# Categorical grouping — the phase distribution, counted by hand above
# --------------------------------------------------------------------------------------


def test_phase_distribution_matches_hand_count(records: list[NormalizedRecord]) -> None:
    result = aggregate(Plan(legs=[Leg(label="All trials")], group_by="phases"), one_leg(records))

    assert values(result) == {
        "PHASE1|PHASE2": 3.0,
        "NA": 4.0,
        "NOT_REPORTED": 3.0,
        "PHASE3": 1.0,
    }
    assert result.retrieved == 11
    assert result.used == 11
    assert result.excluded_by_reason == {}


def test_phase_buckets_sum_to_the_trial_count(records: list[NormalizedRecord]) -> None:
    """The property that distinguishes us from ClinicalTrials.gov's double-counting facets."""
    result = aggregate(Plan(legs=[Leg(label="All trials")], group_by="phases"), one_leg(records))
    assert sum(values(result).values()) == len(records)


def test_absent_phase_is_a_bucket_not_an_exclusion(records: list[NormalizedRecord]) -> None:
    """`phases` is the one dimension where absence is a recorded fact, not a gap."""
    result = aggregate(Plan(legs=[Leg(label="All trials")], group_by="phases"), one_leg(records))
    assert "NOT_REPORTED" in values(result)
    assert missing_reason("phases") not in result.excluded_by_reason


# --------------------------------------------------------------------------------------
# Temporal grouping — where absence *is* an exclusion
# --------------------------------------------------------------------------------------


def test_yearly_counts_and_the_one_dateless_trial(records: list[NormalizedRecord]) -> None:
    plan = Plan(
        legs=[Leg(label="All trials")],
        group_by="start_date",
        granularity=Granularity.YEAR,
        sort=Sort.DIMENSION_ASC,
    )
    result = aggregate(plan, one_leg(records))

    # NCT05844436 has no startDateStruct at all and is the only excluded record.
    assert values(result) == {
        "2004": 1.0,
        "2008": 2.0,
        "2009": 1.0,
        "2010": 1.0,
        "2015": 1.0,
        "2020": 1.0,
        "2021": 1.0,
        "2023": 1.0,
        "2027": 1.0,
    }
    assert result.used == 10
    assert result.excluded_by_reason == {missing_reason("start_date"): 1}
    assert [b.dimension for b in result.buckets] == sorted(values(result))


def test_future_estimated_start_date_is_kept_not_silently_dropped(
    records: list[NormalizedRecord],
) -> None:
    """NCT07725679 starts in 2027. It is a real registry record and a real forward tail."""
    plan = Plan(
        legs=[Leg(label="All trials")], group_by="start_date", granularity=Granularity.YEAR
    )
    result = aggregate(plan, one_leg(records))
    assert values(result)["2027"] == 1.0


def test_quarter_granularity_excludes_year_only_dates(records: list[NormalizedRecord]) -> None:
    """A year-only date cannot be placed in a quarter, and Q1 is not a safe guess."""
    plan = Plan(
        legs=[Leg(label="All trials")],
        group_by="start_date",
        granularity=Granularity.QUARTER,
        sort=Sort.DIMENSION_ASC,
    )
    result = aggregate(plan, one_leg(records))

    # Every fixture carries at least month precision, so only the dateless one drops.
    assert result.used == 10
    assert all("-Q" in b.dimension for b in result.buckets)
    assert values(result)["2008-Q2"] == 1.0  # NCT00676871, 2008-06
    assert values(result)["2023-Q4"] == 1.0  # NCT06077760, 2023-12-06


# --------------------------------------------------------------------------------------
# Multi-valued dimensions
# --------------------------------------------------------------------------------------


def test_multi_valued_dimension_overcounts_and_says_so(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="countries")
    result = aggregate(plan, one_leg(records))

    # A trial running in three countries lands in three buckets, so the totals exceed the
    # trial count. That is correct, and it must be stated rather than left for the reader.
    assert sum(values(result).values()) >= result.used
    assert "multi-valued" in result.counting_semantics or "distinct" in result.counting_semantics
    assert any("more than the number of distinct trials" in w for w in result.warnings)


def test_the_measured_fields_note_is_warned_about_not_just_the_grouped_one(
    records: list[NormalizedRecord],
) -> None:
    """The caveat that decides whether a value means what it looks like is on the metric.

    Warnings were emitted only for `group_by`, so charting the median of a field whose
    registry note says "compare against its own denominator" dropped exactly that note.
    """
    plan = Plan(
        legs=[Leg(label="All trials")],
        group_by="status",
        metric=Metric.MEDIAN,
        metric_field="enrollment",
    )
    result = aggregate(plan, one_leg(records))

    assert any("right-skewed" in w for w in result.warnings)  # enrollment's own note
    assert any("UNKNOWN is a real recorded value" in w for w in result.warnings)  # status'


def test_trials_without_locations_are_excluded_and_counted(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="countries")
    result = aggregate(plan, one_leg(records))

    without = sum(1 for r in records if not r.get("countries"))
    assert result.excluded_by_reason.get(missing_reason("countries"), 0) == without
    assert result.used + without == result.retrieved


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------


def test_median_enrollment_skips_the_null_and_keeps_the_zero(
    records: list[NormalizedRecord],
) -> None:
    """Zero enrollment is a reported fact (NCT04193930); absent enrollment is not."""
    plan = Plan(
        legs=[Leg(label="All trials")],
        metric=Metric.MEDIAN,
        metric_field="enrollment",
    )
    result = aggregate(plan, one_leg(records))

    reported = sorted(r.get("enrollment") for r in records if r.get("enrollment") is not None)
    assert 0 in reported, "the zero-enrollment fixture must survive to the fold"
    assert len(reported) == 10
    assert values(result)["All"] == float(reported[4] + reported[5]) / 2
    assert result.excluded_by_reason == {missing_reason("enrollment"): 1}


def test_sum_and_median_diverge_on_a_skewed_distribution(
    records: list[NormalizedRecord],
) -> None:
    """Two fixtures enrol over a million people. This is why the plan prefers median."""
    base = {"legs": [Leg(label="All trials")], "metric_field": "enrollment"}
    total = values(aggregate(Plan(**base, metric=Metric.SUM), one_leg(records)))["All"]
    median = values(aggregate(Plan(**base, metric=Metric.MEDIAN), one_leg(records)))["All"]

    assert total > 2_000_000
    assert median < 1_000
    assert total / 10 > median * 100  # the mean would be off by orders of magnitude


def test_distinct_count_counts_values_not_trials(records: list[NormalizedRecord]) -> None:
    plan = Plan(
        legs=[Leg(label="All trials")],
        group_by="sponsor_class",
        metric=Metric.DISTINCT_COUNT,
        distinct_of="conditions",
    )
    result = aggregate(plan, one_leg(records))

    for bucket in result.buckets:
        expected = {c for r in records if r.get("sponsor_class") == bucket.dimension
                    for c in r.get("conditions")}
        assert bucket.value == float(len(expected))
    assert "distinct" in result.counting_semantics


# --------------------------------------------------------------------------------------
# top_n and the Other bucket
# --------------------------------------------------------------------------------------


def test_top_n_collapses_the_tail_into_other_without_losing_records(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases", top_n=2)
    result = aggregate(plan, one_leg(records))

    assert set(values(result)) == {"NA", "PHASE1|PHASE2", OTHER}
    assert values(result)[OTHER] == 4.0  # NOT_REPORTED (3) + PHASE3 (1)
    assert sum(values(result).values()) == 11.0
    assert result.collapsed_dimensions == 2
    assert any("collapsed into" in w for w in result.warnings)


def test_other_sorts_last_even_when_it_is_the_largest(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases", top_n=1)
    result = aggregate(plan, one_leg(records))
    assert [b.dimension for b in result.buckets][-1] == OTHER


def test_other_carries_citations_like_any_other_bucket(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases", top_n=2)
    result = aggregate(plan, one_leg(records))
    other = next(b for b in result.buckets if b.dimension == OTHER)
    assert len(other.nct_ids) == 4


# --------------------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------------------


def test_legs_become_the_series_dimension(records: list[NormalizedRecord]) -> None:
    industry = [r for r in records if r.get("sponsor_class") == "INDUSTRY"]
    other = [r for r in records if r.get("sponsor_class") != "INDUSTRY"]
    plan = Plan(legs=[Leg(label="Industry"), Leg(label="Non-industry")], group_by="phases")

    result = aggregate(plan, {"Industry": industry, "Non-industry": other})

    assert set(result.series) == {"Industry", "Non-industry"}
    assert result.retrieved == len(records)
    assert sum(b.value for b in result.buckets) == float(len(records))


def test_overlapping_legs_count_twice_and_warn(records: list[NormalizedRecord]) -> None:
    """A trial matching both legs is in both populations; the overlap is reported."""
    shared = records[0]
    plan = Plan(legs=[Leg(label="A"), Leg(label="B")], group_by="phases")

    result = aggregate(plan, {"A": [shared, records[1]], "B": [shared, records[2]]})

    assert result.retrieved == 4
    assert result.used == 4
    assert result.overlapping_trials == 1
    assert any("shared between legs" in w for w in result.warnings)
    assert "overlap" in result.counting_semantics


# --------------------------------------------------------------------------------------
# Invariants — these must raise, never warn
# --------------------------------------------------------------------------------------


def test_kpi_plan_folds_to_a_single_bucket(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All trials")])
    result = aggregate(plan, one_leg(records))
    assert values(result) == {"All": 11.0}


def test_upstream_exclusions_are_carried_into_the_reconciliation(
    records: list[NormalizedRecord],
) -> None:
    """Normalizer and local-filter drops must still add up against what the API returned."""
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases")
    result = aggregate(plan, one_leg(records), prior_exclusions={"malformed_record": 3})

    assert result.excluded_by_reason["malformed_record"] == 3
    assert result.retrieved == 14, "retrieved is what the API returned, not what survived"
    assert result.used == 11
    assert result.used + sum(result.excluded_by_reason.values()) == result.retrieved


def test_invariant_raises_when_counts_do_not_reconcile(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases")
    result = aggregate(plan, one_leg(records))

    result.used += 1  # simulate a record lost between exclusion accounting and the fold
    with pytest.raises(InvariantError, match="do not reconcile"):
        check_invariants(plan, result)


def test_invariant_raises_when_buckets_do_not_partition_used_records(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases")
    result = aggregate(plan, one_leg(records))

    result.buckets.pop()  # a bucket silently disappearing is the failure being caught
    with pytest.raises(InvariantError, match="does not partition"):
        check_invariants(plan, result)


def test_citation_check_rejects_an_id_that_was_never_fetched(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="phases")
    result = aggregate(plan, one_leg(records))

    fetched = {r.nct_id for r in records}
    check_citations(result, fetched)  # the honest case passes

    with pytest.raises(InvariantError, match="never fetched"):
        check_citations(result, fetched - {records[0].nct_id})


# --------------------------------------------------------------------------------------
# Citations are born in the aggregation, not looked up afterwards
# --------------------------------------------------------------------------------------


def test_every_contribution_carries_the_path_that_justifies_its_bucket(
    records: list[NormalizedRecord],
) -> None:
    plan = Plan(legs=[Leg(label="All trials")], group_by="sponsor_class")
    result = aggregate(plan, one_leg(records))

    by_id: dict[str, Any] = {r.nct_id: r for r in records}
    for bucket in result.buckets:
        for contribution in bucket.contributions:
            assert contribution.field_path.endswith("leadSponsor.class")
            assert contribution.field_value == bucket.dimension
            assert by_id[contribution.nct_id].get("sponsor_class") == bucket.dimension


def test_multi_valued_contributions_record_the_array_index(
    records: list[NormalizedRecord],
) -> None:
    """A citation for a country bar has to point at *which* country, not at the array."""
    plan = Plan(legs=[Leg(label="All trials")], group_by="countries")
    result = aggregate(plan, one_leg(records))

    for bucket in result.buckets:
        for contribution in bucket.contributions:
            assert contribution.field_path.endswith("]")
            record = next(r for r in records if r.nct_id == contribution.nct_id)
            index = int(contribution.field_path.rsplit("[", 1)[1].rstrip("]"))
            assert record.get("countries")[index] == bucket.dimension
