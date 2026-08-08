# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This repository currently contains only design documents — no source code, no build tooling, no
tests exist yet. Implementation has not started.

- `assignment-specs.md` — the take-home assignment brief (verbatim). This is the spec being
  satisfied; treat it as the source of truth for *requirements*.
- `plan.md` — the design plan for the system. This is the source of truth for *architecture and
  implementation approach*. Read it in full before writing code; the summary below is not a
  substitute.
- `docs/api-findings.md` — what the ClinicalTrials.gov API actually does, verified by curl. Two
  items marked **CORRECTION** contradict `plan.md`; the findings win, because they were measured.
- `docs/corpus-facts.md` — corpus-wide statistics, each with the exact query that produced it and
  raw responses saved under `docs/corpus-evidence/`.

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
