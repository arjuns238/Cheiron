# Corpus facts, with the queries that produced them

**→ These belong in the README's "Limitations" section, each cited with its query.**

Every figure below is reproducible by pasting the command. All were captured on
2026-08-08 against API version `2.0.5`, data timestamp `2026-08-07T09:00:05`. Raw
responses are saved under `docs/corpus-evidence/` so the numbers in the README can be
checked without re-running anything.

The denominator throughout:

```bash
curl -s 'https://clinicaltrials.gov/api/v2/stats/size' | jq .totalStudies
# 597691
```

Note on method: `/stats/field/values` accepts **no** query or filter parameters (see
`docs/api-findings.md`), so every figure here is corpus-wide, not scoped to any slice.
That is the correct scope for a limitations section — these are properties of the
registry, not of one question.

---

## 1. Phase is unusable for ~63% of the registry

```bash
curl -s -G 'https://clinicaltrials.gov/api/v2/stats/field/values' \
     --data-urlencode 'fields=Phase'
```

```json
{"type":"ENUM","piece":"Phase","field":"protocolSection.designModule.phases",
 "missingStudiesCount":141685,"uniqueValuesCount":6,
 "topValues":[{"value":"NA","studiesCount":233881},{"value":"PHASE2","studiesCount":89652},
              {"value":"PHASE1","studiesCount":65325},{"value":"PHASE3","studiesCount":49614},
              {"value":"PHASE4","studiesCount":35625},{"value":"EARLY_PHASE1","studiesCount":6431}]}
```

`(141685 + 233881) / 597691 = 62.8%`

**Two distinct populations, not one.** `missingStudiesCount` (141,685) is studies with no
`phases` key at all — observational and expanded-access studies, where phase is not a
concept. `NA` (233,881) is interventional studies where phase does not apply: devices,
procedures, behavioural interventions. Conflating them is wrong, and dropping either is
worse: a phase chart that silently excludes both is computed over 37% of the registry
while appearing to describe all of it.

This system buckets them separately as "Not Reported" and "Not Applicable", and counts
both into `meta.record_counts` rather than into `excluded_by_reason`.

## 2. Sponsor names are free text with 51,497 spellings

```bash
curl -s -G 'https://clinicaltrials.gov/api/v2/stats/field/values' \
     --data-urlencode 'fields=LeadSponsorName'
```

```json
{"type":"STRING","piece":"LeadSponsorName","missingStudiesCount":0,"uniqueValuesCount":51497,
 "longest":{"value":"Association Grenobloise pour le Developpement D'etudes et de Recherches
             en Physiopathologie Endocrinienne, Diabetologie et Maladies de la Nutrition",
            "length":147,"nctId":"NCT00973492"},
 "topValues":[{"value":"Cairo University","studiesCount":4787},
              {"value":"Assiut University","studiesCount":4740},
              {"value":"GlaxoSmithKline","studiesCount":3625},
              {"value":"National Cancer Institute (NCI)","studiesCount":3551},
              {"value":"Assistance Publique - Hôpitaux de Paris","studiesCount":3534}]}
```

There is no canonical sponsor identifier in the registry. 51,497 distinct strings across
597,691 studies, and the same organisation appears under multiple spellings, with and
without legal suffixes, in more than one language. **This system does not deduplicate
them**, so a sponsor bar chart or a sponsor network node counts *spellings*, not
*organisations*, and undercounts any sponsor whose name varies.

Worth stating plainly in the README: entity resolution over these strings is a real piece
of work and was deliberately not attempted in the time box.

## 3. Intervention names are worse — 528,741 distinct values

```bash
curl -s -G 'https://clinicaltrials.gov/api/v2/stats/field/values' \
     --data-urlencode 'fields=Condition,InterventionName,LocationCountry'
```

| Field | Distinct values | Studies missing it |
|---|---:|---:|
| `Condition` | 132,657 | 1,021 |
| `InterventionName` | 528,741 | 60,685 |
| `LocationCountry` | 226 | 60,146 |

528,741 distinct intervention names across 597,691 studies is very nearly one new string
per study: brand names, generic names, internal code names, dose-specific variants, and
placebo arms all appear as separate values.

**This is the argument for the MeSH fields.** A drug-drug or sponsor-drug network built on
raw `InterventionName` produces a hairball of near-duplicate nodes. Built on
`InterventionMeshTerm` — a controlled vocabulary assigned by ClinicalTrials.gov's own
indexer — the nodes are meaningful. The tradeoff, also worth stating: MeSH indexing is
lossy, so novel agents without an assigned MeSH heading drop out of the graph entirely.

`LocationCountry` at 226 distinct values is a genuinely clean field by comparison — but
missing on 60,146 studies (10.1%), which is why geographic charts report their exclusions.

## 4. Enrollment spans zero to 188 million

```bash
curl -s -G 'https://clinicaltrials.gov/api/v2/stats/field/values' \
     --data-urlencode 'fields=EnrollmentCount'
```

```json
{"type":"INTEGER","piece":"EnrollmentCount",
 "field":"protocolSection.designModule.enrollmentInfo.count",
 "missingStudiesCount":7131,"min":0,"max":188814085,"avg":5510.118443417497}
```

**This single stat justifies the choice of median over mean.** A mean of 5,510 does not
describe any real clinical trial; it is an artefact of a right tail reaching 188,814,085 —
a value that is either a population-registry observational study or a data-entry error, and
either way is not a trial that enrolled 188 million people. One such record in a bucket
moves a mean by orders of magnitude and moves a median not at all.

`min: 0` is also real and is not a null: withdrawn trials record an ACTUAL enrollment of
zero (fixture `NCT04193930`). A chart that treats 0 as missing would silently discard the
withdrawn population.

Enrollment additionally mixes ACTUAL and ESTIMATED counts in one field, distinguishable
only via `EnrollmentType` — which must be requested explicitly in `fields=` or it is
silently omitted from the response.

---

## Also for the README's limitations section

Not corpus statistics, but established by the same reconnaissance and documented in
`docs/api-findings.md`:

- **`startDateStruct.type` is absent on many older records** — 1000/1000 in a 1990–2005
  sample. "Estimated" and "unrecorded" are different populations, so date certainty is
  three-valued here, not boolean.
- **Entity search is fuzzy and synonym-expanded.** `query.intr=pembrolizumab` returns
  2,922 studies including ones whose titles do not name it. Quoting does not force exact
  match. Recall is favoured over precision, and the number is a superset.
- **Trial-level status ≠ site-level status.** `AREA[LocationCountry]France` matches 42,635
  studies; `SEARCH[Location](AREA[LocationCountry]France AND AREA[LocationStatus]RECRUITING)`
  matches 9,347. Only the second answers "recruiting trials in France".
- **`UNKNOWN` is a recorded value in both `Status` and `AgencyClass`**, not a null. It
  means the sponsor has not verified the record recently.
