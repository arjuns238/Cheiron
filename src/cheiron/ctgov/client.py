"""The ClinicalTrials.gov API client: pagination, retry, and count reconciliation.

The client's job is not merely to fetch. It is to fetch and then be able to *say what it
fetched*, because every number downstream is only as trustworthy as the record set it was
folded over. So every fetch returns not just records but the reconciliation: how many the
registry said matched, how many actually arrived, whether the page cap stopped it early,
and which URLs were issued.

That reconciliation is checked, not merely reported. If the registry says 4,213 studies
match and 4,180 arrive with no truncation, something went wrong during pagination and the
chart built on those records would be quietly short. `FetchResult.check()` raises.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from cheiron.ctgov.cache import Cache, NullCache, cache_key
from cheiron.ctgov.compiler import CompiledRequest, request_url

log = logging.getLogger(__name__)

BASE_URL = "https://clinicaltrials.gov/api/v2"

#: Stop after this many pages. At `pageSize=1000` that is 20,000 records, which covers the
#: overwhelming majority of realistic queries whole — the largest single-country slice in
#: the corpus (`query.locn=France`, 42,724) is one of the few that exceeds it. Past the cap
#: the chart is a sample, and it says so: `truncated` is set, surfaced as a warning, and
#: `matched` vs `retrieved` in `meta.record_counts` shows exactly how much was seen.
MAX_PAGES = 20

#: Retried on: the registry rate-limits and occasionally 502s behind its CDN. A 400 is a
#: malformed query and retrying it would just be slower failure.
#:
#: 500 is included on evidence rather than principle: mid-development the whole API —
#: including `/version` — returned 500 for about twenty seconds and then recovered. A
#: transient upstream failure is not something to hand to the user as an error.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 1.0

#: Politeness, not correctness — but a 429 is indistinguishable from a real failure to the
#: user, so it is worth avoiding rather than merely surviving. Legs are fetched
#: concurrently and each paginates, so an unthrottled two-leg comparison can burst a dozen
#: requests at the registry in under a second; that is what provoked the 429 that put
#: these limits here.
MAX_CONCURRENCY = 3
MIN_REQUEST_INTERVAL = 0.15


class ApiError(RuntimeError):
    """A request failed in a way retrying will not fix.

    Carries the registry's own error text, which is unusually good — an unknown Essie area
    name comes back as `Unknown area name: 'NotAField'` — and that text is worth showing
    rather than replacing with a generic message.
    """

    def __init__(self, status: int, detail: str, url: str) -> None:
        super().__init__(f"ClinicalTrials.gov returned {status}: {detail}")
        self.status = status
        self.detail = detail
        self.url = url


class ReconciliationError(AssertionError):
    """The registry's match count and the records received disagree.

    An assertion rather than a warning, for the same reason the aggregator's invariants
    are: a short record set produces a chart that is wrong without looking wrong.
    """


@dataclass
class FetchResult:
    """Everything one leg's fetch produced, including its own audit trail."""

    leg_label: str
    studies: list[dict[str, Any]] = field(default_factory=list)
    #: What the registry said matched, from `countTotal` on the first page.
    matched: int = 0
    truncated: bool = False
    pages: int = 0
    urls: list[str] = field(default_factory=list)
    cache_hits: int = 0

    @property
    def retrieved(self) -> int:
        return len(self.studies)

    @property
    def cache_hit(self) -> bool:
        """True only when the whole fetch was served from disk."""
        return self.pages > 0 and self.cache_hits == self.pages

    def check(self) -> None:
        """Reconcile what arrived against what the registry said would.

        Raises:
            ReconciliationError: if records went missing without the cap explaining it.
        """
        if self.truncated:
            if self.retrieved > self.matched:
                raise ReconciliationError(
                    f"{self.leg_label}: retrieved {self.retrieved} exceeds the registry's "
                    f"reported match count of {self.matched}"
                )
            return
        if self.retrieved != self.matched:
            raise ReconciliationError(
                f"{self.leg_label}: registry reported {self.matched} matching studies but "
                f"{self.retrieved} were retrieved without hitting the page cap"
            )


class CtGovClient:
    """Async client over `/studies`.

    Args:
        client: An `httpx.AsyncClient`. Injected rather than constructed so tests can pass
            a transport backed by fixtures and so the caller controls connection reuse.
        cache: Off by default — see `cache.py` for why live queries are not cached.
        base_url: Overridable for tests.
        max_pages: Overridable so a test can exercise truncation without 20,000 records.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        cache: Cache | None = None,
        base_url: str = BASE_URL,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._client = client
        self._cache = cache or NullCache()
        self.base_url = base_url.rstrip("/")
        self.max_pages = max_pages
        self._slots = asyncio.Semaphore(MAX_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def _throttle(self) -> None:
        """Space requests out, so concurrent legs interleave instead of bursting.

        The lock is held only long enough to claim a slot on the timeline, not for the
        sleep itself, so N waiters queue up at N × interval rather than serializing behind
        each other's full wait.
        """
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + MIN_REQUEST_INTERVAL
        if wait:
            await asyncio.sleep(wait)

    async def _get_page(self, params: dict[str, str]) -> dict[str, Any]:
        """One request, with retry on the transient statuses only."""
        url = f"{self.base_url}/studies"
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._slots:
                    await self._throttle()
                    response = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                # Connection reset, DNS blip, timeout: all worth another go.
                last_error = exc
                await asyncio.sleep(BACKOFF_SECONDS * 2**attempt)
                continue

            if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                # Honour Retry-After when the registry sends it; it knows its own limits
                # better than a fixed backoff curve does.
                delay = _retry_after(response) or BACKOFF_SECONDS * 2**attempt
                log.warning("ct.gov %s, retrying in %.1fs", response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code != 200:
                raise ApiError(response.status_code, response.text.strip()[:500], str(response.url))
            return response.json()

        raise ApiError(0, f"transport failure after {MAX_ATTEMPTS} attempts: {last_error}", url)

    async def fetch(self, request: CompiledRequest) -> FetchResult:
        """Fetch every page for one leg, up to the cap.

        Returns:
            A `FetchResult` whose `check()` has already been called, so a caller holding
            one can rely on its counts reconciling.
        """
        result = FetchResult(leg_label=request.leg_label)
        page_token: str | None = None

        while result.pages < self.max_pages:
            params = dict(request.params)
            if page_token:
                params["pageToken"] = page_token
                # `countTotal` is only meaningful on the first page and costs the registry
                # a full count on every subsequent one.
                params.pop("countTotal", None)

            key = cache_key(params, page_token)
            payload = self._cache.get(key) if self._cache.enabled else None
            if payload is not None:
                result.cache_hits += 1
            else:
                payload = await self._get_page(params)
                if self._cache.enabled:
                    self._cache.put(key, payload)

            issued = CompiledRequest(request.leg_label, params)
            result.urls.append(request_url(self.base_url, issued))
            result.pages += 1
            result.studies.extend(payload.get("studies") or [])

            if page_token is None:
                result.matched = int(payload.get("totalCount") or 0)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        # Truncation is diagnosed from the counts rather than from a surviving page token,
        # because the token is the registry's business and the shortfall is the user's.
        result.truncated = result.pages >= self.max_pages and result.retrieved < result.matched
        result.check()
        return result

    async def fetch_all(self, requests: list[CompiledRequest]) -> list[FetchResult]:
        """Fetch every leg concurrently.

        Legs are independent by construction, so a two-leg comparison costs one leg's
        latency rather than two. Exceptions propagate: a comparison missing half its data
        is not a partial success.
        """
        return list(await asyncio.gather(*(self.fetch(r) for r in requests)))


def _retry_after(response: httpx.Response) -> float | None:
    """Parse `Retry-After` in its seconds form, ignoring the HTTP-date form."""
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


__all__ = [
    "BASE_URL",
    "MAX_PAGES",
    "ApiError",
    "CtGovClient",
    "FetchResult",
    "ReconciliationError",
]
