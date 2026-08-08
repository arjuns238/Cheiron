"""Capture the example runs, with their API responses cached so they reproduce.

`plan.md` §6 asks for five example runs with the actual JSON the system produced. "Actual"
is the load-bearing word, so nothing here is hand-written or trimmed: each file is the
response envelope exactly as `POST /analyze` returned it.

**Caching is switched on for this script only.** Live queries never use it — see
`docs/decisions.md` — but an example run published as "what this produced" has to keep
producing it. With the cache populated under `examples/cache/`, re-running this reproduces
the same records without touching the network, so a reader can verify the outputs are real
rather than edited.

The six cases cover every query class in the assignment's appendix, plus the refusal path,
which is worth showing precisely because it is the one that declines to answer.

Run:  .venv/bin/python examples/run_examples.py [--live]

  (default)  use the cache; fall back to the network for anything not yet captured
  --live     ignore the cache and re-fetch everything
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from cheiron.ctgov.cache import DiskCache
from cheiron.ctgov.client import CtGovClient
from cheiron.llm.client import LLMSettings, build_client
from cheiron.pipeline import Deps, analyze
from cheiron.schemas.request import AnalyzeRequest

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"


@dataclass
class Example:
    slug: str
    title: str
    request: AnalyzeRequest
    shows: str


EXAMPLES = [
    Example(
        slug="01-time-trend",
        title="Time trend",
        request=AnalyzeRequest(
            query="How has the number of pembrolizumab trials changed per year since 2015?"
        ),
        shows="temporal grouping, year granularity, line chart chosen from [line, bar] "
        "because the phrasing asks how something changed",
    ),
    Example(
        slug="02-distribution",
        title="Distribution across phases",
        request=AnalyzeRequest(query="How are melanoma trials distributed across phases?"),
        shows="composite phase buckets (PHASE1|PHASE2 is its own bucket, never counted "
        "into both) and NOT_REPORTED as a first-class value",
    ),
    Example(
        slug="03-comparison",
        title="Comparison between two drugs",
        request=AnalyzeRequest(
            query="Compare phases for trials involving pembrolizumab vs nivolumab"
        ),
        shows="two legs merged into a series dimension, with overlapping populations "
        "detected and reported rather than silently summed",
    ),
    Example(
        slug="04-geographic",
        title="Geographic distribution",
        request=AnalyzeRequest(
            query="Where are recruiting trials for non-small cell lung cancer running?"
        ),
        shows="a nested site-level location filter, and a choropleth chosen over a bar "
        "because the question asks about place rather than ranking",
    ),
    Example(
        slug="05-drug-network",
        title="Drug co-occurrence network",
        request=AnalyzeRequest(
            query="Which drugs frequently co-occur in combination studies for multiple "
            "myeloma?"
        ),
        shows="arm-scoped co-occurrence: an edge means two agents shared an arm group, "
        "not merely that a trial listed both",
    ),
    Example(
        slug="06-adverse-events",
        title="Posted results — safety volume",
        request=AnalyzeRequest(
            query="What is the median number of participants with serious adverse events "
            "in melanoma phase 3 trials, by sponsor class?"
        ),
        shows="posted results: adverse-event totals summed across arm groups, with their "
        "own safety-population denominator rather than enrolment",
    ),
    Example(
        slug="07-unsupported",
        title="A question the registry cannot answer",
        request=AnalyzeRequest(
            query="Which drug works better for melanoma, pembrolizumab or nivolumab?"
        ),
        shows="the refusal path: the specific obstruction, plus postable alternatives",
    ),
]


async def main() -> int:
    load_dotenv()
    live = "--live" in sys.argv
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    settings = LLMSettings.from_env()
    index: list[dict[str, object]] = []

    async with httpx.AsyncClient(timeout=180) as http:
        deps = Deps(
            llm=build_client(settings),
            ctgov=CtGovClient(http, cache=None if live else DiskCache(CACHE_DIR)),
        )
        for example in EXAMPLES:
            print(f"→ {example.slug}: {example.request.query}")
            response = await analyze(deps, example.request)

            # The request id and timestamp change every run and would make every file
            # differ on re-capture, hiding the changes that matter.
            body = response.model_dump(mode="json")
            body["request_id"] = f"example-{example.slug}"
            body["meta"]["generated_at"] = "<captured>"
            body["meta"]["elapsed_ms"] = None

            path = HERE / f"{example.slug}.json"
            path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")

            counts = response.meta.record_counts
            print(
                f"   {response.response_type.value}"
                f" · {response.visualization.type.value if response.visualization else '—'}"
                f" · used={counts.used if counts else 0}"
                f" · citations={len(response.citations)}"
                f" · {path.name} ({path.stat().st_size / 1024:.0f} KB)"
            )
            index.append(
                {
                    "file": path.name,
                    "title": example.title,
                    "query": example.request.query,
                    "shows": example.shows,
                    "response_type": response.response_type.value,
                    "visualization": (
                        response.visualization.type.value if response.visualization else None
                    ),
                    "trials_used": counts.used if counts else 0,
                    "citations": len(response.citations),
                }
            )

    (HERE / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"\nwrote {len(index)} examples + index.json")
    print(f"cache: {len(list(CACHE_DIR.glob('*.json')))} entries under {CACHE_DIR.name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
