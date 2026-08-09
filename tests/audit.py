"""Auditors that check a response against itself.

These answer "does this response contradict itself?" — not "is it right". A broken
aggregator produces internally perfect nonsense, and nothing here would notice; that is
`ground_truth.py`'s job. Both are needed, and neither substitutes for the other.

Every check below exists because something got through without it. The provenance is
recorded per function, because a check whose motivating bug is forgotten is a check
someone deletes as redundant.

Findings carry a severity:

* ``wrong``   — a value or a claim is false. The response should not have been returned.
* ``misleading`` — every number is correct and the words around them are not. This is the
  category that keeps recurring, and the one no test suite catches by accident.
* ``suspect`` — defensible, but worth a human deciding.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "wrong" | "misleading" | "suspect"
    detail: str


def _datums(body: dict) -> list[dict]:
    """Flat datums, or edges for a network — the units a chart actually draws."""
    viz = body.get("visualization") or {}
    data = viz.get("data")
    if isinstance(data, dict):
        return list(data.get("edges") or [])
    return list(data or [])


def _dimension_key(body: dict) -> str | None:
    """The key holding each datum's label, or None when nothing is bound.

    An `unsupported` response carries an empty visualization block whose encoding is null,
    so every accessor here has to tolerate absence rather than assume the happy shape.
    """
    encoding = (body.get("visualization") or {}).get("encoding") or {}
    return (encoding.get("x") or {}).get("field")


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------


def check_response_type_matches_content(body: dict) -> Iterator[Finding]:
    """`conversational` is the only null visualization, and refusals draw nothing."""
    kind = body.get("response_type")
    viz = body.get("visualization")
    if kind == "conversational":
        if viz is not None:
            yield Finding("response_type", "wrong", "conversational carries a visualization")
        return
    if viz is None:
        yield Finding("response_type", "wrong", f"{kind} has a null visualization")
        return
    if kind in ("unsupported", "no_results") and _datums(body):
        yield Finding(
            "response_type", "wrong",
            f"{kind} carries {len(_datums(body))} datum(s) — an empty state presented as data",
        )
    if kind == "visualization" and not _datums(body):
        yield Finding(
            "response_type", "wrong",
            "response_type is 'visualization' with no datums; should be no_results",
        )
    if kind in ("unsupported", "no_results") and not (body.get("answer") or "").strip():
        yield Finding(
            "response_type", "misleading",
            f"{kind} with no prose — the obstruction is the whole answer and it is missing",
        )


def check_every_datum_carries_the_encoded_dimension(body: dict) -> Iterator[Finding]:
    """A renderer reads `encoding.x.field`; a datum missing that key cannot be drawn.

    The demo frontend learns the key from the encoding rather than hardcoding one, which
    is only safe if the key is actually present on every datum.
    """
    viz = body.get("visualization") or {}
    # A network binds node ids, and a KPI is a single number with no dimension at all —
    # `Encoding` documents that only `y` is set for it.
    if viz.get("type") in ("network", "kpi") or not _datums(body):
        return
    key = _dimension_key(body)
    if not key:
        yield Finding("encoding", "wrong", "encoding.x.field is missing on a flat chart")
        return
    missing = [i for i, d in enumerate(_datums(body)) if key not in d]
    if missing:
        yield Finding(
            "encoding", "wrong",
            f"{len(missing)} datum(s) lack the encoded key {key!r} (first at index {missing[0]})",
        )


def check_values_are_finite(body: dict) -> Iterator[Finding]:
    for i, datum in enumerate(_datums(body)):
        value = datum.get("value", datum.get("weight"))
        if value is None:
            yield Finding("values", "wrong", f"datum {i} has no value")
        elif not math.isfinite(float(value)):
            yield Finding("values", "wrong", f"datum {i} value is {value}")


def check_sample_never_exceeds_its_total(body: dict) -> Iterator[Finding]:
    """`nct_ids` is a sample of `nct_id_total`; the reverse would overstate every bar."""
    for i, datum in enumerate(_datums(body)):
        ids, total = datum.get("nct_ids") or [], datum.get("nct_id_total")
        if total is None:
            continue
        if len(ids) > total:
            yield Finding(
                "sampling", "wrong", f"datum {i}: {len(ids)} ids sampled from a total of {total}"
            )
        if total == 0 and float(datum.get("value", datum.get("weight", 0)) or 0) > 0:
            yield Finding(
                "sampling", "wrong", f"datum {i} has a non-zero value and no contributing trials"
            )


# --------------------------------------------------------------------------------------
# Citations — the class of bug that verified perfectly while being wrong
# --------------------------------------------------------------------------------------

#: Both composite forms the aggregator produces, mirroring `citations._COMPOSITE_SEPARATORS`.
#: A composite label is never a literal substring of the record — the registry stores
#: `["PHASE1","PHASE2"]`, not `"PHASE1|PHASE2"` — so it is satisfied component by component.
_COMPOSITES = (" || ", "|")


def _components(value: str) -> list[str]:
    for separator in _COMPOSITES:
        if separator in value:
            return [p for p in value.split(separator) if p]
    return [value]


def _renderings(value: str) -> list[str]:
    """Forms of a coded value that an excerpt may legitimately use.

    The citation builder prefers a prose span from the title when the title states the
    value, and no title contains the literal token `PHASE2` — it says "Phase 2" or
    "Phase II". Treating that as a mismatch would flag the citations closest to the
    assignment's own illustrative example.
    """
    out = [value]
    match = re.fullmatch(r"(EARLY_)?PHASE(\d)", value)
    if match:
        number = match.group(2)
        roman = {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(number, number)
        prefix = "early phase " if match.group(1) else "phase "
        out += [f"{prefix}{number}", f"{prefix}{roman}"]
    return out


def check_citations_state_their_own_datum(body: dict) -> Iterator[Finding]:
    """Provenance: clicking Canada showed `"country":"United States"`.

    Citations were keyed by NCT ID at response level, so on a multi-valued dimension the
    first datum to claim a trial won and every later one read a citation for a different
    bucket. 32 of 55 lookups were wrong. Each verified perfectly at its offsets — offsets
    were never the problem, which is why this check is separate from re-verification.
    """
    viz = body.get("visualization") or {}
    key = _dimension_key(body)
    is_network = viz.get("type") == "network"

    for i, datum in enumerate(_datums(body)):
        label = str(datum.get(key)) if key and not is_network else None
        for citation in datum.get("citations") or []:
            if citation.get("supports") == "series":
                continue
            stated = str(citation.get("field_value", ""))
            excerpt = str(citation.get("excerpt", ""))

            # The excerpt must state what it is cited for. A composite is satisfied when
            # every component appears; a temporal bucket cites the fuller date it derives
            # from, which is correct rather than a mismatch.
            parts = _components(stated)
            folded = excerpt.casefold()
            if not all(
                any(r.casefold() in folded for r in _renderings(p)) for p in parts
            ):
                yield Finding(
                    "citation-excerpt", "wrong",
                    f"datum {i}: cited for {stated!r} but the excerpt does not say it: "
                    f"{excerpt[:80]!r}",
                )
            if label is None or label == "Other":
                continue
            agrees = (
                stated.casefold() == label.casefold()
                or stated in label.split("|")
                or stated.startswith(label)      # 2015 <- 2015-06-03
                or _same_number(stated, label)   # 0 <- 0.0 on a scatter axis
                or _falls_inside(stated, label)  # 3 <- "1-3.2"; 2020-02-12 <- "2020-Q1"
            )
            if not agrees:
                yield Finding(
                    "citation-bucket", "wrong",
                    f"datum {i} labelled {label!r} cites a trial for {stated!r}",
                )


_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
_BIN = re.compile(r"^(-?[\d.]+)[\u2013-]([\d.]+)$")


def _falls_inside(stated: str, label: str) -> bool:
    """Whether a raw value legitimately sits in a derived bucket.

    A histogram bin and a quarter are both *computed* labels: no record contains the
    string "1-3.2" or "2020-Q1", so the citation quotes the enrolment or the date that put
    the trial there. That is the citation doing its job, not disagreeing with the bucket.
    """
    quarter = _QUARTER.match(label)
    if quarter and stated.startswith(quarter.group(1)):
        parts = stated.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            return (int(parts[1]) - 1) // 3 + 1 == int(quarter.group(2))
        return False
    edges = _BIN.match(label.replace(",", ""))
    if edges:
        try:
            return float(edges.group(1)) <= float(stated) <= float(edges.group(2))
        except ValueError:
            return False
    return False


def _same_number(a: str, b: str) -> bool:
    """Whether two strings denote one number. A scatter labels x as a float, the record
    states an integer, and `"0" != "0.0"` is a string fact rather than a data one."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def check_offsets_are_plausible(body: dict) -> Iterator[Finding]:
    """Offsets must be a well-formed half-open span matching the excerpt's length.

    Re-slicing against the record needs the record, so `ground_truth.py` does that. This
    catches the cheap cases without the network.
    """
    for i, datum in enumerate(_datums(body)):
        for citation in datum.get("citations") or []:
            offset, excerpt = citation.get("offset"), citation.get("excerpt", "")
            if not offset or len(offset) != 2:
                yield Finding("citation-offset", "wrong", f"datum {i}: malformed offset {offset}")
                continue
            start, end = offset
            if not (0 <= start < end):
                yield Finding(
                    "citation-offset", "wrong", f"datum {i}: offset {offset} is not a span"
                )
            elif end - start != len(excerpt):
                yield Finding(
                    "citation-offset", "wrong",
                    f"datum {i}: offset spans {end - start} chars, excerpt is {len(excerpt)}",
                )


# --------------------------------------------------------------------------------------
# Filters — a response must not report a filter it did not issue
# --------------------------------------------------------------------------------------

#: Filter field -> a fragment that must appear in the issued query string when set.
#: Provenance: `site_status` compiled to nothing unless `country` was also set, so a plan
#: filtering on it issued a bare query while `filters_applied` reported the filter —
#: 7,744 trials answered for a question claiming 1,295.
_FILTER_EVIDENCE: dict[str, tuple[str, ...]] = {
    "condition": ("query.cond",),
    "intervention": ("query.intr",),
    "sponsor": ("query.spons", "AREA[LeadSponsorName]", "AREA[Sponsor]"),
    "free_text": ("query.term",),
    "country": ("query.locn", "AREA[LocationCountry]"),
    "site_status": ("AREA[LocationStatus]",),
    "status": ("filter.overallStatus", "AREA[OverallStatus]"),
    "phase": ("AREA[Phase]",),
    "start_year_min": ("AREA[StartDate]",),
    "start_year_max": ("AREA[StartDate]",),
    "study_type": ("AREA[StudyType]",),
    "sponsor_class": ("AREA[LeadSponsorClass]",),
    "intervention_type": ("AREA[InterventionType]",),
    "enrollment_min": ("AREA[EnrollmentCount]",),
    "enrollment_max": ("AREA[EnrollmentCount]",),
    "has_results": ("AREA[HasResults]",),
}

#: Set on every leg by default and meaning "no constraint", so its absence proves nothing.
_NOT_A_FILTER = {"date_certainty"}


def check_every_planned_filter_reaches_the_api(body: dict) -> Iterator[Finding]:
    """The filter in the plan must appear in a URL the service says it issued.

    `meta.api_requests` carries the issued URLs verbatim, which makes this checkable from
    the response alone — and it is the one field that cannot lie about what was fetched.
    """
    plan = (body.get("meta") or {}).get("plan") or {}
    urls = " ".join(unquote(u) for u in (body.get("meta") or {}).get("api_requests") or [])
    if not urls or not plan.get("legs"):
        return
    for leg in plan["legs"]:
        for name, value in (leg.get("filters") or {}).items():
            if value in (None, [], "") or name in _NOT_A_FILTER:
                continue
            fragments = _FILTER_EVIDENCE.get(name)
            if not fragments:
                yield Finding(
                    "filter-coverage", "suspect",
                    f"filter {name!r} has no known query fragment — this auditor cannot "
                    f"tell whether it was applied",
                )
                continue
            if not any(f in urls for f in fragments):
                yield Finding(
                    "filter-applied", "wrong",
                    f"leg {leg.get('label')!r} filters on {name}={value!r} but no issued "
                    f"URL contains {fragments[0]!r} — the filter was reported, not applied",
                )


def check_counts_reconcile(body: dict) -> Iterator[Finding]:
    """`used + excluded == retrieved`, restated here rather than trusted.

    Production raises `InvariantError` on this, so a violation reaching a response means
    the check itself was bypassed.
    """
    counts = (body.get("meta") or {}).get("record_counts")
    if not counts:
        return
    used = counts.get("used", 0)
    excluded = sum((counts.get("excluded_by_reason") or {}).values())
    retrieved = counts.get("retrieved", 0)
    if used + excluded != retrieved:
        yield Finding(
            "invariant", "wrong",
            f"used({used}) + excluded({excluded}) = {used + excluded} != retrieved({retrieved})",
        )
    if counts.get("matched") and retrieved > counts["matched"]:
        yield Finding(
            "invariant", "wrong",
            f"retrieved({retrieved}) exceeds matched({counts['matched']})",
        )


# --------------------------------------------------------------------------------------
# Description — every number right, the words around them wrong
# --------------------------------------------------------------------------------------


def check_nothing_claims_a_fold_that_did_not_happen(body: dict) -> Iterator[Finding]:
    """Provenance: a scatter's subtitle said "the median of enrollment ... in that bucket".

    A point layout aggregates nothing — every datum is one trial — but `metric` must be
    set for the plan to validate, and the generic wording believed it. 3,625 buckets of
    one trial each, described as medians.
    """
    plan = (body.get("meta") or {}).get("plan") or {}
    if plan.get("layout") != "point":
        return
    viz = body.get("visualization") or {}
    subtitle = (viz.get("subtitle") or "").lower()
    for word in ("median", "sum of", "average", "mean"):
        if word in subtitle:
            yield Finding(
                "semantics", "misleading",
                f"point layout, but the subtitle claims a {word!r}: {viz.get('subtitle')!r}",
            )
    for warning in (body.get("meta") or {}).get("warnings") or []:
        if warning.lower().startswith("median is reported"):
            yield Finding(
                "semantics", "misleading",
                "point layout, but a warning explains a median that was never taken",
            )


def check_the_answer_agrees_with_the_data(body: dict) -> Iterator[Finding]:
    """The prose names a leader and a figure; both must come from the datums.

    A sentence a reader trusts more than the chart, generated from a template — so a
    template that drifts from the data is worse than no sentence at all.
    """
    answer = body.get("answer") or ""
    datums = _datums(body)
    if not datums or not answer:
        return
    # Standalone numbers only: the "2" inside "PHASE2" is part of a label, not a figure
    # the sentence is asserting.
    numbers = {
        n.replace(",", "").rstrip(".")
        # The trailing guard must include the en-dash: a histogram bin reads "330–1100",
        # and splitting it yields two figures the sentence never claimed.
        for n in re.findall(
            "(?<![A-Za-z0-9\u2013-])\\d[\\d,]*(?:\\.\\d+)?(?![A-Za-z0-9\u2013-])", answer
        )
    }
    if not numbers:
        return
    raw = [float(d.get("value", d.get("weight", 0)) or 0) for d in datums]
    # Plain digits, not %g: 5,147,269 formatted as "5.14727e+06" would make a correct
    # figure look invented.
    values = {f"{v:.10g}" for v in raw} | {str(int(v)) for v in raw if v.is_integer()}
    counts = (body.get("meta") or {}).get("record_counts") or {}
    allowed = values | {str(counts.get(k)) for k in ("used", "matched", "retrieved")}
    # The sentence names the leading bucket as well as its value, and a bucket label is
    # frequently itself a number — a year, a bin edge, a site count.
    key = _dimension_key(body)
    if key:
        allowed |= {str(d.get(key)) for d in datums}
        allowed |= {f"{float(d[key]):g}" for d in datums
                    if isinstance(d.get(key), (int, float))}
    # "A further 6,175 fall outside the top 10" quotes the plan's own `top_n`, which is a
    # parameter of the chart rather than a figure read off it.
    config = (body.get("visualization") or {}).get("config") or {}
    for setting in ("top_n", "granularity", "suggested_min_occurrences"):
        if config.get(setting) is not None:
            allowed.add(str(config[setting]))
    allowed |= {str(len(datums)), str(len((body.get("visualization") or {}).get("data") or []))}
    viz = body.get("visualization") or {}
    if isinstance(viz.get("data"), dict):
        allowed |= {
            str(len(viz["data"].get("nodes") or [])),
            str(len(viz["data"].get("edges") or [])),
        }
    stray = {n for n in numbers if n not in allowed and float(n) > 1}
    if stray:
        yield Finding(
            "answer-text", "misleading",
            f"the answer states {sorted(stray)}, which appear in no datum or count: "
            f"{answer[:100]!r}",
        )


def check_labels_are_not_case_duplicates(body: dict) -> Iterator[Finding]:
    """Provenance: `Dexamethasone` and `dexamethasone` were two nodes on one graph.

    Sponsor-authored names are free text, so the same entity arrives under several
    capitalisations and splits its own weight.
    """
    viz = body.get("visualization") or {}
    if viz.get("type") == "network":
        labels = [n.get("label", "") for n in (viz.get("data") or {}).get("nodes") or []]
    else:
        key = _dimension_key(body)
        labels = [str(d.get(key)) for d in _datums(body) if key and key in d]
    seen: dict[str, str] = {}
    for label in labels:
        folded = label.casefold()
        if folded in seen and seen[folded] != label:
            yield Finding(
                "labels", "wrong",
                f"{seen[folded]!r} and {label!r} are one entity split into two buckets",
            )
        seen.setdefault(folded, label)


def check_config_is_coherent(body: dict) -> Iterator[Finding]:
    """Rendering hints that contradict the plan mislead a renderer that trusts them."""
    viz = body.get("visualization") or {}
    config, plan = viz.get("config") or {}, (body.get("meta") or {}).get("plan") or {}
    if not plan:
        return
    if config.get("granularity") and plan.get("group_by") not in (
        "start_date", "completion_date", "primary_completion_date"
    ):
        yield Finding(
            "config", "misleading",
            f"granularity={config['granularity']!r} on a non-temporal grouping "
            f"({plan.get('group_by')!r})",
        )
    if config.get("other_bucket") and not config.get("top_n"):
        yield Finding("config", "misleading", "other_bucket is set with no top_n to have caused it")
    labels = {str(d.get(_dimension_key(body))) for d in _datums(body)}
    if config.get("other_bucket") and "Other" not in labels:
        yield Finding("config", "misleading", "other_bucket is set but no 'Other' datum exists")
    # On a network `top_n` caps the *entities*; edges are pairs drawn from them, so ten
    # nodes legitimately produce up to forty-five edges.
    bounded = (
        len((viz.get("data") or {}).get("nodes") or [])
        if viz.get("type") == "network"
        else len(_datums(body))
    )
    if config.get("top_n") and bounded > config["top_n"] + 1:
        yield Finding(
            "config", "wrong",
            f"top_n={config['top_n']} but {bounded} "
            f"{'nodes' if viz.get('type') == 'network' else 'datums'} were returned",
        )


def check_review_is_recorded(body: dict) -> Iterator[Finding]:
    """An approval that leaves no trace is indistinguishable from a reviewer that never ran."""
    meta = body.get("meta") or {}
    if body.get("response_type") in ("conversational", "unsupported"):
        return
    if meta.get("plan") and meta.get("review") is None:
        yield Finding("audit-trail", "suspect", "a plan was committed with no recorded review")


def check_overrides_are_reported(body: dict, params: dict) -> Iterator[Finding]:
    """A supplied parameter must be visible in the plan and stated as an assumption."""
    if not params:
        return
    plan = (body.get("meta") or {}).get("plan") or {}
    assumptions = " ".join((body.get("meta") or {}).get("assumptions") or [])
    for name, value in params.items():
        applied = any(
            str(value).casefold() in str(leg.get("filters") or {}).casefold()
            for leg in plan.get("legs") or []
        )
        if not applied:
            yield Finding(
                "parameters", "wrong",
                f"parameter {name}={value!r} appears in no leg's filters",
            )
        elif str(value).casefold() not in assumptions.casefold():
            yield Finding(
                "parameters", "misleading",
                f"parameter {name}={value!r} was applied but not stated in assumptions",
            )


CHECKS = [
    check_response_type_matches_content,
    check_every_datum_carries_the_encoded_dimension,
    check_values_are_finite,
    check_sample_never_exceeds_its_total,
    check_citations_state_their_own_datum,
    check_offsets_are_plausible,
    check_every_planned_filter_reaches_the_api,
    check_counts_reconcile,
    check_nothing_claims_a_fold_that_did_not_happen,
    check_the_answer_agrees_with_the_data,
    check_labels_are_not_case_duplicates,
    check_config_is_coherent,
    check_review_is_recorded,
]


def audit(body: dict, params: dict[str, Any] | None = None) -> list[Finding]:
    """Run every self-consistency check. Ground truth is checked elsewhere."""
    findings = [f for check in CHECKS for f in check(body)]
    findings += list(check_overrides_are_reported(body, params or {}))
    return findings


__all__ = ["CHECKS", "Finding", "audit"]
