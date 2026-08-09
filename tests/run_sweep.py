"""Run the sweep corpus, audit every response, and report what is broken.

Three layers, and the order matters:

1. **Self-consistency** (`audit.py`) — does the response contradict itself? Cheap, no
   network, runs on every result.
2. **Ground truth** (`ground_truth.py`) — does it match ClinicalTrials.gov? Costs
   requests, and reports which level of verification each row actually reached.
3. **Judgement** — whether the plan answers the question. Not automatable; the runner
   prints what a reviewer needs to decide it, which is the plan, the answer and the
   counts.

Results are written per query so a failing case can be re-run and diffed on its own.

Run:  .venv/bin/python tests/run_sweep.py [openai|anthropic]
                                          [--only PREFIX] [--no-truth] [--concurrency N]

Not collected by pytest, and it costs real model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from cheiron.ctgov.client import CtGovClient
from cheiron.llm.client import LLMSettings, build_client
from cheiron.pipeline import Deps, analyze
from cheiron.schemas.request import AnalyzeRequest, OverrideConflict

sys.path.insert(0, str(Path(__file__).parent))
from audit import audit  # noqa: E402
from ground_truth import verify  # noqa: E402
from sweep_queries import QUERIES  # noqa: E402

OUT = Path(__file__).parent / "sweep_results"


async def run_one(deps: Deps, case, results: dict) -> dict:
    """One query, end to end, with every failure captured rather than raised."""
    started = time.monotonic()
    record: dict = {"key": case.key, "query": case.query, "probes": case.probes}
    try:
        request = AnalyzeRequest(query=case.query, **case.params)
        response = await analyze(deps, request)
        body = response.model_dump(mode="json")
        record["body"] = body
        record["response_type"] = body.get("response_type")
        record["chart"] = (body.get("visualization") or {}).get("type")
    except OverrideConflict as exc:
        record.update(response_type="error", error="OverrideConflict", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — a crash is a finding, not a stop
        record.update(response_type="crash", error=type(exc).__name__, detail=str(exc))
    record["elapsed_s"] = round(time.monotonic() - started, 1)
    results[case.key] = record
    return record


def review(case, record: dict, check_truth: bool) -> dict:
    """Audit one captured result. Separated from running so it can be redone offline."""
    findings: list[dict] = []
    expected = case.expect_type
    actual = record.get("response_type")

    if actual == "crash":
        findings.append({"check": "crash", "severity": "wrong",
                         "detail": f"{record['error']}: {record['detail'][:200]}"})
    elif expected and expected != actual:
        # `no_results` is a legitimate outcome wherever a chart was expected, so it is
        # reported for a human rather than counted as a failure.
        severity = "suspect" if actual == "no_results" else "wrong"
        findings.append({"check": "response-type", "severity": severity,
                         "detail": f"expected {expected!r}, got {actual!r}"})

    body = record.get("body")
    if body:
        findings += [
            {"check": f.check, "severity": f.severity, "detail": f.detail}
            for f in audit(body, case.params)
        ]
        if case.expect_chart and record.get("chart") != case.expect_chart:
            findings.append({"check": "chart", "severity": "suspect",
                             "detail": f"expected {case.expect_chart!r}, got {record['chart']!r}"})
        if check_truth:
            try:
                truth = verify(body)
                record["truth_level"] = truth.level
                record["truth_detail"] = truth.detail
                findings += [{"check": f"ground-truth:{truth.level}", "severity": "wrong",
                              "detail": m} for m in truth.mismatches]
            except Exception as exc:  # noqa: BLE001
                record["truth_level"] = "error"
                findings.append({"check": "ground-truth", "severity": "suspect",
                                 "detail": f"verification itself failed: {exc}"})
    record["findings"] = findings
    return record


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    args = sys.argv[1:]
    if args and args[0] in ("openai", "anthropic"):
        os.environ["LLM_PROVIDER"] = args[0]
    only = args[args.index("--only") + 1] if "--only" in args else None
    check_truth = "--no-truth" not in args
    concurrency = int(args[args.index("--concurrency") + 1]) if "--concurrency" in args else 3

    cases = [q for q in QUERIES if not only or q.key.startswith(only)]
    OUT.mkdir(exist_ok=True)

    # Re-auditing must not mean re-paying for the model calls. Running and reviewing are
    # separate phases precisely so a fix to an auditor can be re-checked against the saved
    # bodies — which is how the first sweep's lost ground-truth layer was recovered.
    if "--review-only" in args:
        results = {}
        for case in cases:
            path = OUT / f"{case.key}.json"
            if not path.is_file():
                continue
            try:
                results[case.key] = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                print(f"  skipped {case.key}: saved result is unreadable ({exc})")
        print(f"re-auditing {len(results)} saved result(s), no model calls\n")
        return report(cases, results, check_truth)
    settings = LLMSettings.from_env()
    print(f"sweep — {len(cases)} queries, {settings.provider.value}/{settings.model_large}, "
          f"concurrency {concurrency}, ground truth {'on' if check_truth else 'off'}\n")

    results: dict = {}
    gate = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=300) as http:
        deps = Deps(llm=build_client(settings), ctgov=CtGovClient(http))

        async def guarded(case):
            async with gate:
                record = await run_one(deps, case, results)
                print(f"  ran  {case.key:22} {record.get('response_type','?'):14} "
                      f"{str(record.get('chart') or '—'):12} {record['elapsed_s']:>5}s")
                return record

        await asyncio.gather(*(guarded(c) for c in cases))

    return report(cases, results, check_truth)


def report(cases, results: dict, check_truth: bool) -> int:
    print("\nauditing (ground truth costs requests, so this is the slow half)\n")
    worst: list[tuple[str, dict]] = []
    for case in cases:
        if case.key not in results:
            continue
        record = review(case, results[case.key], check_truth)
        # Written whole. Truncating produced invalid JSON that the review-only pass could
        # not read back, which defeated the point of saving it; the directory is gitignored.
        (OUT / f"{case.key}.json").write_text(json.dumps(record, indent=2))
        bad = [f for f in record["findings"] if f["severity"] == "wrong"]
        mis = [f for f in record["findings"] if f["severity"] == "misleading"]
        mark = "FAIL" if bad else ("MISLEAD" if mis else "ok")
        print(f"  {mark:8} {case.key:22} truth={record.get('truth_level','—'):11} "
              f"{len(record['findings'])} finding(s)")
        for finding in record["findings"]:
            print(f"           [{finding['severity']}] {finding['check']}: "
                  f"{finding['detail'][:150]}")
        if bad or mis:
            worst.append((case.key, record))

    total = sum(len(r["findings"]) for r in results.values() if "findings" in r)
    print(f"\n{len(cases)} queries · {total} findings · {len(worst)} needing attention")
    print(f"results written to {OUT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
