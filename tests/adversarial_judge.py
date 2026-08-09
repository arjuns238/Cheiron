"""The judge's adversarial set — run against a live model, not in the test suite.

`plan.md` §7 asks for deliberately wrong plans, a check that the judge flags them, and an
honest report of the result "including if it doesn't". A verification layer nobody has
tested is a claim, not a feature.

Each case below is *legal* — it passes every plan-validator rule — and answers a different
question than the one asked. That is exactly the gap the judge exists to cover, and the
gap the validator provably cannot.

The control cases matter as much as the failures. A judge that flags everything is as
useless as one that flags nothing, and only the correct plans reveal which it is.

Run:  .venv/bin/python tests/adversarial_judge.py [anthropic|openai]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from cheiron.llm.client import LLMSettings, build_client
from cheiron.llm.judge import review
from cheiron.schemas.plan import Filters, Granularity, Layout, Leg, Metric, Plan
from cheiron.schemas.request import Phase, Status


@dataclass
class Case:
    name: str
    query: str
    plan: Plan
    should_flag: bool
    #: What a correct review would notice, for reporting when it does not.
    expected: str = ""
    #: Structured parameters the caller supplied, for the class-7 cases.
    overrides: dict[str, object] | None = None
    #: True when the correct verdict is the fatal `contradiction`, not an advisory concern.
    fatal: bool = False


CASES = [
    # ---- deliberately wrong: each is legal and answers the wrong question -------------
    Case(
        name="metric mismatch",
        query="What is the median enrollment of melanoma phase 3 trials?",
        plan=Plan(
            legs=[
                Leg(
                    label="Melanoma",
                    filters=Filters(condition="melanoma", phase=[Phase.PHASE3]),
                )
            ],
            group_by="sponsor_class",
            metric=Metric.COUNT,
        ),
        should_flag=True,
        expected="asked about enrolment, plan counts trials",
    ),
    Case(
        name="collapsed comparison",
        query="Compare trial phases for pembrolizumab versus nivolumab.",
        plan=Plan(
            legs=[
                Leg(
                    label="Immunotherapy",
                    filters=Filters(free_text="pembrolizumab nivolumab"),
                )
            ],
            group_by="phases",
        ),
        should_flag=True,
        expected="two drugs compared, plan has one leg",
    ),
    Case(
        name="wrong field",
        query="How many melanoma trials are there by phase?",
        plan=Plan(
            legs=[Leg(label="Melanoma", filters=Filters(intervention="melanoma"))],
            group_by="phases",
        ),
        should_flag=True,
        expected="a disease placed in the intervention filter",
    ),
    Case(
        name="time basis",
        query="How many trials started each year since 2015?",
        plan=Plan(
            legs=[
                Leg(label="All", filters=Filters(condition="melanoma", start_year_min=2015))
            ],
            group_by="completion_date",
            granularity=Granularity.YEAR,
        ),
        should_flag=True,
        expected="asked about start dates, plan groups on completion",
    ),
    # ---- controls: correct plans that must NOT be flagged -----------------------------
    Case(
        name="control: phase distribution",
        query="How are melanoma trials distributed across phases?",
        plan=Plan(
            legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
            group_by="phases",
        ),
        should_flag=False,
    ),
    Case(
        name="control: real comparison",
        query="Compare trial phases for pembrolizumab versus nivolumab.",
        plan=Plan(
            legs=[
                Leg(label="Pembrolizumab", filters=Filters(intervention="pembrolizumab")),
                Leg(label="Nivolumab", filters=Filters(intervention="nivolumab")),
            ],
            group_by="phases",
        ),
        should_flag=False,
    ),
    Case(
        name="control: median enrollment",
        query="What is the median enrollment of melanoma phase 3 trials by sponsor class?",
        plan=Plan(
            legs=[
                Leg(
                    label="Melanoma",
                    filters=Filters(condition="melanoma", phase=[Phase.PHASE3]),
                )
            ],
            group_by="sponsor_class",
            metric=Metric.MEDIAN,
            metric_field="enrollment",
        ),
        should_flag=False,
    ),
    Case(
        name="control: trials per year",
        query="How many melanoma trials started each year since 2015?",
        plan=Plan(
            legs=[
                Leg(
                    label="Melanoma",
                    filters=Filters(condition="melanoma", start_year_min=2015),
                )
            ],
            group_by="start_date",
            granularity=Granularity.YEAR,
        ),
        should_flag=False,
    ),
    Case(
        name="unquantified superlative: 'frequently' with no top_n",
        query="Which drugs frequently co-occur in combination studies for multiple myeloma?",
        plan=Plan(
            legs=[Leg(label="Multiple myeloma", filters=Filters(condition="multiple myeloma"))],
            group_by="intervention_names",
            layout=Layout.COOCCURRENCE,
        ),
        should_flag=True,
        expected=(
            "'frequently' asks for the common pairings; with top_n null the plan returns "
            "every pair that occurs at all, including pairs seen once. Observed live: "
            "5,215 edges over 1,234 nodes, 76% of which appear in a single trial."
        ),
    ),
    Case(
        name="unquantified superlative: 'most common' sponsors",
        query="Who are the most common sponsors of melanoma trials?",
        plan=Plan(
            legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
            group_by="sponsor_name",
        ),
        should_flag=True,
        expected=(
            "'most common' asks for a ranked head; 51,497 distinct lead sponsors exist "
            "corpus-wide, so an unrestricted plan is a list rather than an answer."
        ),
    ),
    Case(
        name="control: 'which' with no superlative is fine unrestricted",
        query="Which phases are melanoma trials in?",
        plan=Plan(
            legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
            group_by="phases",
        ),
        should_flag=False,
    ),
    Case(
        name="control: the superlative is already expressed by top_n",
        query="What are the top 10 conditions studied in phase 3 trials?",
        plan=Plan(
            legs=[Leg(label="Phase 3", filters=Filters(phase=[Phase.PHASE3]))],
            group_by="conditions",
            top_n=10,
        ),
        should_flag=False,
    ),
    Case(
        name="parameter contradiction: question and condition disagree",
        query="How have melanoma trials changed over time?",
        plan=Plan(
            legs=[Leg(label="Glioblastoma", filters=Filters(condition="glioblastoma"))],
            group_by="start_date",
            granularity=Granularity.YEAR,
        ),
        overrides={"condition": "glioblastoma"},
        should_flag=True,
        fatal=True,
        expected=(
            "the question names melanoma and the caller pinned condition=glioblastoma; "
            "no plan satisfies both, so this is fatal rather than advisory"
        ),
    ),
    Case(
        name="control: the question leaves the dimension open",
        query="How has the number of trials for this drug changed over time?",
        plan=Plan(
            legs=[Leg(label="Pembrolizumab", filters=Filters(intervention="Pembrolizumab"))],
            group_by="start_date",
            granularity=Granularity.YEAR,
        ),
        overrides={"drug_name": "Pembrolizumab"},
        should_flag=False,
    ),
    Case(
        name="control: parameter agrees with the question",
        query="How have melanoma trials changed over time?",
        plan=Plan(
            legs=[Leg(label="Melanoma", filters=Filters(condition="melanoma"))],
            group_by="start_date",
            granularity=Granularity.YEAR,
        ),
        overrides={"condition": "Melanoma"},
        should_flag=False,
    ),
    Case(
        name="dropped qualifier: 'actual recorded start date' not filtered",
        query="How many trials with an actual recorded start date began in 2023, by phase?",
        plan=Plan(
            legs=[Leg(label="2023", filters=Filters(start_year_min=2023, start_year_max=2023))],
            group_by="phases",
        ),
        should_flag=True,
        expected=(
            "the question restricts to actual (not estimated) start dates and the plan "
            "sets no date_certainty, so it answers about every 2023 trial. Observed live "
            "in the sweep, where it also silently truncated at the page cap."
        ),
    ),
    Case(
        name="dropped qualifier: 'posted results' not filtered",
        query="How many phase 3 trials that have posted results started each year since 2015?",
        plan=Plan(
            legs=[
                Leg(label="Phase 3", filters=Filters(phase=[Phase.PHASE3], start_year_min=2015))
            ],
            group_by="start_date",
            granularity=Granularity.YEAR,
        ),
        should_flag=True,
        expected="has_results is never set, so trials without posted results are included",
    ),
    Case(
        name="control: a question with no qualifier needs no filter",
        query="How many trials are there by phase?",
        plan=Plan(legs=[Leg(label="All")], group_by="phases"),
        should_flag=False,
    ),
    Case(
        name="control: every stated qualifier is present",
        query="How many recruiting phase 3 melanoma trials are there by sponsor class?",
        plan=Plan(
            legs=[
                Leg(
                    label="Melanoma",
                    filters=Filters(
                        condition="melanoma", phase=[Phase.PHASE3], status=[Status.RECRUITING]
                    ),
                )
            ],
            group_by="sponsor_class",
        ),
        should_flag=False,
    ),
]


async def main() -> int:
    load_dotenv()
    if len(sys.argv) > 1:
        os.environ["LLM_PROVIDER"] = sys.argv[1]
    settings = LLMSettings.from_env()
    client = build_client(settings)

    print(f"judge adversarial set — {settings.provider.value} / {settings.model_large}\n")
    caught = missed = correct = false_alarm = 0

    for case in CASES:
        verdict = await review(
            client, case.query, case.plan, overrides=case.overrides
        )
        flagged = verdict.is_concerned
        ok = flagged == case.should_flag
        # Class 7 must be *fatal*, not merely flagged: an advisory concern would spend a
        # re-plan on a disagreement no plan can resolve.
        if ok and case.fatal:
            ok = verdict.is_contradiction
        if ok and case.should_flag and not case.fatal:
            ok = not verdict.is_contradiction

        if case.should_flag:
            caught += ok
            missed += not ok
        else:
            correct += ok
            false_alarm += not ok

        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case.name}")
        print(
            f"       verdict={verdict.verdict!r} flagged={flagged} "
            f"expected={case.should_flag}" + (" (fatal)" if case.fatal else "")
        )
        if case.expected and not ok:
            print(f"       should have noticed: {case.expected}")
        for concern in verdict.concerns[:2]:
            print(f"       - {concern}")
        print()

    bad = sum(1 for c in CASES if c.should_flag)
    good = len(CASES) - bad
    print(f"wrong plans caught : {caught}/{bad}")
    print(f"correct plans kept : {correct}/{good}")
    if missed:
        print(f"MISSED {missed} wrong plan(s) — report this in the README as measured")
    if false_alarm:
        print(
            f"FALSE ALARM on {false_alarm} correct plan(s) — "
            f"a judge that flags everything is useless"
        )
    return 0 if not (missed or false_alarm) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
