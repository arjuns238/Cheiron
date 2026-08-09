"""Compile a `Plan` into ClinicalTrials.gov API requests, one set per leg.

This is the boundary where the system's own vocabulary becomes the registry's. Above it
everything speaks in flattener keys (`sponsor_class`, `start_date`); below it everything
speaks Essie (`AREA[LeadSponsorClass]`, `AREA[StartDate]RANGE[...]`). Keeping the
translation in one deterministic function is what lets the planner be validated against a
closed vocabulary instead of against a query language.

Every clause emitted here was verified by curl against the live API; see
`docs/api-findings.md`. Nothing is constructed from documentation alone.

**Everything is pushed down.** `plan.md` §3 classified six filters as local, to be applied
after fetch because the API had no equivalent. It does: `AREA[StudyType]`,
`AREA[LeadSponsorClass]`, `AREA[InterventionType]`, `AREA[EnrollmentCount]RANGE`,
`AREA[HasResults]` and `AREA[StartDateType]` all work, and are recorded as CORRECTION 3.
Pushing them down means the page cap bites far less often, so fewer charts are samples —
which matters more than preserving the plan's `retrieved` vs `used` divergence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cheiron.schemas.fields import FIELDS
from cheiron.schemas.plan import DateCertainty, Filters, Layout, Metric, Plan

#: The registry clamps `pageSize` at 1000 silently; asking for more returns 1000 without
#: complaint. Requested explicitly because the default is 10, which would turn every fetch
#: into a hundred round trips.
PAGE_SIZE = 1000

#: Fields fetched for every request regardless of plan, because a datum without an
#: identifier and a title cannot be cited.
CITATION_PROJECTION = ("NCTId", "BriefTitle")

#: Co-occurrence fields whose edges come from a *derived* field rather than the named one.
#: Mirrors `agg.aggregator._ARM_SCOPED`; the two are asserted equal in the tests so they
#: cannot drift and leave the compiler under-projecting.
ARM_SCOPED_SOURCES: dict[str, str] = {"intervention_names": "combination_groups"}

#: Pieces that let a leg's term be quoted from the record, best evidence first. The
#: sponsor's own intervention name, then ClinicalTrials.gov's MeSH concept — which is what
#: covers the trials whose only intervention name is "Immune checkpoint inhibitor".
SERIES_EVIDENCE: tuple[str, ...] = ("InterventionName", "InterventionMeshTerm")

#: Essie treats bare whitespace as a token separator, so any value that contains a space
#: (`United States`, `Novo Nordisk A/S`) is quoted. Both forms happened to return identical
#: counts when tested, but relying on that would be relying on an unspecified tokenizer.
_NEEDS_QUOTING = re.compile(r"[\s()\[\]]")


def escape(value: str) -> str:
    """Render a filter value safe to embed in an Essie expression."""
    cleaned = value.replace('"', " ").strip()
    return f'"{cleaned}"' if _NEEDS_QUOTING.search(cleaned) else cleaned


@dataclass(frozen=True)
class CompiledRequest:
    """One leg's API request, before pagination.

    Attributes:
        leg_label: The leg this fetch belongs to. Carried through so the aggregator can
            key records by leg without re-deriving the association.
        params: Query parameters, ready to hand to the client. `pageToken` is added by the
            client as it paginates; everything else is fixed here.
    """

    leg_label: str
    params: dict[str, str]


def projection(plan: Plan) -> tuple[str, ...]:
    """The `fields=` set needed to answer this plan, and nothing more.

    A bar chart of phases needs four pieces, not a 17 KB record. Narrowing the projection
    is the single cheapest thing the compiler does: it cuts payload by roughly an order of
    magnitude on a large fetch, which is what makes the 20-page cap generous rather than
    tight.

    Note the caveat from `docs/api-findings.md`: a struct's sub-field is only returned if
    named. Requesting `StartDate` alone yields `startDateStruct.date` with no `.type`, so
    the ACTUAL/ESTIMATED distinction vanishes silently. `FieldSpec.projection` therefore
    lists every piece a field needs, and this function unions those lists rather than
    guessing.
    """
    pieces: list[str] = list(CITATION_PROJECTION)
    referenced = [plan.group_by, plan.series_by, plan.metric_field, plan.distinct_of]

    # A co-occurrence network on interventions is built from `combination_groups`, which
    # is derived from arm-group membership rather than from `intervention_names` itself.
    # Projecting only the named field returns interventions with no `type` and no
    # `armGroupLabels`, so every trial yields no pairs and the graph comes back empty —
    # silently, because an empty graph looks like a slice with no combinations in it.
    if plan.layout is Layout.COOCCURRENCE and plan.group_by in ARM_SCOPED_SOURCES:
        referenced.append(ARM_SCOPED_SOURCES[plan.group_by])

    for key in referenced:
        if key:
            pieces.extend(FIELDS[key].projection)

    # A multi-leg plan has a second coordinate — which leg a trial fell in — and that is
    # cited separately. The leg's term has to be *somewhere in the fetched record* to be
    # quotable, and a narrow projection is exactly what removes it: a phases-by-drug
    # comparison projects `NCTId,BriefTitle,Phase`, so the only place the drug can be
    # found is the title. Measured, that evidenced 56% of contributions; with the
    # intervention name and the registry's own MeSH concept in the projection it reaches
    # what the records actually support. Same failure as ARM_SCOPED_SOURCES above — a
    # projection that omits the evidence does not error, it just quietly cites less.
    if len(plan.legs) > 1:
        pieces.extend(SERIES_EVIDENCE)

    return tuple(dict.fromkeys(pieces))


def _advanced_clauses(filters: Filters) -> list[str]:
    """Every filter that belongs in `filter.advanced`, as separate AND-ed clauses."""
    clauses: list[str] = []

    if filters.phase:
        # A union, not a conjunction: "Phase 2 or Phase 3 trials". Verified against
        # melanoma, where PHASE2 (1534) and PHASE3 (219) union to 1728 rather than 1753 —
        # the difference is the multi-phase trials, counted once, which is correct.
        members = " OR ".join(p.value for p in filters.phase)
        clauses.append(f"AREA[Phase]({members})" if len(filters.phase) > 1 else
                       f"AREA[Phase]{filters.phase[0].value}")

    if filters.start_year_min is not None or filters.start_year_max is not None:
        low = f"{filters.start_year_min}-01-01" if filters.start_year_min else "MIN"
        high = f"{filters.start_year_max}-12-31" if filters.start_year_max else "MAX"
        clauses.append(f"AREA[StartDate]RANGE[{low},{high}]")

    if filters.site_status and not filters.country:
        # `site_status` alone used to compile to nothing at all: it was only ever emitted
        # inside the country branch below, so a plan filtering on it without a country
        # issued an unfiltered query while `meta.filters_applied` still reported the
        # filter. Measured on non-small cell lung cancer: 8,493 trials unfiltered against
        # 2,107 with this clause — the geographic example silently answered a question
        # about 7,744 trials instead of the 1,295 it claimed.
        #
        # On its own the clause means "has at least one site in this status, anywhere",
        # which is a different question from trial-level status and is not a substitute
        # for it: 2,107 against 1,295 for the same slice. The gap is trials whose overall
        # status is not RECRUITING but which still carry a recruiting site.
        statuses = " OR ".join(s.value for s in filters.site_status)
        clauses.append(f"SEARCH[Location](AREA[LocationStatus]({statuses}))")

    if filters.country:
        if filters.site_status:
            # Nested, and the nesting is load-bearing. The unnested form matches trials
            # that have a French site and are *separately* recruiting somewhere else;
            # the nested form requires the French site itself to be recruiting. For
            # "which countries have the most recruiting trials", only the nested form is
            # the right question. Measured: 42,635 unnested vs 9,347 nested.
            statuses = " OR ".join(s.value for s in filters.site_status)
            clauses.append(
                f"SEARCH[Location](AREA[LocationCountry]{escape(filters.country)} "
                f"AND AREA[LocationStatus]({statuses}))"
            )
        else:
            clauses.append(f"AREA[LocationCountry]{escape(filters.country)}")

    if filters.study_type:
        clauses.append(f"AREA[StudyType]{filters.study_type.value}")
    if filters.sponsor_class:
        clauses.append(f"AREA[LeadSponsorClass]{filters.sponsor_class.value}")
    if filters.intervention_type:
        clauses.append(f"AREA[InterventionType]{filters.intervention_type.value}")

    if filters.enrollment_min is not None or filters.enrollment_max is not None:
        low = filters.enrollment_min if filters.enrollment_min is not None else "MIN"
        high = filters.enrollment_max if filters.enrollment_max is not None else "MAX"
        clauses.append(f"AREA[EnrollmentCount]RANGE[{low},{high}]")

    if filters.has_results is not None:
        clauses.append(f"AREA[HasResults]{str(filters.has_results).lower()}")

    # ACTUAL_ONLY and EXCLUDE_ESTIMATED are genuinely different populations, because
    # `startDateStruct.type` is absent on many older records. ACTUAL_ONLY keeps only what
    # the sponsor confirmed; EXCLUDE_ESTIMATED keeps confirmed *and* unrecorded, dropping
    # only what the sponsor flagged as a projection. Verified to partition exactly:
    # 3,528 NOT-ESTIMATED + 215 ESTIMATED = 3,743 total.
    if filters.date_certainty is DateCertainty.ACTUAL_ONLY:
        clauses.append("AREA[StartDateType]ACTUAL")
    elif filters.date_certainty is DateCertainty.EXCLUDE_ESTIMATED:
        clauses.append("NOT AREA[StartDateType]ESTIMATED")

    return clauses


def compile_leg(leg_label: str, filters: Filters, fields: tuple[str, ...]) -> CompiledRequest:
    """Translate one leg's filters into a request."""
    params: dict[str, str] = {
        "format": "json",
        "pageSize": str(PAGE_SIZE),
        "countTotal": "true",
        "fields": ",".join(fields),
    }

    if filters.condition:
        params["query.cond"] = filters.condition
    if filters.intervention:
        params["query.intr"] = filters.intervention
    if filters.sponsor:
        params["query.spons"] = filters.sponsor
    if filters.free_text:
        params["query.term"] = filters.free_text
    if filters.status:
        params["filter.overallStatus"] = ",".join(s.value for s in filters.status)

    clauses = _advanced_clauses(filters)
    if clauses:
        params["filter.advanced"] = " AND ".join(clauses)

    return CompiledRequest(leg_label=leg_label, params=params)


def compile_plan(plan: Plan) -> list[CompiledRequest]:
    """Translate a validated plan into one request per leg.

    Legs are independent fetches by design. A trial matching two legs is fetched twice and
    counted under both, because each leg is a population rather than a partition — see the
    aggregator's overlap accounting.
    """
    fields = projection(plan)
    return [compile_leg(leg.label, leg.filters, fields) for leg in plan.legs]


def request_url(base_url: str, request: CompiledRequest) -> str:
    """The full URL for a request, for `meta.api_requests`.

    Every request the system made is echoed into the response so a reader can paste it
    into a terminal and get the same records back. That reproducibility is the practical
    form of the traceability claim.
    """
    from urllib.parse import urlencode

    return f"{base_url.rstrip('/')}/studies?{urlencode(request.params)}"


def describe(plan: Plan) -> str:
    """One line naming what will be fetched, for `meta.interpretation` and debugging."""
    metric = plan.metric.value if plan.metric is not Metric.COUNT else "trial count"
    dimension = f" by {FIELDS[plan.group_by].label}" if plan.group_by else ""
    legs = ", ".join(leg.label for leg in plan.legs)
    return f"{metric}{dimension} across {len(plan.legs)} leg(s): {legs}"


__all__ = [
    "CITATION_PROJECTION",
    "PAGE_SIZE",
    "CompiledRequest",
    "compile_leg",
    "compile_plan",
    "describe",
    "escape",
    "projection",
    "request_url",
]
