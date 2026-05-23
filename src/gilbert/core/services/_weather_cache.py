"""Single-flight + LRU cache used by ``WeatherService``.

Keyed by ``(backend_name, method, location_key, units, params)`` so
two AI turns asking for the same location at the same time result in
**one** backend HTTP request: the second caller awaits the first
caller's ``Future``.

LRU eviction caps the entry map so an unbounded keyspace (per-user
locations + varying ``hours``/``days`` + per-user units) cannot grow
without limit. Lazy expiry on read drops entries past ``expires_at``
before returning. There is no long-lived per-key lock dict — the
single-flight ``Future`` is removed from ``_inflight`` once it
resolves (success or failure) so lock leaks are not possible.
"""

from __future__ import annotations

import asyncio
import collections
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gilbert.interfaces.weather import GeoLocation, WeatherUnits


@dataclass
class _CacheEntry:
    """One cached value with its monotonic-clock fetch / expiry stamps."""

    value: Any
    fetched_at: float            # ``time.monotonic()`` at write time
    expires_at: float            # ``time.monotonic()`` past which entry is stale


class WeatherCache:
    """Single-flight + bounded LRU cache.

    Public methods:

    - :meth:`get_or_fetch` — return a cached value or call the loader.
      Returns ``(value, stale_seconds)`` where ``stale_seconds`` is the
      age of the value (0 for a fresh fetch) so the tool layer can
      surface "as of about N minutes ago" caveats.
    - :meth:`invalidate_prefix` — drop all entries whose key starts
      with a prefix. Used when a SEVERE alert publishes for a location
      and we want the next "is it raining now?" call to hit live data.
    - :meth:`make_key` — build a stable key for a request.

    Stale-on-failure is **not** implemented. When the loader raises,
    the error propagates and the (possibly-stale) cached value is NOT
    returned — honesty over best-effort.
    """

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._entries: collections.OrderedDict[str, _CacheEntry] = collections.OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._max_entries = max_entries

    @staticmethod
    def make_key(
        backend: str,
        method: str,
        location: GeoLocation,
        units: WeatherUnits,
        **kw: Any,
    ) -> str:
        """Build a stable cache key.

        Lat/lon are rounded to 4 decimals (~11 m) so close-but-not-
        identical callers share a cache slot. Open-Meteo grids at much
        coarser resolution anyway.

        ``backend`` is included in the key so a backend swap doesn't
        pollute the cache, and so the future multi-backend pattern
        (one ``WeatherService`` holding distinct backends per method)
        slots in without a cache-key change.
        """
        lat = round(location.latitude, 4)
        lon = round(location.longitude, 4)
        extra = ",".join(f"{k}={v}" for k, v in sorted(kw.items()))
        return f"{backend}:{method}:{lat},{lon}:{units.value}:{extra}"

    async def get_or_fetch(
        self,
        key: str,
        ttl_s: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, float]:
        """Return ``(value, stale_seconds)``.

        On a cache hit within TTL the cached entry is moved to the
        most-recently-used end and the elapsed monotonic seconds since
        write is returned as ``stale_seconds``.

        On a cache miss (or expired entry) a single-flight ``Future``
        coordinates concurrent callers — the second caller awaits the
        first caller's loader rather than firing a duplicate request.
        """
        now = time.monotonic()

        # Fast-path cache hit (lazy expiry)
        entry = self._entries.get(key)
        if entry is not None:
            if entry.expires_at > now:
                # Move to MRU position
                self._entries.move_to_end(key)
                stale = now - entry.fetched_at
                return entry.value, max(0.0, stale)
            # Expired — drop and re-fetch
            self._entries.pop(key, None)

        # Single-flight: if another caller is already loading this key,
        # await their future rather than firing a duplicate request.
        existing = self._inflight.get(key)
        if existing is not None:
            value = await existing
            return value, 0.0

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as exc:
            # Surface the failure to every awaiter; do NOT cache the
            # error or serve stale entries.
            if not future.done():
                future.set_exception(exc)
            self._inflight.pop(key, None)
            raise
        else:
            self._entries[key] = _CacheEntry(
                value=value,
                fetched_at=time.monotonic(),
                expires_at=time.monotonic() + ttl_s,
            )
            # LRU eviction
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            if not future.done():
                future.set_result(value)
            self._inflight.pop(key, None)
            return value, 0.0

    def invalidate_prefix(self, prefix: str) -> None:
        """Drop all entries whose key starts with ``prefix``.

        Used by ``WeatherService._poll_alerts`` to soft-bypass the
        current-conditions cache for a location after a SEVERE /
        EXTREME alert publishes. In-flight loaders are NOT cancelled —
        their resolved values won't be inserted because the key was
        already removed (a future ``get_or_fetch`` will start a fresh
        single-flight if it doesn't see the entry).
        """
        keys = [k for k in self._entries if k.startswith(prefix)]
        for k in keys:
            self._entries.pop(k, None)

    def clear(self) -> None:
        """Drop all entries. Does not touch in-flight loaders."""
        self._entries.clear()

    def size(self) -> int:
        """Return the current number of cached entries (for tests)."""
        return len(self._entries)

    def inflight_size(self) -> int:
        """Return the current number of single-flight futures (for tests)."""
        return len(self._inflight)

