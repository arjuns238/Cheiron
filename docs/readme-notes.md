# README notes — things that must be stated explicitly

A running list of behaviours a reader would otherwise have to discover by reading the
source or, worse, by being surprised in production. Each entry records **the problem**,
**why silence is not acceptable**, and **what the README has to say**.

The test for inclusion is narrow: would a reasonable person, having read the README,
still be wrong about what the system did? If yes, it belongs here. Ordinary design
choices that behave the way a reader would already assume do not.

`docs/api-findings.md` and `docs/corpus-facts.md` hold measurements. This file holds
disclosures.

---

## 1. Planner capability differs by LLM provider

**The problem.** The planner's vocabulary is not the same on both providers. Anthropic
caps a structured-output schema at 16 union-typed and 24 optional parameters; `Plan` plus
`Filters` expose 24 unions and 31 optionals, so seven fields are withheld from the
Anthropic schema (`NARROW_SCHEMA_FIELDS` in `llm/planner.py`): `viz_hint`, `bin_scale`,
`site_status`, `date_certainty`, `has_results`, `enrollment_min`, `enrollment_max`.

Both limits were learned from live 400s, not from documentation:

```
Schemas contains too many parameters with union types (24 parameters with type arrays
or anyOf). … (limit: 16 parameters with unions).
Schemas contains too many optional parameters (31), which would make grammar compilation
inefficient. … (limit: 24).
```

**Why silence is not acceptable.** The same question can produce a different plan
depending on `LLM_PROVIDER`. A reader who benchmarks the system on OpenAI and deploys it
on Anthropic gets a system that quietly plans differently — and "the model chose not to"
and "the model was never offered the choice" are indistinguishable from the outside. A
provider switch reads like a configuration change; it is a capability change.

**What the README must say.**

- Name the seven withheld fields and the two Anthropic limits verbatim, with the error
  text, so the constraint reads as measured rather than assumed.
- State what is *not* lost, specifically: `viz_hint` is advisory and the deterministic viz
  rules decide chart legality regardless; the five filters remain settable as structured
  request fields, which override the planner on both providers anyway.
- State that `bin_scale` **is** derived rather than defaulted, and why — see §2.
- Say the schema translation is per-provider by necessity, not preference: OpenAI's strict
  mode requires every property in `required`, so optionality is expressed as
  `anyOf [T, null]`, which is precisely the construct Anthropic's union cap rejects. The
  two providers want opposite idioms for the same Pydantic model.

---

## 2. A withheld choice is derived, never silently defaulted

**The problem.** Dropping `bin_scale` from the Anthropic schema leaves it at its Pydantic
default of `linear`. Enrollment spans 0 to over 1.1 million with a median in the low
hundreds, so equal-width bins put nearly every trial in the first bar — the histogram is
arithmetically correct and useless. Withholding the field would therefore have
reintroduced the exact failure the log scale exists to prevent.

It is instead derived from the field registry's `skewed` flag (`enrollment`,
`site_count`), applied after the model answers.

**Why silence is not acceptable.** This is the general hazard with any per-provider trim:
a withheld field is a decision the model no longer makes, not a decision that disappears.
Someone extending `NARROW_SCHEMA_FIELDS` later needs the rule, or they will withhold a
field whose default is wrong and produce a plausible chart built on it.

**What the README must say.** State the rule as a rule: a field may only be withheld from
a provider's schema when its default is correct or derivable, and `bin_scale` is the
worked example of the derivable case.

---

## 3. `max_records` is not currently enforced

**The problem.** `AnalyzeRequest.max_records` is documented in the request schema as an
upper bound on records fetched, defaulting to 5,000 with a ceiling of 20,000. The API
client does not read it. Retrieval is governed by a fixed 20-page cap at `pageSize=1000`,
so the effective bound is 20,000 records per leg regardless of what the caller asked for.

**Why silence is not acceptable.** This is worse than an unimplemented feature: it is a
documented input that appears to work. A caller who sets `max_records: 500` to bound cost
or latency gets 20,000 records and no indication the parameter was ignored, and
`meta.record_counts` will show a number that contradicts their own request.

**What the README must say.** Either the field is wired to the client's page cap before
submission, or the README states plainly that it is accepted and not yet honoured, and the
request-schema documentation says the same. The one unacceptable outcome is documenting it
as effective while it is inert. Reconcile when `/analyze` is assembled.

---

## 4. Filters are pushed down, contradicting `plan.md`

**The problem.** `plan.md` §3 splits filters into pushdown and local, the local ones
applied after fetch "because the API has no equivalent". All six have one — measured in
`docs/api-findings.md` CORRECTION 3 — so every filter is pushed down.

**Why silence is not acceptable.** `plan.md` leans on that split to explain why
`retrieved` and `used` diverge in `meta.record_counts`. A reader holding the plan next to
the code will find the discrepancy and have to guess which is current.

**What the README must say.** That the split was removed on evidence, with the measured
counts, and that `retrieved` and `used` now diverge only through normalizer rejections and
missing grouping dimensions — simpler to explain, not weaker.

---

## 5. Counting semantics that differ from ClinicalTrials.gov's own facets

**The problem.** A multi-phase trial (`PHASE1|PHASE2`) forms its own bucket and is never
counted into both Phase 1 and Phase 2. ClinicalTrials.gov's own facets do the opposite:
their per-phase counts plus their missing count sum to 622,213 against a corpus of
597,691 — an excess of 24,522, exactly the multi-phase population.

**Why silence is not acceptable.** Anyone checking a phase chart against the registry's
own UI will find different numbers and reasonably conclude ours are wrong.

**What the README must say.** The divergence, the arithmetic above, and the reason: a
Phase 1/Phase 2 trial is one kind of trial, not one of each. Our totals sum to the trial
count; theirs do not.

---

## 6. Where trials leave the chart

Three distinct disappearances, each reported, none obvious from a chart alone:

- **Missing grouping dimension** — a trial with no value for `group_by` is excluded and
  counted under `missing_<field>`, not bucketed as "Unknown". The exception is `phases`,
  where absence is a recorded fact and becomes a first-class bucket.
- **Page cap** — beyond 20 pages the chart is a sample; `truncated` is set and the warning
  leads the list.
- **Network `Other`** — after a top-N collapse those trials appear in `record_counts` but
  in no node or edge, because "Other" is a residue rather than an entity that co-occurs.
  Live example: 365 of 393 trials on a top-8 sponsor↔condition graph.

**Why silence is not acceptable.** Each one makes the chart's totals disagree with
`record_counts`, and a reader who notices without an explanation will assume a bug.

**What the README must say.** All three, with the network case stated as a number rather
than a caveat — it can be most of the population.

---

## 7. Overlapping legs do not sum

**The problem.** In a comparison, a trial matching both legs contributes to both — a
combination study genuinely involves both drugs. Series totals therefore overlap and do
not add to a distinct trial count.

**Why silence is not acceptable.** A grouped bar chart looks like a partition. Readers add
the bars.

**What the README must say.** That legs are populations, not a partition, and that the
overlap is detected and reported in `meta.counting_semantics` and `meta.warnings` with a
count.

---

## 8. Caching is off for live queries

**The problem.** The disk cache is opt-in. Live user queries always hit the registry;
caching is enabled for recording the README's example runs and for tests.

**Why silence is not acceptable.** The README will present example runs as "the actual
JSON this system produced". That claim depends on the cache being populated, and a reader
who reruns them against a moved registry needs to know which mode they are in.

**What the README must say.** The default (off), what the cache is for, that
`meta.cache_hit` reports it per response, and how to clear it.

---

## 9. What a drug↔drug edge actually claims

**The problem.** An edge in the drug network asserts that two agents were *given together*.
The registry does not record that directly — it records interventions, arm groups, and a
membership between them — so the edge rule is an interpretation, and three separate
judgements go into it.

*Arm-scoping.* Two drugs in the same trial are frequently the two sides of a comparison
rather than a combination. Measured over 500 melanoma trials: 217 co-list two or more
agents, but only 157 have two or more sharing an arm group. Pairing at trial level would
assert combinations for roughly a third more trials than have one. NCT01748448 lists
Vitamin D and Placebo — naive pairing draws an edge between a drug and its own control.
So pairing happens strictly within an arm group, which is the registry's own statement of
what was administered together.

*Placebo exclusion is a name heuristic.* Arm membership alone is not sufficient.
Double-dummy blinding places a placebo *in the active arm* so both groups receive the same
number of injections, and sponsors type those placebos as `DRUG`: NCT01721772 yields the
arm `BMS-936558 (Nivolumab) || Placebo matching Dacarbazine`. The registry has no
"is placebo" flag, so these are excluded by matching name text (`placebo`, `sham`,
`vehicle control`). That is prose matching, and it will miss a placebo named in some other
way.

*`BIOLOGICAL` counts as a drug, diverging from `plan.md`.* `plan.md` says DRUG.
ClinicalTrials.gov's DRUG/BIOLOGICAL split is regulatory — which FDA centre reviews the
filing — rather than pharmacological, and sponsors apply it inconsistently to the *same
molecule*: pembrolizumab appears as `DRUG` in 405 records and `BIOLOGICAL` in 94. Excluding
`BIOLOGICAL` would make a combination appear or vanish depending on who filed it.

**Why silence is not acceptable.** This is the chart most likely to be read as clinical
fact, and it is the one the assignment weights highest. Every one of the three judgements
changes which edges exist. A reader comparing the graph against their own knowledge of a
treatment landscape needs to know what was counted before concluding the system is wrong —
or, worse, before concluding it is right about a combination it inferred from a comparison.

**What the README must say.**

- The arm-scoping rule, with the 217-versus-157 measurement, and that it is why edges are
  trustworthy rather than merely plentiful.
- That placebo exclusion is a name heuristic with no coded field behind it, so it is
  best-effort and will have misses.
- The `BIOLOGICAL` divergence from `plan.md` and the 405/94 evidence for it.
- That the same machinery pairs `conditions` and `intervention_mesh` at *trial* level,
  because those have no arm structure — and that those graphs therefore mean "co-listed",
  which the response states in `meta.warnings` per chart.

---

## 10. Network node labels are free text

**The problem.** Nodes in an intervention network are raw intervention names, and the
registry holds 528,741 distinct ones. They are not a controlled vocabulary and are not
deduplicated. A live run produced the edge `Nivolumab — Nivolumab + Relatlimab`, because a
single intervention record is *named* "Nivolumab + Relatlimab": one node is a drug, the
other is a two-drug regimen written into one name field.

The same drug also appears under brand names, code names, and dosage-bearing strings
(`BMS-936558 (Nivolumab)`), so one agent can occupy several nodes and its true edge weight
is split between them.

**Why silence is not acceptable.** Node identity is what a network graph is *for*. A reader
counts distinct drugs by counting nodes, and here that count is an overestimate by an
unknown factor. The failure is not visible from the chart: a duplicated node looks like a
different drug.

**What the README must say.** That intervention nodes are free text with the 528,741 figure
and the query behind it, that no entity resolution is performed, and the concrete
`Nivolumab + Relatlimab` example so the shape of the problem is unambiguous. State the
alternative the system already offers — `intervention_mesh` uses ClinicalTrials.gov's own
MeSH indexing and gives normalized nodes, at the cost of trial-level rather than arm-level
edges (§9) and of dropping agents with no MeSH heading. Entity resolution across raw names
belongs in "what I'd do with more time".

---

## 11. Networks are returned complete, on purpose

**The problem.** A co-occurrence network is not bounded by the system. `query.cond=cancer`
returns **4,274 nodes and 11,341 edges** — a 2.4 MB response. A reviewer opening that will
read it as a failure to bound output.

It is a deliberate choice, not an oversight. Thresholding a graph is a *presentation*
decision, and the most useful thing a client can do with a co-occurrence network is move
the threshold interactively — which only works if the client holds the whole network.
VOSviewer, the standard tool for bibliometric co-occurrence graphs, works exactly this
way: its minimum-occurrence filter is an interactive control, not a property of the data
it is given. Trimming server-side would make the primary interaction impossible, and a
client handed a trimmed graph cannot recover the rest without another request.

The payload objection is also weaker than it looks. JSON of this shape compresses **8.9×**
(measured, not estimated), so that 2.4 MB response is **288 KB** over the wire, and the
20,000-record retrieval cap bounds the worst case to roughly 1 MB gzipped.

What the system provides instead of a decision:

- `Node.weight` is the node's distinct trial count — the input a client filters on.
- `config.suggested_min_occurrences` is the smallest threshold that would render legibly,
  offered as a starting position. Advisory; nothing is removed on account of it. It is
  null when the graph already fits or when every node occurs equally often, since no
  threshold would separate them.
- An explicit `top_n` in the plan **is** honoured — that is a request rather than a default
  — and the trials it removes are counted and reported (§6).

**Why silence is not acceptable.** Returning 4,274 nodes looks identical to failing to
bound the output. The distinction is entirely in the intent, so the intent has to be
stated or the choice reads as a defect.

**What the README must say.** That networks are complete by design, with the VOSviewer
precedent and the 8.9× / 288 KB measurement, and that
`config.suggested_min_occurrences` plus `Node.weight` are how a client renders it legibly.

---

## 12. Edge `strength` is derived, and misleading on its own

**The problem.** Ranking edges by raw co-occurrence count ranks by *ubiquity*. On multiple
myeloma the five heaviest edges all contain dexamethasone — not because those pairings are
distinctive, but because dexamethasone is in nearly every regimen. `Edge.strength` is the
standard bibliometric correction, `2m·w / (k_source · k_target)`, which divides out each
endpoint's degree.

Two caveats, and both matter:

*It has no citations behind it.* `weight` is a fold over a trial list and every value in
this system is otherwise traceable to records. `strength` is arithmetic over the graph. It
ranks; it never replaces the countable value.

*Alone, it is worse than raw counts.* A pair occurring only with each other scores
maximally on a single trial. Measured live on myeloma, the top strengths were all `w=1`
pairs. It is informative combined with an occurrence threshold and noise without one —
which is precisely why VOSviewer applies its threshold first and normalizes second.

**Why silence is not acceptable.** A frontend developer sorting by `strength` because it
sounds more sophisticated than `weight` gets a chart of one-trial coincidences at the top,
and nothing in the output looks wrong.

**What the README must say.** The formula, that it is derived rather than counted, the
dexamethasone example showing what it fixes, and the explicit instruction not to sort by it
without applying `suggested_min_occurrences` first. The schema field description carries
the same warning, since that is what a frontend developer actually reads.

---

## 13. The judge was measured, and the measurement found a bug in the harness

**The problem.** `plan.md` §7 asks for an adversarial set, a check that the judge flags
deliberately wrong plans, and an honest report "including if it doesn't". A verification
layer nobody has tested is a claim, not a feature — and a judge that approves everything is
indistinguishable from no judge at all.

`tests/adversarial_judge.py` holds eight cases: four plans that are **legal** (they pass
every plan-validator rule) but answer a different question than the one asked, and four
correct plans as controls. The controls matter as much as the failures, since a judge that
flags everything is as useless as one that flags nothing, and only the correct plans reveal
which it is.

**First run:**

| Provider | Wrong plans caught | Correct plans kept |
|---|---|---|
| OpenAI `gpt-5.4` | 4/4 | 4/4 |
| Anthropic `claude-opus-5` | **3/4** | 4/4 |

Anthropic consistently missed the metric mismatch — a plan counting trials for a question
asking *median enrolment* — with `ok` on four repeat runs, so a reproducible gap rather
than sampling noise.

**The cause was not the model.** The plan was serialized into the review prompt with
`exclude_defaults=True`, which drops `metric: "count"` *precisely because count is the
default*. The judge was being asked whether the metric matched the question's noun while
never being shown the metric. Serializing with `exclude_none=True` instead — so defaulted
fields appear — took both providers to **4/4 and 4/4**.

The first fix attempted was a more explicit prompt rule, and it also produced 4/4. That was
a workaround treating the symptom: with the metric visible, the *original* short rule scores
3/3 on the previously-failing case and its control. The explicit rule was kept because it
covers `sum` and `distinct_count` too, but it is not what fixed this.

**Why silence is not acceptable.** Claiming a review layer without saying how it was
measured is the unearned assurance this system is otherwise built to avoid. This case also
shows the measurement doing its actual job: the adversarial set found a harness bug, not a
model weakness, and without running it the system would have shipped a judge structurally
unable to check one of its five rules.

**What the README must say.** The set, both providers' before-and-after scores, that the
miss traced to a serialization bug rather than the model, and that eight cases is a smoke
test rather than an evaluation. State that the judge is advisory and bounded to one
re-plan, so even a missed concern degrades to the plan the planner would have produced
anyway.

---

## 14. The chart selector was measured too, and it had the same shape of bug

**The problem.** The selector cannot produce an *illegal* chart — membership is enforced in
code, so the worst it can do is fall back to the rules' default. That safety property makes
it easy to assume it needs no evaluation. What it can still do is pick the *worse* of two
legal options, and nothing downstream notices.

`tests/adversarial_selector.py` pairs questions over the **same result shape**, differing
only in phrasing — which is the entire reason the stage exists, since the aggregation
cannot tell "how has X changed" from "which year had the most".

First run discriminated correctly on line-vs-bar and bar-vs-pie, and failed on geography:
both providers chose `bar` for *"Where are recruiting NSCLC trials running?"*, the canonical
map question, where `choropleth` is the rules' own default. The selector was **actively
downgrading** the chart — worse than not running it.

Cause: the prompt's guidance list covered line, bar, pie, stacked_area and grouped_bar, and
never mentioned maps. A chart type with no guidance is one the model will not choose.
Adding a geography rule — with the ranking exception, so "the top five countries" still
gets a bar — took both providers to 8/8, including 3/3 on the cases whose right answer is
*not* the default. Those three are the only ones that measure the model at all; the rest
would pass with no model.

**Why silence is not acceptable.** "The model cannot produce an illegal chart" is true and
is not the same as "the model is helping". Reporting only the safety property would overstate
what was verified.

**What the README must say.** That the selector is constrained by rules and separately
measured for usefulness, the geography miss and its fix, and that a bounded stage still
needs evaluating — being unable to do harm is not evidence of doing good.

---

## 15. First-request latency differs sharply by provider

**The problem.** Anthropic compiles a grammar for each distinct structured-output schema
and caches it. The first request carrying a new schema is dramatically slower than the
rest, and can fail outright:

```
400 invalid_request_error: Grammar compilation timed out.
```

Measured on the planner's schema: **78–90 s cold, ~5 s warm.** OpenAI shows no comparable
effect (~1–2 s throughout). The failure is transient — compilation continues server-side,
so a retry finds the cache warm — and is now retried inside the LLM client rather than
surfaced.

That placement matters. Left to propagate, the failure looked like a rejected plan and
consumed the planner's repair budget: a `/plan` call was observed spending three of its
revisions on cold compiles. A genuinely wrong plan would then have had fewer chances to be
repaired, for reasons entirely unrelated to the plan.

**Why silence is not acceptable.** A reviewer running the first Anthropic query against a
fresh deployment may wait over a minute and reasonably conclude the system is broken or
unusably slow. The second query answers in seconds. Without the explanation the first
impression is simply wrong.

**What the README must say.** The cold/warm figures with the provider named, that the
timeout is retried automatically, and that a first slow call is expected rather than
representative. Worth adding to "how to run": issue one warm-up query before demoing on
Anthropic.

---

## 16. "Recruiting trials in a country" has two readings, and the planner picks one

**The problem.** `04-geographic` answers "where are recruiting trials for NSCLC running?"
by filtering trial-level status (`filter.overallStatus=RECRUITING`) and then grouping by
the countries those trials list sites in. That shows countries where a *recruiting trial*
has **any** site — including sites that are closed.

The other reading is site-level: countries where recruitment is actually open, via
`SEARCH[Location](AREA[LocationStatus]RECRUITING)`. The system supports it (`site_status`),
and the planner did not choose it here.

The two differ by a lot. Corpus-wide, France has 42,635 trials under the unnested reading
and 9,347 under the nested one (`api-findings.md`). On this example the independent
recount initially used the site-level form and disagreed by hundreds of trials per country.

**Why silence is not acceptable.** A map captioned "recruiting trials" invites the reading
"you can enrol here", which the trial-level filter does not support. Both readings are
defensible; publishing one without naming it lets the reader assume the stronger claim.

**What the README must say.** That both readings exist, which one a given response used —
visible in `meta.plan` and `meta.api_requests` — and that `site_status` selects the nested
one. Note that on Anthropic `site_status` is among the withheld fields (§1), so that
provider cannot choose the nested reading from a natural-language query at all; a caller
who needs it must set it structurally.

---

## 17. Posted results ARE available, and the earlier limitations list said otherwise

**The problem.** An earlier version of this system listed "posted results" among the
registry's limitations, alongside genuine ones like per-site enrolment. That was wrong.
`resultsSection` is present and substantial — **789 of 3,743 melanoma trials carry one** —
with participant flow, baseline characteristics, adverse events and outcome measures.
Declining to read it was a scope decision (`plan.md` says "v2"), described as though the
data did not exist.

Two further items in that list were also miscategorised:

- *"Patient-level questions"* was too broad. `baselineCharacteristicsModule` reports
  aggregate `Age, Continuous` and `Sex: Female, Male` for the whole enrolled population, so
  "what is the typical age of participants" is answerable. The system was refusing a
  question it could answer.
- *"Semantic search over eligibility criteria"* is a scope cut, not a data gap. The text is
  in the API; it is simply not indexed here.

Results are now read. Nine fields were added — serious adverse events and deaths with
**their own separate denominators**, participant flow, baseline age with its statistic
type, and sex counts. Live: median serious-AE participants by sponsor class over melanoma
Phase 3 trials (industry 184.5, n=62), and baseline age 58 across 321 completed melanoma
trials.

**Outcome measures remain deliberately unread**, and that distinction is the point.
Measured: 25 melanoma trials with results carried **157 outcome measures under 144 distinct
titles in 34 distinct units** — even `"Percentage of participants"` and
`"Percentage of Participants"` are separate. There is nothing to aggregate across trials
without an ontology this project does not have, and reducing them to a number would be
exactly the plausible-but-wrong output the rest of the system refuses.

Two extraction rules that are easy to get wrong, and are load-bearing:

- **Adverse events are summed across arms; baseline figures are not.** `eventGroups` has no
  total row and each participant belongs to one arm, so summing is the trial total. Baseline
  tables *do* have a `Total` column (present on every trial sampled), and an unweighted mean
  of arm means is not the population mean unless the arms are equal size.
- **Deaths keep their own denominator.** `deathsNumAtRisk` and `seriousNumAtRisk` are
  allowed to differ; sharing one would compute a mortality rate against the wrong population.

Absent results are `None`, never zero — a trial reporting no deaths and a trial that never
reported are different populations, and folding them together makes safety look better the
less it was reported. The captured example shows the scale of this: **219 melanoma Phase 3
trials match, and 140 of them — 64% — have posted no results at all**. Those 140 appear in
`excluded_by_reason`, so the chart's 79 trials reconcile against the 219 retrieved and the
reader can see what the median was actually taken over.

One bug this example caught, worth stating because it is the failure mode the whole system
is built against. The chart published a median of 184.5 participants with serious adverse
events, and dropped the field's own registry note — *compare against `serious_ae_at_risk`
rather than enrolment* — because warnings were emitted for the grouped field only, never
the measured one. Every number was correct; the one sentence that made them interpretable
was missing. Notes are now emitted for `metric_field` and `distinct_of` as well.

**Why silence is not acceptable.** A limitations list is a claim about the data source. One
that misattributes a scope decision to the registry misleads the reader about what is
possible, and in this case caused the router to refuse answerable questions with reasons
that were untrue.

**What the README must say.** Limitations split into two headed groups — *the registry does
not hold it* (cross-trial efficacy with the 144-titles figure, per-person data, per-site
enrolment, free-text name duplication) and *this version does not read it* (outcome
measures, eligibility-text search). Never one list, because the reader cannot tell which is
which, and the difference is exactly what "what I would do with more time" is made of.

---

## 18. Citations belong to the datum, and a per-trial map cannot express that

**The problem.** Citations were a response-level map keyed by NCT ID, deduplicated with
`if nct_id in citations: continue`. That is correct for a single-valued dimension, where a
trial belongs to exactly one bucket. It is wrong for a multi-valued one, where a trial
belongs to several: the first bucket to claim a trial won, and every later bucket looked up
the same key and got a citation stating a **different bucket's value**.

In the geographic example, clicking Canada showed:

```json
{"nct_id": "NCT06758401", "field_value": "United States",
 "excerpt": "\"country\":\"United States\"", "offset": [310, 335]}
```

Measured: **32 of 55 citation lookups stated another bucket's value.** Zero on the three
single-valued examples (`phases`, `phases`×series, `sponsor_class`). Seven grouping fields
are exposed to it: `countries`, `conditions`, `intervention_names`, `intervention_types`,
`intervention_mesh`, `condition_mesh`, `collaborators`.

**Why silence is not acceptable.** Every one of those excerpts verified at its offsets.
Offset verification was working exactly as designed and could not have caught this, because
the text really was in the record — at a position supporting a different claim. This is the
distinction the project states as an invariant: *a citation must both verify at its offsets
and state the value it is cited for.* It is the second half that failed, and the first half
made it look trustworthy. A README that shows offset verification as the guarantee of
citation correctness, without this, overstates what that check buys.

Worth stating plainly: **the unit tests did not find this, and could not have.** They
asserted that every emitted citation verifies and that its `field_value` appears in its
excerpt — both true here. It was found by building the mock frontend and clicking a country.

**What the README must say.** Citations hang off each datum (`Datum.citations`,
`Edge.citations`); there is no response-level map. Give the reason — one trial, several
datums — and the 32/55 measurement, because it is the evidence that the shape was chosen
rather than assumed.

---

## 19. A leg is a search expression, so its evidence is sometimes honestly absent

**The problem.** A grouped datum has two coordinates — the bucket and the series — and one
excerpt rarely states both. Citing only the bucket leaves half the datum unevidenced: in
the comparison example all 76 citations quoted `protocolSection.designModule.phases`, and
nothing showed why a trial sat in the Nivolumab series rather than the Pembrolizumab one.

Evidencing the series is harder than it looks, because a leg is not a field. Measured over
200 trials matching `query.intr=pembrolizumab`:

| | |
|---|---|
| State the term literally in an intervention name or title | **86%** |
| Do not, but carry it as a MeSH concept in the same response | **6%** |
| State it nowhere quotable | **8%** |

The 6% are rescued by ClinicalTrials.gov's *own* index rather than any outside ontology:

```json
"interventions": [{"name": "Immune checkpoint inhibitor"}],
"meshes": [{"id": "C582435", "term": "pembrolizumab"}]
```

The remaining 8% are more interesting than a gap. They include a sildenafil
pharmacokinetics trial and one whose only intervention is "Anti-angiogenic agents plus
anti-PD-1/PD-L1 antibodies". These are the registry's **search** matching loosely, not
records we failed to cite. An uncitable contribution is therefore a signal that the search
recalled something the record does not support.

**Why silence is not acceptable.** The tempting fix is to quote the nearest available text
— showing `"Immune checkpoint inhibitor"` as evidence that a trial is a pembrolizumab
trial. That excerpt is real, verifies, and is not evidence of the claim. It would be a
worse failure than the citation bug above, because it looks more convincing.

**A projection can silently starve the evidence.** The first working version evidenced only
**56%** of contributions, not 86%. The compiler projects the narrowest `fields=` set that
answers the plan, and for a phases-by-drug comparison that is `NCTId,BriefTitle,Phase` — so
the drug name was absent from every fetched record and the title was the only place it
could ever be found. Adding `InterventionName` and `InterventionMeshTerm` to the projection
for multi-leg plans took it to **86%**, sourced 60 from intervention names, 8 from MeSH
concepts and 1 from a title, with 11 honestly uncited. This is the same failure as the
arm-fields trap: *a projection that omits the evidence does not error, it just quietly
cites less*, and the only symptom is a number that looks plausible.

**What the README must say.** Series membership is cited separately (`supports: "series"`),
sourced from the sponsor's intervention name first and the registry's MeSH concept second,
with the `derivedSection` path making clear which is which. Where the record states the
term nowhere, no citation is emitted and the contribution is counted. Quote the 86/6/8
split, and say that the 8% is partly the registry's own search recall rather than a defect
here — that is the difference between reporting a limitation and understanding it.


---

## 20. An edge cites both endpoints, and the payload is deliberately uncapped

**The problem.** A co-occurrence edge claims two agents shared an arm group. One excerpt
cannot show that: the smallest span containing both drug names is usually the entire
`interventions` array — 1,435 characters on one measured record — which is a true substring
and useless as evidence. The first implementation quoted one endpoint plus its arm labels,
and **85 of 179 edge citations named only one of the two drugs**, leaving the pairing
itself unevidenced.

Each endpoint is now cited separately, on its own intervention entry, so the shared arm
label is visible in both:

```json
{"type":"DRUG","name":"Dexamethasone","armGroupLabels":["SCTC21C + VRd (S-VRd)","VRd"]}
{"type":"DRUG","name":"Lenalidomide", "armGroupLabels":["SCTC21C + VRd (S-VRd)","VRd"]}
```

Measured on the live network: 100% of edge contributions locatable, **0 of 13,780 excerpts
name a drug other than the one cited**, 96% show a shared arm label directly. The other 4%
are entries too long to quote whole, which fall back to the bare name leaf — weaker
evidence, and stated as such rather than padded out.

**The trap, because it is the third instance of one shape.** The obvious implementation —
"narrowest intervention object containing the term" — is wrong. An arm label reads
`"Arm B: Daratumumab + Lenalidomide + Dexamethasone"`, so searching a subtree for
"Dexamethasone" returns *Daratumumab's* entry: a citation whose `field_value` and whose
excerpt's `name` disagree. Matching is therefore on the intervention's own `name`, widening
to the parent afterwards. If either endpoint cannot be quoted, neither is emitted — half a
pairing is not evidence of a pairing.

**Why silence is not acceptable on the payload.** Citations hang off every datum and are
**not capped**. On a complete 5,215-edge graph that is 13,780 citations, 85% of an 8 MB
response (554 KB gzipped) against 1.2 MB for the graph alone. That is a deliberate choice —
every datum carries its own evidence rather than a privileged subset — but a reader who
meets an 8 MB response without warning will reasonably think nobody measured it.

**What the README must say.** That edges cite both endpoints and why one excerpt cannot;
the 0/13,780 wrong-drug figure as evidence the subtree trap was avoided; and the payload
arithmetic with the `suggested_min_occurrences` filter named as the client-side lever. Note
that a planner-chosen `top_n` changes this by two orders of magnitude — the same query
returned 41 edges on one run and 5,215 on another — so quote both.


---

## 21. The reviewer earns its place, and its verdict is always recorded

**The problem.** Asked *"Which drugs **frequently** co-occur in combination studies for
multiple myeloma?"*, the system returned **every** pair that co-occurs at all — 5,215 edges
over 1,234 nodes, 76% of which appear in a single trial. Every number was correct. It
answered "which drugs co-occur", which is not the question asked.

Nothing was broken. `top_n` is planner-chosen (`plan.py:200`) with no rule and no default,
so the same query produced `top_n: 10` on one run and `null` on the next — 87 KB against
11 MB for the same question. And the reviewer approved it, correctly: its prompt names five
failure classes and says *"This is the whole list — do not invent other grounds"*, and none
covered a question whose quantifier the plan had dropped.

The fix is a sixth class, UNQUANTIFIED SUPERLATIVE, judged from the wording alone — the
reviewer never learns how many values there will be, and does not need to. Live result
after the change:

```json
"review": {"verdict": "concern", "revised": true,
           "concerns": ["UNQUANTIFIED SUPERLATIVE — the question asks which drugs
             'frequently' co-occur, so the plan should set a top_n ... rather than
             leaving the result unrestricted to all co-occurrences, including one-off
             pairs."]}
```

The plan was revised to `top_n: 10` and the response went from 11 MB to 270 KB. **12/12 on
the adversarial set on both providers**, six wrong plans caught and six correct plans left
alone — the controls matter as much, since a reviewer that flags everything is as useless
as one that flags nothing.

**Why the fix is not a default.** The tempting alternative is to default `top_n` whenever
the layout is co-occurrence. That is the "hard node cap" already rejected under Graph size,
and it is wrong for the same reason: it would trim a genuine "which drugs co-occur" query
that legitimately wants the whole network. The restriction has to come from the question
asking for a frequent subset, not from policy. The reviewer is the right place precisely
because it is the only stage that reads the question and the plan together.

**Why the verdict is always recorded.** Previously only *unactioned concerns* produced a
warning, so an approval left no trace and `meta` could not distinguish "the reviewer
approved this plan" from "the reviewer never ran". Working out which had happened meant
reading the prompt in the source. `meta.review` now carries `verdict`, `concerns` and
`revised` on every reviewed request, and is null only when the reviewer genuinely did not
run.

**What the README must say.** This is the strongest available evidence that the reviewer is
load-bearing rather than decorative — a real question, a legal plan, a wrong answer, caught
and repaired, with the repair visible in the response. Quote the concern text and the
11 MB → 270 KB result. Say that `top_n` remains planner-chosen and the reviewer is
advisory with one re-plan, so this raises the odds rather than guaranteeing the outcome.
