"""Persistent cache for Altmetric tracker URL resolutions (V13 finish).

Backs the per-row Resolve, per-entry "Resolve all on entry", and global
"Resolve all trackers" sweeps. The cache records every attempt
(resolved + failed alike) so subsequent fetches never re-hit a known
result.

Storage shape mirrors `pubmed_sync.py` — a single JSON sidecar with a
`version` key + atomic write (tmp + fsync + os.replace + dir fsync).
Lives in `.cache/altmetric/trackers.json` (regenerable derived state,
gitignored).

Retry rules (per ResolveResult.status):
    resolved   never re-attempt (default); re-attempt only when caller
               passes verify=True AND the entry is older than
               RESOLVED_TTL_DAYS (V20 D3 verify-resolved sweep).
    failed_*   ALWAYS re-attempt — `should_attempt()` returns True for
               any non-resolved entry (2026-05-25, Stage B / I9 — user
               direction: "drop the failure TTL entirely; every Resolve
               click should re-attempt").

               Failures ARE persisted (2026-06-08), with `attempt_count`
               + `last_attempt_ts`, so the Trackers page can show how
               many times a URL was tried and when. This is independent
               of the always-re-attempt rule above (the metadata is for
               display; should_attempt ignores it for non-resolved
               entries). Failures are never SERVED as a result — the
               resolver short-circuits only on fresh resolved entries.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

CACHE_VERSION = 1

ResolveStrategy = Literal["head", "get", "meta_refresh", "unshorten_me"]
ResolveStatus = Literal[
    "resolved",
    "failed_rate_limit",
    "failed_network",
    "failed_no_redirect",
]


@dataclass
class ResolveResult:
    """Outcome of one resolve attempt against a tracker URL.

    `strategy` is the strategy that produced `final_url` (only set on
    success); `error` is a short human-readable reason on failure.
    `from_cache` is True when `resolve_tracker_url_with_cache` short-
    circuited on a cached resolved hit without making any HTTP call
    (2026-05-25, Stage B / I9 — drives the SSE message format
    distinguishing "kept [resolved]" from "resolved [<strategy>]").
    """

    final_url: str | None = None
    strategy: ResolveStrategy | None = None
    status: ResolveStatus = "failed_network"
    error: str | None = None
    from_cache: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved" and bool(self.final_url)


@dataclass
class CacheEntry:
    final_url: str | None
    strategy: ResolveStrategy | None
    status: ResolveStatus
    first_seen_ts: str
    last_attempt_ts: str
    attempt_count: int
    error: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> "CacheEntry":
        return cls(
            final_url=raw.get("final_url"),
            strategy=raw.get("strategy"),
            status=raw.get("status") or "failed_network",
            first_seen_ts=raw.get("first_seen_ts") or "",
            last_attempt_ts=raw.get("last_attempt_ts") or "",
            attempt_count=int(raw.get("attempt_count") or 1),
            error=raw.get("error"),
        )

    def to_result(self) -> ResolveResult:
        return ResolveResult(
            final_url=self.final_url,
            strategy=self.strategy,
            status=self.status,
            error=self.error,
        )


# V20 (2026-05-18 — D3, R2-H4 fix): TTL for `resolved` entries when the
# caller passes `verify=True`. Default behavior (verify=False) preserves
# the pre-V20 invariant — resolved entries never re-attempt. Verify mode
# is invoked only from explicit Trackers-page sweeps, never page-render.
# 30 days mirrors `verify_urls.URL_CACHE_TTL_DAYS`. Journalism URLs may
# rot faster (press-release retirements, newsroom URL scheme changes) —
# tune via this constant if the first URL-rot incident lands.
RESOLVED_TTL_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class TrackerCache:
    """JSON-sidecar cache for resolved tracker URLs.

    Concurrency: the editor is single-user / single-process; a filelock
    would be over-engineering. Two near-simultaneous saves are still
    safe because of os.replace atomicity — last-writer-wins is fine
    here (resolutions are idempotent).
    """

    DEFAULT_PATH = Path(".cache/altmetric/trackers.json")

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else self.DEFAULT_PATH
        self._entries: dict[str, CacheEntry] = {}
        self._load()

    # ----- IO -----

    def _load(self) -> None:
        from cv_editor.versioned_json import load_versioned

        # V20 (2026-05-18): delegate to shared load_versioned. Same
        # behavior — corrupt/version-mismatch warns to stderr with the
        # `[altmetric_tracker_cache]` prefix, missing file is silent.
        # Catch OSError here to preserve pre-extraction tolerance:
        # the cache file lives in .cache/altmetric/ which might be
        # missing, locked, or permission-denied in test fixtures.
        try:
            raw = load_versioned(
                self.path,
                CACHE_VERSION,
                component_name="altmetric_tracker_cache",
            )
        except OSError as e:
            print(
                f"[altmetric_tracker_cache] WARNING: {self.path} could "
                f"not be read ({e}); starting from empty state.",
                file=sys.stderr,
            )
            self._entries = {}
            return
        if raw is None:
            self._entries = {}
            return
        trackers = raw.get("trackers") or {}
        self._entries = {url: CacheEntry.from_json(rec) for url, rec in trackers.items()}

    def save(self) -> None:
        """Atomic write via cv_editor.atomic_json (V14 extraction, 2026-05-17)."""
        from cv_editor.atomic_json import atomic_write_json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "version": CACHE_VERSION,
                "trackers": {url: e.to_json() for url, e in self._entries.items()},
            },
        )

    # ----- access -----

    def get(self, url: str) -> CacheEntry | None:
        return self._entries.get(url)

    def get_result(self, url: str) -> ResolveResult | None:
        e = self._entries.get(url)
        return e.to_result() if e else None

    def record(self, url: str, result: ResolveResult) -> CacheEntry:
        """Record a resolution attempt — resolved OR failed.

        Every attempt is persisted with an incremented `attempt_count`
        and a fresh `last_attempt_ts` so the Trackers page can show how
        many times a URL was tried and when (2026-06-08 — fixes the
        always-blank "Last tried" / always-0 "Attempts" columns; failed
        attempts used to be evicted, so unresolved rows — the only ones
        the page lists — never had attempt metadata).

        Persisting failures does NOT make them a re-attempt barrier: the
        2026-05-25 / I9 intent ("every Resolve click re-attempts") is
        preserved because `should_attempt()` returns True for any
        non-resolved entry. A later success overwrites the failed entry
        and carries the accumulated `attempt_count` forward.

        Returns the persisted `CacheEntry` (always — never None now).
        """
        existing = self._entries.get(url)
        now = _now_iso()
        if existing is None:
            first_seen = now
            attempt = 1
        else:
            first_seen = existing.first_seen_ts or now
            attempt = existing.attempt_count + 1
        resolved = result.is_resolved
        entry = CacheEntry(
            final_url=result.final_url if resolved else None,
            strategy=result.strategy if resolved else None,
            status=result.status,
            first_seen_ts=first_seen,
            last_attempt_ts=now,
            attempt_count=attempt,
            error=None if resolved else result.error,
        )
        self._entries[url] = entry
        return entry

    def should_attempt(
        self,
        url: str,
        *,
        force: bool = False,
        verify: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Decide whether the URL is eligible for a (re-)attempt.

        Returns True for anything that is NOT a fresh resolved entry.
        Specifically:
          - force=True → always True.
          - Cache miss → True.
          - Non-resolved (failed_*) entry → True. Failures are persisted
            (2026-06-08) for their attempt metadata, but they never gate
            a re-attempt: every Resolve click re-attempts the network
            (Stage B / I9, 2026-05-25). record() then overwrites the
            failed entry with the fresh outcome + bumped attempt_count.
          - Resolved entry + verify=False → False (page-render path).
          - Resolved entry + verify=True → False if within
            RESOLVED_TTL_DAYS, True if stale (V20 D3 verify sweep).
        """
        if force:
            return True
        e = self._entries.get(url)
        if e is None or e.status != "resolved":
            return True
        if not verify:
            return False
        # verify=True: re-probe resolved entries past TTL (V20 D3).
        ref = now or datetime.now(timezone.utc)
        last = _parse_iso(e.last_attempt_ts)
        if last is None:
            return True
        return (ref - last) >= timedelta(days=RESOLVED_TTL_DAYS)

    def stale_resolved(
        self,
        *,
        now: datetime | None = None,
    ) -> Iterator[tuple[str, CacheEntry]]:
        """Yield (url, entry) for `resolved` entries past RESOLVED_TTL_DAYS.

        Used by the Trackers page "Verify resolved" sweep — callers
        iterate, HEAD-probe each, and update via `record()` /
        `touch_resolved()`. Pure read; no side effects.
        """
        ref = now or datetime.now(timezone.utc)
        cutoff = ref - timedelta(days=RESOLVED_TTL_DAYS)
        for url, e in self._entries.items():
            if e.status != "resolved":
                continue
            last = _parse_iso(e.last_attempt_ts)
            if last is None or last < cutoff:
                yield url, e

    def touch_resolved(self, url: str) -> None:
        """Refresh `last_attempt_ts` on a resolved entry. Used after a
        successful re-verification HEAD probe (URL still works; reset
        the TTL clock without minting a new CacheEntry from scratch).
        """
        e = self._entries.get(url)
        if e is None or e.status != "resolved":
            return
        e.last_attempt_ts = _now_iso()

    def iter_unresolved(self) -> Iterator[tuple[str, CacheEntry]]:
        """Yield (url, entry) pairs for every non-resolved tracker we know about."""
        for url, e in self._entries.items():
            if e.status != "resolved":
                yield url, e

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {
            "total": 0,
            "resolved": 0,
            "failed_network": 0,
            "failed_rate_limit": 0,
            "failed_no_redirect": 0,
        }
        for e in self._entries.values():
            out["total"] += 1
            key = e.status if e.status in out else "failed_network"
            out[key] += 1
        return out

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, url: object) -> bool:
        return isinstance(url, str) and url in self._entries
