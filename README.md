# Cheiron Take Home Assignment

A backend service that turns a natural-language question about clinical trials into a
structured visualization specification, backed by live data from the
[ClinicalTrials.gov Data API](https://clinicaltrials.gov/data-api/api).

```
POST /analyze  {"query": "How are melanoma trials distributed across phases?"}
    → a bar chart specification, 3,743 trials, every bar carrying excerpts
      from the records that produced it
```

A demo frontend is served at `GET /ui`. It is deliberately a plain client of the documented
schema: it reads `encoding` to find the dimension key rather than hardcoding one, so it
doubles as evidence the schema is implementable without guessing.

---

## Contents

- [Running it](#running-it)
- [Endpoints](#endpoints)
- [System design](#system-design)
- [Request schema](#request-schema)
- [Response schema](#response-schema)
- [Key design decisions](#key-design-decisions)
- [Future work](#future-work)
- [Tools, validation, and what was designed deliberately](#tools-validation-and-what-was-designed-deliberately)
- [Where the reasoning lives](#where-the-reasoning-lives)

---

## Running it

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
cp .env.example .env          # then fill in one provider's API key
.venv/bin/python -m uvicorn cheiron.api.app:app --port 8000
```

Then:

| | |
|---|---|
| `http://localhost:8000/ui` | the demo frontend |
| `http://localhost:8000/docs` | Swagger, generated from the Pydantic models |

### Configuration

`.env` needs `LLM_PROVIDER` plus that provider's key. OpenAI and Anthropic are supported.

```ini
LLM_PROVIDER=openai          # anthropic | openai

OPENAI_API_KEY=
OPENAI_MODEL_LARGE=gpt-5.4
OPENAI_MODEL_SMALL=gpt-5.4-mini
```
---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/analyze` | The main endpoint. Question in, visualization specification out. |
| `POST` | `/plan` | Returns the committed plan without retrieving anything. Costs the planner, probes and reviewer; no page fetches. Useful for inspecting the agent layer cheaply. |
| `GET` | `/capabilities` | Groupable fields, metrics, chart types, limits. Generated from the field registry, so it cannot drift from the code. |
| `GET` | `/schema` | JSON Schema for the request and response models. Generated from Pydantic. |
| `GET` | `/health` | Liveness, plus whether ClinicalTrials.gov is reachable and a provider is configured. |
| `GET` | `/ui` | The demo frontend. |

### Error responses

Each failure mode is distinct on purpose, because collapsing them would let the service say
something false about the data.

| Status | `error` | Meaning |
|---|---|---|
| `422` | *(FastAPI validation)* | The request body is malformed. Unknown fields are rejected rather than ignored — a typo'd field name is an error, not a silently dropped filter. |
| `422` | `override_conflict` | The question and a structured parameter disagree on the same dimension. See [design decisions](#a-queryparameter-contradiction-is-refused-not-resolved). |
| `500` | `invariant_failure` | The record counts did not reconcile, so the chart would have been wrong. No chart is returned rather than an unverified one. |
| `502` | `upstream_unavailable` | ClinicalTrials.gov did not answer. Deliberately **not** `no_results`, which is the claim that the registry holds no matching trials. |
| `503` | — | The LLM provider is unavailable. |

---

## System design

One invariant drives the layering:

> **The models see aggregate facts about the data and never a trial record. No LLM-visible
> number ever reaches the output.**

There are exactly four model touchpoints. Everything else is deterministic, and every model
stage is bracketed by deterministic code that constrains what it can do — the planner's
output must pass a validator, the judge is advisory, the chart selector may only pick
within a set the rules already computed.

```
                               POST /analyze
                                    │
                       ┌────────────▼────────────┐
                       │ request validator  ·det │──► 422  malformed body, or a
                       └────────────┬────────────┘         parameter/query contradiction
                                    │
                       ┌────────────▼────────────┐
                       │ ① ROUTER           ·LLM │──► conversational | unsupported
                       └────────────┬────────────┘     (zero registry calls)
                                    │ in-domain
 ┌──────────────────────────────────▼───────────────────────────────────────┐
 │ PLAN LOOP                                                                │
 │                                                                          │
 │   ┌─────────────────────┐  ≤4 probes  ┌───────────────────────────────┐  │
 │   │ ② PLANNER      ·LLM │ ◄─────────► │ probe tools ·det  ──► ct.gov │  │
 │   └──────────┬──────────┘             │ counts, never rows            │  │
 │              │                        └───────────────────────────────┘  │
 │   ┌──────────▼──────────┐                                                │
 │   │ plan validator ·det │ ── errors ─────────► re-plan, ≤3 revisions     │
 │   └──────────┬──────────┘                                                │
 │              │ legal                                                     │
 │   ┌──────────▼──────────┐ ── concern ────────► re-plan, ≤1               │
 │   │ ③ JUDGE        ·LLM │ ── contradiction ──► 422                       │
 │   └──────────┬──────────┘                                                │
 └──────────────┼───────────────────────────────────────────────────────────┘
                │ committed plan
     ┌──────────▼───────────┐
     │ query compiler  ·det │  Plan → one query per leg, narrowest `fields=`
     ├──────────────────────┤
     │ API client      ·det │  paginate ≤100 pages, retry, rate-limit, cache
     ├──────────────────────┤
     │ normalizer      ·det │  flatten to scalars; every drop counted by reason
     ├──────────────────────┤
     │ aggregator      ·det │  ◄── every charted value and every citation is born here
     ├──────────────────────┤
     │ INVARIANT CHECK ·det │ ── mismatch ───────► 500, no chart
     ├──────────────────────┤
     │ viz rules       ·det │  result shape → the legal chart set
     └──────────┬───────────┘
                │ legal set
     ┌──────────▼───────────┐
     │ ④ CHART SELECTOR·LLM │  picks within the set; sees shape, never values
     └──────────┬───────────┘  any failure → legal[0]
                │
     ┌──────────▼───────────┐
     │ spec assembler  ·det │  envelope + offset-verified citations
     └──────────┬───────────┘
                ▼
          AnalyzeResponse
```

The models choose **what to compute** (the planner) and **how to display it** (the
selector). They never choose **what a value is** — that is the aggregator folding over
lists of source records, and the invariant check is what makes the claim testable rather
than aspirational.

### How correctness is enforced: rules first, judgement second

Two different kinds of check, at two different layers, because they catch different things.
A rule can only test what is expressible in code; a judge can read the question. Neither
alone is enough.

**Deterministic gates**

| Gate | Rejects | On failure |
|---|---|---|
| Request validator | Malformed body, unknown field, inverted range, empty enum list | `422` |
| Plan validator | A plan naming a field that does not exist, or a metric its field cannot support | Back to the planner with the error text |
| Invariant check | `used + Σ excluded ≠ retrieved`, or `retrieved ≠ matched` without `truncated` | `500`, **no chart** |
| Viz rules | Any chart type illegal for the result shape | The type never enters the selector's choices |
| Citation verifier | An excerpt that is not a literal substring at its stated offsets | The citation is dropped and counted |

The invariant check is the load-bearing one, and it is deliberately not a warning. A
reconciliation failure means the chart would have been wrong, and shipping a wrong chart
with a caveat is exactly the quiet failure the architecture exists to prevent.

**The LLM judge — for what a rule cannot express.** A plan can pass every validator rule
and still answer a different question than the one asked. That is not expressible as a
constraint over the plan alone: it needs the question. So the judge reads both, against a
**closed list of eight failure classes**:

```
1  METRIC MISMATCH          5  SPARSE GROUPING
2  COLLAPSED COMPARISON     6  UNQUANTIFIED SUPERLATIVE
3  WRONG FIELD              7  PARAMETER CONTRADICTION  → 422, the one fatal verdict
4  TIME BASIS               8  DROPPED QUALIFIER
```

The list is closed on purpose — the prompt says *"this is the whole list, do not invent
other grounds"*, which is what stops a reviewer manufacturing concerns. "Assess quality"
invites agreement; a checklist forces a decision per item.

The judge is **advisory** everywhere except class 7. Its verdict is recorded in `meta.review` on every reviewed request — including approvals, so that "the judge approved this" and "the judge never ran" are distinguishable from the response alone.

**Feedback loops — three, each bounded, each degrading to something safe.**

| Loop | Trigger | Budget | When exhausted |
|---|---|---|---|
| Planner repair | Plan validator errors, handed back verbatim | 3 revisions | Ship the best-scoring attempt, flag `contested` |
| Judge re-plan | A concern from the closed list | 1 | Commit anyway; the concern becomes a warning |
| Selector fallback | Malformed or unusable choice | 0 | Fall back to `legal[0]` |

Every failure direction is chosen so an outage costs nothing rather than something wrong:
the router fails **open** to in-domain (a wrong refusal looks broken; a wrongly-analysed
greeting merely returns nothing), the judge fails toward **approval** (it is advisory, so a
provider outage should not spend a re-plan on no evidence), and a malformed verdict is
treated as a concern rather than an approval — a garbled token must not read as silent
assent.

### How correctness is evaluated

Four harnesses. Full results and what they found are in [validation](#tools-validation-and-what-was-designed-deliberately).

| Harness | Scope | Runs | Catches |
|---|---|---|---|
| `pytest` | 466 tests, fully offline | Every commit | Regressions in the deterministic core |
| `adversarial_judge.py` | 19 cases, live models | After any prompt change | A judge that flags everything, or nothing |
| `adversarial_selector.py` | 8 cases, live models | After any prompt change | A bounded stage that is safe but useless |
| `run_sweep.py` | 39 queries × 3 audit levels | After aggregator / viz / pipeline changes | Right number, wrong words around it |
---

## Request schema

`POST /analyze` and `POST /plan` take the same body. Only `query` is required.

```json
{
  "query": "How has the number of trials for this drug changed per year since 2015?",
  "drug_name": "Pembrolizumab",
  "start_year": 2015
}
```

Unknown fields are rejected (`extra="forbid"`).

### Required

| Field | Type | Validation |
|---|---|---|
| `query` | `string` | 1–1000 characters. A natural-language question about clinical trials. |

### Optional structured parameters

Thirteen fields, each mapping to a filter ClinicalTrials.gov can push down server-side.
They exist to remove ambiguity from the query, not to become a second, parallel query
language.

| Field | Type | Notes |
|---|---|---|
| `drug_name` | `string` ≤200 | Intervention or drug name → `query.intr`. Matching is fuzzy and synonym-expanded by the registry, so related agents may be included. |
| `condition` | `string` ≤200 | Condition or disease → `query.cond`. Also synonym-expanded. |
| `sponsor` | `string` ≤200 | Sponsor or collaborator organisation → `query.spons`. |
| `country` | `string` ≤100 | Country as ClinicalTrials.gov spells it (`United States`, `Korea, Republic of`). Maps to a nested location filter so site-level status is respected. |
| `phase` | `string[]` | `NA`, `EARLY_PHASE1`, `PHASE1`–`PHASE4`. Multiple values OR-ed. Must be omitted or non-empty. |
| `status` | `string[]` | 14 overall statuses (`RECRUITING`, `COMPLETED`, `TERMINATED`, …). Multiple values OR-ed. |
| `study_type` | `string` | `INTERVENTIONAL` \| `OBSERVATIONAL` \| `EXPANDED_ACCESS`. |
| `sponsor_class` | `string` | `INDUSTRY`, `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `NETWORK`, `AMBIG`, `OTHER`, `UNKNOWN`. |
| `intervention_type` | `string` | `DRUG`, `BIOLOGICAL`, `DEVICE`, `PROCEDURE`, `RADIATION`, … (11 values). |
| `start_year` | `int` | 1900–2100. Earliest trial start year, inclusive. Must not exceed `end_year`. |
| `end_year` | `int` | 1900–2100. Latest trial start year, inclusive. |
| `enrollment_min` | `int` ≥0 | Minimum enrollment. Must not exceed `enrollment_max`. |
| `enrollment_max` | `int` ≥0 | Maximum enrollment. |

The categorical enums mirror the registry's own `/studies/enums` exactly, and a test
asserts they still match the live endpoint. Bad input is rejected at the edge with a 422
rather than passed to the planner.

### Execution options

| Field | Type | Default | Notes |
|---|---|---|---|
| `include_citations` | `bool` | `true` | Emit each datum's `citations`. Disabling reduces payload size but changes no chart value. |
| `include_planning_trace` | `bool` | `true` | Include the planner's probe calls and results in `meta.planning_trace`. |

---

## Response schema

One envelope for every response. `response_type` discriminates; a frontend written against
this schema never branches on anything else to know what it received.

```jsonc
{
  "request_id": "…",
  "response_type": "visualization",   // | conversational | unsupported | no_results
  "answer": "Across 3,743 trials, the highest phase is PHASE2 at 1,033 trials.",
  "visualization": { "type": …, "title": …, "encoding": …, "data": …, "config": … },
  "meta": { … }
}
```

`visualization` is null **if and only if** `response_type` is `conversational`. This was done so that a visualization is not produced if the user simply says a "hi". 
`unsupported` and `no_results` still carry a full visualization block with empty data and
the reason in `meta.warnings`, so a frontend renders one shape always and never
special-cases an empty state into a different code path.

`answer` is templated with numeric slots filled from the aggregator. It is never written by
a model.

### `visualization`

| Field | Type | Notes |
|---|---|---|
| `type` | enum | Which renderer to use. Chosen by deterministic rules from the result shape; a model may only pick *within* the legal set, never outside it. |
| `title` | `string` | Derived from the plan, not model-written. |
| `subtitle` | `string?` | The counting semantics when they are not obvious — "each trial counted once per bucket", "median of enrollment over the trials in that bucket". Null when the default reading is correct. |
| `encoding` | `Encoding` | Which key on each datum drives which channel. |
| `data` | `Datum[]` \| `NetworkData` | Flat list for every type except `network`. |
| `config` | `VizConfig` | Rendering hints that are not field bindings. |

Eleven chart types supported:

`line` · `bar` · `grouped_bar` · `stacked_bar` · `stacked_area` · `pie` · `scatter` ·
`histogram` · `network` · `choropleth` · `kpi`

### `encoding`

```json
"encoding": {
  "x": {"field": "phases", "label": "Phase",  "type": "ordinal", "unit": null},
  "y": {"field": "value",  "label": "Trials", "type": "quantitative", "unit": "trials"},
  "series": null
}
```

Each channel carries `field`, `label`, `type`
(`quantitative` | `temporal` | `nominal` | `ordinal`) and an optional `unit`.

Read `x.field` and `y.field`. This is the one object that makes
the payload self-describing.

`series` is present only when a datum carries a second coordinate

**Networks reuse `x`/`y` rather than adding node/edge channels.** `x` binds `Node.id`, `y`
binds the `weight` carried by both nodes and edges, and `data` is a `NetworkData` object
rather than a list — that switch is what tells a renderer to draw a graph. Optional `node`
and `edge` channels on `Encoding` were considered and not taken: they would add two channels
that are null for ten of the eleven chart types, to express something the shape of `data`
already states unambiguously.

Concretely, from `examples/05-drug-network.json` — *"Which drugs frequently co-occur in
combination studies for multiple myeloma?"*:

```json
"encoding": {
  "x": {"field": "id", "label": "Entity", "type": "nominal", "unit": null},
  "y": {"field": "weight", "label": "Trials in common", "type": "quantitative", "unit": "trials"},
  "series": null
}
```

### `Datum`

```json
{
  "phases": "PHASE2",
  "value": 1033,
  "nct_ids": ["NCT00001144", "NCT04598009", "NCT03917069", "NCT00387751", "NCT07448831"],
  "nct_id_total": 1033,
  "citations": [ … ]
}
```

| Field | Notes |
|---|---|
| *dimension key* | Added dynamically and named by `encoding.x.field`. |
| `value` | Always a deterministic fold over the underlying records of `nct_ids`. Never model-authored; the invariant check enforces it. |
| `nct_ids` | Up to 5 contributing trial IDs, as a sample. |
| `nct_id_total` | How many trials actually contributed, before sampling. |
| `citations` | Evidence for **this** datum. |

### `NetworkData`

`{"nodes": [...], "edges": [...]}`.

**Node** — `id` (`kind:label`, so two entities sharing a label but drawn from different
fields stay distinct), `label`, `kind` (the field key it came from), `weight` (distinct
trials the node appears in). `weight` is the value a client filters on.

**Edge** — `source`, `target` (unordered: `(A,B)` and `(B,A)` are one edge), `weight`,
`strength`, `nct_ids`, `nct_id_total`, `citations`.

`weight` is the number of trials in which both endpoints appear, computed as the length of
the same trial list that produced the edge's citations, so the two cannot disagree.

`strength` is association strength, `2m·w / (k_source · k_target)`. It corrects for degree:
ranked by raw weight, an agent present in most regimens dominates purely by ubiquity — on
multiple myeloma the five heaviest edges all contain dexamethasone. Two caveats are carried
in the field description itself, because that is what a frontend developer actually reads:
it is **derived arithmetic with no citations behind it**, unlike every other value in the
system; and **it must not be sorted on alone**, because a pair occurring only with each
other scores maximally on a single trial. Apply `config.suggested_min_occurrences` to
`Node.weight` first.

### `Citation`

Deep citations hang off each datum, and off each edge. There is **no response-level map**.

```json
{
  "nct_id": "NCT04598009",
  "url": "https://clinicaltrials.gov/study/NCT04598009",
  "brief_title": "Binimetinib and Imatinib for Unresectable Stage III-IV KIT-Mutant Melanoma",
  "field_path": "protocolSection.designModule.phases",
  "field_value": "PHASE2",
  "excerpt": "\"phases\":[\"PHASE2\"]",
  "offset": [173, 192],
  "supports": "value"
}
```

`excerpt` is a **literal substring of the fetched API payload** at `offset`, never
generated by a model. The assembler re-asserts the substring match at those offsets before emitting, and drops the citation rather than emit an unverified one.

Offsets are into `json.dumps(record, separators=(",",":"), ensure_ascii=False)` — a
reproducible, documented basis, since per-record wire spans would need incremental parsing.

`supports` distinguishes which half of a datum an excerpt evidences. A grouped datum has two
coordinates — the bucket and the series — and one excerpt rarely states both, so they are
cited separately. `series` citations are **absent** when the record never states the leg's
term: a leg is a search expression, and the registry expands it in ways the record does not
repeat. Measured over 200 trials matching `query.intr=pembrolizumab`, 86% state the term
literally, 6% carry it only as a MeSH concept, and 8% state it nowhere quotable. Nothing
adjacent is quoted in place of the missing 8% — showing `"Immune checkpoint inhibitor"` as
evidence that a trial is a pembrolizumab trial would be a real excerpt that verifies and is
not evidence of the claim.

Two rules hold everywhere:

- **A citation must both verify at its offsets and state the value it is cited for.** The
  first without the second is the dangerous case, and offset verification cannot catch it —
  the text really is in the record, at a position supporting a different claim.
- **An unverifiable citation is dropped and counted**, never emitted with a caveat. An
  unverified excerpt looks like evidence.

### `meta`

| Field | Notes |
|---|---|
| `interpretation` | Plain-language restatement of what was computed. |
| `plan` | Echo of the committed plan. |
| `filters_applied` | The filters actually used. |
| `counting_semantics` | Set whenever the rule is not "one trial, one unit" — e.g. a multi-valued grouping field, where column sums exceed the distinct trial count. |
| `record_counts` | See below. |
| `assumptions` | Including every structured parameter that was pinned. |
| `warnings` | |
| `planning_trace` | The planner's probe calls and their results. Probe results are aggregate counts and can never become a chart value. |
| `review` | The plan reviewer's `verdict`, `concerns` and whether it `revised`. Present whenever the reviewer ran; **null means it did not run at all**, which is a different fact from an approval. |
| `api_requests` | The issued ClinicalTrials.gov URLs, verbatim. |
| `suggested_requests` | Complete, postable request bodies. Populated on `unsupported`, so a refusal becomes a redirect. |
| `generated_at`, `cache_hit`, `llm_provider`, `elapsed_ms` | |


### `record_counts` — the transparency block, which is also the invariant

```json
{"matched": 3743, "retrieved": 3743, "used": 3743, "excluded_by_reason": {}, "truncated": false}
```

The same four numbers that guard correctness are the ones reported, so they cannot drift
apart. Two invariants are **enforced in production, not merely reported**:

```
used + sum(excluded_by_reason.values()) == retrieved
retrieved == matched  or  truncated is True
```

A failure raises and returns HTTP 500 with no chart. Nothing disappears silently: every
dropped record is counted by reason.

---

## Key design decisions

### A query/parameter contradiction is refused, not resolved

"How have melanoma trials changed?" with `condition="glioblastoma"` is not a precedence
question. It is a contradiction, and honouring either side answers something the caller did
not ask.

Three obvious options existed — the parameter wins, the query wins, or warn and continue —
and all three are defensible. The service takes the fourth: **HTTP 422 naming the specific
disagreement.**

```json
{
  "error": "override_conflict",
  "detail": ["the question asks about melanoma; condition is pinned to glioblastoma"],
  "message": "The question and the structured parameters disagree: … Remove one side, or make them agree."
}
```

The position here is that either answer is a question the caller did not ask, and there is no basis for choosing between two things they stated themselves.

Detection lives in the plan reviewer.

### The page cap is at 100,000

The cap is now **100 pages / 100,000 records per leg**. This is because the constraint is **time, not data**, and the reason is the registry's own pagination. `pageSize` **clamps silently at 1,000** — asking for 2,000 returns 1,000, with nothing in the response saying so. There is no bulk endpoint and no way to widen a page, so the number of records you want is not a size question, it is a count of sequential round trips: one request per 1,000 records, each about 0.7 s.

That is what makes a large slice expensive. Fetching all 585,468 trials for "how many
started each year since 2000" is only **141 MB** — genuinely not much — but at 1,000 per page
it is **586 requests, about seven minutes**. No browser, proxy or client library waits that
long; the request would time out having shown nothing.

The cap is therefore a bound on round trips rather than on bytes.

**The cap is set per deployment, via `CHEIRON_MAX_PAGES`.** It defaults to 100 pages and is
read from the environment, because the right ceiling is a property of the machine rather than of the question:

---

## Future work

### Remove the page cap completely:
Parallelize the calls. Then payload retention.

### Entity resolution over free-text intervention names

Intervention names are sponsor-authored free text, and the registry holds 528,741 distinct
ones. Case folding is applied today and merged 58 groups on a 1,000-trial myeloma slice
(783 distinct names → 721), which are the busiest drugs in the slice. But **66 groups
(219 names) remain split**, and they are the interesting ones:

```
dexamethasone · dexamethasone (iv) · dexamethasone (oral) · dexamethasone (tablets)
bortezomib · bortezomib for injection · bortezomib injection
lenalidomide · lenalidomide (revlimid®) · lenalidomide po (25mg)
```

The reason it stops at case is the sharpest limitation in the project:
**`melphalan hydrochloride` and `melphalan flufenamide` share a stem and are different
drugs** — flufenamide is a peptide-drug conjugate, not a salt form of melphalan. Any
string-based merge that unified the dexamethasone variants would also unify those two, and
would be silently wrong in a way no warning could undo.

So the real fix is an actual ontology — RxNorm or UNII — not a better heuristic. That is a
data-source decision rather than an afternoon's work, which is why the line is currently
drawn where the evidence is unambiguous: two strings differing only in capitalisation are
the same string, and everything past that stays split and stays disclosed.
`intervention_mesh` remains available today for anyone who wants brand and generic collapsed
by the registry's own indexing.

### Extension of Query Types — Cross-trial efficacy from outcome measures

The biggest capability gap, and the reason is worth stating precisely. Posted results *are*
read — participant flow, adverse events with their own denominators, baseline demographics —
but `outcomeMeasuresModule` is deliberately not extracted.

Measured: **25 melanoma trials with results carried 157 outcome measures under 144 distinct
titles in 34 distinct units**, where even `"Percentage of participants"` and
`"Percentage of Participants"` count separately.

There is nothing to aggregate across trials without an outcome ontology and a unit
normalizer. Reducing them to a number without one would be exactly the plausible-but-wrong
output the rest of the system refuses to produce.

---

## Tools, validation, and what was designed deliberately

### Tools

Built with **Claude Code**. The architecture, the core invariant, the decision log and every measurement-driven choice were specified deliberately and recorded as they were made — `docs/decisions.md` carries each decision with what was **rejected** and why, because the rejected option is what stops a choice being silently reversed later.

### How correctness was validated

**A 39-query sweep**, chosen to force every axis: all eleven chart families, all four
metrics, all three layouts, every filter, posted results, multi-leg comparisons, the
non-chart response types, and deliberate traps ("trials in Georgia" — country or US state?).
Audited on three levels: self-consistency (14 checks, each documenting the bug that
motivated it), ground truth (`tests/ground_truth.py`, which **imports nothing from
`cheiron`** and refetches from the registry), and human judgement.


**What that found.** Across the final sweep, **35 of 39 queries reached independent
verification against the live registry with zero mismatches**, and a separate pass re-sliced
**96 citations from freshly refetched records with 0 mismatched**. Eight defects were found,
and **not one was a wrong value** — seven of the eight were *right number, wrong words
around it*: a unit, a label, an answer sentence, an encoding, a response type. Values were
never the problem, which is precisely why unit tests alone were not enough.

### What was generated and adapted

**Generated and adapted** — mechanical surface where the shape was specified and the typing
was not: Pydantic model boilerplate, the HTTP client's pagination and retry loop, test
scaffolding, the demo frontend's rendering code, and the first draft of most prose.

**Iterated under measurement** — the prompts. None of the four survived first contact
unchanged, and the changes were driven by the adversarial sets rather than by taste: the
reviewer gained two failure classes, the chart selector gained a geography rule after both
providers were found to be *actively downgrading* the canonical map question to a bar chart.

---

## Where the reasoning lives

The docs are the working record, not an afterthought — much of the evidence quoted above is
worked through in full there.

| | |
|---|---|
| `docs/decisions.md` | Every decision, with what was **rejected** and why. Read before changing anything. |
| `docs/api-findings.md` | ClinicalTrials.gov behaviour verified by curl. Items marked `CORRECTION` overrule the original plan, because they were measured. |
| `docs/corpus-facts.md` | Corpus statistics, each with the exact query that produced it. |
| `docs/readme-notes.md` | The long-form version of everything in this README, plus the disclosures that did not fit. |
| `plan.md` | The original design, superseded by `docs/decisions.md` where they disagree. |
| `examples/` | Eight captured runs with their actual JSON output, plus `verify_examples.py`, which independently reconciles three of them against the registry and imports no `cheiron` code. |

---

## Example runs

Eight captured runs live in `examples/`, loadable in the demo UI at `GET /ui`.

| | query | type |
|---|---|---|
| 01 | How has the number of pembrolizumab trials changed per year since 2015? | `line` |
| 02 | How are melanoma trials distributed across phases? | `bar` |
| 03 | Compare phases for trials involving pembrolizumab vs nivolumab | `grouped_bar` |
| 04 | Where are recruiting trials for non-small cell lung cancer running? | `choropleth` |
| 05 | Which drugs frequently co-occur in combination studies for multiple myeloma? | `network` |
| 06 | What is the median number of participants with serious adverse events in melanoma phase 3 trials, by sponsor class? | `bar` |
| 07 | Is there a relationship between enrollment and the number of sites in melanoma trials? | `scatter` |
| 08 | Which drug works better for melanoma, pembrolizumab or nivolumab? | `unsupported` |
