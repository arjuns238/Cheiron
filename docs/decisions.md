# Decision log

Decisions the user made explicitly, or that diverge from `plan.md`. Each records what was
chosen, **why**, and what was rejected — the rejected option matters most, because without
it the next person re-litigates the same question or quietly reverses the answer.

`plan.md` is the original design. Where this file contradicts it, this file is current and
says why. Measurements live in `api-findings.md` and `corpus-facts.md`; user-facing
disclosures in `readme-notes.md`.

---

## Stack and process

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Language / framework | Python 3.11, FastAPI, Pydantic v2 | — | Already implied by `plan.md` §1 (`/schema` generated from Pydantic, FastAPI `/docs`) |
| Package manager | `uv` | poetry, pip | Already installed; lockfile reproducibility |
| LLM providers | **Both** Anthropic and OpenAI | Single provider | User asked for both; small model for router/chart, large for planner/judge |
| No-LLM fallback | **None** — API key required | Heuristic planner | User's call. `plan.md`'s README outline mentioned one; that claim must be dropped |
| Cadence | Milestone-by-milestone per `plan.md` §6, pausing at each | — | User's call |

## Models

`gpt-5.6-sol` / `gpt-5.6-terra` were configured but **do not exist** — verified against the
live model list. Corrected to the below; re-verify before assuming any model ID is real.

| Tier | Anthropic | OpenAI |
|---|---|---|
| large (planner, judge) | `claude-opus-5` | `gpt-5.4` |
| small (router, chart selector) | `claude-haiku-4-5` | `gpt-5.4-mini` |

Small tier is justified: both small-tier calls pick one member of a closed set that
deterministic code has already constrained, so a weaker model can produce a *suboptimal*
answer but never an illegal one.

## Aggregation semantics

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Missing `group_by` value | Exclude, count as `missing_<field>` | "Not Reported" bucket per field | Keeps invariant 4 exact; matches `plan.md`'s own `missing_start_date` example. `phases` is the exception — absence there is a recorded fact |
| Overlapping legs | Count in **both**, detect and warn | Assign to first leg | Each leg is a population, not a partition; a combination trial genuinely involves both drugs. First-leg assignment silently under-reports and depends on leg order |
| Histogram + scatter | Build both | Defer | Named in the assignment's viz list. Needed `bins`/`bin_scale` and `Layout.POINT` |
| Bin scale | Derived from `FieldSpec.skewed` | Model choice everywhere | Enrollment spans 0–1.1M; linear bins put nearly everything in one bar |

## Retrieval

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Page cap | 20 pages / 20,000 records | 5, 50, unlimited | Covers most queries whole; ~20s worst case cold |
| Caching | **Off** for live queries; on for demo recording and tests | Always-on, 24h TTL | User's call. Live answers should reflect the registry now; examples must reproduce |
| Filter pushdown | **Everything** pushed down | `plan.md`'s pushdown/local split | All six "local" filters work server-side — see `api-findings.md` CORRECTION 3. Fewer records fetched, far less truncation |
| Rate limiting | Concurrency 3 + 0.15s spacing | Retry only | Added after a live 429; retry alone survives it but does not avoid it |

## LLM layer

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Structured output | Native per provider, with a repair loop over validator errors | Prompt-guided JSON | Generation-time guarantee where available |
| Optionality idiom | Per provider: nullable-required (OpenAI) / omittable (Anthropic) | One schema for both | The providers want **opposite** idioms; see `readme-notes.md` §1 |
| Anthropic schema fit | Withhold 7 fields (`NARROW_SCHEMA_FIELDS`) | Prompt-guided JSON for Anthropic; restructure `Filters` | User's call. Anthropic caps optional params at 24; `Plan` has 31 |
| Exhausted repair loop | Ship best-scoring attempt, flag `contested` | Fail closed | `plan.md` §3; a nearly-right chart with a caveat beats no answer |
| Probe budget | 4 calls, enforced in code | Prompt-only limit | A model that ignores the prompt should be stopped, not asked |

## Network graph

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Drug↔drug edges | Arm-scoped for `intervention_names`; trial-level for others, each labelled | One rule for all | User's call. Co-listing ≠ combination: 217 of 500 melanoma trials co-list ≥2 agents but only 157 share an arm |
| Agent types | `DRUG` **+ `BIOLOGICAL`** | `DRUG` only (per `plan.md`) | The split is regulatory, not pharmacological; pembrolizumab is `DRUG` 405× and `BIOLOGICAL` 94× |
| Placebos | Excluded by name heuristic | Arm membership alone | Double-dummy designs put placebos in the *active* arm, typed `DRUG` |
| Graph size | **Return complete**; advise via `suggested_min_occurrences` | Server-side threshold; hard node cap | User's call. Thresholding is presentation, and VOSviewer-style interactive filtering needs the whole network. 2.4 MB → 288 KB gzipped (measured 8.9×) |
| Edge ranking | `weight` (counted) + `strength` (derived, labelled) | Replace weight with strength | Only `weight` has citations behind it |

## LLM touchpoints (judge, router, selector)

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Router failure | Fail **open** to in-domain | Fail closed | A wrong refusal looks broken; a wrongly-analysed greeting merely returns nothing |
| Judge failure | Fail toward **approval** | Fail toward concern | Advisory, so an outage should cost nothing; failing toward concern spends a re-plan on no evidence |
| Judge authority | One re-plan, then commit regardless | Veto; unlimited revisions | A reviewer that can veto is a second planner with no repair loop; unlimited revisions turn one disagreement into a loop |
| Malformed verdict | Treated as a concern | Treated as approval | A garbled token must not read as silent approval |
| `concern` with no concerns | **Not** a concern | Block anyway | A re-plan with no feedback has nothing to act on |
| Selector failure | Fall back to `legal[0]` | Error | The model can only ever downgrade; membership is enforced, not trusted |

The review prompt serializes the plan with `exclude_none=True`, **not** `exclude_defaults`.
The latter drops `metric: "count"` because count is the default, leaving the judge asked to
check a field it cannot see — which reproducibly broke METRIC MISMATCH on Anthropic. It also
receives `request.overrides()`, so a caller-pinned filter is not read as a field error.
See `readme-notes.md` §13.

## Citations

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Excerpt content | Prose span when the title states the value, else JSON span at the field | Prose only; JSON only | Prose matches the assignment's example but is unavailable for sponsors (1%) and countries (0%) |
| Offset basis | `json.dumps(record, separators=(",",":"), ensure_ascii=False)` | Retain raw wire bytes | Reproducible and documented; per-record wire spans need incremental parsing |
| Unverifiable citation | **Dropped**, and counted | Emit with a caveat | An unverified excerpt looks like evidence |
| Absent values | No citation; counted separately from verification failures | Fabricate context | `NOT_REPORTED` is our label for an absent key — nothing to quote |

---

## Traps that cost real time

Recorded because each one produced output that looked correct.

1. **A resolving field path can point at the wrong value.** Deduplication means the third
   distinct country is not `locations[2]`. The excerpt verifies — offsets are internally
   consistent — so verification alone does not catch it. Check that the span states the
   value.
2. **The projection must include *derived* sources.** `combination_groups` needs
   `InterventionType` and `InterventionArmGroupLabel`; without them every trial yields no
   pairs and the graph returns **empty with no error**, looking like a slice with no
   combinations.
3. **Composite labels are not literal substrings.** `PHASE1|PHASE2` is ours; the registry
   stores `["PHASE1","PHASE2"]`.
4. **Association strength alone surfaces noise.** Pairs occurring only with each other
   score maximally on one trial. Threshold first, normalize second.
5. **`/stats/field/values` cannot be filtered** (`api-findings.md` CORRECTION 1), so
   `plan.md`'s one-call stats route does not exist.
6. **Anthropic's schema limits are undocumented until you hit them** — 16 unions, 24
   optional params, no `maxItems`, no `$ref` with siblings.
7. **The registry returns 500s and 429s.** A full outage lasted ~20 seconds mid-build.
