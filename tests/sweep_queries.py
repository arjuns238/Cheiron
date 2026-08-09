"""The sweep corpus: questions chosen to force every path, not to look representative.

`examples/` holds eight captured runs, one per query class, curated for a reader. This is
the opposite: a coverage matrix built to *break* things. Each entry records what it is
meant to exercise, so a run that fails says which capability is broken rather than just
which question went wrong.

Every bug found by hand so far survived because no query reached the code that held it —
an empty co-occurrence graph, a scatter that described itself as a median, a filter that
compiled to nothing. Each was one query away from being obvious. That is what this is for.

Run:  .venv/bin/python tests/run_sweep.py [openai|anthropic] [--only PREFIX]

Not collected by pytest: no `test_` prefix, and it costs real model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Query:
    """One sweep case.

    Attributes:
        key: Stable identifier, used for result filenames and for re-running one case.
        probes: What this question is meant to exercise. Written as the reason the query
            is in the set, so a reviewer can tell whether the set still covers its axes.
        query: The natural-language question.
        params: Structured request parameters, for the cases that test them.
        expect_type: The response type this should produce, when that is the point of the
            case. `None` means any visualization is acceptable.
        expect_chart: The chart type this should produce, where the question genuinely
            determines it. Deliberately sparse — the chart selector is allowed preference
            within the legal set, so pinning every case would test conformity, not
            correctness.
        note: A caveat for the reviewer reading the result.
    """

    key: str
    probes: str
    query: str
    params: dict[str, object] = field(default_factory=dict)
    expect_type: str | None = "visualization"
    expect_chart: str | None = None
    note: str = ""


QUERIES: list[Query] = [
    # ---------------------------------------------------------------------------------
    # A. Chart families. Five of these have never been produced by any captured run.
    # ---------------------------------------------------------------------------------
    Query(
        key="a01-line",
        probes="temporal grouping, year granularity",
        query="How has the number of Alzheimer's disease trials changed each year since 2010?",
        expect_chart="line",
    ),
    Query(
        key="a02-bar-enum",
        probes="categorical grouping on a closed enum",
        query="How many diabetes trials are there by sponsor class?",
    ),
    Query(
        key="a03-grouped-bar",
        probes="two legs merged into a series",
        query="Compare phases for trials of metformin versus insulin",
    ),
    Query(
        key="a04-stacked",
        probes="a composition question, which is what stacked_bar exists for",
        query="Show the phase composition of oncology trials by sponsor class",
        note="stacked_bar has never been produced; if this returns grouped_bar the rules "
        "or the selector may never reach it",
    ),
    Query(
        key="a05-stacked-area",
        probes="composition over time — the only route to stacked_area",
        query="How has the mix of trial phases changed each year for melanoma since 2015?",
    ),
    Query(
        key="a06-pie",
        probes="a share-of-whole question on few categories",
        query="What is the breakdown of study types for asthma trials?",
    ),
    Query(
        key="a07-histogram",
        probes="numeric grouping, binning, log bin scale",
        query="What is the distribution of enrollment sizes in phase 3 breast cancer trials?",
        expect_chart="histogram",
    ),
    Query(
        key="a08-scatter",
        probes="point layout, two numeric measures, derived-field citations",
        query="Is there a relationship between enrollment and start year for gene therapy trials?",
        expect_chart="scatter",
    ),
    Query(
        key="a09-choropleth",
        probes="country grouping, per-trial dedup",
        query="Which countries run the most Parkinson's disease trials?",
    ),
    Query(
        key="a10-network-arm",
        probes="arm-scoped co-occurrence, the combination_groups path",
        query="Which drugs are frequently given together in combination trials for lymphoma?",
        expect_chart="network",
    ),
    Query(
        key="a11-network-colisted",
        probes="co-occurrence on a field with NO arm structure — the other pairing rule, "
        "which no captured example exercises",
        query="Which conditions are frequently studied together in the same trial?",
        expect_chart="network",
    ),
    Query(
        key="a12-kpi",
        probes="a single scalar answer",
        query="How many cystic fibrosis trials are there in total?",
    ),
    # ---------------------------------------------------------------------------------
    # B. Metrics. `count` is everywhere; the other three are barely exercised.
    # ---------------------------------------------------------------------------------
    Query(
        key="b01-distinct-count",
        probes="distinct_count, whose counting semantics differ from a trial count",
        query="How many distinct sponsors run clinical trials in Japan?",
    ),
    Query(
        key="b02-sum",
        probes="sum over a numeric field, where totals can exceed the population",
        query="What is the total enrollment of phase 3 HIV trials by year since 2015?",
    ),
    Query(
        key="b03-median",
        probes="median, and the skew warning that justifies it",
        query="What is the median enrollment by phase for lung cancer trials?",
    ),
    Query(
        key="b04-median-vs-count",
        probes="a question whose noun is participants, not trials — the judge's "
        "METRIC MISMATCH class",
        query="How many participants are enrolled in Alzheimer's trials by sponsor class?",
    ),
    # ---------------------------------------------------------------------------------
    # C. Posted results. Nine fields exist; one captured run touches them.
    # ---------------------------------------------------------------------------------
    Query(
        key="c01-deaths",
        probes="deaths with its own denominator, distinct from serious AEs",
        query="What is the median number of deaths in phase 3 cardiovascular trials, "
        "by sponsor class?",
    ),
    Query(
        key="c02-flow",
        probes="participant flow, first reporting period only",
        query="What is the median number of participants who completed phase 3 diabetes trials?",
    ),
    Query(
        key="c03-baseline-age",
        probes="baseline demographics read from the registry's own Total column",
        query="What is the typical baseline age of participants in completed obesity trials?",
    ),
    Query(
        key="c04-sex",
        probes="sex counts, and whether they are read as two fields rather than a ratio",
        query="How many female participants are there in phase 3 trials by sponsor class?",
    ),
    # ---------------------------------------------------------------------------------
    # D. Filters. Each of these is a clause that must appear in the issued URL.
    # ---------------------------------------------------------------------------------
    Query(
        key="d01-enrollment-range",
        probes="enrollment_min pushed down",
        query="How many recruiting trials have more than 1000 participants, by condition?",
    ),
    Query(
        key="d02-has-results",
        probes="has_results, one of the seven fields withheld from the Anthropic schema",
        query="How many phase 3 trials have posted results, by year since 2015?",
    ),
    Query(
        key="d03-study-type",
        probes="study_type as a filter rather than a grouping",
        query="How many observational obesity trials are there compared with interventional ones?",
    ),
    Query(
        key="d04-intervention-type",
        probes="intervention_type filter",
        query="Which countries run the most device trials for cardiac conditions?",
    ),
    Query(
        key="d05-date-certainty",
        probes="date_certainty, withheld from the Anthropic schema",
        query="How many trials with an actual recorded start date began in 2023, by phase?",
    ),
    Query(
        key="d06-site-status",
        probes="site_status WITHOUT a country — the clause that compiled to nothing",
        query="Which countries have actively recruiting sites for gene therapy trials?",
    ),
    Query(
        key="d07-sponsor",
        probes="sponsor filter on free text with no canonical identifier",
        query="How many trials does Pfizer sponsor, by phase?",
    ),
    Query(
        key="d08-quarter",
        probes="quarter granularity, which excludes year-only dates under its own reason",
        query="How many COVID-19 trials started each quarter in 2020?",
    ),
    # ---------------------------------------------------------------------------------
    # E. Multi-leg.
    # ---------------------------------------------------------------------------------
    Query(
        key="e01-three-way",
        probes="three legs, not two",
        query="Compare the number of trials for pembrolizumab, nivolumab and atezolizumab by year",
    ),
    Query(
        key="e02-overlapping",
        probes="legs that genuinely overlap, and the warning that says so",
        query="Compare trials involving aspirin with trials involving clopidogrel by phase",
    ),
    # ---------------------------------------------------------------------------------
    # F. Response types other than a chart.
    # ---------------------------------------------------------------------------------
    Query(
        key="f01-conversational",
        probes="the router's conversational branch: zero API calls, null visualization",
        query="Hi there — what kinds of questions can you answer?",
        expect_type="conversational",
    ),
    Query(
        key="f02-unsupported",
        probes="a question the registry cannot answer, with a stated obstruction",
        query="Which hospital has the best outcomes for heart surgery?",
        expect_type="unsupported",
    ),
    Query(
        key="f03-no-results",
        probes="an empty slice — a real answer, not an error",
        query="How many trials are there for xyzzyitis, by phase?",
        expect_type=None,
        note="expect no_results; the interesting failure is a chart with zero datums "
        "presented as though it had data",
    ),
    # ---------------------------------------------------------------------------------
    # G. Traps: questions whose obvious reading is wrong.
    # ---------------------------------------------------------------------------------
    Query(
        key="g01-superlative",
        probes="judge class 6 — a superlative with no top_n",
        query="What are the most common conditions studied in clinical trials?",
    ),
    Query(
        key="g02-deictic-param",
        probes="the assignment's own example shape: the query names nothing, the "
        "parameter supplies it",
        query="How many trials are there for this sponsor, by phase?",
        params={"sponsor": "Novartis Pharmaceuticals"},
    ),
    Query(
        key="g03-contradiction",
        probes="judge class 7 — question and parameter disagree, expect 422",
        query="How have melanoma trials changed over time?",
        params={"condition": "glioblastoma"},
        expect_type="error",
    ),
    Query(
        key="g04-ambiguous-place",
        probes="'Georgia' is a country and a US state; whichever is chosen must be stated "
        "in assumptions rather than silently picked",
        query="How many diabetes trials are running in Georgia?",
    ),
    Query(
        key="g05-whole-corpus",
        probes="no condition filter at all — page cap, truncation, and whether the "
        "response admits it is a sample",
        query="How many clinical trials started each year since 2000?",
        note="~600k trials match; the honest outcomes are truncation stated in "
        "record_counts, or a plan that bounds itself",
    ),
    Query(
        key="g06-mesh-vs-freetext",
        probes="whether the planner prefers intervention_mesh for a drug network, which "
        "is what the field registry's own note recommends",
        query="Which drug classes appear together most often in cancer trials?",
    ),
]


BY_KEY = {q.key: q for q in QUERIES}

__all__ = ["BY_KEY", "QUERIES", "Query"]
