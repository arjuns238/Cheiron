"""Plan → records, with the accounting carried the whole way.

This is the seam between the network and the deterministic core: it compiles a plan,
fetches every leg, normalizes what comes back, and hands the aggregator both the records
and the exclusions that occurred before the aggregator could see them.

That last part is the reason this module exists rather than the caller doing three calls in
a row. A record the normalizer rejects has already been counted in `retrieved` by the
registry, and if it were simply dropped here the aggregator's reconciliation would run
against a total that had quietly shrunk. Exclusions travel forward; nothing disappears
between stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cheiron.ctgov.client import CtGovClient, FetchResult
from cheiron.ctgov.compiler import compile_plan
from cheiron.ctgov.normalizer import NormalizedRecord, normalize_studies
from cheiron.schemas.plan import Plan


@dataclass
class Retrieval:
    """Everything the aggregator and the response envelope need from the network.

    Attributes:
        records_by_leg: Normalized records keyed by leg label, ready for `aggregate`.
        exclusions: Normalizer rejections, counted by reason, to be passed as
            `prior_exclusions` so the reconciliation holds against what the API returned.
        matched: What the registry said matched, summed across legs.
        truncated: True if any leg hit the page cap, meaning the chart is a sample.
        urls: Every request issued, for `meta.api_requests`.
        cache_hit: True only if every leg was served entirely from disk.
        fetched_ids: Every NCT ID that actually arrived, for the citation invariant.
    """

    records_by_leg: dict[str, list[NormalizedRecord]] = field(default_factory=dict)
    exclusions: dict[str, int] = field(default_factory=dict)
    matched: int = 0
    truncated: bool = False
    urls: list[str] = field(default_factory=list)
    cache_hit: bool = False
    fetched_ids: set[str] = field(default_factory=set)

    @property
    def retrieved(self) -> int:
        return sum(len(r) for r in self.records_by_leg.values()) + sum(self.exclusions.values())


async def retrieve(client: CtGovClient, plan: Plan) -> Retrieval:
    """Fetch and normalize every leg of a validated plan."""
    results = await client.fetch_all(compile_plan(plan))
    return assemble(results)


def assemble(results: list[FetchResult]) -> Retrieval:
    """Normalize fetched pages into a `Retrieval`.

    Split from `retrieve` so tests can drive it from saved payloads with no network and no
    client at all.
    """
    retrieval = Retrieval()
    for result in results:
        normalized = normalize_studies(result.studies)

        # Legs are fetched independently and a trial can match two of them, so records are
        # keyed by leg rather than merged. Duplicate leg labels would silently overwrite;
        # the plan validator guarantees labels are distinct.
        retrieval.records_by_leg[result.leg_label] = normalized.records
        for reason, count in normalized.excluded_by_reason.items():
            retrieval.exclusions[reason] = retrieval.exclusions.get(reason, 0) + count

        retrieval.matched += result.matched
        retrieval.truncated = retrieval.truncated or result.truncated
        retrieval.urls.extend(result.urls)
        retrieval.fetched_ids.update(r.nct_id for r in normalized.records)

    retrieval.cache_hit = bool(results) and all(r.cache_hit for r in results)
    return retrieval


__all__ = ["Retrieval", "assemble", "retrieve"]
