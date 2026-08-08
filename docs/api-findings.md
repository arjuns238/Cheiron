# ClinicalTrials.gov API — verified findings

All checked live against `https://clinicaltrials.gov/api/v2` on 2026-08-08.
API version `2.0.5`, data timestamp `2026-08-07T09:00:05`, 597,691 total studies.

This file records what was *confirmed by curl*, not what the docs claim. It exists so the
query compiler and normalizer are written against observed behavior. Items marked
**CORRECTION** contradict an assumption in `plan.md`.

## Confirmed as planned

| Assumption (plan.md) | Status |
|---|---|
| `pageSize` defaults to 10 | confirmed |
| `pageSize` clamps silently at 1000 (asked 2000, got 1000) | confirmed |
| cursor pagination via `nextPageToken` | confirmed |
| `countTotal=true` returns `totalCount` | confirmed |
| `fields` is **comma**-separated | confirmed (pipe also parses, comma is documented; use comma) |
| `filter.advanced` = `AREA[StartDate]RANGE[2015-01-01,2020-12-31]` | confirmed |
| `/studies/enums` returns usable vocab | confirmed — 41 enum types |
| `/studies/metadata` returns the field tree | confirmed — 175 KB |

`RANGE` accepts `MAX` (and `MIN`) as an open bound: `RANGE[2015-01-01,MAX]`.
`filter.advanced` supports boolean composition: `AREA[Phase]PHASE3 AND AREA[StartDate]RANGE[...]`.
Unknown area names fail loudly: `Error parsing query in advanced filter: Unknown area name: ...`.

## CORRECTION 1 — the stats endpoint cannot be filtered

`plan.md` §3 (query compiler) routes to the stats endpoint when `group_by` is an indexed
enum and `metric` is `count`, expecting a filtered distribution in one call. It does not
work:

```
GET /stats/field/values?fields=Phase&query.cond=melanoma
→ 400  Invalid prefix in parameter name: query.cond

GET /stats/field/values?fields=Phase&filter.advanced=AREA[Condition]melanoma
→ 400  Invalid prefix in parameter name: filter.advanced
```

`/stats/field/values` returns **corpus-wide** statistics only. Since essentially every
supported query is filtered by condition/intervention/sponsor, this optimization applies
to almost nothing. **The one-call stats route should be dropped**; fetch records and
aggregate locally. This is also the correct outcome for the core invariant — citations
require the underlying records, and the stats endpoint returns none.

The endpoint is still useful for two non-retrieval purposes:
- boot-time vocabulary (`uniqueValuesCount`, `topValues`, `missingStudiesCount` per field)
- a static `fill_rate` prior for the planner when a probe budget is tight

## CORRECTION 2 — date formats and the missing `type`

`plan.md` §3 states dates arrive as `2019`, `2019-03`, or `2019-03-14`. Census over 1000
records with start dates in 1990–2005:

```
date length: {7: 901, 10: 99}     # YYYY-MM and YYYY-MM-DD only
type:        {None: 1000}         # startDateStruct.type absent on ALL of them
```

Two consequences:

1. **Year-only (`YYYY`) start dates were not observed.** The v2 API appears to normalize
   to at least month precision. The normalizer should still accept `YYYY` defensively, but
   it is not the common case; `YYYY-MM` is.
2. **`startDateStruct.type` is frequently absent entirely** — not `ESTIMATED`, not
   `ACTUAL`, simply missing on older records. So `start_is_actual` must be **tri-state**
   (`True` / `False` / `None`), not `bool` as the plan's normalizer table implies. The same
   applies to `enrollment_is_actual`. Filtering on `date_certainty` must therefore
   distinguish "estimated" from "unknown", and the ESTIMATED-start warning must not silently
   swallow unknowns.

## CORRECTION 3 — every "local" filter is actually pushable

`plan.md` §3 splits `Filters` into pushdown and local, the local ones being those "with no
API equivalent", applied after fetch. All six have an equivalent. Measured against
`query.cond=melanoma` (baseline 3,743):

| plan.md calls it local | Working clause | Count |
|---|---|---|
| `study_type` | `AREA[StudyType]INTERVENTIONAL` | 3,111 |
| `sponsor_class` | `AREA[LeadSponsorClass]INDUSTRY` | 1,274 |
| `intervention_type` | `AREA[InterventionType]DRUG` | 2,113 |
| `enrollment_min/max` | `AREA[EnrollmentCount]RANGE[100,MAX]` | 1,139 |
| `has_results` | `AREA[HasResults]true` | 789 |
| `date_certainty` | `AREA[StartDateType]ACTUAL` | 2,301 |

**Everything is pushed down.** Fetching records only to discard them locally wastes the
page budget, and the page cap is the thing that turns a chart into a sample. The
consequence is that `retrieved` and `used` now diverge only through normalizer rejections
and missing grouping dimensions, not through filtering — simpler to explain, not weaker.

Composition confirmed:

```
AREA[Phase]PHASE3 AND AREA[LeadSponsorClass]INDUSTRY              → 122
AREA[Phase](PHASE2 OR PHASE3)                                     → 1,728
  vs PHASE2 (1,534) + PHASE3 (219) = 1,753 — the 25-trial difference is the
  multi-phase population, counted once by the union. A naive sum over-reports.
NOT AREA[StartDateType]ESTIMATED                                  → 3,528
AREA[StartDateType]ESTIMATED                                      → 215
  3,528 + 215 = 3,743 exactly, so NOT partitions cleanly and "unrecorded type"
  falls on the NOT side — which is why ACTUAL_ONLY and EXCLUDE_ESTIMATED differ.
```

Multi-word values (`"United States"`) returned identical counts quoted and unquoted, but
the compiler quotes them anyway rather than depend on an unspecified tokenizer.

## Field projection

`fields` accepts **piece names** (`NCTId`, `Phase`, `LeadSponsorName`) or **module paths**
(`protocolSection.designModule`). Bare dotted leaf paths are rejected. Piece-name
projection preserves the full nested envelope and strips everything not requested — exactly
what the normalizer wants, and it cuts payload substantially (average record is 17 KB).

Projection set covering the normalizer's core table:

```
NCTId, BriefTitle, StartDate, StartDateType, PrimaryCompletionDate, CompletionDate,
OverallStatus, WhyStopped, Phase, StudyType, EnrollmentCount, EnrollmentType,
LeadSponsorName, LeadSponsorClass, CollaboratorName, Condition, InterventionName,
InterventionType, LocationCountry, HasResults, InterventionMeshTerm, ConditionMeshTerm
```

Caveat: a struct's sub-field is only returned if requested by name. Requesting `StartDate`
alone returns `startDateStruct.date` with **no** `.type`. `StartDateType` and
`EnrollmentType` must be requested explicitly or the ACTUAL/ESTIMATED distinction silently
vanishes.

## resultsSection — present, and its piece names are not guessable

`resultsSection` exists on trials with `hasResults: true` — **789 of 3,743** melanoma
trials. It carries `participantFlowModule`, `baselineCharacteristicsModule`,
`outcomeMeasuresModule` and `adverseEventsModule`.

**Piece names are prefixed by their module and cannot be inferred from the JSON path.**
Guessing `SeriousNumAffected` from `adverseEventsModule.eventGroups[].seriousNumAffected`
returns a 400. The working names, all verified:

```
EventGroupSeriousNumAffected   EventGroupSeriousNumAtRisk
EventGroupDeathsNumAffected    EventGroupDeathsNumAtRisk
FlowMilestoneType              FlowAchievementNumSubjects
BaselineMeasureTitle           BaselineMeasureParamType
BaselineCategoryTitle          BaselineMeasurementValue
BaselineMeasurementGroupId     BaselineGroupId            BaselineGroupTitle
```

Find any of them from `/studies/metadata`: walk the tree and read `altPieceName`/`piece`
on the leaf. Do not derive them from the field path.

### Payload cost

Measured over 200 melanoma trials with results:

| Projection | Size |
|---|---|
| `NCTId,BriefTitle,Phase` | 44 KB |
| + adverse-event counts | 116 KB |
| + participant flow | 172 KB |
| + baseline (full results set) | 778 KB |

Roughly **4–18× a registration-only fetch**, so a results plan is far heavier per record.
The projection is narrowed per plan, so only queries that reference these fields pay it.

### Shape facts that change the arithmetic

- `adverseEventsModule.eventGroups[]` has **no total row**, and each participant belongs to
  exactly one arm — so summing arms *is* the trial total.
- `baselineCharacteristicsModule.groups[]` **does** carry an explicit `Total` group (present
  on every trial sampled, e.g. `BG003`). Use it: summing is right for counts and wrong for a
  mean age.
- `deathsNumAtRisk` and `seriousNumAtRisk` are separate and may differ. Sharing one computes
  a rate against the wrong population.
- `participantFlowModule.periods[]` can hold several periods (crossover, extensions).
  Milestones repeat per period, so summing across them double-counts participants.
- Milestone `type` values are not a closed enum — observed `STARTED`, `COMPLETED`, and also
  free text such as `Treated` and `NOT COMPLETED`.

### Outcome measures are not comparable across trials

Measured over 25 melanoma trials with results: **157 outcome measures, 144 distinct titles,
34 distinct units.** Even `"Percentage of participants"` and `"Percentage of Participants"`
are separate strings. There is no cross-trial aggregation without an ontology, which is why
outcome measures are the one results module this system does not read.

## Filters — confirmed working

| Purpose | Parameter | Verified count |
|---|---|---|
| condition | `query.cond=melanoma` | 3,743 |
| intervention | `query.intr=pembrolizumab` | 2,922 |
| sponsor | `query.spons=Merck` | 5,191 |
| location | `query.locn=France` | 42,724 |
| free text | `query.term=immunotherapy` | 10,021 |
| status | `filter.overallStatus=RECRUITING,COMPLETED` | comma-separated, works |
| phase | `filter.advanced=AREA[Phase]PHASE3` | 219 (× melanoma) |
| sponsor class | `filter.advanced=AREA[LeadSponsorClass]INDUSTRY` | 131,495 |
| country | `filter.advanced=AREA[LocationCountry]France` | 42,635 |
| geo radius | `filter.geo=distance(39.0035,-77.1013,50mi)` | 29,714 |

**`SEARCH[Location]` nesting works and matters.** Trial-level status ≠ site-level status.

```
AREA[LocationCountry]France                                          → 42,635
SEARCH[Location](AREA[LocationCountry]France AND AREA[LocationStatus]RECRUITING) → 9,347
```

The unnested form matches trials that have *a* French site and are *separately* recruiting
somewhere; the nested form requires the French site itself to be recruiting. For
"which countries have the most recruiting trials for X" the nested form is the correct
one. This should be pushed down, not handled as a local filter.

**Entity matching is fuzzy.** `query.intr=pembrolizumab` returns trials whose top hit does
not name pembrolizumab in the title — the API expands synonyms and related terms. Quoting
(`query.intr="pembrolizumab"`) returned an identical count, so quotes do not force exact
match here. This is a coverage/precision tradeoff to state in the README, and a reason the
planner's `probe_count` is worth its cost.

## Enum vocabulary (boot-time, from `/studies/enums`)

41 types. The ones the plan's validator needs:

- `Phase` (6): `NA`, `EARLY_PHASE1`, `PHASE1`, `PHASE2`, `PHASE3`, `PHASE4`
- `Status` (14): the 8 common ones plus `AVAILABLE`, `NO_LONGER_AVAILABLE`,
  `TEMPORARILY_NOT_AVAILABLE`, `APPROVED_FOR_MARKETING`, `WITHHELD`, **`UNKNOWN`**
- `AgencyClass` (9): `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `INDUSTRY`, `NETWORK`, `AMBIG`,
  `OTHER`, `UNKNOWN`
- `StudyType` (3), `InterventionType` (11), `PrimaryPurpose` (10), `DesignMasking` (5),
  `DesignAllocation` (3), `Sex` (3), `StandardAge` (3)

Note `UNKNOWN` is a real member of both `Status` and `AgencyClass` — it is a recorded value,
not a null. One fixture (NCT00874328) carries `overallStatus: UNKNOWN`.

Corpus-wide phase distribution, for calibration:

```
missing (no phases key): 141,685   NA: 233,881   PHASE2: 89,652
PHASE1: 65,325   PHASE3: 49,614   PHASE4: 35,625   EARLY_PHASE1: 6,431
```

**`NA` and missing together are ~63% of the corpus.** Any phase chart that silently drops
them is wrong by a wide margin. `phases` is absent entirely for observational and expanded
-access studies — distinct from the `NA` bucket, which means "interventional, not applicable".

## Fixtures

11 records in `tests/fixtures/raw_studies/`, chosen for nastiness, not representativeness.

| NCT ID | Why it's here |
|---|---|
| `NCT00676871` | multi-phase `PHASE1+PHASE2`, WITHDRAWN, `whyStopped` set, **0 locations**, 7 interventions, no `startDateType` |
| `NCT00874328` | `overallStatus: UNKNOWN`, partial date, collaborator present |
| `NCT00987428` | `phases: [NA]`, TERMINATED, `whyStopped` set, enrollment ACTUAL |
| `NCT02229435` | OBSERVATIONAL — **`phases` key absent entirely**; enrollment 1,031,336 |
| `NCT02248896` | OBSERVATIONAL, enrollment 1,129,062, **no completion date**, 0 locations |
| `NCT02803307` | clean happy path: ACTUAL dates both ends, `hasResults: true`, 3 sites |
| `NCT04078230` | full `YYYY-MM-DD` dates, **future ESTIMATED completion (2027)**, 13 sites / 2 countries |
| `NCT04193930` | **enrollment 0**, WITHDRAWN, start == completion date |
| `NCT05844436` | EXPANDED_ACCESS — no phases, **no dates at all**, no enrollment, status `AVAILABLE` |
| `NCT06077760` | **33 countries, 229 sites** — choropleth and site-count exercise |
| `NCT07725679` | **future ESTIMATED start (2027-02)**, NOT_YET_RECRUITING, 0 locations |

One further record lives in `tests/fixtures/results_studies/`, kept separate because the
eleven above back hand-counted golden assertions that a twelfth would silently shift:

| NCT ID | Why it's here |
|---|---|
| `NCT01866319` | **full `resultsSection`** — three arms, participant flow, adverse events, baseline age and sex. 144 KB against ~17 KB for a registration-only record |

Between them these cover every quirk in the plan's normalizer section plus the two
corrections above: absent `phases`, absent dates, absent `type`, zero and million-scale
enrollment, zero and 33-country location lists, and every date-certainty combination.
