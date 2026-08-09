"""Check a response against ClinicalTrials.gov, not against itself.

`audit.py` asks whether a response contradicts itself. It would pass a chart that is
internally flawless and systematically wrong, because a broken aggregator produces
perfectly consistent nonsense. This module goes back to the registry.

**It imports nothing from `cheiron`.** Not the normalizer, not the aggregator, not the
compiler. It issues its own HTTP requests and counts with a `dict`. A verification sharing
code with the thing it verifies mostly proves the code is self-consistent, which is the
one thing already known.

Four levels, strongest first. Each response gets the strongest one its plan allows, and
the level reached is reported — "verified" has to mean something specific per row rather
than being a blanket claim:

1. ``recount``     — refetch the slice and recompute every bucket in plain Python.
2. ``membership``  — refetch sampled trials by ID and confirm each really holds the value
                     its bucket claims.
3. ``total``       — compare `retrieved` against the registry's own `countTotal`.
4. ``none``        — the semantics need the aggregator to restate (arm-scoped
                     co-occurrence, medians over derived results fields), so no independent
                     check is claimed rather than a weak one being dressed up.

A level is not skipped silently: `verify` records which ran and which did not apply.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://clinicaltrials.gov/api/v2/studies"
TIMEOUT = 120

#: Registry field pieces needed to recount each grouping we can restate independently.
_RECOUNTABLE: dict[str, tuple[str, str]] = {
    "phases": ("Phase", "phase"),
    "status": ("OverallStatus", "scalar"),
    "study_type": ("StudyType", "scalar"),
    "sponsor_class": ("LeadSponsorClass", "scalar"),
    "sponsor_name": ("LeadSponsorName", "scalar"),
    "countries": ("LocationCountry", "list"),
    "conditions": ("Condition", "list"),
    "intervention_names": ("InterventionName", "list"),
}


@dataclass
class Verification:
    level: str                       # recount | membership | total | none
    ok: bool
    detail: str
    mismatches: list[str]


#: The registry rate-limits, and it answers a throttled request with an HTML error page
#: rather than JSON — which surfaces as "Expecting value: line 1 column 1". A verifier
#: without backoff therefore reports "verification failed" for what is really its own
#: impatience, and the first sweep lost its entire ground-truth layer that way.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _get(params: dict[str, str], attempts: int = 5) -> dict:
    delay = 1.0
    for attempt in range(attempts):
        try:
            response = httpx.get(BASE, params=params, timeout=TIMEOUT)
            if response.status_code in _RETRY_STATUS:
                raise httpx.HTTPError(f"HTTP {response.status_code}")
            return response.json()
        except (httpx.HTTPError, ValueError):
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


_KEEP = ("query.cond", "query.intr", "query.term", "query.spons", "query.locn",
         "filter.overallStatus", "filter.advanced", "filter.ids")


def _issued_queries(body: dict) -> list[dict[str, str]]:
    """Every distinct query the service says it issued — **one per leg**.

    Replayed rather than reconstructed: rebuilding it from the plan would re-derive the
    compiler's logic and could reproduce its bugs. `meta.api_requests` is what went out.

    First pages only. A paginated fetch repeats the same filters with a `pageToken`, and
    counting those again would multiply a leg's total by its page count. A comparison
    issues one query *per leg*, and `matched` is their sum — checking only the first leg
    against that sum reports a mismatch on every multi-leg plan, which is exactly what the
    first version of this function did.
    """
    urls = (body.get("meta") or {}).get("api_requests") or []
    seen: list[dict[str, str]] = []
    for url in urls:
        parsed = parse_qs(urlparse(url).query)
        if "pageToken" in parsed:
            continue
        query = {k: v[0] for k, v in parsed.items() if k in _KEEP}
        if query and query not in seen:
            seen.append(query)
    return seen


def _issued_params(body: dict) -> dict[str, str] | None:
    """The first leg's query, for the checks that examine a single slice."""
    queries = _issued_queries(body)
    return queries[0] if queries else None


# --------------------------------------------------------------------------------------
# Level 3 — does the slice itself have the size the response claims?
# --------------------------------------------------------------------------------------


def verify_total(body: dict) -> Verification:
    """`matched` against the registry's own `countTotal` for the same query.

    This is the cheap check that catches a filter which compiled to nothing: the response
    reports a filter, the URL does not carry it, and the total is far larger than it
    should be. It compares against `matched` rather than `used`, since exclusions and the
    page cap legitimately reduce what is used.
    """
    queries = _issued_queries(body)
    counts = (body.get("meta") or {}).get("record_counts") or {}
    if not queries or counts.get("matched") is None:
        return Verification("none", True, "no issued query to replay", [])

    per_leg = [
        _get({**q, "countTotal": "true", "pageSize": "1"}).get("totalCount", 0) for q in queries
    ]
    total, matched = sum(per_leg), counts["matched"]
    ok = total == matched
    breakdown = " + ".join(f"{n:,}" for n in per_leg) if len(per_leg) > 1 else f"{total:,}"
    return Verification(
        "total", ok,
        f"registry countTotal {breakdown} = {total:,} vs meta.matched={matched:,}",
        [] if ok else [
            f"the issued queries match {total:,} trials ({breakdown}), "
            f"response claims {matched:,}"
        ],
    )


# --------------------------------------------------------------------------------------
# Level 2 — is each cited trial really in the bucket it was placed in?
# --------------------------------------------------------------------------------------

_MEMBERSHIP_PATHS: dict[str, tuple[str, ...]] = {
    "phases": ("protocolSection.designModule.phases",),
    "status": ("protocolSection.statusModule.overallStatus",),
    "study_type": ("protocolSection.designModule.studyType",),
    "sponsor_class": ("protocolSection.sponsorCollaboratorsModule.leadSponsor.class",),
    "sponsor_name": ("protocolSection.sponsorCollaboratorsModule.leadSponsor.name",),
    "countries": ("protocolSection.contactsLocationsModule.locations[].country",),
    "conditions": ("protocolSection.conditionsModule.conditions",),
    "intervention_names": ("protocolSection.armsInterventionsModule.interventions[].name",),
    "intervention_types": ("protocolSection.armsInterventionsModule.interventions[].type",),
}


def _dig(record: Any, path: str) -> list[str]:
    """Values at a dotted path, flattening `[]` segments. No cheiron code involved."""
    nodes: list[Any] = [record]
    for part in path.split("."):
        key, is_list = (part[:-2], True) if part.endswith("[]") else (part, False)
        nxt: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            value = node.get(key)
            if value is None:
                continue
            nxt.extend(value if isinstance(value, list) else [value])
        nodes = nxt
        if is_list:
            continue
    out: list[str] = []
    for node in nodes:
        out.extend(str(v) for v in node) if isinstance(node, list) else out.append(str(node))
    return out


def verify_membership(body: dict, sample: int = 12) -> Verification:
    """Refetch sampled trials by ID and confirm each holds its bucket's value.

    Independent of our normalizer: the record is read straight from the registry and the
    value is looked for at the documented path. Applies to any grouping whose value the
    registry stores directly, which is most of them.
    """
    plan = (body.get("meta") or {}).get("plan") or {}
    group_by = plan.get("group_by")
    viz = body.get("visualization") or {}
    key = ((viz.get("encoding") or {}).get("x") or {}).get("field")
    paths = _MEMBERSHIP_PATHS.get(group_by or "")
    if not paths or viz.get("type") == "network" or plan.get("layout") == "point":
        return Verification("none", True, f"no membership rule for group_by={group_by!r}", [])

    wanted: list[tuple[str, str]] = []
    for datum in viz.get("data") or []:
        label = str(datum.get(key))
        if label in ("Other", "NOT_REPORTED"):
            continue
        for nct in (datum.get("nct_ids") or [])[:2]:
            wanted.append((nct, label))
        if len(wanted) >= sample:
            break
    if not wanted:
        return Verification("none", True, "no sampled trials to check", [])

    ids = sorted({n for n, _ in wanted})
    payload = _get({
        "filter.ids": "|".join(ids),
        "pageSize": str(len(ids)),
        "fields": "NCTId," + ",".join(
            {"phases": "Phase", "status": "OverallStatus", "study_type": "StudyType",
             "sponsor_class": "LeadSponsorClass", "sponsor_name": "LeadSponsorName",
             "countries": "LocationCountry", "conditions": "Condition",
             "intervention_names": "InterventionName",
             "intervention_types": "InterventionType"}[group_by]
            for _ in [0]
        ),
    })
    records = {
        s["protocolSection"]["identificationModule"]["nctId"]: s
        for s in payload.get("studies") or []
    }

    mismatches: list[str] = []
    for nct, label in wanted:
        record = records.get(nct)
        if record is None:
            mismatches.append(f"{nct} was cited but the registry did not return it")
            continue
        found = {v.casefold() for p in paths for v in _dig(record, p)}
        # A composite phase label is the trial's whole phase list joined; every member
        # must be present, which is the same rule the bucket was built under.
        members = (
            [m.casefold() for m in label.split("|")]
            if group_by == "phases"
            else [label.casefold()]
        )
        if not all(m in found for m in members):
            mismatches.append(
                f"{nct} is in bucket {label!r} but its record says {sorted(found) or 'nothing'}"
            )
    return Verification(
        "membership", not mismatches,
        f"{len(wanted)} sampled trial(s) re-fetched and checked against the registry",
        mismatches,
    )


# --------------------------------------------------------------------------------------
# Level 1 — recompute every bucket from the raw records
# --------------------------------------------------------------------------------------


def _canonical_phase(study: dict) -> str:
    """Re-derived from the rule, not from our code: a trial's whole phase list, joined."""
    order = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
    phases = (study.get("protocolSection", {}).get("designModule", {}) or {}).get("phases")
    if not phases:
        return "NOT_REPORTED"
    unique = sorted({p for p in phases if p}, key=lambda p: order.index(p) if p in order else 99)
    return "|".join(unique)


#: This verifier's own fetch ceiling, deliberately far above the service's page cap. It
#: cannot import that constant — nothing here imports from `cheiron` — so the two are kept
#: apart on purpose and this one is set high enough that it is never the binding limit.
#:
#: It was 20, matching the service's cap at the time. When the service's cap rose to 100
#: pages this verifier kept stopping at 20,000 and reported the difference as *the system
#: being wrong*: "INDUSTRY: published 6,606, recounted 5,500", nine such lines on one
#: query, every one of them the verifier's own truncation. A verifier that cannot see the
#: whole slice must say so, not accuse.
RECOUNT_PAGE_CEILING = 200


def verify_recount(body: dict, page_cap: int = RECOUNT_PAGE_CEILING) -> Verification:
    """Refetch the slice and recompute the buckets with plain Python.

    Only for `count` metrics on a grouping the registry stores directly, and only when the
    response was not truncated — a sample cannot be recounted against a whole slice.
    """
    plan = (body.get("meta") or {}).get("plan") or {}
    counts = (body.get("meta") or {}).get("record_counts") or {}
    viz = body.get("visualization") or {}
    group_by = plan.get("group_by")
    key = ((viz.get("encoding") or {}).get("x") or {}).get("field")
    params = _issued_params(body)

    if (
        params is None
        or plan.get("metric") != "count"
        or plan.get("layout") != "aggregate"
        or len(plan.get("legs") or []) != 1
        or plan.get("top_n")
        or counts.get("truncated")
        or group_by not in _RECOUNTABLE
    ):
        return Verification(
            "none", True, f"not independently recountable (group_by={group_by!r})", []
        )

    piece, kind = _RECOUNTABLE[group_by]
    studies: list[dict] = []
    token: str | None = None
    for _ in range(page_cap):
        query = {**params, "pageSize": "1000", "fields": f"NCTId,{piece}"}
        if token:
            query["pageToken"] = token
        payload = _get(query)
        studies.extend(payload.get("studies") or [])
        token = payload.get("nextPageToken")
        if not token:
            break

    if token is not None:
        # The ceiling was hit and more pages remain, so this recount covers less than the
        # response does. Comparing now would report the shortfall as a defect in the thing
        # being verified.
        return Verification(
            "none", True,
            f"recount abandoned: more than {page_cap:,} pages of results, so an "
            f"independent count would itself be truncated",
            [],
        )

    tally: dict[str, int] = defaultdict(int)
    for study in studies:
        if kind == "phase":
            tally[_canonical_phase(study)] += 1
        elif kind == "scalar":
            for value in _dig(study, _MEMBERSHIP_PATHS[group_by][0]):
                tally[value] += 1
        else:
            for value in {v for v in _dig(study, _MEMBERSHIP_PATHS[group_by][0]) if v}:
                tally[value] += 1

    published = {str(d.get(key)): float(d.get("value", 0)) for d in viz.get("data") or []}
    if sum(tally.values()) != counts.get("used"):
        # Totals must agree before buckets can be compared. If they do not, the two sides
        # saw different populations and every per-bucket line would be noise.
        return Verification(
            "none", True,
            f"recount saw {sum(tally.values()):,} trials against the response's "
            f"{counts.get('used'):,}; not comparable",
            [],
        )
    mismatches = [
        f"{label}: published {published.get(label, 0):g}, recounted {tally.get(label, 0)}"
        for label in sorted(set(published) | set(tally))
        if published.get(label, 0) != tally.get(label, 0) and label != "Other"
    ]
    return Verification(
        "recount", not mismatches,
        f"{len(studies):,} trials refetched, {len(tally)} buckets recomputed independently",
        mismatches,
    )


def verify(body: dict) -> Verification:
    """The strongest independent check this response's plan allows."""
    if body.get("response_type") not in ("visualization", "no_results"):
        return Verification("none", True, f"{body.get('response_type')} draws nothing", [])

    total = verify_total(body)
    if not total.ok:
        return total
    for stronger in (verify_recount, verify_membership):
        result = stronger(body)
        if result.level != "none":
            result.detail = f"{result.detail}; {total.detail}"
            return result
    total.detail += " (no stronger check applies)"
    return total


__all__ = ["Verification", "verify", "verify_membership", "verify_recount", "verify_total"]
