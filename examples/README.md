# Example runs

Captured responses, each the exact JSON `POST /analyze` returned — not hand-written, not
trimmed. Only `request_id` and `generated_at` are normalised, so re-capturing shows real
changes rather than churn.

| File | Query | Type | Chart | Trials | Citations |
|---|---|---|---|---|---|
| `01-time-trend.json` | pembrolizumab trials per year since 2015 | visualization | line | 2,857 | 63 |
| `02-distribution.json` | melanoma trials across phases | visualization | bar | 3,743 | 40 |
| `03-comparison.json` | pembrolizumab vs nivolumab by phase | visualization | grouped_bar | 4,938 | 149 |
| `04-geographic.json` | where recruiting NSCLC trials run | visualization | choropleth | 7,744 | 55 |
| `05-drug-network.json` | drugs co-occurring in myeloma combinations | visualization | network | 1,391 | 370 |
| `06-adverse-events.json` | median serious AEs by sponsor class | visualization | bar | 79 | 17 |
| `07-scatter.json` | enrollment against site count, one point per trial | visualization | scatter | 3,625 | 522 |
| `08-unsupported.json` | which drug works better for melanoma | unsupported | kpi | 0 | 0 |

`05-drug-network` shows the reviewer doing real work. The planner first set `top_n: null`,
which returns every pair that co-occurs at all — 5,215 edges, including pairs seen once —
and does not answer *"which drugs **frequently** co-occur"*. The judge raised
UNQUANTIFIED SUPERLATIVE, the plan was revised to `top_n: 20`, and `meta.review` records
both. The concern is visible in the response, not just in the outcome.

`07-scatter` is the point layout: one datum per trial rather than per bucket. Capturing it
found three bugs that unit tests had not — a subtitle claiming a median over 3,625 buckets
of one trial each, linear axes that crushed 99.6% of points against the edge, and a
frontend plotting row index instead of site count. An untested chart family is an untested
code path.

`06-adverse-events` is the posted-results path, and its `used` count is the point: 219
melanoma phase 3 trials match, and **140 of them have posted no results at all**. Those are
excluded and counted in `excluded_by_reason`, never read as zero — "no safety data" and "no
serious adverse events" are different facts, and averaging them together would understate
every bucket.

## Reproducing

```bash
.venv/bin/python examples/run_examples.py          # replays from examples/cache/
.venv/bin/python examples/run_examples.py --live   # ignores the cache, re-fetches
```

The registry responses are cached under `examples/cache/` so these reproduce without the
network. Live queries never use that cache — see `docs/decisions.md`.

## Hand-verification

`plan.md` §7 asks that two examples be counted manually and confirmed. That check is
`verify_examples.py`, and it **imports nothing from `cheiron`** — it issues its own HTTP
requests and counts with a `dict`. A verification sharing code with the thing it verifies
mostly proves the code is self-consistent.

```bash
.venv/bin/python examples/verify_examples.py
```

Three are checked rather than two, and all three reconcile exactly: nine phase buckets,
eleven country buckets including the collapsed `Other`, and four sponsor-class medians
plus the 140-trial exclusion count. The third was added because posted results are the
newest and least-exercised path, and its arithmetic is the one most easily got wrong —
event groups carry one row per arm and no total row, so a trial's figure is a sum over
arms before any median is taken.

Worth knowing what the first attempt did, because it is the reason `meta.api_requests`
exists. The recount initially disagreed with the country example by hundreds of trials
(China 569 vs 1,066). The system was right: the plan had filtered on **trial-level**
`filter.overallStatus=RECRUITING`, and the recount had guessed at a **site-level**
`SEARCH[Location](AREA[LocationStatus]RECRUITING)`. Those are different questions — 42,635
against 9,347 corpus-wide, per `docs/api-findings.md`. Replaying the URL the response
published settled it, which is what an audit trail is for.

That distinction is also a real caveat on `04-geographic`: it shows countries where a
recruiting trial has *any* site, which is not the same as countries where recruitment is
open. The nested form is supported (`site_status`); the planner chose the trial-level
reading of "recruiting trials for NSCLC", which is defensible but worth stating.
