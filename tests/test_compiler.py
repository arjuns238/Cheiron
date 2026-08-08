"""Query compiler tests.

The expected Essie strings here are not invented: each one was run against the live API
and its match count recorded in `docs/api-findings.md`. Where a test asserts a specific
clause shape, the docstring says what that shape returned, so a future change that
"simplifies" the clause has to explain why the count would still be right.

No network, no LLM.
"""

from __future__ import annotations

import pytest

from cheiron.ctgov.compiler import (
    PAGE_SIZE,
    compile_leg,
    compile_plan,
    escape,
    projection,
    request_url,
)
from cheiron.schemas.plan import (
    DateCertainty,
    Filters,
    Granularity,
    Leg,
    Metric,
    Plan,
)
from cheiron.schemas.request import InterventionType, Phase, SponsorClass, Status, StudyType


def advanced(**filters: object) -> str:
    """The `filter.advanced` string a set of filters compiles to."""
    request = compile_leg("leg", Filters(**filters), ("NCTId",))
    return request.params.get("filter.advanced", "")


def params(**filters: object) -> dict[str, str]:
    return compile_leg("leg", Filters(**filters), ("NCTId",)).params


# --------------------------------------------------------------------------------------
# Text search parameters
# --------------------------------------------------------------------------------------


def test_text_filters_map_to_their_query_parameters() -> None:
    """Verified counts: cond=melanoma 3,743 · intr=pembrolizumab 2,922 · spons=Merck 5,191."""
    assert params(condition="melanoma")["query.cond"] == "melanoma"
    assert params(intervention="pembrolizumab")["query.intr"] == "pembrolizumab"
    assert params(sponsor="Merck")["query.spons"] == "Merck"
    assert params(free_text="immunotherapy")["query.term"] == "immunotherapy"


def test_status_is_a_comma_separated_pushdown_not_an_advanced_clause() -> None:
    """`filter.overallStatus=RECRUITING,COMPLETED` is its own parameter and works."""
    compiled = params(status=[Status.RECRUITING, Status.COMPLETED])
    assert compiled["filter.overallStatus"] == "RECRUITING,COMPLETED"
    assert "filter.advanced" not in compiled


def test_every_request_asks_for_the_full_page_and_the_total() -> None:
    """The default pageSize is 10, so not asking would mean a hundred round trips."""
    compiled = params(condition="melanoma")
    assert compiled["pageSize"] == str(PAGE_SIZE) == "1000"
    assert compiled["countTotal"] == "true"


# --------------------------------------------------------------------------------------
# Advanced clauses — each shape was measured
# --------------------------------------------------------------------------------------


def test_single_phase_is_bare_and_multiple_phases_are_a_union() -> None:
    """Measured on melanoma: PHASE2 1,534 · PHASE3 219 · (PHASE2 OR PHASE3) 1,728.

    The union is 1,728 rather than 1,753 because multi-phase trials are counted once. An
    AND here would have returned nearly nothing, and a naive sum would have over-reported
    by exactly the 25 multi-phase trials.
    """
    assert advanced(phase=[Phase.PHASE3]) == "AREA[Phase]PHASE3"
    assert advanced(phase=[Phase.PHASE2, Phase.PHASE3]) == "AREA[Phase](PHASE2 OR PHASE3)"


def test_start_year_range_uses_open_bounds_for_missing_ends() -> None:
    """`RANGE[2015-01-01,MAX]` returned 2,140 against melanoma; MIN/MAX are accepted."""
    assert advanced(start_year_min=2015) == "AREA[StartDate]RANGE[2015-01-01,MAX]"
    assert advanced(start_year_max=2020) == "AREA[StartDate]RANGE[MIN,2020-12-31]"
    assert (
        advanced(start_year_min=2015, start_year_max=2020)
        == "AREA[StartDate]RANGE[2015-01-01,2020-12-31]"
    )


def test_country_without_site_status_is_a_plain_area_clause() -> None:
    """`AREA[LocationCountry]France` — 42,635 corpus-wide."""
    assert advanced(country="France") == "AREA[LocationCountry]France"


def test_country_with_site_status_nests_so_the_site_itself_must_match() -> None:
    """The nesting is the difference between 42,635 and 9,347.

    Unnested, a trial qualifies if it has a French site *and* is recruiting anywhere.
    Nested, the French site itself must be recruiting — which is what "recruiting trials
    in France" actually asks.
    """
    clause = advanced(country="France", site_status=[Status.RECRUITING])
    assert clause == (
        "SEARCH[Location](AREA[LocationCountry]France AND AREA[LocationStatus](RECRUITING))"
    )


def test_multi_word_country_is_quoted() -> None:
    """`United States` must not be tokenized into two terms by the Essie parser."""
    assert advanced(country="United States") == 'AREA[LocationCountry]"United States"'


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"study_type": StudyType.INTERVENTIONAL}, "AREA[StudyType]INTERVENTIONAL"),
        ({"sponsor_class": SponsorClass.INDUSTRY}, "AREA[LeadSponsorClass]INDUSTRY"),
        ({"intervention_type": InterventionType.DRUG}, "AREA[InterventionType]DRUG"),
        ({"enrollment_min": 100}, "AREA[EnrollmentCount]RANGE[100,MAX]"),
        ({"enrollment_max": 500}, "AREA[EnrollmentCount]RANGE[MIN,500]"),
        ({"enrollment_min": 100, "enrollment_max": 500}, "AREA[EnrollmentCount]RANGE[100,500]"),
        ({"has_results": True}, "AREA[HasResults]true"),
    ],
)
def test_plan_md_local_filters_are_actually_pushed_down(
    filters: dict[str, object], expected: str
) -> None:
    """CORRECTION 3: every filter plan.md called local works server-side.

    Measured against melanoma: INTERVENTIONAL 3,111 · INDUSTRY 1,274 · DRUG 2,113 ·
    enrollment ≥100 1,139 · hasResults 789. Pushing these down is what keeps the 20-page
    cap from truncating ordinary queries.
    """
    assert advanced(**filters) == expected


def test_date_certainty_distinguishes_estimated_from_unrecorded() -> None:
    """3,528 NOT-ESTIMATED + 215 ESTIMATED = 3,743 melanoma trials — an exact partition.

    `startDateStruct.type` is absent on many older records, so "not actual" and "estimated"
    are different populations. ACTUAL_ONLY keeps only confirmed dates; EXCLUDE_ESTIMATED
    keeps confirmed and unrecorded, dropping only declared projections.
    """
    assert advanced(date_certainty=DateCertainty.ACTUAL_ONLY) == "AREA[StartDateType]ACTUAL"
    assert (
        advanced(date_certainty=DateCertainty.EXCLUDE_ESTIMATED)
        == "NOT AREA[StartDateType]ESTIMATED"
    )
    assert advanced(date_certainty=DateCertainty.ANY) == ""


def test_clauses_compose_with_and() -> None:
    """`AREA[Phase]PHASE3 AND AREA[LeadSponsorClass]INDUSTRY` returned 122 for melanoma."""
    clause = advanced(phase=[Phase.PHASE3], sponsor_class=SponsorClass.INDUSTRY)
    assert clause == "AREA[Phase]PHASE3 AND AREA[LeadSponsorClass]INDUSTRY"


def test_empty_filters_produce_no_advanced_parameter() -> None:
    """An empty `filter.advanced` would be a parse error, not a no-op."""
    assert "filter.advanced" not in params()


# --------------------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("France", "France"),
        ("United States", '"United States"'),
        ("Novo Nordisk A/S", '"Novo Nordisk A/S"'),
        ('a "quoted" name', '"a  quoted  name"'),
        ("AREA[Phase]", '"AREA[Phase]"'),
    ],
)
def test_escape_quotes_only_what_needs_it(value: str, expected: str) -> None:
    """Embedded quotes are stripped rather than escaped: Essie has no escape character,
    so a value carrying a quote could otherwise terminate the expression early."""
    assert escape(value) == expected


# --------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------


def test_projection_fetches_only_what_the_plan_needs() -> None:
    """A phase bar chart needs four pieces, not a 17 KB record."""
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    assert projection(plan) == ("NCTId", "BriefTitle", "Phase")


def test_projection_includes_the_type_pieces_that_would_otherwise_vanish() -> None:
    """Requesting StartDate alone returns the date with no `.type`, silently losing the
    ACTUAL/ESTIMATED distinction. `FieldSpec.projection` names both, and this proves it."""
    plan = Plan(
        legs=[Leg(label="All")], group_by="start_date", granularity=Granularity.YEAR
    )
    assert "StartDate" in projection(plan)
    assert "StartDateType" in projection(plan)


def test_projection_covers_every_referenced_dimension() -> None:
    plan = Plan(
        legs=[Leg(label="All")],
        group_by="sponsor_class",
        series_by="phases",
        metric=Metric.MEDIAN,
        metric_field="enrollment",
    )
    fields = projection(plan)
    for piece in ("NCTId", "BriefTitle", "LeadSponsorClass", "Phase", "EnrollmentCount"):
        assert piece in fields


def test_projection_never_repeats_a_piece() -> None:
    """Two fields sharing a projection piece must not send it twice."""
    plan = Plan(legs=[Leg(label="All")], group_by="countries", metric_field=None)
    assert len(projection(plan)) == len(set(projection(plan)))


# --------------------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------------------


def test_each_leg_compiles_to_its_own_request_sharing_one_projection() -> None:
    plan = Plan(
        legs=[
            Leg(label="Pembrolizumab", filters=Filters(intervention="pembrolizumab")),
            Leg(label="Nivolumab", filters=Filters(intervention="nivolumab")),
        ],
        group_by="phases",
    )
    requests = compile_plan(plan)

    assert [r.leg_label for r in requests] == ["Pembrolizumab", "Nivolumab"]
    assert requests[0].params["query.intr"] == "pembrolizumab"
    assert requests[1].params["query.intr"] == "nivolumab"
    assert requests[0].params["fields"] == requests[1].params["fields"]


def test_request_url_is_reproducible_by_hand() -> None:
    """`meta.api_requests` exists so a reader can paste the URL and get the same records."""
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="melanoma"))], group_by="phases")
    url = request_url("https://clinicaltrials.gov/api/v2", compile_plan(plan)[0])

    assert url.startswith("https://clinicaltrials.gov/api/v2/studies?")
    assert "query.cond=melanoma" in url
    assert "countTotal=true" in url
