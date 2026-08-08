"""The field registry.

This module is the single source of truth for what a "field" means in this system.
`plan.md` requires that field metadata drive behaviour automatically rather than being
hand-coded per query, so exactly one table is maintained here and four things are
*derived* from it:

1. the normalizer's output keys (what a flattened record looks like)
2. the planner's `LEGAL_FIELDS` prompt context and the plan validator's membership checks
3. the viz-legality rules (which chart types a result shape admits)
4. the automatic warnings (a `multi` field always produces the "totals exceed distinct
   trial count" warning; a `temporal` field always produces the registry-lag warning)

Adding a field here updates all four at once. That is the whole point of the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FieldKind(StrEnum):
    """What a field *is*, which determines what may be done with it.

    The distinction between CATEGORICAL and ENTITY is load-bearing and is not cosmetic:
    a categorical field has a small closed vocabulary (6 phases, 14 statuses) and can be
    charted whole; an entity field has an open, high-cardinality vocabulary (51,497 distinct
    lead sponsors in the corpus) and *must* be top-N'd, sorted, and given an "other" bucket.
    Only entity fields can form network nodes.
    """

    ID = "id"
    TEXT = "text"
    TEMPORAL = "temporal"
    CATEGORICAL = "categorical"
    ENTITY = "entity"
    NUMERIC = "numeric"
    BOOL = "bool"


@dataclass(frozen=True)
class FieldSpec:
    """One flattened field.

    Attributes:
        key: The flattened output key. This is the name the planner, the plan, and the
            response envelope all use. It is deliberately snake_case and API-independent.
        kind: See `FieldKind`.
        source: The dotted path in the ClinicalTrials.gov record, for documentation and
            for building the `field_path` recorded on a citation.
        projection: The `fields=` piece name(s) needed to make the API return this. Empty
            for fields derived from other fields rather than fetched.
        multi: True when one trial can contribute more than one value. Drives the
            counting-semantics warning and changes the aggregator's invariant check.
        enum_type: Name of the `/studies/enums` type constraining this field's values,
            when one exists. The plan validator checks filter values against it.
        groupable: May appear as `group_by` or `series_by`.
        measurable: May appear as `metric_field` for sum/median.
        filterable: May appear in a leg's filters.
        label: Human-readable axis title, used by the spec assembler.
        note: A data-quality caveat surfaced to the user when the field is used.
        skewed: True when the value distribution is heavy-tailed enough that equal-width
            histogram bins collapse into a single bar. Drives the default bin scale, so a
            numeric field added here brings its own binning behaviour rather than relying
            on whoever writes the plan to remember.
    """

    key: str
    kind: FieldKind
    source: str
    projection: tuple[str, ...] = ()
    multi: bool = False
    enum_type: str | None = None
    groupable: bool = True
    measurable: bool = False
    filterable: bool = True
    label: str = ""
    note: str | None = None
    skewed: bool = False

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.key.replace("_", " ").title())

    @property
    def is_entity(self) -> bool:
        return self.kind is FieldKind.ENTITY

    @property
    def is_temporal(self) -> bool:
        return self.kind is FieldKind.TEMPORAL

    @property
    def is_numeric(self) -> bool:
        return self.kind is FieldKind.NUMERIC


# --------------------------------------------------------------------------------------
# The table.
#
# Ordering follows `plan.md` §3: core fields first, then the stretch fields it lists in
# priority order. Sources and quirks are all confirmed against live records; see
# `docs/api-findings.md`.
# --------------------------------------------------------------------------------------

_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="nct_id",
        kind=FieldKind.ID,
        source="protocolSection.identificationModule.nctId",
        projection=("NCTId",),
        groupable=False,
        filterable=False,
        label="NCT ID",
    ),
    FieldSpec(
        key="brief_title",
        kind=FieldKind.TEXT,
        source="protocolSection.identificationModule.briefTitle",
        projection=("BriefTitle",),
        groupable=False,
        filterable=False,
        label="Title",
    ),
    # --- temporal -----------------------------------------------------------------
    # `startDateStruct.date` arrives as YYYY-MM or YYYY-MM-DD (year-only was not observed
    # in v2, but is accepted defensively). `.type` is frequently ABSENT on older records,
    # which is why `start_is_actual` is tri-state rather than boolean.
    FieldSpec(
        key="start_date",
        kind=FieldKind.TEMPORAL,
        source="protocolSection.statusModule.startDateStruct.date",
        projection=("StartDate", "StartDateType"),
        label="Start Date",
        note="Registry lag undercounts the most recent periods; sponsors register and "
        "update on their own schedule.",
    ),
    FieldSpec(
        key="start_is_actual",
        kind=FieldKind.BOOL,
        source="protocolSection.statusModule.startDateStruct.type",
        projection=("StartDateType",),
        label="Start Date Is Actual",
        note="Absent on many older records, so this is tri-state: true / false / unknown. "
        "'Unknown' is not the same as 'estimated' and is never silently folded into it.",
    ),
    FieldSpec(
        key="completion_date",
        kind=FieldKind.TEMPORAL,
        source="protocolSection.statusModule.completionDateStruct.date",
        projection=("CompletionDate", "CompletionDateType"),
        label="Completion Date",
    ),
    FieldSpec(
        key="completion_is_actual",
        kind=FieldKind.BOOL,
        source="protocolSection.statusModule.completionDateStruct.type",
        projection=("CompletionDateType",),
        label="Completion Date Is Actual",
    ),
    FieldSpec(
        key="primary_completion_date",
        kind=FieldKind.TEMPORAL,
        source="protocolSection.statusModule.primaryCompletionDateStruct.date",
        projection=("PrimaryCompletionDate", "PrimaryCompletionDateType"),
        label="Primary Completion Date",
    ),
    # --- status -------------------------------------------------------------------
    FieldSpec(
        key="status",
        kind=FieldKind.CATEGORICAL,
        source="protocolSection.statusModule.overallStatus",
        projection=("OverallStatus",),
        enum_type="Status",
        label="Overall Status",
        note="UNKNOWN is a real recorded value, not a null. It means the sponsor has not "
        "verified the record recently.",
    ),
    FieldSpec(
        key="why_stopped",
        kind=FieldKind.TEXT,
        source="protocolSection.statusModule.whyStopped",
        projection=("WhyStopped",),
        groupable=False,
        label="Why Stopped",
    ),
    # --- design -------------------------------------------------------------------
    # ~63% of the corpus is either NA or has no `phases` key at all. Both are first-class
    # buckets, never exclusions. They mean different things: NA is an interventional trial
    # for which phase does not apply (device, procedure, behavioural); absent means the
    # study is observational or expanded-access, where phase is not a concept.
    #
    # `multi=False` despite the source being a list, and this is deliberate. The API stores
    # phases as an array, but a Phase 1/Phase 2 trial is one kind of trial, not one Phase 1
    # trial plus one Phase 2 trial. The normalizer collapses the array into a single
    # composite value ("PHASE1|PHASE2") which buckets on its own. ClinicalTrials.gov's own
    # facets do the opposite and double-count: their per-phase counts plus their missing
    # count sum to 622,213 against a corpus of 597,691, an excess of 24,522 — exactly the
    # multi-phase population. Ours sum to the trial count. See docs/corpus-facts.md.
    FieldSpec(
        key="phases",
        kind=FieldKind.CATEGORICAL,
        source="protocolSection.designModule.phases",
        projection=("Phase",),
        multi=False,
        enum_type="Phase",
        label="Phase",
        note="'Not Applicable' and 'Not Reported' together are roughly 63% of the registry. "
        "Combination phases (e.g. Phase 1/Phase 2) form their own bucket and are never "
        "double-counted into Phase 1 and Phase 2, so these totals will not match "
        "ClinicalTrials.gov's own phase facets, which do double-count.",
    ),
    FieldSpec(
        key="study_type",
        kind=FieldKind.CATEGORICAL,
        source="protocolSection.designModule.studyType",
        projection=("StudyType",),
        enum_type="StudyType",
        label="Study Type",
    ),
    FieldSpec(
        key="enrollment",
        kind=FieldKind.NUMERIC,
        source="protocolSection.designModule.enrollmentInfo.count",
        projection=("EnrollmentCount", "EnrollmentType"),
        measurable=True,
        skewed=True,
        label="Enrollment",
        note="Heavily right-skewed (observed values range from 0 to over 1.1 million), so "
        "median is preferred over mean. Mixes ACTUAL and ESTIMATED counts.",
    ),
    FieldSpec(
        key="enrollment_is_actual",
        kind=FieldKind.BOOL,
        source="protocolSection.designModule.enrollmentInfo.type",
        projection=("EnrollmentType",),
        label="Enrollment Is Actual",
    ),
    # --- sponsors -----------------------------------------------------------------
    FieldSpec(
        key="sponsor_name",
        kind=FieldKind.ENTITY,
        source="protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        projection=("LeadSponsorName",),
        label="Lead Sponsor",
        note="Free text with no canonical identifier: 51,497 distinct values corpus-wide. "
        "The same organisation appears under multiple spellings and is not deduplicated.",
    ),
    FieldSpec(
        key="sponsor_class",
        kind=FieldKind.CATEGORICAL,
        source="protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
        projection=("LeadSponsorClass",),
        enum_type="AgencyClass",
        label="Sponsor Class",
    ),
    FieldSpec(
        key="collaborators",
        kind=FieldKind.ENTITY,
        source="protocolSection.sponsorCollaboratorsModule.collaborators[].name",
        projection=("CollaboratorName",),
        multi=True,
        label="Collaborator",
    ),
    # --- subject matter -----------------------------------------------------------
    FieldSpec(
        key="conditions",
        kind=FieldKind.ENTITY,
        source="protocolSection.conditionsModule.conditions",
        projection=("Condition",),
        multi=True,
        label="Condition",
        note="Sponsor-authored free text, not a controlled vocabulary. Prefer "
        "condition_mesh when a normalized grouping is wanted.",
    ),
    FieldSpec(
        key="intervention_names",
        kind=FieldKind.ENTITY,
        source="protocolSection.armsInterventionsModule.interventions[].name",
        projection=("InterventionName",),
        multi=True,
        label="Intervention",
        note="Free text: brand names, code names, and generics all appear. Prefer "
        "intervention_mesh for drug-network nodes.",
    ),
    FieldSpec(
        key="intervention_types",
        kind=FieldKind.CATEGORICAL,
        source="protocolSection.armsInterventionsModule.interventions[].type",
        projection=("InterventionType",),
        multi=True,
        enum_type="InterventionType",
        label="Intervention Type",
    ),
    # --- geography ----------------------------------------------------------------
    FieldSpec(
        key="countries",
        kind=FieldKind.ENTITY,
        source="protocolSection.contactsLocationsModule.locations[].country",
        projection=("LocationCountry",),
        multi=True,
        label="Country",
        note="Deduplicated per trial. Location lists are frequently incomplete relative to "
        "the trial's prose, and trial-level status differs from site-level status.",
    ),
    FieldSpec(
        key="site_count",
        kind=FieldKind.NUMERIC,
        source="len(protocolSection.contactsLocationsModule.locations)",
        projection=("LocationCountry",),
        measurable=True,
        skewed=True,
        label="Site Count",
        note="Counts listed sites only; absent location lists yield 0, which is a reporting "
        "gap rather than a trial with no sites.",
    ),
    FieldSpec(
        key="has_results",
        kind=FieldKind.BOOL,
        source="hasResults",
        projection=("HasResults",),
        label="Has Posted Results",
    ),
    # --- stretch: MeSH normalization ----------------------------------------------
    # These are already in the payload and cost almost nothing, and they are what make a
    # drug-network graph credible: MeSH terms are a controlled vocabulary, raw
    # intervention names are not.
    FieldSpec(
        key="intervention_mesh",
        kind=FieldKind.ENTITY,
        source="derivedSection.interventionBrowseModule.meshes[].term",
        projection=("InterventionMeshTerm",),
        multi=True,
        label="Intervention (MeSH)",
        note="Derived by ClinicalTrials.gov's own indexer, so it is normalized but "
        "lossy: novel agents without a MeSH heading are absent.",
    ),
    FieldSpec(
        key="condition_mesh",
        kind=FieldKind.ENTITY,
        source="derivedSection.conditionBrowseModule.meshes[].term",
        projection=("ConditionMeshTerm",),
        multi=True,
        label="Condition (MeSH)",
    ),
)

FIELDS: dict[str, FieldSpec] = {f.key: f for f in _FIELDS}

LEGAL_FIELDS: tuple[str, ...] = tuple(FIELDS)
GROUPABLE_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.groupable)
MEASURABLE_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.measurable)
ENTITY_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.is_entity)
TEMPORAL_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.is_temporal)
MULTI_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.multi)
SKEWED_FIELDS: tuple[str, ...] = tuple(k for k, f in FIELDS.items() if f.skewed)

#: Every `fields=` piece name needed to populate the whole table. The query compiler
#: narrows this per plan; this is the upper bound and the default for fixtures.
ALL_PROJECTION: tuple[str, ...] = tuple(
    dict.fromkeys(piece for f in _FIELDS for piece in f.projection)
)


def spec(key: str) -> FieldSpec:
    """Look up a field, raising a message the plan validator can hand back verbatim."""
    try:
        return FIELDS[key]
    except KeyError:
        raise KeyError(
            f"unknown field {key!r}; legal fields are: {', '.join(LEGAL_FIELDS)}"
        ) from None
