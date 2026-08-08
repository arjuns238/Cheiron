"""Flatten ClinicalTrials.gov study records into a shape the aggregator can fold over.

This module is the only place in the system that knows how messy the source data is. It
exists so that the aggregator has exactly two shapes to handle — a scalar or a flat list
of strings — instead of N traversal cases, and so that every quirk has one documented
home rather than being rediscovered at each call site.

Two rules govern everything here:

1. **Nothing disappears silently.** A record that cannot be used is returned as an
   `Exclusion` carrying a machine-countable reason, never dropped. The reasons are summed
   into `meta.record_counts.excluded_by_reason` and checked by the invariant
   `used + sum(excluded) == retrieved`.

2. **Absent is not zero, and unknown is not false.** Fields that the registry does not
   record come back as `None`, and downstream code decides what that means for the
   question being asked. The normalizer never invents a value to make the shape tidy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Canonical ordering for composite phase labels, so that a trial recorded as
# ["PHASE2", "PHASE1"] and one recorded as ["PHASE1", "PHASE2"] land in the same bucket.
_PHASE_ORDER = ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA")

#: Value used when the registry records no phase at all. Distinct from "NA", which the
#: registry records deliberately to mean "phase does not apply to this interventional
#: trial". Absent means the study is observational or expanded-access.
PHASE_NOT_REPORTED = "NOT_REPORTED"

_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")

#: Separator inside a `combination_groups` entry. Chosen because it does not occur in
#: intervention names, which routinely contain commas, plus signs, slashes and brackets
#: ("paclitaxel + cisplatin", "DTIC (dacarbazine)").
COMBINATION_SEPARATOR = " || "

#: Intervention types treated as administered therapeutic agents for combination
#: detection.
#:
#: `plan.md` says DRUG. BIOLOGICAL is included because the ClinicalTrials.gov distinction
#: is regulatory (which FDA centre reviews the filing) rather than pharmacological, and
#: sponsors apply it inconsistently to the *same molecule*: pembrolizumab appears as DRUG
#: in 405 records and BIOLOGICAL in 94. Excluding BIOLOGICAL would make a combination
#: appear or vanish depending on who filed it.
AGENT_TYPES = frozenset({"DRUG", "BIOLOGICAL"})

#: Names excluded from combination detection even when typed as an agent.
#:
#: Arm membership is not sufficient on its own. Double-dummy blinding puts a placebo *in
#: the active arm* so that both groups receive the same number of injections, and sponsors
#: type those placebos as DRUG: NCT01721772 yields the arm
#: "BMS-936558 (Nivolumab) || Placebo matching Dacarbazine". An edge there would assert
#: that a drug is combined with a sham of a different drug.
#:
#: This is a name heuristic and therefore imperfect — it is matching prose, not a coded
#: field, because the registry has no "is placebo" flag. It is deliberately narrow: it
#: drops the term from pairing rather than dropping the trial.
_PLACEBO_TERMS = ("placebo", "sham", "vehicle control", "matching injection")


class ExclusionReason(StrEnum):
    """Why a fetched record did not become a used record.

    Every member is counted, reported in `meta.record_counts.excluded_by_reason`, and
    surfaced as a warning when the count is non-trivial. Kept as a closed enum rather than
    free-text strings so that the counts are aggregatable and testable.
    """

    MISSING_NCT_ID = "missing_nct_id"
    MALFORMED_RECORD = "malformed_record"
    UNPARSEABLE_DATE = "unparseable_date"


@dataclass(frozen=True)
class Exclusion:
    """A record that was fetched but cannot be used, and why."""

    reason: ExclusionReason
    nct_id: str | None = None
    detail: str | None = None


@dataclass
class NormalizedRecord:
    """One flattened trial.

    Attributes:
        nct_id: Always present; a record without one is excluded rather than normalized.
        values: The flattened fields, keyed by the registry keys in `schemas.fields`.
            Every value is a scalar (`str | int | float | bool | None`) or a flat
            `list[str]`. No nesting, ever.
        raw: The original record. Retained solely so the spec assembler can locate
            citation excerpts by offset in the serialized payload; nothing else reads it.
    """

    nct_id: str
    values: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def get(self, key: str) -> Any:
        return self.values.get(key)


# --------------------------------------------------------------------------------------
# Primitive parsers
# --------------------------------------------------------------------------------------


def parse_partial_date(value: str | None) -> str | None:
    """Normalize a registry date to `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`.

    The registry emits at least month precision in practice (a 1000-record sample of
    1990–2005 starts contained only `YYYY-MM` and `YYYY-MM-DD`), but year-only is accepted
    defensively because the API documents the field as a partial date.

    The precision is preserved rather than padded. Padding `2019-03` to `2019-03-01` would
    invent a day the sponsor never reported, and quarterly bucketing would then silently
    depend on that invention. Callers ask for the precision they need via `date_precision`.

    Returns None for absent, empty, or unparseable values.
    """
    if not value:
        return None
    match = _ISO_DATE.match(value.strip())
    if not match:
        return None
    year, month, day = match.groups()
    if month is not None and not 1 <= int(month) <= 12:
        return None
    if day is not None and not 1 <= int(day) <= 31:
        return None
    return "-".join(p for p in (year, month, day) if p is not None)


def date_precision(value: str | None) -> str | None:
    """`"year"`, `"month"`, `"day"`, or None — derived from the normalized string's length."""
    if not value:
        return None
    return {4: "year", 7: "month", 10: "day"}.get(len(value))


def date_year(value: str | None) -> int | None:
    return int(value[:4]) if value else None


def date_quarter(value: str | None) -> str | None:
    """`"2019-Q1"`, or None when the date lacks month precision.

    A year-only date cannot be assigned to a quarter, and guessing Q1 would put a
    fabricated spike at the start of every year. The aggregator excludes such records from
    quarterly charts and counts them.
    """
    if not value or date_precision(value) == "year":
        return None
    return f"{value[:4]}-Q{(int(value[5:7]) - 1) // 3 + 1}"


def parse_certainty(value: str | None) -> bool | None:
    """ACTUAL → True, ESTIMATED → False, absent → None.

    Tri-state, not boolean. `startDateStruct.type` is absent on many older records — all
    1000 in a 1990–2005 sample — and "the sponsor did not say" is a different population
    from "the sponsor said this is an estimate". Collapsing them would misreport how much
    of a time series is projection rather than fact.
    """
    if value is None:
        return None
    return value.upper() == "ACTUAL"


def canonical_phase(phases: list[str] | None) -> str:
    """Collapse the phase array into one composite bucket value.

    A Phase 1/Phase 2 trial is one kind of trial, not one Phase 1 trial plus one Phase 2
    trial, so it gets its own bucket and is never counted into both. See the note on the
    `phases` field in `schemas.fields` for why this diverges from ClinicalTrials.gov's own
    facets.
    """
    if not phases:
        return PHASE_NOT_REPORTED
    ordered = sorted(
        {p for p in phases if p},
        key=lambda p: _PHASE_ORDER.index(p) if p in _PHASE_ORDER else len(_PHASE_ORDER),
    )
    return "|".join(ordered) if ordered else PHASE_NOT_REPORTED


def is_placebo(name: str) -> bool:
    """Whether an intervention name denotes a control rather than an agent."""
    lowered = name.lower()
    return any(term in lowered for term in _PLACEBO_TERMS)


def combination_groups(interventions: list[dict[str, Any]]) -> list[str]:
    """Sets of agents administered together, one entry per arm that has more than one.

    This is what makes a drug↔drug network mean anything. Two drugs appearing in the same
    *trial* are frequently not a combination at all — they are the two sides of a
    comparison. Measured over 500 melanoma trials: 217 co-list two or more agents, but only
    157 have two or more sharing an arm, so a third of co-listing trials would produce
    edges asserting a combination that does not exist. NCT01748448 lists Vitamin D and
    Placebo; naive pairing draws an edge between a drug and its own control.

    Arm group membership is the registry's own statement of what was given together, so
    pairing happens within an arm and nowhere else.

    Returned as a flat list of separator-joined strings rather than a list of lists,
    because the normalizer's contract with the aggregator is scalars and flat lists of
    strings. The aggregator splits them back apart.
    """
    by_arm: dict[str, list[str]] = {}
    for intervention in interventions:
        if intervention.get("type") not in AGENT_TYPES:
            continue
        name = (intervention.get("name") or "").strip()
        if not name or is_placebo(name):
            continue
        for label in intervention.get("armGroupLabels") or []:
            by_arm.setdefault(label, []).append(name)

    groups = []
    for members in by_arm.values():
        distinct = sorted(set(members))
        if len(distinct) > 1:
            groups.append(COMBINATION_SEPARATOR.join(distinct))
    return sorted(set(groups))


def _dedupe(values: list[str | None]) -> list[str]:
    """Order-preserving dedupe, dropping blanks."""
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def _dig(node: Any, *path: str) -> Any:
    """Walk a dotted path, returning None the moment anything is missing.

    Any module, array, or leaf in a ClinicalTrials.gov record can be absent, so every
    access goes through here rather than through chained `.get()` calls that would each
    need their own default.
    """
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _items(node: Any, *path: str) -> list[dict[str, Any]]:
    """Fetch a list of objects, tolerating absent, null, and non-list values."""
    value = _dig(node, *path)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _strings(node: Any, *path: str) -> list[str]:
    value = _dig(node, *path)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


# --------------------------------------------------------------------------------------
# The flattener
# --------------------------------------------------------------------------------------


def normalize_study(raw: dict[str, Any]) -> NormalizedRecord | Exclusion:
    """Flatten one study record.

    Returns a `NormalizedRecord`, or an `Exclusion` if the record is structurally unusable.

    Note what is *not* an exclusion: a missing start date, an absent phase, an empty
    location list, and a null enrollment all produce a normalized record with `None` in
    that slot. Whether such a record is usable depends on the question — a trial with no
    start date is fine for a phase distribution and unusable for a time series — so that
    judgement belongs to the aggregator, which counts its own exclusions against the
    specific dimension being grouped on. Excluding here would drop records from charts
    that never needed the missing field.
    """
    if not isinstance(raw, dict):
        return Exclusion(
            ExclusionReason.MALFORMED_RECORD,
            detail=f"expected object, got {type(raw).__name__}",
        )

    protocol = raw.get("protocolSection")
    if not isinstance(protocol, dict):
        return Exclusion(ExclusionReason.MALFORMED_RECORD, detail="no protocolSection")

    nct_id = _dig(protocol, "identificationModule", "nctId")
    if not isinstance(nct_id, str) or not nct_id.strip():
        return Exclusion(ExclusionReason.MISSING_NCT_ID)
    nct_id = nct_id.strip()

    status_module = protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    enrollment_info = design.get("enrollmentInfo") or {}
    sponsor_module = protocol.get("sponsorCollaboratorsModule") or {}
    lead_sponsor = sponsor_module.get("leadSponsor") or {}
    derived = raw.get("derivedSection") or {}

    interventions = _items(protocol, "armsInterventionsModule", "interventions")
    locations = _items(protocol, "contactsLocationsModule", "locations")

    values: dict[str, Any] = {
        "nct_id": nct_id,
        "brief_title": _dig(protocol, "identificationModule", "briefTitle"),
        # --- temporal ---------------------------------------------------------------
        "start_date": parse_partial_date(_dig(status_module, "startDateStruct", "date")),
        "start_is_actual": parse_certainty(_dig(status_module, "startDateStruct", "type")),
        "completion_date": parse_partial_date(
            _dig(status_module, "completionDateStruct", "date")
        ),
        "completion_is_actual": parse_certainty(
            _dig(status_module, "completionDateStruct", "type")
        ),
        "primary_completion_date": parse_partial_date(
            _dig(status_module, "primaryCompletionDateStruct", "date")
        ),
        # --- status -----------------------------------------------------------------
        "status": status_module.get("overallStatus"),
        "why_stopped": status_module.get("whyStopped"),
        # --- design -----------------------------------------------------------------
        "phases": canonical_phase(_strings(design, "phases")),
        "study_type": design.get("studyType"),
        "enrollment": enrollment_info.get("count"),
        "enrollment_is_actual": parse_certainty(enrollment_info.get("type")),
        # --- sponsors ---------------------------------------------------------------
        "sponsor_name": lead_sponsor.get("name"),
        "sponsor_class": lead_sponsor.get("class"),
        "collaborators": _dedupe([c.get("name") for c in _items(sponsor_module, "collaborators")]),
        # --- subject matter ---------------------------------------------------------
        "conditions": _dedupe(_strings(protocol, "conditionsModule", "conditions")),
        "intervention_names": _dedupe([i.get("name") for i in interventions]),
        "intervention_types": _dedupe([i.get("type") for i in interventions]),
        "combination_groups": combination_groups(interventions),
        # --- geography --------------------------------------------------------------
        # Deduplicated per trial: a trial with 40 German sites is one German trial, and
        # counting it 40 times would make the site list a proxy for trial size.
        "countries": _dedupe([loc.get("country") for loc in locations]),
        "site_count": len(locations),
        "has_results": raw.get("hasResults"),
        # --- MeSH -------------------------------------------------------------------
        "intervention_mesh": _dedupe(
            [m.get("term") for m in _items(derived, "interventionBrowseModule", "meshes")]
        ),
        "condition_mesh": _dedupe(
            [m.get("term") for m in _items(derived, "conditionBrowseModule", "meshes")]
        ),
    }

    # Enrollment must be a number or absent. The registry occasionally carries a string
    # here; a string that silently reached a `sum` fold would either crash or concatenate.
    if values["enrollment"] is not None and not isinstance(values["enrollment"], (int, float)):
        try:
            values["enrollment"] = int(str(values["enrollment"]).strip())
        except (TypeError, ValueError):
            values["enrollment"] = None

    if not isinstance(values["has_results"], bool):
        values["has_results"] = None

    return NormalizedRecord(nct_id=nct_id, values=values, raw=raw)


@dataclass
class NormalizationResult:
    """The outcome of normalizing a page or a whole fetch."""

    records: list[NormalizedRecord] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)

    @property
    def excluded_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exclusion in self.excluded:
            counts[exclusion.reason.value] = counts.get(exclusion.reason.value, 0) + 1
        return counts

    @property
    def retrieved(self) -> int:
        return len(self.records) + len(self.excluded)


def normalize_studies(raws: list[dict[str, Any]]) -> NormalizationResult:
    """Normalize a batch, partitioning into usable records and counted exclusions."""
    result = NormalizationResult()
    for raw in raws:
        outcome = normalize_study(raw)
        if isinstance(outcome, Exclusion):
            result.excluded.append(outcome)
        else:
            result.records.append(outcome)
    return result
