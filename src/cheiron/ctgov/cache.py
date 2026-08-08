"""An opt-in disk cache for API responses.

Caching is **off by default**. Live user queries go to the registry every time, because a
question about clinical trials should be answered against the registry as it is now, and a
stale cache is a wrong answer that looks like a right one.

It is switched on for exactly two purposes:

1. **Recording the README's example runs.** Those are published as "the actual JSON this
   system produced", and that claim only holds if the run reproduces. With the cache
   populated, the examples replay byte-for-byte and need no network.
2. **Tests that exercise the client end to end** without depending on the registry being
   reachable or unchanged.

Entries never expire. A TTL would make the examples reproduce for a day and then quietly
stop, which is the worst of both options. Freshness is instead controlled by whether the
cache is enabled at all, and `meta.cache_hit` reports when an answer came from disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol


class Cache(Protocol):
    """Minimal read/write interface, so the client never branches on caching."""

    enabled: bool

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, payload: dict[str, Any]) -> None: ...


def cache_key(params: dict[str, str], page_token: str | None) -> str:
    """A stable digest of everything that determines a response.

    Sorted so that parameter ordering cannot produce two entries for one request, and
    inclusive of the page token so that page 3 of a fetch is not served page 1's records.
    """
    material = json.dumps(
        {"params": dict(sorted(params.items())), "pageToken": page_token},
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class NullCache:
    """The default. Stores nothing, reports nothing as a hit."""

    enabled = False

    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        return None


class DiskCache:
    """One JSON file per request, named by digest.

    Deliberately plain files rather than a database: the cache is meant to be inspectable
    and deletable by a reviewer who wants to prove the examples were not hand-edited.
    """

    enabled = True

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt entry is a cache miss, never an error. The request is simply made
            # again and the bad file overwritten.
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        try:
            self._path(key).write_text(json.dumps(payload, separators=(",", ":")))
        except OSError:
            # Failing to cache must never fail the request that produced the data.
            pass

    def clear(self) -> int:
        """Delete every entry, returning how many were removed."""
        removed = 0
        for path in self.directory.glob("*.json"):
            path.unlink()
            removed += 1
        return removed


__all__ = ["Cache", "DiskCache", "NullCache", "cache_key"]
