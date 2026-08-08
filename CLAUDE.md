# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Implementation follows the build order in `plan.md` §6.

**Done:** ①–⑬ — schemas, normalizer, aggregator + invariants, API client + cache, viz rules +
spec assembler, planner + repair loop, probe tools, network graph + co-occurrence, deep
citations with offset verification, judge + router + chart selector, the pipeline and the
five HTTP endpoints, and the captured example runs. Posted-results support (`resultsSection`)
was added after ⑬ at the user's request.

**Remaining:** ⑭ the README itself, and ⑮ a self-review against the assignment's rubric.

All **7** examples are captured, and `verify_examples.py` independently reconciles three of
them (phases, countries, and the posted-results medians) with no mismatches.

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
- `docs/readme-notes.md` — 17 disclosures the README must carry, each with the problem, why
  silence is unacceptable, and what to write. This is the raw material for ⑭.

### Commands

```bash
uv sync --all-extras                      # install
.venv/bin/pytest -q                       # 444 tests, offline, no API key needed
.venv/bin/ruff check src tests examples

.venv/bin/python -m uvicorn cheiron.api.app:app --port 8000   # serve; /docs for Swagger
.venv/bin/python examples/run_examples.py                     # capture examples (cached)
.venv/bin/python examples/verify_examples.py                  # independent recount
```

The test suite runs **entirely offline**: the API client against a mock transport, the LLM
stages against fake clients, the deterministic core against 11 real records in
`tests/fixtures/raw_studies/` plus one results-bearing record in `results_studies/`.

**Two live evaluations are deliberately *not* pytest tests**, because they call a real model
and cost money. `pytest` will not collect them (they are not named `test_*`):

```bash
.venv/bin/python tests/adversarial_judge.py    [anthropic|openai]   # 8 cases, expect 8/8
.venv/bin/python tests/adversarial_selector.py [anthropic|openai]   # 8 cases, expect 8/8
```

Run both after touching a prompt. Each has already caught a real bug that unit tests could
not — see `docs/readme-notes.md` §13 and §14.

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

## README: cite the corpus evidence

When writing the README, the "Limitations" section **must** carry the figures in
`docs/corpus-facts.md` *together with the query that produced each one* — 51,497 distinct lead
sponsors, ~63% of studies with `NA` or absent phase, 528,741 distinct intervention names,
enrollment spanning 0 to 188,814,085 with a mean of 5,510. Quoting the command alongside the
number is what distinguishes having examined the corpus from having guessed about it, and each
figure is the justification for a specific design decision (no sponsor deduplication; NA/absent
phase as first-class buckets; MeSH terms for network nodes; median rather than mean). Do not
paraphrase these into vague claims like "sponsor names are messy."

There is no README, package manifest, or language chosen yet in the repo. If you scaffold the
project, follow the build order in `plan.md` §6 and update this file with real commands (install,
run, test, lint) once they exist — do not invent commands here.

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

**Measure before asking.** Several of these questions had a factual answer available from the
live API or the corpus, and the measurement changed the recommendation — co-listing versus
arm-sharing, prose versus JSON citation coverage, network payload size. Bring numbers to the
question rather than options alone.

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
from the ClinicalTrials.gov Data API. No frontend is required; the output is a documented JSON
schema a frontend could render against.

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
        → ③ Judge «LLM»: advisory, triggers ≤1 re-plan
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
- **Two things must never disappear silently:** dropped/excluded records (counted into
  `excluded_by_reason`) and citations that fail offset verification (dropped, not emitted).
- Build order and cut-order priorities if time-constrained are specified in `plan.md` §6 — the
  network graph visualization is explicitly the highest-value/last-to-cut item.
