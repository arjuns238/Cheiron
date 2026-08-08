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
