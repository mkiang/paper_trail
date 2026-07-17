"""V14 citation-count cache (sidecar) + snapshot (V14, 2026-05-17).

Two files, two roles:

- **Sidecar** (`.cache/citation_counts.json`, gitignored): full state per
  DOI — count, source, status, timestamps, attempt count, error. The
  fetcher's source of truth. `should_attempt(doi)` enforces retry rules.
- **Snapshot** (`data/citation_counts.json`, committed): minimal
  `{doi: count}` map plus `generated_at` + per-DOI `fetched_at`. The
  Typst renderer reads this. User commits when satisfied.

Both files are versioned (`version: 1`); writes are atomic via
`cv_editor.atomic_json.atomic_write_json`.

DOI keys are normalized to **lowercase** in BOTH files (per critique R1-H3:
Crossref returns lowercase; YAML may have mixed case). Renderer must
look up via `lower(e.doi)`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from cv_editor.atomic_json import atomic_write_json

CACHE_VERSION = 1
SNAPSHOT_VERSION = 1

# V13-V19-D R3-L4 (2026-05-18): explicit constant for the snapshot-drift
# threshold. 60s is a buffer covering wall-clock jitter between the sidecar
# and snapshot atomic writes (which use os.replace; mtime resolution on
# common filesystems is ~1s) plus an allowance for the fetcher batching
# many DOIs before the snapshot regeneration step. Anything tighter would
# false-positive on every fetch.
SNAPSHOT_DRIFT_THRESHOLD_SECONDS = 60


class CountStatus(str, Enum):
    FETCHED = "fetched"
    FAILED_NETWORK = "failed_network"
    FAILED_NOT_FOUND = "failed_not_found"
    FAILED_RATE_LIMIT = "failed_rate_limit"
    FAILED_OTHER = "failed_other"


# Retry rules: how long after the last attempt before we try again.
_TTL_BY_STATUS: dict[str, timedelta | None] = {
    CountStatus.FETCHED: timedelta(days=30),
    CountStatus.FAILED_NETWORK: timedelta(hours=1),
    CountStatus.FAILED_NOT_FOUND: timedelta(days=7),
    CountStatus.FAILED_RATE_LIMIT: timedelta(hours=24),
    # V13-V19-D R2-M5 (2026-05-18): FAILED_OTHER was force-only, which
    # turned transient Crossref weirdness (Cloudflare WAF challenge page,
    # 200-with-empty-body, JSON parse anomaly) into a permanent failure
    # until the user clicked the Force button. 7 days mirrors
    # FAILED_NOT_FOUND — terminal-looking errors get the same patience.
    # True permanent failure (e.g., DOI confirmed invalid by Crossref's
    # 404) is already FAILED_NOT_FOUND.
    CountStatus.FAILED_OTHER: timedelta(days=7),
}


@dataclass
class CountEntry:
    """One row in the sidecar cache."""

    count: Optional[int]
    source: Optional[str]
    status: str
    fetched_at: Optional[str]
    first_seen_at: str
    attempt_count: int
    error: Optional[str] = None

    @classmethod
    def from_json(cls, d: dict) -> "CountEntry":
        return cls(
            count=d.get("count"),
            source=d.get("source"),
            status=d.get("status", CountStatus.FAILED_OTHER),
            fetched_at=d.get("fetched_at"),
            first_seen_at=d.get("first_seen_at", _now_iso()),
            attempt_count=int(d.get("attempt_count", 0)),
            error=d.get("error"),
        )

    def to_json(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _doi_key(doi: str) -> str:
    """Canonical lowercase key (R1-H3)."""
    return doi.strip().lower()


@dataclass
class CitationCache:
    """Sidecar JSON store. Always-on-disk; load + save are explicit."""

    path: Path
    _entries: dict[str, CountEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "CitationCache":
        c = cls(path=Path(path))
        if c.path.exists():
            c._load_from_disk()
        return c

    def _load_from_disk(self) -> None:
        # V20 (2026-05-18): delegate version-check + JSON-shape to
        # cv_editor.versioned_json; layer the rename-on-corrupt policy
        # on top of None returns. The helper warns to stderr on
        # corrupt JSON or version mismatch; this caller historically
        # was silent on those, so we suppress stderr via silent=True.
        # The rename-to-.corrupt-<ts> sibling preserves the post-mortem
        # value of a broken cache file.
        from cv_editor.versioned_json import load_versioned

        try:
            body = load_versioned(
                self.path,
                CACHE_VERSION,
                component_name="citation_counts",
                silent=True,
            )
        except OSError:
            return
        if body is None:
            # Stash a `.corrupt-<ts>` sibling so post-mortem is possible,
            # then start fresh. Version mismatches also land here.
            if self.path.exists():
                backup = self.path.with_suffix(f".corrupt-{_now_iso()}.json")
                try:
                    self.path.rename(backup)
                except OSError:
                    pass
            return
        for doi, rec in (body.get("counts") or {}).items():
            self._entries[doi] = CountEntry.from_json(rec)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "version": CACHE_VERSION,
                "counts": {k: e.to_json() for k, e in self._entries.items()},
            },
        )

    # ----- access -----

    def get(self, doi: str) -> Optional[CountEntry]:
        return self._entries.get(_doi_key(doi))

    def all(self) -> dict[str, CountEntry]:
        return dict(self._entries)

    def should_attempt(self, doi: str, *, force: bool = False) -> bool:
        if force:
            return True
        e = self.get(doi)
        if e is None:
            return True
        ttl = _TTL_BY_STATUS.get(e.status)
        if ttl is None:
            return False
        last = _parse_iso(e.fetched_at) or _parse_iso(e.first_seen_at)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last >= ttl

    def record(
        self,
        doi: str,
        *,
        count: Optional[int],
        source: Optional[str],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Record an attempt for `doi`. Preserves last-known-good `count`
        and `source` when the new attempt FAILED — a transient 500 must
        not destroy a previously-good 42 (post-impl R1-H1, 2026-05-17)."""
        k = _doi_key(doi)
        existing = self._entries.get(k)
        now = _now_iso()
        # Preserve prior count when the new attempt failed and we have one.
        preserve = (
            status != CountStatus.FETCHED and existing is not None and existing.count is not None
        )
        kept_count = existing.count if preserve else count
        kept_source = existing.source if preserve else source
        if existing is None:
            self._entries[k] = CountEntry(
                count=kept_count,
                source=kept_source,
                status=status,
                fetched_at=now,
                first_seen_at=now,
                attempt_count=1,
                error=error,
            )
        else:
            self._entries[k] = CountEntry(
                count=kept_count,
                source=kept_source,
                status=status,
                fetched_at=now,
                first_seen_at=existing.first_seen_at,
                attempt_count=existing.attempt_count + 1,
                error=error,
            )

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out


def write_snapshot(
    cache: CitationCache,
    snapshot_path: Path,
    *,
    valid_dois: Optional[set[str]] = None,
) -> dict:
    """Derive the committed `data/citation_counts.json` from the sidecar.

    Snapshot includes only `status == FETCHED` entries with `count > 0`.
    Keys are already lowercase (sidecar enforces this). Each entry carries
    `fetched_at` so Machine B can show "Last fetched <date>" without the
    sidecar (per critique R2-L1).

    V13-V19-D R2-M1 / R2-M8 (2026-05-18): when `valid_dois` is provided,
    filter the snapshot to that set. Lets the fetcher and the
    `/citations/snapshot` route prune orphan DOIs (preprint→published
    swaps, typo fixes, deletions) from the committed file. The sidecar
    keeps full history for diagnostics; only the snapshot is gated.
    Callers MUST pass lowercase-canonical DOIs (use `_doi_key`).
    """
    counts: dict[str, dict] = {}
    for doi, e in cache.all().items():
        if e.status != CountStatus.FETCHED or e.count is None or e.count <= 0:
            continue
        if valid_dois is not None and doi not in valid_dois:
            continue
        counts[doi] = {"count": e.count, "fetched_at": e.fetched_at}
    body = {
        "version": SNAPSHOT_VERSION,
        "generated_at": _now_iso(),
        "counts": counts,
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(snapshot_path, body)
    return body


def load_snapshot(snapshot_path: Path) -> dict:
    """Read the committed snapshot. Returns the body dict (counts may be empty).

    V13-V19-D R2-M3 (2026-05-18): defensively normalize all DOI keys to
    lowercase on read. The fetcher already writes lowercase keys, but a
    hand-edited or externally-generated snapshot with mixed case would
    silently miss renderer lookups (`lower(e.doi)`). Emits one stderr
    warning if any key required normalization.
    """
    empty = {"version": SNAPSHOT_VERSION, "generated_at": None, "counts": {}}
    # V20 (2026-05-18): silent=True preserves the pre-extraction
    # quiet-on-corrupt behavior (snapshots are committed; warnings on
    # every renderer build would be noise).
    from cv_editor.versioned_json import load_versioned

    try:
        body = load_versioned(
            snapshot_path,
            SNAPSHOT_VERSION,
            component_name="citation_counts",
            silent=True,
        )
    except OSError:
        return empty
    if body is None:
        return empty
    counts = body.get("counts") or {}
    if isinstance(counts, dict):
        normalized: dict[str, dict] = {}
        needed_norm: list[str] = []
        for k, v in counts.items():
            lk = k.lower() if isinstance(k, str) else k
            if isinstance(k, str) and lk != k:
                needed_norm.append(k)
            normalized[lk] = v
        if needed_norm:
            import sys as _sys

            _sys.stderr.write(
                f"[citation_counts] WARNING: snapshot {snapshot_path.name} "
                f"had {len(needed_norm)} non-lowercase DOI key(s); "
                f"normalized on load (e.g. {needed_norm[0]!r}). "
                f"Re-save to fix the file.\n"
            )
        body["counts"] = normalized
    return body


def snapshot_drift(
    cache: CitationCache,
    snapshot_path: Path,
    *,
    valid_dois: Optional[set[str]] = None,
) -> dict:
    """Compare sidecar vs snapshot state (R2-H2).

    Returns a dict with mtime fields + drift_seconds + count_delta so the
    editor can warn when the snapshot is stale.

    V13-V19-D tail R1-M5 (2026-05-18): `valid_dois` (if provided) filters
    `sidecar_count` to match the post-F7 write_snapshot scoping. Without
    this, any cv with orphan DOIs in the sidecar would have
    sidecar_count > snapshot_count permanently → `stale: True` on every
    refresh → false alarm. Callers that pass `valid_dois` to
    `write_snapshot` should pass the same set here.
    """
    sidecar_mtime = cache.path.stat().st_mtime if cache.path.exists() else 0
    snap_mtime = snapshot_path.stat().st_mtime if snapshot_path.exists() else 0
    snap_body = load_snapshot(snapshot_path)
    sidecar_fetched_count = sum(
        1
        for doi, e in cache.all().items()
        if e.status == CountStatus.FETCHED
        and e.count
        and e.count > 0
        and (valid_dois is None or doi in valid_dois)
    )
    return {
        "sidecar_mtime": sidecar_mtime,
        "snapshot_mtime": snap_mtime,
        "drift_seconds": max(0, sidecar_mtime - snap_mtime),
        "sidecar_count": sidecar_fetched_count,
        "snapshot_count": len(snap_body.get("counts") or {}),
        "stale": (
            (sidecar_mtime - snap_mtime) > SNAPSHOT_DRIFT_THRESHOLD_SECONDS
            and sidecar_fetched_count > 0
        ),
    }
