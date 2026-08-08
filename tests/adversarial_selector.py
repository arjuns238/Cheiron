"""Chart selector evaluation — run against a live model, not in the test suite.

The selector cannot produce an *illegal* chart; membership is enforced in code. What it
can do is pick the worse of two legal options, and nothing downstream would notice. So the
question worth measuring is not safety but usefulness: does it beat the rules' default?

Each case is a pair of questions over the **same result shape**, differing only in
phrasing. That is the whole reason this stage exists — the aggregation cannot tell "how has
X changed" from "which year had the most", and only the wording can.

A case where the expectation equals the rules' default tests nothing about the model, so
each pair contains one of each.

Run:  .venv/bin/python tests/adversarial_selector.py [anthropic|openai]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from cheiron.llm.client import LLMSettings, build_client
from cheiron.llm.selector import select
from cheiron.schemas.fields import FieldKind
from cheiron.schemas.plan import Layout, Metric
from cheiron.viz.rules import Shape, legal_charts


def shape(kind: FieldKind, field: str, buckets: int, labels: tuple[str, ...]) -> Shape:
    return Shape(
        group_kind=kind,
        series_kind=None,
        metric=Metric.COUNT,
        layout=Layout.AGGREGATE,
        binned=False,
        bucket_count=buckets,
        series_count=0,
        has_other=False,
        sample_labels=labels,
        group_field=field,
    )


TEMPORAL = shape(FieldKind.TEMPORAL, "start_date", 12, ("2015", "2016", "2017"))
PHASES = shape(FieldKind.CATEGORICAL, "phases", 6, ("PHASE2", "PHASE3", "NA"))
COUNTRIES = shape(FieldKind.ENTITY, "countries", 9, ("China", "United States", "Spain"))


@dataclass
class Case:
    query: str
    shape: Shape
    expected: str


CASES = [
    Case("How has the number of melanoma trials changed since 2015?", TEMPORAL, "line"),
    Case("Which year had the most melanoma trials?", TEMPORAL, "bar"),
    Case("What is the trend in melanoma trials over time?", TEMPORAL, "line"),
    Case("What share of melanoma trials is in each phase?", PHASES, "pie"),
    Case("Which phase has the most melanoma trials?", PHASES, "bar"),
    Case("Where are recruiting NSCLC trials running?", COUNTRIES, "choropleth"),
    Case("What is the geographic distribution of NSCLC trials?", COUNTRIES, "choropleth"),
    Case("Which are the top five countries for NSCLC trials?", COUNTRIES, "bar"),
]


async def main() -> int:
    load_dotenv()
    if len(sys.argv) > 1:
        os.environ["LLM_PROVIDER"] = sys.argv[1]
    settings = LLMSettings.from_env()
    client = build_client(settings)

    print(f"chart selector — {settings.provider.value} / {settings.model_small}\n")
    hits = beat_default = 0

    for case in CASES:
        legal = legal_charts(case.shape)
        chosen = (await select(client, case.query, case.shape)).value
        ok = chosen == case.expected
        hits += ok
        # Cases where the right answer is not the default are the ones that measure the
        # model; the rest would pass with no model at all.
        if case.expected != legal[0].value:
            beat_default += ok

        print(f"[{'ok' if ok else 'NO'}] {case.query}")
        print(f"     legal={[c.value for c in legal]} chose={chosen} expected={case.expected}")

    non_default = sum(1 for c in CASES if c.expected != legal_charts(c.shape)[0].value)
    print(f"\ncorrect            : {hits}/{len(CASES)}")
    print(f"beat the default   : {beat_default}/{non_default}  (the cases a model is needed for)")
    return 0 if hits == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
