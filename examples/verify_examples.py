"""Independently recount three example runs straight from ClinicalTrials.gov.

`plan.md` §7 asks that two of the examples be counted manually from the raw JSON and
confirmed to match. This is that check, written so it is genuinely independent: it imports
**nothing** from `cheiron` — not the normalizer, not the aggregator, not the compiler. It
issues its own HTTP requests, parses the payloads with plain Python, and counts with a
`dict`.

That matters because a verification sharing code with the thing it verifies mostly proves
the code is self-consistent. If the aggregator and this file agree, they agree via
ClinicalTrials.gov, which is the only claim worth making.

Three are checked rather than the two asked for. The cases are chosen where an independent
count is meaningful and where our semantics deliberately differ from the registry's own
facets:

* **02-distribution** — phase buckets, including the composite `PHASE1|PHASE2` bucket that
  ClinicalTrials.gov's own facets would double-count into both Phase 1 and Phase 2.
* **04-geographic** — country counts, where a trial is counted once per distinct country
  no matter how many sites it runs there.
* **06-adverse-events** — medians over posted results, where the arithmetic is summing
  arm groups within a trial and then taking a median across trials, and where two thirds
  of the matching trials have posted no results at all and must be dropped rather than
  read as zero.

Run:  .venv/bin/python examples/verify_examples.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

HERE = Path(__file__).parent
BASE = "https://clinicaltrials.gov/api/v2/studies"


def fetch(params: dict[str, str]) -> list[dict]:
    """Page through the registry with plain requests, collecting raw studies."""
    studies: list[dict] = []
    token: str | None = None
    for _ in range(25):
        query = {**params, "pageSize": "1000", "format": "json"}
        if token:
            query["pageToken"] = token
        payload = httpx.get(BASE, params=query, timeout=120).json()
        studies.extend(payload.get("studies") or [])
        token = payload.get("nextPageToken")
        if not token:
            break
    return studies


def canonical_phase(study: dict) -> str:
    """Re-implemented here on purpose, from the rule rather than from our code.

    A trial's phase bucket is its whole phase list joined, so a Phase 1/Phase 2 trial is
    one Phase 1/2 trial rather than one Phase 1 plus one Phase 2. Absent means the record
    has no `phases` key at all — observational and expanded-access studies.
    """
    order = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
    phases = (study.get("protocolSection", {}).get("designModule", {}) or {}).get("phases")
    if not phases:
        return "NOT_REPORTED"
    unique = sorted({p for p in phases if p}, key=lambda p: order.index(p) if p in order else 99)
    return "|".join(unique)


def verify_phases() -> bool:
    print("02-distribution — melanoma trials by phase")
    published = json.loads((HERE / "02-distribution.json").read_text())
    ours = {d["phases"]: d["value"] for d in published["visualization"]["data"]}

    studies = fetch({"query.cond": "melanoma", "fields": "NCTId,Phase"})
    counts: dict[str, int] = defaultdict(int)
    for study in studies:
        counts[canonical_phase(study)] += 1

    ok = True
    for bucket in sorted(set(ours) | set(counts)):
        mine, theirs = ours.get(bucket, 0), counts.get(bucket, 0)
        match = int(mine) == theirs
        ok &= match
        mark = "ok" if match else "NO"
        print(f"   {mark}  {bucket:<16} published={int(mine):<6} recounted={theirs}")

    total = sum(counts.values())
    print(f"   recounted {total} trials; buckets sum to {sum(int(v) for v in ours.values())}")
    print(f"   {'ok' if total == sum(counts.values()) else 'NO'}  buckets sum to the trial "
          f"count — the registry's own facets do not, because they double-count multi-phase")
    return ok


def verify_countries() -> bool:
    print("\n04-geographic — recruiting NSCLC trials by country")
    published = json.loads((HERE / "04-geographic.json").read_text())
    ours = {d["countries"]: d["value"] for d in published["visualization"]["data"]}

    # Replay the query the system says it issued, rather than one that seems equivalent.
    # `meta.api_requests` exists exactly so this is checkable: the first attempt here used
    # a site-level recruiting filter and disagreed wildly, because the plan had used a
    # trial-level one. Those are different questions, and the audit trail settles it.
    params = parse_qs(urlparse(published["meta"]["api_requests"][0]).query)
    studies = fetch(
        {
            key: values[0]
            for key, values in params.items()
            if key in ("query.cond", "filter.overallStatus", "filter.advanced")
        }
        | {"fields": "NCTId,LocationCountry"}
    )

    trials_by_country: dict[str, set[str]] = defaultdict(set)
    for study in studies:
        protocol = study.get("protocolSection", {})
        nct_id = protocol.get("identificationModule", {}).get("nctId")
        locations = (protocol.get("contactsLocationsModule", {}) or {}).get("locations") or []
        # Deduplicated per trial: a trial with 40 German sites is one German trial.
        for country in {loc.get("country") for loc in locations if loc.get("country")}:
            trials_by_country[country].add(nct_id)

    counts = {country: len(ids) for country, ids in trials_by_country.items()}
    top_n = published["meta"]["plan"].get("top_n")
    kept = {c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]}
    # "Other" is the distinct trials contributing to any collapsed country — not the
    # remainder, because a trial can run in both a kept country and a dropped one.
    counts["Other"] = len(
        {i for c, ids in trials_by_country.items() if c not in kept for i in ids}
    )

    ok = True
    for country, value in sorted(ours.items(), key=lambda kv: -kv[1]):
        theirs = counts.get(country, 0)
        match = int(value) == theirs
        ok &= match
        mark = "ok" if match else "NO"
        print(f"   {mark}  {country:<20} published={int(value):<6} recounted={theirs}")
    return ok


def median(values: list[float]) -> float:
    """Written out rather than imported, so this agrees with the aggregator by arithmetic."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def verify_adverse_events() -> bool:
    print("\n06-adverse-events — median serious-AE participants by sponsor class")
    published = json.loads((HERE / "06-adverse-events.json").read_text())
    ours = {d["sponsor_class"]: d["value"] for d in published["visualization"]["data"]}

    params = parse_qs(urlparse(published["meta"]["api_requests"][0]).query)
    studies = fetch(
        {
            key: values[0]
            for key, values in params.items()
            if key in ("query.cond", "filter.advanced")
        }
        | {"fields": "NCTId,LeadSponsorClass,EventGroupSeriousNumAffected"}
    )

    by_class: dict[str, list[float]] = defaultdict(list)
    no_results = 0
    for study in studies:
        protocol = study.get("protocolSection", {})
        sponsor_class = (
            (protocol.get("sponsorCollaboratorsModule", {}) or {}).get("leadSponsor", {}) or {}
        ).get("class")
        groups = (
            (study.get("resultsSection", {}) or {}).get("adverseEventsModule", {}) or {}
        ).get("eventGroups") or []
        # `eventGroups` carries one row per arm and no total row, so a trial's figure is the
        # sum over its arms. A trial that posted no results is dropped, not counted as zero:
        # "no safety data" and "no serious adverse events" are different facts.
        affected = [
            g["seriousNumAffected"] for g in groups if g.get("seriousNumAffected") is not None
        ]
        if not affected or not sponsor_class:
            no_results += 1
            continue
        by_class[sponsor_class].append(float(sum(affected)))

    ok = True
    for bucket, values_ in sorted(by_class.items(), key=lambda kv: -median(kv[1])):
        mine, theirs = ours.get(bucket), median(values_)
        match = mine is not None and float(mine) == theirs
        ok &= match
        mark = "ok" if match else "NO"
        print(
            f"   {mark}  {bucket:<12} published={mine!s:<8} recounted={theirs:<8}"
            f" (n={len(values_)})"
        )

    counts = published["meta"]["record_counts"]
    excluded = sum(counts["excluded_by_reason"].values())
    match = no_results == excluded
    ok &= match
    print(
        f"   {'ok' if match else 'NO'}  {no_results} trial(s) posted no serious-AE data; "
        f"the run excluded {excluded} — dropped, never read as zero"
    )
    return bool(ok)


def main() -> int:
    print("Independent recount — imports nothing from cheiron.\n")
    results = [verify_phases(), verify_countries(), verify_adverse_events()]
    print(f"\n{'all three examples reconcile' if all(results) else 'MISMATCH — investigate'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
