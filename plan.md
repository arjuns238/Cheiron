# ClinicalTrials.gov Query-to-Visualization Agent — Design Plan

## 0. The core invariant

> The LLM sees aggregate facts about the data and never a trial record. No
> LLM-visible number ever reaches the output.

Every architectural decision below follows from this. Numbers are produced by
deterministic code that folds over lists of source records; the LLM chooses
*what to compute* and *how to display it*, never *what the value is*.

Consequence: when the system is wrong, it is loudly wrong (validation error,
invariant failure, explicit `unsupported` response) rather than quietly wrong (a
plausible chart built on a bad aggregation).

**Justification for the README.** This is the semantic-layer pattern from BI
tooling, not an ad-hoc choice. Current benchmarks put constrained-vocabulary
approaches at or near 100% accuracy within scope versus roughly 84–90% for
direct query generation, and the failure modes differ in kind: constrained
systems return an error, unconstrained systems return a wrong number. Same
conclusion appears independently in the NL2VIS literature under the name "DSL
mediation."

---

## 1. Response envelope

One shape for every response. `response_type` discriminates; `visualization` is
null in exactly one case.

```
{
  "request_id":    "uuid",
  "response_type": "visualization" | "conversational" | "unsupported" | "no_results",
  "answer":        "one-sentence templated summary, numbers substituted from aggregator",

  "visualization": {
    "type":     "line | bar | grouped_bar | stacked_area | scatter | histogram | network | choropleth | kpi",
    "title":    "human-readable",
    "subtitle": "optional",
    "encoding": { "x": {...}, "y": {...}, "series": {...} | null },
    "data":     [ ... ],
    "config":   { "sort": ..., "units": ..., "granularity": ..., "top_n": ... }
  },

  "citations": {
    "NCT01234567": {
      "nct_id":      "NCT01234567",
      "url":         "https://clinicaltrials.gov/study/NCT01234567",
      "brief_title": "...",
      "field_path":  "protocolSection.designModule.phases[0]",
      "field_value": "PHASE3",
      "excerpt":     "verbatim substring of the API response",
      "offset":      [start, end]
    }
  },

  "meta": {
    "interpretation":      "plain-language restatement of the plan",
    "plan":                { ...echo of the committed Plan... },
    "filters_applied":     { ... },
    "counting_semantics":  "trials counted once per distinct country; column sums exceed trial count",
    "record_counts":       { "matched": 4213, "retrieved": 4213, "used": 3980,
                             "excluded_by_reason": {"missing_start_date": 233},
                             "truncated": false },
    "assumptions":         [ "..." ],
    "warnings":            [ "..." ],
    "planning_trace":      [ {"tool": "probe_count", "args": {...}, "result": {...}} ],
    "api_requests":        [ "https://..." ],
    "generated_at":        "iso8601",
    "cache_hit":           true
  }
}
```

### Rules

- `conversational` is the **only** type with `visualization: null`.
- `no_results` and `unsupported` still return a full `visualization` block with
  empty `data` and the reason in `meta.warnings`, so the frontend renders one
  shape always.
- `data[i].nct_ids` holds up to 5 IDs plus `nct_id_total`; full attribution
  lives in the top-level `citations` map, deduped across datapoints.
- `answer` is **templated**, not LLM-written. Slots filled from the aggregator.

### Endpoints

| Route | Purpose |
|---|---|
| `POST /analyze` | main |
| `POST /plan` | returns the committed Plan only, no retrieval — demos the agent layer, useful for tests |
| `GET /capabilities` | supported fields, metrics, viz types, known limitations |
| `GET /schema` | JSON Schema for request and response |
| `GET /health` | liveness |

`/capabilities` and `/schema` are generated from the Pydantic models, not
hand-written. FastAPI gives `/docs` free.

---

## 2. Pipeline

```
POST /analyze
     │
     ▼
REQUEST VALIDATOR ·det·
     │
     ▼
① ROUTER «LLM» ──── chit-chat ──► conversational response, 0 API calls
     │ in-domain
     ▼
╔═══════════ PLAN LOOP · max 3 revisions ═══════════╗
║  ② PLANNER «LLM» ◄──► PROBE TOOLS ·det· ──► ct.gov ║
║        │  (≤4 calls: probe_count, field_values,    ║
║        │   fill_rate — return counts, never rows)  ║
║        ▼                                          ║
║  PLAN VALIDATOR ·det· ─── errors ────────┐        ║
║        │                                  │        ║
║        ▼                                  │        ║
║  ③ JUDGE «LLM» ─── concerns ──────────────┤        ║
║        │  (query + plan + probe results)  │        ║
║   verdict: ok                             └► PLANNER
╚═══════════════════╪═══════════════════════════════╝
                    ▼
QUERY COMPILER ·det·        Plan → API requests, per leg
                    ▼
API CLIENT ·det·            paginate, retry, cache  ◄──► ct.gov
                    ▼
NORMALIZER ·det·            flatten, parse, log exclusions
                    ▼
AGGREGATOR ·det·            bucket → [(nct_id, value)] ; citations born here
                    ▼
INVARIANT CHECK ·det·       raises on failure
                    ▼
VIZ RULES ·det·             result shape → legal chart set
                    ▼
④ CHART SELECTOR «LLM»      picks within legal set; sees shape, not values
                    ▼
SPEC ASSEMBLER ·det·
                    ▼
RESPONSE ENVELOPE
```

Four LLM touchpoints. Everything between QUERY COMPILER and SPEC ASSEMBLER is
deterministic and unit-testable with fixtures, no API key and no network.

---

## 3. Stage contracts

### ① Router «LLM»

| | |
|---|---|
| In | `query` |
| Out | `{ "in_domain": bool }` |
| Sees data | no |
| Failure | default `in_domain: true`; a wrong chart beats a wrong refusal |

Only pre-retrieval gate. "hi" costs one cheap classification and zero API
requests. Bar for `false` is high: anything mentioning trials, drugs,
conditions, sponsors, or countries is in-domain.

### ② Planner «LLM» + probe tools

| | |
|---|---|
| In | `query`, structured overrides, `LEGAL_FIELDS` with types, enum members |
| Tools | `probe_count(filters) → {total}` · `field_values(field, filters) → {value: count}` · `fill_rate(field, filters) → float` |
| Budget | ≤4 tool calls per attempt |
| Out | `Plan` |
| Sees data | aggregate counts only, never a record |

Static context in the prompt is **generated** from the flattener's output keys
plus the enum lists fetched from `/studies/enums` at boot. Adding a field to the
flattener updates the prompt, the validator, and the legal set at once.

Probes exist because these decisions are unanswerable from schema alone:

| Decision | Probe |
|---|---|
| does this entity resolve at all | `probe_count` |
| `query.intr` vs `query.cond` vs `query.term` | `probe_count` on each |
| `top_n` needed? how many buckets? | `field_values` |
| granularity: year vs quarter | `probe_count` + date span |
| will the page cap truncate? | `probe_count` |
| is `metric_field` populated in this slice? | `fill_rate` |


**Trap:** the planner now sees numbers (`probe_count` returned 4213). That
number must never appear in `visualization.data`. Probe results go to
`meta.planning_trace` and may set plan fields like `top_n`. Assert that every
datum value originates from the aggregator.

### Plan schema

```
Plan {
  legs:         [ Leg ]                 # 1 for simple, N for comparisons
  group_by:     str | null              # a flattener output key
  series_by:    str | null
  metric:       "count" | "distinct_count" | "sum" | "median"
  metric_field: str | null              # required for sum/median
  distinct_of:  str | null              # required for distinct_count
  granularity:  "year" | "quarter" | null
  top_n:        int | null
  sort:         "value_desc" | "value_asc" | "dimension_asc"
  viz_hint:     str | null              # advisory
  assumptions:  [ str ]
}

Leg { label: str, filters: Filters }
```

**Comparisons are legs, not agents.** "Compare sponsor categories across two
conditions" = two legs, one `group_by`, merged into a series dimension. Every
comparison and grouped-bar example in the assignment appendix falls out of this.
No sub-planner needed.

### Filters

Pushdown (server-side, into the API request):

| Filter | Param |
|---|---|
| condition | `query.cond` |
| intervention | `query.intr` |
| sponsor | `query.spons` |
| location | `query.locn` |
| free_text | `query.term` |
| status[] | `filter.overallStatus` |
| phase[] | `filter.phase` |
| start_date_range | `filter.advanced` → `AREA[StartDate]RANGE[...]` |

Local (post-fetch, no API equivalent): `study_type`, `sponsor_class`,
`intervention_type`, `enrollment_min/max`, `has_results`, `date_certainty`
(ACTUAL vs ESTIMATED), exact `country`.

Local filters make `retrieved` and `used` diverge. Both go in
`meta.record_counts`.

### Plan validator ·det·

Errors are returned to the planner verbatim as feedback.

1. `group_by` / `series_by` / `metric_field` / `distinct_of` must be in `LEGAL_FIELDS`
2. `sum` / `median` require a numeric `metric_field`
3. `distinct_count` requires `distinct_of`
4. `granularity` requires a temporal `group_by`
5. `top_n` requires an entity-kind `group_by`
6. `group_by != series_by`
7. `series_by` and `len(legs) > 1` are mutually exclusive — legs *become* the series
8. every filter value must be a member of that field's enum
9. `network` viz_hint requires two entity-kind dimensions
10. at least one of `group_by` / `legs[].filters` must be non-empty

### ③ Judge «LLM»

| | |
|---|---|
| In | `query`, `Plan`, probe results |
| Out | `{"verdict": "ok"}` or `{"verdict": "concern", "concerns": [str]}` |
| Sees data | no records; probe aggregates only |
| Authority | advisory — triggers at most one re-plan, then proceeds |

Silent unless concerned, per requirement. Two guards against a
rubber-stamp judge:

- **Verdict token is always required.** `{"verdict": "ok"}` is distinguishable
  from a failed call, and approval rate is loggable.
- **Prompt names the failure classes.** Not "assess quality" but this checklist:
  - metric doesn't match the question's noun (counting trials when asked about money)
  - comparison collapsed into one series (missing legs)
  - filter applied to the wrong field
  - time basis mismatched (start vs completion date)
  - grouping field is mostly null in this slice (visible from probes)

Validate honestly: feed it three known-bad plans and confirm it catches them. If
it doesn't, say so in the README rather than claiming an unvalidated
verification layer.

### Plan loop exhaustion

- Max **3** revisions.
- Keep a set of rejected plans; the planner cannot repropose one.
- On exhaustion, ship the **best-scoring** attempt, not the last one — later
  attempts drift as the model over-corrects. Usually attempt 1.
- Add `meta.warnings`: "plan was contested and not fully resolved."
- Never fail closed.

Optional latency win: the judge needs only query + plan + probes, so it can run
concurrently with the compiler and API client. A late concern triggers re-plan
and refetch; the cache makes the second fetch nearly free.

### Query compiler ·det·

Plan → one API request set per leg. Owns:

- Essie `filter.advanced` construction
- field projection derived from `group_by`, `series_by`, `metric_field`, and
  citation needs — don't pull full records for a bar chart
- routing: if `group_by` is an indexed enum, `metric` is `count`, and citations
  aren't required, the stats endpoint answers in one call; otherwise fetch
  records

### API client ·det·

- `pageSize=1000` explicitly — default is 10 and it clamps silently above 1000
- cursor pagination via `nextPageToken`
- `countTotal=true` on the first request → reconciliation baseline
- retry with backoff, polite rate limit
- disk cache keyed on the full request → reproducible example runs, fast demo
- page cap with a `truncated` flag surfaced in `meta`

### Normalizer ·det·

Flattens each record to a dict of **scalars and flat lists of strings**. This
is the only place API messiness lives, and it exists so the aggregator has two
shapes to handle instead of N traversal cases.

Target fields (core):

| Key | Source | Kind | Multi |
|---|---|---|---|
| `nct_id` | `identificationModule.nctId` | id | |
| `brief_title` | `identificationModule.briefTitle` | text | |
| `start_year` | `statusModule.startDateStruct.date` | temporal | |
| `start_is_actual` | same struct `.type` | bool | |
| `completion_year` | `completionDateStruct.date` | temporal | |
| `status` | `statusModule.overallStatus` | categorical | |
| `why_stopped` | `statusModule.whyStopped` | text | |
| `phases` | `designModule.phases[]` | categorical | ✓ |
| `study_type` | `designModule.studyType` | categorical | |
| `enrollment` | `enrollmentInfo.count` | numeric | |
| `enrollment_is_actual` | `enrollmentInfo.type` | bool | |
| `sponsor_name` | `leadSponsor.name` | entity | |
| `sponsor_class` | `leadSponsor.class` | categorical | |
| `collaborators` | `collaborators[].name` | entity | ✓ |
| `conditions` | `conditionsModule.conditions[]` | entity | ✓ |
| `intervention_names` | `interventions[].name` | entity | ✓ |
| `intervention_types` | `interventions[].type` | categorical | ✓ |
| `countries` | `locations[].country`, deduped | entity | ✓ |
| `site_count` | `len(locations)` | numeric | |
| `has_results` | `hasResults` | bool | |

Stretch (add in this order — the first two are already in the payload and cost
almost nothing):

| Key | Source | Unlocks |
|---|---|---|
| `intervention_mesh` | `derivedSection.interventionBrowseModule.meshes[].term` | credible drug network nodes |
| `condition_mesh` | `conditionBrowseModule.meshes[].term` | condition normalization |
| `therapeutic_area` | `conditionBrowseModule.ancestors[]` rollup | ~20 buckets from 40k strings |
| `duration_days` | start → primaryCompletion, ACTUAL only | scatter |
| `is_combination` | >1 DRUG intervention sharing an `armGroupLabel` | meaningful drug↔drug edges |
| `primary_purpose`, `masking`, `allocation`, `sex` | designModule / eligibilityModule | one line each |

Real quirks it must handle (all confirmed against live records):

- dates arrive as `2019`, `2019-03`, or `2019-03-14`
- `phases` is a list; `NA` is common (procedure/device/behavioral trials);
  `PHASE1|PHASE2` combos exist and are their own bucket, never double-counted
- future-dated `ESTIMATED` start dates produce a phantom forward tail
- `locations[]` is often incomplete relative to the prose; site-level `status`
  differs from trial-level
- `minimumAge` is a string with mixed units (`"40 Years"`, `"6 Months"`)
- any array can be absent or empty
- `enrollment` mixes ACTUAL and ESTIMATED

Every drop is counted by reason into `excluded_by_reason`. Nothing disappears
silently.

### Aggregator ·det·

One structure:

```
buckets: { bucket_label: [ (nct_id, contributed_value) ] }
```

Chart value is a fold over the bucket. Citations are the same list's first
elements. **They cannot disagree because they are one object.**

| Metric | Fold |
|---|---|
| `count` | length of list |
| `distinct_count` | distinct values of `distinct_of` |
| `sum` | sum of non-null values |
| `median` | median of non-null values |

Prefer `median` over mean for enrollment — it is heavily skewed. Say so in the
README.

Multi-valued dimensions: a trial contributes to every bucket its list touches.
This makes column sums exceed distinct trial count, which **must** appear as
`meta.counting_semantics`. Derived automatically from the field's `multi` flag,
not hand-written per query.

Network graphs: an edge's weight is `len()` of the list of trials in which both
nodes appear. Same structure, same provenance.

### Invariant check ·det· — raises, never warns

Anything caught here means a wrong chart.

1. `used + sum(excluded_by_reason.values()) == retrieved`
2. `retrieved == matched` OR `truncated is True`
3. every cited `nct_id` is in the set actually fetched
4. for single-valued `group_by`: `sum(len(b) for b in buckets) == used`
5. every datum value is traceable to a fold over a bucket (no probe leakage)

These four numbers are also `meta.record_counts`, so the code guarding
correctness produces the transparency block.

### Viz rules ·det· → legal set

Run **after** aggregation. Chart choice depends on bucket count and dtype,
which aren't known before the data comes back.

| group_by | series / legs | metric | Legal set |
|---|---|---|---|
| none | none | any | `[kpi]` |
| temporal | none | any | `[line, bar, area]` |
| temporal | present | any | `[stacked_area, grouped_bar, line]` |
| categorical ≤12 | none | any | `[bar, pie]` |
| categorical ≤12 | present | any | `[grouped_bar, stacked_bar]` |
| entity, high card | none | any | `[bar]` + sort desc + top_n + "other" |
| entity | entity | `count` | `[network]` |
| `countries` | none | any | `[choropleth, bar]` |
| numeric | none | `count` | `[histogram]` |
| numeric | numeric | — | `[scatter]` |

### ④ Chart selector «LLM»

| | |
|---|---|
| In | `query`, shape summary (bucket count, dtypes, min/max, top labels), legal set |
| Out | one member of the legal set |
| Sees data | shape only, no values |
| Failure | fall back to `legal_set[0]` |

Membership is validated, so the model can only ever downgrade to the rule's
default. It cannot produce an illegal chart.

Why it earns its place: `[line, bar, area]` are all defensible for the same
data. "How has X changed" wants a line; "which year had the most" wants a bar.
The difference is in the phrasing, and only the LLM saw the phrasing.

### Spec assembler ·det·

Assembles the envelope. Also builds citations:

- excerpt is a **literal substring** of the fetched payload at recorded offsets
- assert the substring matches at those offsets; drop the citation on failure
  rather than emit it
- never generated by the LLM

README line: *citations are extracted and mechanically verified, not generated.*

---

## 4. Out of scope — state explicitly

Goes in the README, in `/capabilities`, and in `unsupported` responses.

| Not supported | Why |
|---|---|
| comparative efficacy ("which drug works better") | outcome measures use incommensurable endpoints, units, and paramTypes across trials |
| patient-level questions | registry holds aggregate data only |
| enrollment attributed geographically | enrollment is per-trial, not per-site |
| free-text semantic search over eligibility criteria | out of time box |
| anything from `resultsSection` | v2 |

`unsupported` responses name the specific obstruction and attach
`suggested_requests[]` — complete, postable request bodies for what the system
*can* answer. Turns a refusal into a redirect.

---

## 5. Automatic warnings

Derived from field metadata, not hand-coded per query:

- multi-valued `group_by` → totals exceed distinct trial count
- temporal `group_by` → registry lag undercounts recent periods
- temporal `group_by` → N trials excluded for ESTIMATED start dates
- `phases` in `group_by` → N trials bucketed as Not Applicable
- entity `group_by` with `top_n` → M values collapsed into "other"
- local filters applied → `retrieved` vs `used` differ
- `truncated` → page cap hit, chart is a sample

---

## 6. Build order

curl the API by hand. Confirm `fields` separator (comma vs pipe), `filter.advanced` range syntax, and whether `/studies/enums` + `/studies/metadata` return usable vocab. Save 10 raw records as fixtures. 
Freeze the three schemas: request, `Plan`, response envelope. Write the README schema section now while it's fresh. 
Normalizer + fixtures. Unit tests on the ugliest records you saved. No API, no LLM. 
Aggregator + folds + invariant checks. Still no API, no LLM. **This is the heart of the system — get it right here.** 
API client: pagination, cache, retry, countTotal reconciliation. Wire it to the normalizer. 
Viz rules + spec assembler. Four chart types end to end with a hardcoded plan. 
Planner + plan validator + repair loop + heuristic fallback. 
Probe tools, wired into the planner. 
Network graph + co-occurrence. Highest rubric leverage. 
Deep citations with offset verification. 
Judge + router. 
`unsupported` / `no_results` / `conversational` paths, `suggested_requests`. 
Five example runs, capture actual JSON, fix what breaks. 
README. Tidy tests. Optional single-file HTML demo (Chart.js + vis-network). 
Buffer, self-review against the rubric, zip. 

If you have a working end-to-end path with a
hardcoded plan by then, everything after is additive and you cannot fail to
submit. If you don't, cut the judge, cut probes, cut network, and get there.

Cut order if behind: `fill_rate` → `choropleth` → `stacked_area` → `scatter` →
`histogram` → judge → probes. **Keep the network graph regardless** — it is
explicitly called out as scoring higher.

Hard non-goals: no auth, no database beyond the cache, no streaming, no
multi-user, no frontend beyond one static HTML file.

---

## 7. Validation strategy

The README has to answer "how did you validate correctness." Concretely:

1. **Fixture tests on the normalizer.** The 10 saved records, chosen for
   nastiness: partial dates, missing modules, `NA` phases, multi-phase, empty
   locations, future start dates.
2. **Golden tests on the deterministic core.** Hardcoded plan + fixture records
   → snapshot the aggregator output. No LLM, no network. These are your
   regression suite.
3. **Invariant assertions run in production**, not just tests.
4. **`countTotal` reconciliation** on every real request.
5. **Hand-verified example runs.** For 2 of your 5 examples, count the answer
   manually from the raw JSON and confirm it matches. Say you did this.
6. **Judge adversarial set.** 3 deliberately wrong plans, confirm it flags them.
   Report the result honestly, including if it doesn't.
7. **Planner set.** ~15 queries covering each intent, assert the committed plan
   matches expectation. Cheap and catches prompt regressions.

---

## 8. README outline

Roughly in this order, because a reviewer reads top-down and the rubric weights
design at 35%:

1. **What it does** — one paragraph, one example request/response pair
2. **How to run** — install, env vars, start, one curl that works. Note that it
   runs without an API key using the heuristic planner.
3. **Architecture** — the pipeline diagram, the four LLM touchpoints table, and
   the core invariant stated as a single sentence
4. **Design decisions and tradeoffs**
   - semantic layer over free-form generation, with the benchmark justification
   - citations as the aggregation structure, not a post-hoc lookup
   - custom envelope over Vega-Lite (network graphs don't fit; assignment asks
     for your own documented schema)
   - two retrieval strategies behind one interface
   - comparisons as legs, not sub-agents
   - rules for chart legality, LLM for chart preference
5. **Request schema** — fields, types, required/optional, validation, and the
   override precedence rule
6. **Response schema** — every field, with the `response_type` union spelled out
7. **Query coverage** — state it as grammar size, not a list of queries: N
   fields × 4 metrics × 10 viz types, plus what's explicitly out of scope and why
8. **Limitations** — the real-data landmines section. Registry lag,
   incomplete locations, `NA` phases, ESTIMATED dates, incommensurable outcomes.
   Being specific here reads as having actually looked at the data.
9. **Validation** — section 7 above
10. **AI tool usage** — which tools, what you designed deliberately vs generated
    and adapted. Required by section 8 of the assignment. Be specific and
    honest; they explicitly reward evidence of iteration.
11. **What I'd do with more time** — MeSH normalization depth, resultsSection
    metrics, semantic entity resolution, streaming for large slices

---