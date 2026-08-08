"""Tests for the deterministic plan validator.

These matter more than they look. The validator is the only thing standing between a
plausible-sounding LLM plan and a wrong chart, and its error strings are fed back to the
planner verbatim — so a test that a rule *fires* is also a test that the repair loop gets
usable feedback.

No network, no LLM, no API key.
"""

from __future__ import annotations

import pytest

from cheiron.schemas.plan import (
    DateCertainty,
    Filters,
    Granularity,
    Leg,
    Metric,
    Plan,
    Sort,
    validate_plan,
)


def _leg(label: str = "All", **filters: object) -> Leg:
    return Leg(label=label, filters=Filters(**filters))


# --------------------------------------------------------------------------------------
# Plans that must pass. Each corresponds to a query class from the assignment appendix.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "plan"),
    [
        (
            "time trend",  # "How has the number of trials for [drug] changed per year?"
            Plan(
                legs=[_leg("Pembrolizumab", intervention="pembrolizumab")],
                group_by="start_year",
                granularity=Granularity.YEAR,
                metric=Metric.COUNT,
                sort=Sort.DIMENSION_ASC,
            ),
        ),
        (
            "distribution",  # "How are [condition] trials distributed across phases?"
            Plan(
                legs=[_leg("Melanoma", condition="melanoma")],
                group_by="phases",
                metric=Metric.COUNT,
            ),
        ),
        (
            "comparison as legs",  # "Compare phases for Drug A vs Drug B."
            Plan(
                legs=[
                    _leg("Pembrolizumab", intervention="pembrolizumab"),
                    _leg("Nivolumab", intervention="nivolumab"),
                ],
                group_by="phases",
                metric=Metric.COUNT,
            ),
        ),
        (
            "geographic",  # "Which countries have the most recruiting trials for [condition]?"
            Plan(
                legs=[_leg("Melanoma", condition="melanoma", site_status=["RECRUITING"])],
                group_by="countries",
                metric=Metric.COUNT,
                top_n=15,
            ),
        ),
        (
            "network",  # "Show a network of sponsors and drugs for [condition] trials."
            Plan(
                legs=[_leg("Glioblastoma", condition="glioblastoma")],
                group_by="sponsor_name",
                series_by="intervention_mesh",
                metric=Metric.COUNT,
                viz_hint="network",
            ),
        ),
        (
            "median enrollment",
            Plan(
                legs=[_leg("Melanoma", condition="melanoma")],
                group_by="phases",
                metric=Metric.MEDIAN,
                metric_field="enrollment",
            ),
        ),
        (
            "distinct count",
            Plan(
                legs=[_leg("Melanoma", condition="melanoma")],
                group_by="sponsor_class",
                metric=Metric.DISTINCT_COUNT,
                distinct_of="sponsor_name",
            ),
        ),
        (
            "kpi: no group_by but filtered",
            Plan(legs=[_leg("Melanoma", condition="melanoma")], metric=Metric.COUNT),
        ),
        (
            "local-only filter still counts as narrowing",
            Plan(
                legs=[_leg("Actual dates", date_certainty=DateCertainty.ACTUAL_ONLY)],
                metric=Metric.COUNT,
            ),
        ),
    ],
)
def test_legal_plans_pass(name: str, plan: Plan) -> None:
    assert validate_plan(plan) == [], f"{name} should be legal"


# --------------------------------------------------------------------------------------
# Plans that must fail, one rule per case.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "plan", "expect"),
    [
        (
            "unknown group_by",
            Plan(legs=[_leg(condition="melanoma")], group_by="principal_investigator"),
            "is not a known field",
        ),
        (
            "unknown distinct_of",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                metric=Metric.DISTINCT_COUNT,
                distinct_of="nonsense",
            ),
            "is not a known field",
        ),
        (
            "sum without metric_field",
            Plan(legs=[_leg(condition="melanoma")], group_by="phases", metric=Metric.SUM),
            "requires metric_field",
        ),
        (
            "median over a non-numeric field",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                metric=Metric.MEDIAN,
                metric_field="sponsor_name",
            ),
            "requires one of: numeric",
        ),
        (
            "metric_field set for count",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                metric=Metric.COUNT,
                metric_field="enrollment",
            ),
            "only meaningful for metric 'sum' or 'median'",
        ),
        (
            "distinct_count without distinct_of",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                metric=Metric.DISTINCT_COUNT,
            ),
            "requires distinct_of",
        ),
        (
            "distinct_of set for count",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                metric=Metric.COUNT,
                distinct_of="sponsor_name",
            ),
            "only meaningful for metric 'distinct_count'",
        ),
        (
            "granularity on a non-temporal group_by",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="phases",
                granularity=Granularity.YEAR,
            ),
            "requires a temporal group_by",
        ),
        (
            "temporal group_by without granularity",
            Plan(legs=[_leg(condition="melanoma")], group_by="start_year"),
            "requires an explicit granularity",
        ),
        (
            "top_n on a temporal dimension",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="start_year",
                granularity=Granularity.YEAR,
                top_n=10,
            ),
            "requires group_by to be an entity or categorical field",
        ),
        (
            "group_by equals series_by",
            Plan(legs=[_leg(condition="melanoma")], group_by="phases", series_by="phases"),
            "must be different dimensions",
        ),
        (
            "series_by with multiple legs",
            Plan(
                legs=[_leg("A", condition="melanoma"), _leg("B", condition="lymphoma")],
                group_by="phases",
                series_by="sponsor_class",
            ),
            "conflicts with 2 legs",
        ),
        (
            "duplicate leg labels",
            Plan(
                legs=[_leg("Same", condition="melanoma"), _leg("Same", condition="lymphoma")],
                group_by="phases",
            ),
            "leg labels must be distinct",
        ),
        (
            "network on a categorical dimension",
            Plan(
                legs=[_leg(condition="melanoma")],
                group_by="sponsor_name",
                series_by="phases",
                viz_hint="network",
            ),
            "requires group_by and series_by to both be entity",
        ),
        (
            "network with only one dimension",
            Plan(legs=[_leg(condition="melanoma")], group_by="sponsor_name", viz_hint="network"),
            "requires group_by and series_by to both be entity",
        ),
        (
            "no grouping and no filters",
            Plan(legs=[Leg(label="Everything")]),
            "neither a group_by nor any filters",
        ),
    ],
)
def test_illegal_plans_are_rejected(rule: str, plan: Plan, expect: str) -> None:
    errors = validate_plan(plan)
    assert errors, f"{rule} should have been rejected"
    assert any(expect in e for e in errors), (
        f"{rule}: expected an error containing {expect!r}, got {errors}"
    )


def test_errors_are_actionable_for_the_planner() -> None:
    """Rejection feedback must name the offending value and the legal alternatives.

    The repair loop hands these strings straight back to the model, so an error that says
    only "invalid field" would waste a revision.
    """
    plan = Plan(legs=[_leg(condition="melanoma")], group_by="investigator_name")
    (error,) = validate_plan(plan)
    assert "investigator_name" in error  # what was wrong
    assert "sponsor_name" in error  # what would be acceptable


def test_multiple_errors_are_reported_together() -> None:
    """One revision should be able to fix everything, so all rules run before returning."""
    plan = Plan(
        legs=[_leg("A", condition="melanoma"), _leg("B", condition="lymphoma")],
        group_by="phases",
        series_by="phases",
        metric=Metric.SUM,
    )
    assert len(validate_plan(plan)) >= 3
