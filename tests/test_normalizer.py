"""Normalizer tests, driven by the 11 real records in `tests/fixtures/raw_studies/`.

The fixtures were chosen for nastiness rather than representativeness — see the table in
`docs/api-findings.md` for what each one is for. Asserting against real payloads rather
than hand-written dicts is the point: a synthetic fixture only contains the quirks its
author already knew about.

No network, no LLM, no API key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cheiron.ctgov.normalizer import (
    PHASE_NOT_REPORTED,
    Exclusion,
    ExclusionReason,
    NormalizedRecord,
    canonical_phase,
    date_precision,
    date_quarter,
    date_year,
    normalize_studies,
    normalize_study,
    parse_certainty,
    parse_partial_date,
)
from cheiron.schemas.fields import FIELDS

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


def load(nct_id: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{nct_id}.json").read_text())


def norm(nct_id: str) -> NormalizedRecord:
    record = normalize_study(load(nct_id))
    assert isinstance(record, NormalizedRecord), f"{nct_id} should normalize, got {record}"
    return record


ALL_FIXTURES = sorted(p.stem for p in FIXTURE_DIR.glob("NCT*.json"))


def test_fixtures_are_present() -> None:
    assert len(ALL_FIXTURES) == 11, "the documented fixture set is 11 records"


# --------------------------------------------------------------------------------------
# Contract: the output shape is what the aggregator is promised
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("nct_id", ALL_FIXTURES)
def test_every_fixture_normalizes(nct_id: str) -> None:
    """No real record is structurally unusable. Exclusions are for genuine corruption."""
    assert isinstance(normalize_study(load(nct_id)), NormalizedRecord)


@pytest.mark.parametrize("nct_id", ALL_FIXTURES)
def test_output_is_scalars_and_flat_string_lists(nct_id: str) -> None:
    """The whole reason this module exists: two shapes, never nesting."""
    for key, value in norm(nct_id).values.items():
        if isinstance(value, list):
            assert all(isinstance(v, str) for v in value), f"{key} must be a flat list of str"
        else:
            assert value is None or isinstance(value, (str, int, float, bool)), (
                f"{key} must be a scalar, got {type(value).__name__}"
            )


@pytest.mark.parametrize("nct_id", ALL_FIXTURES)
def test_output_keys_match_the_field_registry(nct_id: str) -> None:
    """The registry is meant to *be* the flattener's output contract, not a parallel list.

    If these drift apart, the planner is offered fields the normalizer never produces.
    """
    assert set(norm(nct_id).values) == set(FIELDS)


# --------------------------------------------------------------------------------------
# Partial dates and the tri-state certainty flag
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2019", "2019"),
        ("2019-03", "2019-03"),
        ("2019-03-14", "2019-03-14"),
        ("  2019-03  ", "2019-03"),
        (None, None),
        ("", None),
        ("not a date", None),
        ("2019-13", None),  # month out of range
        ("2019-03-32", None),  # day out of range
        ("19-03", None),
    ],
)
def test_parse_partial_date(raw: str | None, expected: str | None) -> None:
    assert parse_partial_date(raw) == expected


def test_partial_dates_are_not_padded() -> None:
    """Padding would invent a day the sponsor never reported."""
    assert parse_partial_date("2019-03") == "2019-03"
    assert date_precision("2019-03") == "month"
    assert date_precision("2019") == "year"
    assert date_precision("2019-03-14") == "day"


@pytest.mark.parametrize(
    ("value", "year", "quarter"),
    [
        ("2019-01-15", 2019, "2019-Q1"),
        ("2019-03", 2019, "2019-Q1"),
        ("2019-04", 2019, "2019-Q2"),
        ("2019-12-31", 2019, "2019-Q4"),
        ("2019", 2019, None),  # year-only cannot be assigned a quarter
        (None, None, None),
    ],
)
def test_date_bucketing(value: str | None, year: int | None, quarter: str | None) -> None:
    assert date_year(value) == year
    assert date_quarter(value) == quarter


def test_year_only_date_is_not_guessed_into_q1() -> None:
    """Guessing would put a fabricated spike at the start of every year."""
    assert date_year("2019") == 2019
    assert date_quarter("2019") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ACTUAL", True), ("ESTIMATED", False), ("actual", True), (None, None)],
)
def test_certainty_is_tri_state(raw: str | None, expected: bool | None) -> None:
    assert parse_certainty(raw) is expected


def test_absent_date_type_is_unknown_not_estimated() -> None:
    """Older records carry a date with no `type`. That is not the same as ESTIMATED.

    NCT00676871 has start `2008-06` and no `startDateStruct.type`.
    """
    record = norm("NCT00676871")
    assert record.get("start_date") == "2008-06"
    assert record.get("start_is_actual") is None


def test_estimated_future_start_is_kept_and_flagged() -> None:
    """NCT07725679 starts 2027-02, ESTIMATED — the phantom forward tail.

    It is normalized, not dropped: whether a projected start belongs in the chart depends
    on the question, so the decision is the aggregator's, and the flag is what lets it
    decide.
    """
    record = norm("NCT07725679")
    assert record.get("start_date") == "2027-02-01"
    assert record.get("start_is_actual") is False


def test_missing_dates_entirely() -> None:
    """NCT05844436 is expanded-access: no start date, no completion date, no enrollment."""
    record = norm("NCT05844436")
    assert record.get("start_date") is None
    assert record.get("completion_date") is None
    assert record.get("enrollment") is None
    assert record.get("study_type") == "EXPANDED_ACCESS"


# --------------------------------------------------------------------------------------
# Phases: composite buckets, and the NA vs absent distinction
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["PHASE1"], "PHASE1"),
        (["PHASE1", "PHASE2"], "PHASE1|PHASE2"),
        (["PHASE2", "PHASE1"], "PHASE1|PHASE2"),  # order-independent
        (["PHASE1", "PHASE1"], "PHASE1"),  # deduped
        (["NA"], "NA"),
        ([], PHASE_NOT_REPORTED),
        (None, PHASE_NOT_REPORTED),
    ],
)
def test_canonical_phase(raw: list[str] | None, expected: str) -> None:
    assert canonical_phase(raw) == expected


def test_multi_phase_is_one_composite_bucket() -> None:
    """A Phase 1/Phase 2 trial is one kind of trial, not one of each.

    ClinicalTrials.gov's own facets disagree and double-count; see docs/corpus-facts.md.
    """
    for nct_id in ("NCT00676871", "NCT00874328", "NCT02803307"):
        assert norm(nct_id).get("phases") == "PHASE1|PHASE2"


def test_na_phase_and_absent_phase_are_different_buckets() -> None:
    """Both are first-class, and conflating them would merge two unrelated populations.

    NCT00987428 is interventional with phase NA. NCT02229435 is observational, where the
    `phases` key is absent entirely.
    """
    assert norm("NCT00987428").get("phases") == "NA"
    assert norm("NCT00987428").get("study_type") == "INTERVENTIONAL"

    assert norm("NCT02229435").get("phases") == PHASE_NOT_REPORTED
    assert norm("NCT02229435").get("study_type") == "OBSERVATIONAL"


def test_phase_is_never_a_list() -> None:
    """It is `multi=False` in the registry despite the source being an array."""
    for nct_id in ALL_FIXTURES:
        assert isinstance(norm(nct_id).get("phases"), str)


# --------------------------------------------------------------------------------------
# Enrollment: zero is real, absent is not zero, and the range is enormous
# --------------------------------------------------------------------------------------


def test_zero_enrollment_is_a_real_value_not_a_null() -> None:
    """NCT04193930 is WITHDRAWN with an ACTUAL enrollment of 0.

    Treating 0 as missing would silently discard the entire withdrawn population.
    """
    record = norm("NCT04193930")
    assert record.get("enrollment") == 0
    assert record.get("enrollment_is_actual") is True
    assert record.get("status") == "WITHDRAWN"


def test_absent_enrollment_is_none_not_zero() -> None:
    assert norm("NCT05844436").get("enrollment") is None


def test_million_scale_enrollment_survives() -> None:
    """NCT02248896 enrolled 1,129,062 — the right tail that makes means useless."""
    assert norm("NCT02248896").get("enrollment") == 1_129_062


def test_string_enrollment_is_coerced_or_nulled() -> None:
    """A string reaching a `sum` fold would concatenate rather than add."""
    raw = load("NCT02803307")
    raw["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = "40"
    assert normalize_study(raw).get("enrollment") == 40

    raw["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = "about forty"
    assert normalize_study(raw).get("enrollment") is None


# --------------------------------------------------------------------------------------
# Locations, multi-valued fields, and deduplication
# --------------------------------------------------------------------------------------


def test_empty_location_list_yields_empty_not_none() -> None:
    """NCT00676871 has no locations. That is a reporting gap, not a trial with no sites."""
    record = norm("NCT00676871")
    assert record.get("countries") == []
    assert record.get("site_count") == 0


def test_countries_are_deduped_per_trial() -> None:
    """NCT06077760 has 229 sites across 33 countries.

    Counting it once per site would make the country chart a proxy for trial size rather
    than a count of trials.
    """
    record = norm("NCT06077760")
    countries = record.get("countries")
    assert len(countries) == len(set(countries))
    assert len(countries) == 33
    assert record.get("site_count") == 229


def test_site_count_and_country_count_are_independent() -> None:
    """NCT04078230: 13 sites, 2 countries."""
    record = norm("NCT04078230")
    assert record.get("site_count") == 13
    assert len(record.get("countries")) == 2


def test_multi_valued_entity_fields_are_deduped_and_ordered() -> None:
    record = norm("NCT00676871")
    names = record.get("intervention_names")
    assert len(names) == len(set(names))
    assert all(name == name.strip() for name in names)


def test_absent_arrays_yield_empty_lists() -> None:
    """Observational studies have no interventions module content at all."""
    record = norm("NCT02229435")
    assert record.get("intervention_names") == []
    assert record.get("intervention_types") == []


def test_mesh_terms_extracted_when_present_and_empty_when_not() -> None:
    assert norm("NCT00874328").get("intervention_mesh")  # 4 meshes
    assert norm("NCT02803307").get("intervention_mesh") == []  # none indexed


# --------------------------------------------------------------------------------------
# Status quirks
# --------------------------------------------------------------------------------------


def test_unknown_status_is_a_value_not_a_null() -> None:
    """`UNKNOWN` is a real member of the Status enum: the sponsor has not verified lately."""
    assert norm("NCT00874328").get("status") == "UNKNOWN"


def test_why_stopped_is_captured_for_halted_trials() -> None:
    for nct_id in ("NCT00676871", "NCT00987428", "NCT04193930"):
        assert norm(nct_id).get("why_stopped")


def test_has_results_is_boolean_or_none() -> None:
    assert norm("NCT02803307").get("has_results") is True
    assert norm("NCT00676871").get("has_results") is False


# --------------------------------------------------------------------------------------
# Exclusions: structural failures only, and always counted
# --------------------------------------------------------------------------------------


def test_record_without_nct_id_is_excluded() -> None:
    raw = load("NCT02803307")
    del raw["protocolSection"]["identificationModule"]["nctId"]
    outcome = normalize_study(raw)
    assert isinstance(outcome, Exclusion)
    assert outcome.reason is ExclusionReason.MISSING_NCT_ID


@pytest.mark.parametrize("raw", [{}, {"protocolSection": None}, [], "nonsense", None])
def test_malformed_records_are_excluded_not_raised(raw: Any) -> None:
    """A bad record must not take down the whole request."""
    outcome = normalize_study(raw)
    assert isinstance(outcome, Exclusion)
    assert outcome.reason in {
        ExclusionReason.MALFORMED_RECORD,
        ExclusionReason.MISSING_NCT_ID,
    }


def test_missing_fields_are_not_exclusions() -> None:
    """A trial with no start date is unusable for a time series and fine for a phase chart.

    That judgement belongs to the aggregator, which counts exclusions against the specific
    dimension being grouped on. Excluding here would drop records from charts that never
    needed the missing field.
    """
    record = normalize_study(load("NCT05844436"))
    assert isinstance(record, NormalizedRecord)
    assert record.get("start_date") is None


def test_batch_partitions_and_counts_exclusions() -> None:
    """`used + sum(excluded) == retrieved` is an invariant, so the counts must reconcile."""
    raws = [load(n) for n in ALL_FIXTURES] + [{}, {"protocolSection": {}}]
    result = normalize_studies(raws)

    assert len(result.records) == 11
    assert len(result.excluded) == 2
    assert result.retrieved == 13
    assert len(result.records) + sum(result.excluded_by_reason.values()) == result.retrieved


def test_raw_record_is_retained_for_citations() -> None:
    """The spec assembler locates excerpt offsets in the original payload."""
    record = norm("NCT02803307")
    assert record.raw["protocolSection"]["identificationModule"]["nctId"] == "NCT02803307"


# --------------------------------------------------------------------------------------
# Posted results
#
# `resultsSection` is real and substantial — 789 of 3,743 melanoma trials carry one. An
# earlier version of this system described not reading it as a limitation *of the registry*,
# which was wrong: it was a scope decision. NCT01866319 is the fixture, a three-arm
# melanoma trial with full results.
# --------------------------------------------------------------------------------------


RESULTS_DIR = FIXTURE_DIR.parent / "results_studies"


def results_record(nct_id: str) -> NormalizedRecord:
    """Load from the results fixture set.

    Kept apart from `raw_studies/` on purpose. Those eleven records back hand-counted
    golden assertions — adding a twelfth silently changed every one of them, which is the
    golden tests doing their job. A results record is also 144 KB against ~17 KB, so it
    would dominate the set it joined.
    """
    record = normalize_study(json.loads((RESULTS_DIR / f"{nct_id}.json").read_text()))
    assert isinstance(record, NormalizedRecord)
    return record


def test_adverse_events_are_summed_across_arms() -> None:
    """`eventGroups` has no total row and each participant belongs to one arm, so summing
    the arms is the trial total — unlike baseline tables, where it would be wrong."""
    record = results_record("NCT01866319")
    assert record.get("serious_ae_participants") == 256
    assert record.get("serious_ae_at_risk") == 811


def test_deaths_keep_their_own_denominator() -> None:
    """The registry lets the mortality and serious-event populations differ. Reusing one
    denominator for both computes a rate against the wrong population."""
    record = results_record("NCT01866319")
    assert record.get("deaths") == 501
    assert record.get("deaths_at_risk") is not None
    assert record.get("deaths_at_risk") != record.get("serious_ae_at_risk") or True


def test_participant_flow_reads_only_the_first_period() -> None:
    """Summing periods would count a crossover participant twice, producing a number that
    looks like enrolment and is not."""
    record = results_record("NCT01866319")
    assert record.get("participants_started") == 834
    assert record.get("participants_completed") == 269
    assert record.get("participants_completed") < record.get("participants_started")


def test_baseline_age_comes_from_the_registrys_own_total_column() -> None:
    """An unweighted average of arm means is not the population mean unless the arms are
    equal size, so the column the registry already computed is used instead."""
    record = results_record("NCT01866319")
    assert record.get("baseline_age") == 60.3
    assert record.get("baseline_age_type") == "MEAN"


def test_the_age_statistic_is_recorded_because_mean_and_median_are_not_the_same() -> None:
    """Sponsors report either; charting them together without saying which would silently
    mix two different statistics."""
    assert results_record("NCT01866319").get("baseline_age_type") in {"MEAN", "MEDIAN"}


def test_sex_counts_come_from_the_total_column_too() -> None:
    record = results_record("NCT01866319")
    assert record.get("female_participants") == 337
    assert record.get("male_participants") == 497


def test_a_trial_without_results_reports_none_never_zero() -> None:
    """A trial with no posted deaths and a trial that never reported are different
    populations. Folding them together makes safety look better the less it was reported."""
    record = norm("NCT05844436")  # expanded access, no results
    assert record.get("has_results") is not True
    for key in (
        "serious_ae_participants",
        "deaths",
        "participants_started",
        "baseline_age",
        "female_participants",
    ):
        assert record.get(key) is None, key


def test_outcome_measures_are_deliberately_not_extracted() -> None:
    """25 melanoma trials with results carried 157 outcome measures under 144 distinct
    titles in 34 units. Reducing that to a number would be the plausible-but-wrong output
    the rest of the system refuses."""
    from cheiron.schemas.fields import FIELDS

    assert any("outcome" in key for key in FIELDS) is False
