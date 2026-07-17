"""Per-host request throttle (V14 extraction, 2026-05-17).

Consolidates three previously-separate implementations:
- `scripts/verify_urls.py:_polite` (module-level dict + lock; thread-pool callers)
- `scripts/pubmed_sync.py:HostThrottle` (instance-level; single-threaded)
- `scripts/fetch_citation_counts.py` (NEW; thread-pool callers)

API:

    throttle = HostThrottle(
        gap_per_host={"api.crossref.org": 0.25},
        default_gap=0.1,
    )
    throttle.wait("api.crossref.org")  # blocks if last call was < 0.25s ago

The class is **always thread-safe** (uncontended `threading.Lock.acquire()`
is sub-microsecond — no reason to gate on a `use_lock` knob). Per-host
locks mean different hosts don't contend.

Per the V14 critique (R1-H4 2026-05-17): a single class with always-on
locking is the right shape. Distinguishing thread-pooled vs single-
threaded callers via a constructor flag is leaking implementation detail;
the cost of always locking is effectively zero.
"""

from __future__ import annotations

import threading
import time


class HostThrottle:
    """Sleep at least the per-host gap between calls to the same host.

    Different hosts do not block each other. Per-host gap defaults to
    ``default_gap``; per-host overrides via ``gap_per_host``.

    Thread-safe. Multiple threads sharing one instance see the same
    per-host last-call timestamps, so a 4-worker thread pool hitting
    one rate-limited host serializes correctly.
    """

    def __init__(
        self,
        *,
        gap_per_host: dict[str, float] | None = None,
        default_gap: float = 0.1,
    ) -> None:
        self._gap_per_host = dict(gap_per_host) if gap_per_host else {}
        self._default_gap = float(default_gap)
        self._meta_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}

    def gap_for(self, host: str) -> float:
        return self._gap_per_host.get(host, self._default_gap)

    def wait(self, host: str) -> None:
        gap = self.gap_for(host)
        with self._meta_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            now = time.monotonic()
            last = self._host_last.get(host)
            if last is not None:
                delay = gap - (now - last)
                if delay > 0:
                    time.sleep(delay)
            self._host_last[host] = time.monotonic()
