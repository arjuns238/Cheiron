"""Probe tools: the only way the planner learns anything about the data.

The planner has to make decisions that the schema alone cannot answer — does this drug
name resolve at all, is it an intervention or a condition, how many buckets will this
grouping produce, will the page cap truncate, is this field even populated in this slice.
Guessing at those produces a plan that is legal and wrong.

**Probes return aggregates and never records.** Every one of them answers with counts. A
trial's title, its enrollment, its sponsor — none of that reaches the model, so there is no
path by which a probe result could become a chart value even if the model tried.

That said, probe results *are* numbers the model sees, and `plan.md` names the resulting
hazard directly: a planner that has seen `probe_count → 3743` could put 3743 in a plan
field. It cannot reach `visualization.data`, because every datum is folded from records by
the aggregator and the invariant check reconciles bucket membership against the fetched
set. Probe results are recorded in `meta.planning_trace` so a reader can see exactly what
the model was told.

Exactness is per-probe and is always reported:

* `probe_count` and `fill_rate` are **exact** — the registry answers both with `countTotal`.
  `AREA[Field]RANGE[MIN,MAX]` counts records where the field is present, which is what
  makes a real fill rate possible in two requests rather than a full fetch.
* `field_values` is **exact for categorical fields**, whose vocabulary is a closed enum
  small enough to count member by member, and **sampled for entity fields**, whose
  vocabulary is open — 51,497 distinct lead sponsors corpus-wide. A sampled result says so.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cheiron.ctgov.client import ApiError, CtGovClient
from cheiron.ctgov.compiler import compile_leg
from cheiron.ctgov.normalizer import PHASE_NOT_REPORTED, normalize_studies
from cheiron.schemas.fields import FIELDS, FieldKind
from cheiron.schemas.plan import Filters
from cheiron.schemas.request import Phase, Status

log = logging.getLogger(__name__)

#: `plan.md` §3: at most four probe calls per planning attempt. The budget exists because
#: probes are the planner's slowest step, not because they are risky.
PROBE_BUDGET = 4

#: Records pulled when a field's vocabulary is open and has to be sampled. One page is the
#: cheapest useful sample and is enough to answer "roughly how many buckets".
SAMPLE_SIZE = 1000


class ProbeFilters(BaseModel):
    """The subset of `Filters` a probe accepts.

    Deliberately narrower than the real thing. A probe answers "how big is this slice",
    and the fields below are the ones that change that answer materially; carrying all
    sixteen filters would inflate every tool schema for no gain — and Anthropic's schema
    limits are already tight (see `llm.planner.NARROW_SCHEMA_FIELDS`).
    """

    model_config = ConfigDict(extra="forbid")

    condition: str | None = Field(None, description="Disease or condition → query.cond")
    intervention: str | None = Field(None, description="Drug or intervention → query.intr")
    sponsor: str | None = Field(None, description="Sponsor organisation → query.spons")
    free_text: str | None = Field(None, description="Free-text search → query.term")
    country: str | None = Field(None, description="Country name, as the registry spells it")
    phase: list[Phase] | None = Field(None, description="Restrict to these phases")
    status: list[Status] | None = Field(None, description="Restrict to these overall statuses")
    start_year_min: int | None = Field(None, description="Earliest start year, inclusive")
    start_year_max: int | None = Field(None, description="Latest start year, inclusive")

    def to_filters(self) -> Filters:
        """Widen into the real filter model the query compiler consumes.

        Only the fields declared on `ProbeFilters` are passed through. Subclasses add
        arguments that are not filters at all — `field_name` names the field being
        measured — and `Filters` forbids extras, so a blanket dump would fail on every
        `field_values` and `fill_rate` call.
        """
        names = set(ProbeFilters.model_fields)
        return Filters(
            **{k: v for k, v in self.model_dump(exclude_none=True).items() if k in names}
        )


class ProbeCountArgs(ProbeFilters):
    """Arguments for `probe_count` — the filters themselves."""


class FieldValuesArgs(ProbeFilters):
    field_name: str = Field(description="A legal field key to break the slice down by.")


class FillRateArgs(ProbeFilters):
    field_name: str = Field(description="A legal field key to measure the fill rate of.")


@dataclass
class ProbeCall:
    """One probe, recorded for `meta.planning_trace`."""

    tool: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass
class ProbeRunner:
    """Executes probes against the registry, enforcing the per-attempt budget.

    The budget is enforced here rather than in the prompt, because a model that decides to
    probe eight times should be stopped rather than asked nicely. Exceeding it returns a
    result explaining the refusal, which the model can act on — an exception would abort
    planning over something recoverable.
    """

    client: CtGovClient
    budget: int = PROBE_BUDGET
    calls: list[ProbeCall] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - len(self.calls))

    async def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one probe by name, recording the call and its result."""
        if not self.remaining:
            return {
                "error": f"probe budget exhausted ({self.budget} calls). Commit to a plan "
                f"using what you already know."
            }
        try:
            match tool:
                case "probe_count":
                    result = await self._probe_count(ProbeCountArgs.model_validate(args))
                case "field_values":
                    result = await self._field_values(FieldValuesArgs.model_validate(args))
                case "fill_rate":
                    result = await self._fill_rate(FillRateArgs.model_validate(args))
                case _:
                    return {"error": f"unknown probe {tool!r}"}
        except ApiError as exc:
            # The registry's own message is unusually good ("Unknown area name: ...") and
            # is more useful to the model than anything this layer would substitute.
            result = {"error": f"ClinicalTrials.gov rejected the probe: {exc.detail[:200]}"}
        except Exception as exc:  # noqa: BLE001 - a probe must never abort planning
            log.warning("probe %s failed: %s", tool, exc)
            result = {"error": f"probe failed: {exc}"}

        self.calls.append(ProbeCall(tool=tool, args=args, result=result))
        return result

    # -- individual probes ------------------------------------------------------------

    async def _count(self, filters: Filters) -> int:
        """One count request. `countTotal` makes this exact and cheap."""
        request = compile_leg("probe", filters, ("NCTId",))
        request.params["pageSize"] = "1"
        payload = await self.client._get_page(request.params)
        return int(payload.get("totalCount") or 0)

    async def _probe_count(self, args: ProbeCountArgs) -> dict[str, Any]:
        """How many trials match this slice.

        Answers four of the planner's questions at once: does the entity resolve at all
        (zero means it does not), which search field it belongs in (run it per field and
        compare), whether the page cap will truncate, and whether the slice is large
        enough to be worth splitting by quarter.
        """
        total = await self._count(args.to_filters())
        return {
            "total": total,
            "exact": True,
            "note": (
                "no trials match — the term may be misspelled or belong in a different "
                "filter field"
                if total == 0
                else "exceeds the 20,000-record page cap; the chart would be a sample"
                if total > 20_000
                else ""
            ),
        }

    async def _field_values(self, args: FieldValuesArgs) -> dict[str, Any]:
        """How this slice breaks down by a field — how many buckets, and the big ones.

        Exact for a categorical field, whose vocabulary is a closed enum; sampled for an
        entity field, whose vocabulary is open. The distinction is in the result, because
        "12 buckets" and "12 buckets in a 1,000-trial sample" support different decisions.
        """
        spec = FIELDS.get(args.field_name)
        if spec is None:
            return {"error": f"{args.field_name!r} is not a legal field"}
        if not spec.groupable:
            return {"error": f"{args.field_name!r} cannot be grouped on"}

        filters = args.to_filters()
        if spec.kind is FieldKind.CATEGORICAL and spec.enum_type:
            return await self._categorical_values(spec.key, filters)
        return await self._sampled_values(spec.key, filters)

    async def _categorical_values(self, key: str, filters: Filters) -> dict[str, Any]:
        """Exact per-value counts, one count request per enum member.

        Several requests, but each is a count rather than a fetch, and they run
        concurrently under the client's own rate limit. Exactness is worth it here: the
        vocabulary is small and the counts decide whether a chart needs a top_n at all.
        """
        from cheiron.schemas.request import InterventionType, SponsorClass, StudyType

        vocabularies: dict[str, list[str]] = {
            "phases": [p.value for p in Phase],
            "status": [s.value for s in Status],
            "study_type": [s.value for s in StudyType],
            "sponsor_class": [s.value for s in SponsorClass],
            "intervention_types": [i.value for i in InterventionType],
        }
        members = vocabularies.get(key)
        if members is None:
            return await self._sampled_values(key, filters)

        async def count_member(member: str) -> tuple[str, int]:
            scoped = filters.model_copy(update=_member_filter(key, member))
            return member, await self._count(scoped)

        pairs = await asyncio.gather(*(count_member(m) for m in members))
        counts = {member: n for member, n in pairs if n}

        # These are the registry's own facet counts, and the registry double-counts: a
        # Phase 1/Phase 2 trial is returned by both the PHASE1 and the PHASE2 filter. The
        # aggregator does the opposite — composite phases get their own bucket — so these
        # numbers will not match the chart, and the difference is not an error in either.
        # Saying so here keeps a reader of `meta.planning_trace` from concluding one is
        # wrong. See docs/readme-notes.md §5.
        overlapping = FIELDS[key].multi or key == "phases"
        return {
            "values": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "distinct_values": len(counts),
            "exact": True,
            "note": (
                "counts use ClinicalTrials.gov's own faceting, which returns a trial under "
                "every value it carries; the chart groups multi-valued trials differently, "
                "so these are bucket-shape evidence rather than chart values"
                if overlapping
                else ""
            ),
        }

    async def _sampled_values(self, key: str, filters: Filters) -> dict[str, Any]:
        """Approximate breakdown from one page, for open-vocabulary fields.

        A sample cannot give real counts and does not pretend to. What it answers reliably
        is the shape question the planner actually has: is this dimension small enough to
        chart whole, or does it need a top_n and an "Other" bucket.
        """
        spec = FIELDS[key]
        request = compile_leg("probe", filters, ("NCTId", *spec.projection))
        request.params["pageSize"] = str(SAMPLE_SIZE)
        payload = await self.client._get_page(request.params)
        studies = payload.get("studies") or []
        total = int(payload.get("totalCount") or 0)

        counts: dict[str, int] = {}
        for record in normalize_studies(studies).records:
            value = record.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                label = str(item) if item not in (None, "") else PHASE_NOT_REPORTED
                counts[label] = counts.get(label, 0) + 1

        top = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:10])
        return {
            "top_values_in_sample": top,
            "distinct_values_in_sample": len(counts),
            "sample_size": len(studies),
            "matching_total": total,
            "exact": False,
            "note": (
                "counts are from a sample and are not chart values; use them only to judge "
                "how many buckets this dimension produces"
            ),
        }

    async def _fill_rate(self, args: FillRateArgs) -> dict[str, Any]:
        """What fraction of this slice actually records the field.

        Exact, and cheap: `AREA[Field]RANGE[MIN,MAX]` counts records where the field is
        present, so two count requests give a real rate. A grouping field that is mostly
        empty in this slice produces a chart about reporting practice rather than about
        trials, which is the failure this probe exists to prevent.
        """
        spec = FIELDS.get(args.field_name)
        if spec is None:
            return {"error": f"{args.field_name!r} is not a legal field"}
        area = _PRESENCE_AREA.get(spec.key)
        if area is None:
            return {
                "error": f"fill rate is not measurable for {spec.key!r}; measurable fields "
                f"are: {', '.join(_PRESENCE_AREA)}"
            }

        filters = args.to_filters()
        total, present = await asyncio.gather(
            self._count(filters),
            self._count_with_clause(filters, f"AREA[{area}]RANGE[MIN,MAX]"),
        )
        rate = (present / total) if total else 0.0
        return {
            "present": present,
            "total": total,
            "fill_rate": round(rate, 4),
            "exact": True,
            "note": (
                f"only {rate:.0%} of this slice records {spec.label}; grouping on it would "
                f"describe reporting practice as much as trials"
                if total and rate < 0.7
                else ""
            ),
        }

    async def _count_with_clause(self, filters: Filters, clause: str) -> int:
        """Count with one extra Essie clause AND-ed onto the compiled filters."""
        request = compile_leg("probe", filters, ("NCTId",))
        request.params["pageSize"] = "1"
        existing = request.params.get("filter.advanced")
        request.params["filter.advanced"] = f"{existing} AND {clause}" if existing else clause
        payload = await self.client._get_page(request.params)
        return int(payload.get("totalCount") or 0)


def _member_filter(key: str, member: str) -> dict[str, Any]:
    """The `Filters` update that restricts to one member of a categorical vocabulary."""
    match key:
        case "phases":
            return {"phase": [Phase(member)]}
        case "status":
            return {"status": [Status(member)]}
        case "study_type":
            return {"study_type": member}
        case "sponsor_class":
            return {"sponsor_class": member}
        case "intervention_types":
            return {"intervention_type": member}
    raise AssertionError(f"no member filter for {key!r}")


#: Fields whose presence the registry can be asked about directly. `RANGE[MIN,MAX]` on an
#: indexed area matches records that have a value, which is what makes an exact fill rate
#: possible without fetching anything.
_PRESENCE_AREA: dict[str, str] = {
    "enrollment": "EnrollmentCount",
    "start_date": "StartDate",
    "completion_date": "CompletionDate",
    "primary_completion_date": "PrimaryCompletionDate",
    "phases": "Phase",
    "sponsor_class": "LeadSponsorClass",
    "countries": "LocationCountry",
    "intervention_types": "InterventionType",
}


def probe_tool_specs() -> list[dict[str, Any]]:
    """Tool definitions handed to the model, one per probe."""
    from cheiron.llm.client import Optionality, json_schema_for

    return [
        {
            "name": "probe_count",
            "description": (
                "Count trials matching a filter set. Use it to check that a drug or "
                "condition name resolves at all (zero means it does not), to decide which "
                "search field a term belongs in by comparing counts, and to see whether a "
                "slice exceeds the 20,000-record page cap. Exact."
            ),
            "input_schema": json_schema_for(ProbeCountArgs, Optionality.OMITTABLE),
        },
        {
            "name": "field_values",
            "description": (
                "Break a slice down by a field to see how many buckets it produces and "
                "which dominate. Use it to decide whether a grouping needs top_n. Exact "
                "for categorical fields; sampled for entity fields, which have open "
                "vocabularies. Sampled counts are never chart values."
            ),
            "input_schema": json_schema_for(FieldValuesArgs, Optionality.OMITTABLE),
        },
        {
            "name": "fill_rate",
            "description": (
                "What fraction of a slice actually records a field. Use it before grouping "
                "on or measuring a field that may be sparsely reported — a mostly-empty "
                "field produces a chart about reporting practice rather than trials. Exact."
            ),
            "input_schema": json_schema_for(FillRateArgs, Optionality.OMITTABLE),
        },
    ]


__all__ = [
    "PROBE_BUDGET",
    "SAMPLE_SIZE",
    "FieldValuesArgs",
    "FillRateArgs",
    "ProbeCall",
    "ProbeCountArgs",
    "ProbeFilters",
    "ProbeRunner",
    "probe_tool_specs",
]
