# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Implementation follows the build order in `plan.md` §6.

**Done:** ①–⑬ — schemas, normalizer, aggregator + invariants, API client + cache, viz rules +
spec assembler, planner + repair loop, probe tools, network graph + co-occurrence, deep
citations with offset verification, judge + router + chart selector, the pipeline and the
five HTTP endpoints, and the captured example runs.

**Remaining:** ⑭ the README itself, and ⑮ a self-review against the assignment's rubric.

**Added after ⑬**, each at the user's request and each recorded in `docs/decisions.md`:

| | |
|---|---|
| Posted results (`resultsSection`) | 9 fields — adverse events and deaths with separate denominators, participant flow, baseline age and sex |
| Demo frontend (`GET /ui`) | The assignment's stated bonus. Vendored deps, no build step |
| Datum-scoped citations | The response-level `citations` map was **removed**; see invariant 5 |
| Series citations | `supports:"series"` evidences which leg a trial fell in |
| Per-endpoint edge citations | An edge cites both its drugs, so the shared arm label is visible |
| Judge failure class 6 | `UNQUANTIFIED SUPERLATIVE` — see invariant 6 |
| `meta.review` | The reviewer's verdict is now recorded on every reviewed request |
| Free-text name casing | Sponsor-authored entity fields group case-insensitively under the commonest spelling; route/salt/brand variants deliberately stay split |
| Optional parameters | 13 structured fields, applied deterministically; contradiction with the question is judge class 7 → 422. `max_records` removed |

All **7** examples are captured; `verify_examples.py` independently reconciles three of them
(phases, countries, posted-results medians) with no mismatches.

### Read these first

- `assignment-specs.md` — the assignment brief (verbatim). Source of truth for *requirements*.
- `plan.md` — the original design. Source of truth for *architecture*, **except** where
  `docs/decisions.md` supersedes it.
- `docs/decisions.md` — **read this before changing anything.** Every decision the user made
  explicitly, every divergence from `plan.md`, and the traps that produced correct-looking
  wrong output. It records what was *rejected* as well as chosen, so a decision does not get
  silently reversed.
- `docs/api-findings.md` — what the API actually does, verified by curl. Items marked
  **CORRECTION** contradict `plan.md`; the findings win, because they were measured.
- `docs/corpus-facts.md` — corpus statistics with the exact query that produced each.
- `docs/readme-notes.md` — 23 disclosures the README must carry, each with the problem, why
  silence is unacceptable, and what to write. This is the raw material for ⑭.

### Commands

```bash
uv sync --all-extras                      # install
.venv/bin/pytest -q                       # 459 tests, offline, no API key needed
.venv/bin/ruff check src tests examples

.venv/bin/python -m uvicorn cheiron.api.app:app --port 8000   # serve
#   /ui     the demo frontend        /docs   Swagger
#   /examples  lists the captured runs the UI loads
.venv/bin/python examples/run_examples.py                     # capture examples
.venv/bin/python examples/verify_examples.py                  # independent recount
```

`run_examples.py` caches the **registry** responses under `examples/cache/`, but the LLM
stages run live every time, so a recapture costs model calls and the plan can differ between
runs — `top_n` in particular is planner-chosen (see invariant 6).

The test suite runs **entirely offline**: the API client against a mock transport, the LLM
stages against fake clients, the deterministic core against 11 real records in
`tests/fixtures/raw_studies/` plus one results-bearing record in `results_studies/`.

**Two live evaluations are deliberately *not* pytest tests**, because they call a real model
and cost money. `pytest` will not collect them (they are not named `test_*`):

```bash
.venv/bin/python tests/adversarial_judge.py    [anthropic|openai]   # 15 cases, expect 15/15
.venv/bin/python tests/adversarial_selector.py [anthropic|openai]   # 8 cases, expect 8/8
```

Run both after touching a prompt. Each has already caught a real bug that unit tests could
not — see `docs/readme-notes.md` §13, §14 and §21. Their control cases matter as much as
their failure cases: a reviewer that flags everything is as useless as one that flags
nothing, so half of each set is plans that must be left alone.

**The frontend has no automated test.** There is no browser extension in this environment;
it was verified by driving headless Chrome over the DevTools protocol
(`--headless=new --remote-debugging-port=…`, then raw WebSocket JSON) to load each captured
example, assert what rendered, and read the console. That is how the citation bug in
invariant 5 was found. If you change `api/static/`, do something equivalent — `node --check`
proves only syntax, and both frontend bugs found so far were invisible to it.

`.env` needs `LLM_PROVIDER` plus that provider's key; see `.env.example`. Model IDs there
have been verified against both live APIs — **do not assume a model ID exists**, one set in
the original config did not.

**First Anthropic query after a schema change takes ~80s** and may return
`Grammar compilation timed out`; it is retried automatically and warm calls are ~5s. See
`readme-notes.md` §15 before concluding something is broken.

### Layout

```
src/cheiron/
  schemas/     fields.py  ← the field registry; the prompt, validator, viz rules and
                            warnings all derive from it. Add a field here, not in four places.
               plan.py (Plan + validator), request.py, response.py
  ctgov/       normalizer.py (flatten + results extraction), compiler.py (Plan → Essie),
               client.py (pagination, retry, rate limit), cache.py, retrieval.py
  agg/         aggregator.py  ← the heart. Buckets, folds, co-occurrence, invariants.
  viz/         rules.py (chart legality), assembler.py (envelope), citations.py (offsets)
  llm/         client.py (both providers + schema translation), planner.py, probes.py,
               router.py, judge.py, selector.py
  pipeline.py  stages in sequence; decides which of four response types a request gets
  api/app.py   five endpoints; /capabilities and /schema generate from the models
  api/static/  the demo frontend (GET /ui): index.html, app.js, styles.css, vendor/

examples/      captured runs + run_examples.py + verify_examples.py (imports no cheiron code)
tests/         unit tests, plus adversarial_*.py live evaluations
docs/          decisions, api findings, corpus facts, readme notes
```

### Invariants that must not be broken

1. **No LLM-visible number reaches the output.** Values are folded from records by the
   aggregator; probe results go only to `meta.planning_trace`.
2. **`used + sum(excluded_by_reason) == retrieved`**, checked in production, raising
   `InvariantError` → HTTP 500 with no chart. Never downgrade this to a warning.
3. **Invariants are checked before presentation trimming**, not after — trimming
   deliberately drops buckets.
4. **A citation must both verify at its offsets and state the value it is cited for.** The
   first without the second is the dangerous case: it is real text from the wrong place.
   Offset verification cannot catch it — the text really is in the record, at a position
   supporting a different claim.
5. **Citations live on the datum** (`Datum.citations`, `Edge.citations`), never in a
   response-level map keyed by NCT ID. A trial belongs to several datums whenever the
   dimension is multi-valued, so a per-trial map can hold only one of its excerpts and
   every other datum silently reads a citation for a different bucket. This was a real
   bug — clicking Canada showed `"country":"United States"`, 32/55 lookups wrong — and it
   verified perfectly at its offsets the whole time. Do not reintroduce the map.
6. **The judge's failure classes are a closed list, and the plan must express the
   question's quantifier.** Class 6 exists because "which drugs **frequently** co-occur"
   with `top_n: null` returned all 5,215 pairs — correct numbers, wrong question. `top_n`
   is planner-chosen with no default *on purpose*: a blanket default is the hard node cap
   already rejected under Graph size, and would trim a query that legitimately wants the
   whole network. The restriction must come from the question, which is why it lives in the
   reviewer. Keep the list closed — "do not invent other grounds" is what stops the judge
   manufacturing concerns.
7. **Structured parameters are told to the planner, applied deterministically, and
   adjudicated by the judge — three different stages, on purpose.** The planner is told
   them so its probes run on the slice that will actually be fetched. `apply_overrides`
   pins them afterwards, because a prompt-only design produced responses listing a filter
   in `filters_applied` that had not been used. The **judge** owns contradiction (class 7,
   the one fatal verdict → 422), because it is the only stage that reads the question and
   the plan together. A brief attempt to detect contradictions by withholding the
   parameters from the planner is recorded in `decisions.md` as rejected: it worked, but
   left the planner calibrating to a slice nobody asked about.
8. **When you add anything that reads the raw record, check the projection.** The compiler
   fetches the narrowest `fields=` set that answers the plan, so a new reader silently sees
   nothing. This has bitten three times: `combination_groups` (empty graph, no error),
   struct sub-fields (`StartDate` without `.type`), and series citations (56% instead of
   86%). None of them errored.

## README: cite the corpus evidence

When writing the README, the "Limitations" section **must** carry the figures in
`docs/corpus-facts.md` *together with the query that produced each one* — 51,497 distinct lead
sponsors, ~63% of studies with `NA` or absent phase, 528,741 distinct intervention names,
enrollment spanning 0 to 188,814,085 with a mean of 5,510. Quoting the command alongside the
number is what distinguishes having examined the corpus from having guessed about it, and each
figure is the justification for a specific design decision (no sponsor deduplication; NA/absent
phase as first-class buckets; MeSH terms for network nodes; median rather than mean). Do not
paraphrase these into vague claims like "sponsor names are messy."

## Design decisions: ask, don't assume

`plan.md` is a plan, not a finished spec — it will not have an answer for every decision you hit
while building. When you encounter a design decision that isn't already settled explicitly in
`assignment-specs.md` or `plan.md` (language/framework choice, a library, an API field mapping, an
edge case the plan doesn't cover, a schema detail, error-handling behavior, etc.), **do not guess
or pick a reasonable-looking default. Stop and ask the user.** This applies even when the choice
seems small or obvious to you — surface it and let the user decide. Only proceed unprompted on
something already explicitly decided in one of the two planning docs.

**Check `docs/decisions.md` first.** Many such decisions have already been put to the user and
answered; that file records the answer *and the rejected alternatives*. Re-asking a settled
question wastes the user's time, and silently reversing one is worse. Add a row there whenever a
new decision is made, so the next agent inherits it.

**Measure before asking.** Most of these questions had a factual answer available from the live
API or the corpus, and the measurement repeatedly *changed* the recommendation: co-listing versus
arm-sharing (28% false edges), prose versus JSON citation coverage, network payload size, country
name matching (a plain join loses "United States"), MeSH synonym coverage. Bring numbers to the
question rather than options alone.

**And check the measurement itself.** A guessed piece name (`InterventionBrowseLeafName`) returned
an empty module rather than an error, which produced the confident and wrong conclusion "the
registry has no MeSH data" — nearly costing a whole feature. Before reporting that something is
absent, verify against an unprojected record.

## Data source

The system integrates with ClinicalTrials.gov:

- Site: https://clinicaltrials.gov/
- Data API docs: https://clinicaltrials.gov/data-api/api

This is the only data source referenced anywhere in `assignment-specs.md` / `plan.md` — treat it as
the authoritative (and currently only sanctioned) external API for this project. Confirm with the
user before introducing any other external data source or API.

## What is being built

A backend service ("ClinicalTrials.gov Query-to-Visualization Agent") that turns a natural-language
question about clinical trials into a structured visualization specification, backed by live data
from the ClinicalTrials.gov Data API. No frontend is *required* — the deliverable is a documented
JSON schema a frontend could render against — but one exists at `GET /ui` as the assignment's
stated bonus, and it is deliberately a plain client of that schema. It reads `encoding` to find
the dimension key rather than hardcoding one, so it doubles as evidence the schema is
implementable without guessing.

## Core invariant (drives every design decision)

> The LLM sees aggregate facts about the data and never a trial record. No LLM-visible number ever
> reaches the output.

Chart values are produced by deterministic code folding over lists of source records. LLMs choose
*what to compute* and *how to display it*, never *what the value is*. When the system fails, it
should fail loudly (validation error, invariant failure, explicit `unsupported` response), not
quietly (a plausible chart built on a bad aggregation). Keep this invariant intact through any
change — it's the thing the whole architecture (plan.md) is organized to protect.

## Pipeline shape

```
POST /analyze
  → request validator (det)
  → ① Router «LLM»: in-domain? else conversational reply, 0 API calls
  → Plan loop (max 3 revisions):
        ② Planner «LLM» ↔ probe tools (det, counts only, never rows) ↔ ct.gov
        → plan validator (det)
        → ③ Judge «LLM»: advisory, ≤1 re-plan; verdict in `meta.review`
  → query compiler (det): Plan → per-leg API requests
  → API client (det): paginate, retry, cache
  → normalizer (det): flatten API records to scalars/flat-lists, log exclusions
  → aggregator (det): bucket → [(nct_id, value)]; citations are born here
  → invariant check (det): raises, never warns
  → viz rules (det): result shape → legal chart set
  → ④ Chart selector «LLM»: picks within legal set, sees shape not values
  → spec assembler (det): builds response envelope + verified citations
```

Only four LLM touchpoints (Router, Planner, Judge, Chart selector). Everything from the query
compiler through the spec assembler is deterministic and meant to be unit-testable with fixtures —
no API key, no network, no LLM required for that core.

See `plan.md` for: the full response envelope shape, the `Plan` schema and its validator rules,
the normalizer's target field table and known ClinicalTrials.gov data quirks (partial dates, `NA`
phases, multi-valued fields, ESTIMATED vs ACTUAL, etc.), the viz-legality table, citation
verification (excerpts must be a literal, offset-verified substring of the fetched payload — never
LLM-generated), and the validation strategy (fixture tests, golden aggregator snapshots, invariant
assertions in production, judge adversarial set, planner regression set).

## Working conventions from the plan

- **Comparisons are legs, not sub-agents.** A "compare X vs Y" query becomes two `Leg`s in one
  `Plan`, merged into a series dimension — not a separate planning path.
- **Rules decide chart *legality*; the LLM decides chart *preference*** within whatever the rules
  allow. The chart-selector LLM cannot produce an illegal chart, only downgrade to the rule's
  default on failure.
- **Field metadata drives warnings/prompts automatically**, not per-query hand-coding (e.g. a
  field's `multi` flag automatically produces the "totals exceed distinct trial count" warning;
  the planner's legal-field list is generated from the flattener's output keys, not maintained by
  hand).
- **Three things must never disappear silently:** dropped/excluded records (counted into
  `excluded_by_reason`), citations that fail offset verification (dropped, not emitted), and
  values the renderer cannot draw (the choropleth lists countries with no polygon rather than
  leaving them blank, which would read as "no trials here").
- Build order and cut-order priorities if time-constrained are specified in `plan.md` §6 — the
  network graph visualization is explicitly the highest-value/last-to-cut item.
