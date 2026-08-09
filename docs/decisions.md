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
| Scatter axis scale | Derived from the same `skewed` flag into `config.x_scale`/`y_scale` | Frontend heuristic; leave linear and document | Same rule as `bin_scale`, so the same evidence decides both. Measured: median enrolment 44 against a max of 2,953,748, 99.6% of points below 1% of the max |
| Scatter `counting_semantics` | Special-cased for point layout | The generic metric wording | A point layout folds nothing, but `metric` must be set to validate, so the subtitle claimed a median over 3,625 buckets of one trial each |

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
| Free-text name casing | **Fold case, display the commonest spelling** for the 4 sponsor-authored entity fields | Lower-case the labels; stem/prefix merging; MeSH-only nodes; leave as-is | 783 → 721 names on 1,000 myeloma trials, 58 groups, all the busiest drugs. Stops at case: `melphalan hydrochloride` and `melphalan flufenamide` share a stem and are different drugs, so any looser rule is silently wrong |
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
| Judge failure classes | **Six**, closed list — added UNQUANTIFIED SUPERLATIVE | Five; open-ended "assess quality" | A question saying *frequently*/*most common* asks for a ranked subset; with `top_n` null the plan answers "which values occur" instead. Caught live: the myeloma network returned all 5,215 edges. 12/12 on both providers after |
| Judge verdict in `meta` | **Always recorded** (`meta.review`) | Only unactioned concerns produce a warning | An approval that leaves no trace is indistinguishable from a reviewer that never ran |
| Selector failure | Fall back to `legal[0]` | Error | The model can only ever downgrade; membership is enforced, not trusted |

The review prompt serializes the plan with `exclude_none=True`, **not** `exclude_defaults`.
The latter drops `metric: "count"` because count is the default, leaving the judge asked to
check a field it cannot see — which reproducibly broke METRIC MISMATCH on Anthropic. It also
receives `request.overrides()`, so a caller-pinned filter is not read as a field error.
See `readme-notes.md` §13.

## Pipeline and HTTP

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| `unsupported` detection | Three-way router: question / conversational / unsupported, with reason + suggestions | Planner failure; a fifth touchpoint | User's call. Costs one cheap classification and zero retrieval; the reason names the obstruction and the suggestions are postable request bodies |
| Optional parameters | **Applied deterministically** to every leg after planning; 13 fields | Prompt-injected as "constraints"; frontend-only | They were accepted and ignored — measured: request said glioblastoma, service issued `query.cond=melanoma`, `filters_applied` listed both |
| Query/parameter contradiction | **422**, naming the disagreement | Parameter wins; query wins; warn and continue | User's call. Either answer is a question the caller did not ask. Detected by the judge, so best-effort rather than guaranteed |
| Parameters in the planner prompt | **Passed** | Withheld, so the plan is an independent reading | Withholding was tried and reverted: it made contradictions detectable, but the planner's probes then ran on the unfiltered corpus, calibrating granularity/bins/top_n to a population nobody asked about. Contradiction moved to the judge instead |
| Contradiction detection | **Judge class 7**, the one fatal verdict → 422 | Deterministic comparison in `apply_overrides`; planner-side | Only the judge reads the question and the plan together. 15/15 on the adversarial set, both providers |
| `max_records` | **Removed** | Wire it to the page cap; document it as inert | User's call. Documented as an upper bound, never read by the client — a knob that looks effective and is not is worse than none |
| Response shape | `unsupported` and `no_results` carry an empty visualization block | Null visualization | One render path for the frontend; only `conversational` is null |
| Endpoints | All five from `plan.md` §1 | `/analyze` only | User's call. `/plan` shows the agent layer with no retrieval; `/capabilities` and `/schema` generate from the models so they cannot drift |
| `InvariantError` at HTTP | 500 with no chart | Chart plus a caveat | A reconciliation failure means the chart is wrong; failing loudly has to hold at the boundary too |
| `.env` loading | In the app's lifespan | Left to the caller | `uvicorn` reads nothing; a `.env` beside the code would be silently ignored and read as a broken build |
| Cold grammar compile | Retried in the client | Left to the planner's repair loop | Anthropic compiles and caches a grammar per schema: ~80s cold (can 400 with "Grammar compilation timed out") vs ~5s warm. Left to propagate it burned three planner revisions on an infrastructure hiccup, starving genuinely bad plans of repairs |

## Posted results

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Read `resultsSection` | **Yes** — flow, adverse events, baseline demographics | Registration data only (`plan.md` "v2") | User's call. The data exists (789/3,743 melanoma trials); calling it a registry limitation was inaccurate |
| Outcome measures | **Not** extracted | Extract and aggregate | 25 trials → 157 measures, 144 distinct titles, 34 units. Not comparable across trials without an ontology |
| Adverse-event totals | Summed across arm groups | Use a total row | `eventGroups` has none, and each participant is in exactly one arm |
| Baseline totals | The registry's own `Total` column | Sum or average the arms | Summing is right for counts and wrong for a mean age; the Total column was present on every trial sampled |
| Death denominator | Separate `deaths_at_risk` | Reuse `serious_ae_at_risk` | The registry lets them differ; sharing one computes a rate against the wrong population |
| Missing results | `None` | `0` | "Reported no deaths" and "never reported" are different populations |
| Results fixture | Its own directory | Add to `raw_studies/` | The eleven there back hand-counted golden values that a twelfth silently shifted |

## Citations

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Excerpt content | Prose span when the title states the value, else JSON span at the field | Prose only; JSON only | Prose matches the assignment's example but is unavailable for sponsors (1%) and countries (0%) |
| Offset basis | `json.dumps(record, separators=(",",":"), ensure_ascii=False)` | Retain raw wire bytes | Reproducible and documented; per-record wire spans need incremental parsing |
| Unverifiable citation | **Dropped**, and counted | Emit with a caveat | An unverified excerpt looks like evidence |
| Absent values | No citation; counted separately from verification failures | Fabricate context | `NOT_REPORTED` is our label for an absent key — nothing to quote |
| Where citations live | **On each datum** (`Datum.citations`, `Edge.citations`) | Response-level map keyed by NCT ID; both; map reduced to a trial index | A per-trial map holds one excerpt per trial, but on a multi-valued dimension a trial belongs to several datums — measured 32/55 wrong lookups on the geographic example. Also what the assignment's example shows |
| Series (leg) evidence | Cited separately, `supports:"series"`, omitted when the record does not state the term | Bucket only; quote an adjacent term | A leg is a *search expression*: 86% of `query.intr=pembrolizumab` matches state it literally, 6% only as a MeSH concept, 8% nowhere. Quoting `"Immune checkpoint inhibitor"` as evidence of pembrolizumab is the failure this system exists to prevent |
| Edge evidence | **One citation per endpoint**, each on its own intervention entry | One citation quoting the composite; splitting phases too | No single span shows a pairing — the smallest containing both drugs is usually the whole `interventions` array. Two entries make the shared `armGroupLabels` visible side by side. Measured 100% of edge contributions locatable; 0 of 13,780 name the wrong drug |
| Deterministic `top_n` default for networks | **Not added** — the judge enforces it from the question instead | Default `top_n` when layout is cooccurrence | A blanket default is the "hard node cap" already rejected under Graph size. The restriction must come from the question asking for a frequent subset, not from policy — otherwise a genuine "which drugs co-occur" query is silently trimmed |
| Citation payload cap | **None** — every datum carries its evidence | Cap ~100 cited datums; spread a global budget; scale per-datum count | User's call. On a complete 5,215-edge graph citations are 85% of an 8 MB response (554 KB gzipped). Completeness preferred over payload; `suggested_min_occurrences` remains the client-side lever |
| Synonym source | ClinicalTrials.gov's own `derivedSection.interventionBrowseModule.meshes[]` | An external drug ontology; a hand-written synonym list | Already in the response, carries a MeSH id, and the `derivedSection` path tells the reader it is the registry's indexer rather than the sponsor. No outside data source introduced |

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
8. **A per-trial citation map cannot serve a multi-valued dimension.** Citations were
   keyed by NCT ID with `if nct_id in citations: continue`, so the first bucket to claim a
   trial won and every later bucket read a citation stating a different bucket's value.
   Clicking Canada on the map showed `"country":"United States"` as its evidence. Each
   excerpt verified perfectly at its offsets — the offsets were never the problem. Found
   only by building the frontend and clicking. Measured 32/55 wrong on the geographic
   example, 0 on every single-valued one. Fixed by moving citations onto the datum.
9. **A narrow projection starves the citation locator.** Series citations worked, and
   covered 56% of contributions instead of 86%, because the compiler projected
   `NCTId,BriefTitle,Phase` — the drug name was never fetched, so the title was the only
   quotable place. Fixed with `SERIES_EVIDENCE` in `compiler.projection`, guarded by a
   drift test. Third instance of this shape after `combination_groups` and struct
   sub-fields: **when adding anything that reads the raw record, check the projection.**
10. **Guessing piece names silently returns an empty module, not an error.**
   `InterventionBrowseLeafName` produced `interventionBrowseModule: {}` and the conclusion
   "no MeSH data exists" — which was wrong, and would have cost a whole feature. The real
   shape is `meshes[]`/`ancestors[]`, reachable as `InterventionMeshTerm`. Same trap as
   `api-findings.md` CORRECTION 2; check an unprojected record before concluding a field
   is absent.
11. **A filter can compile to nothing and be reported as applied.** `site_status` was only
    ever emitted inside the `country` branch, so `site_status` alone produced no clause at
    all while `meta.filters_applied` still listed it. The geographic example silently
    answered a question about 7,744 trials instead of 1,295. Invisible until a recapture
    happened to plan `site_status` where earlier runs planned trial-level `status`.
12. **Field notes were emitted for `group_by` only, never `metric_field`.** So the caveat on
   the field actually being charted disappeared. Found by capturing the adverse-events
   example: it published a median of 184.5 participants with serious adverse events and
   dropped the registry note saying to compare that against `serious_ae_at_risk` rather
   than enrolment. The chart was right and read wrong. Fixed in `aggregator._warnings`,
   which now emits notes for `metric_field` and `distinct_of` too.
