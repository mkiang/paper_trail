"""Versioned JSON sidecar load helper (V20, 2026-05-18).

Symmetric counterpart to `cv_editor.atomic_json.atomic_write_json`. The
write side was unified at V14; this is the long-deferred load side
(V13-V19-D R1-H3). Four callers had three different corrupt/version-
mismatch behaviors before this extraction:

    TrackerCache._load         → warn + return empty
    CitationCache._load_from_disk → rename-on-corrupt + return empty
    load_snapshot              → silent-return-empty
    pubmed_sync.load_sidecar   → warn + return empty

The unified helper handles three of those uniformly (`silent=` covers
the snapshot caller). CitationCache keeps its rename policy at the
caller boundary, wrapping a call to the helper.

Design notes:

* Missing file is ALWAYS silent — first-run is the normal case for
  every sidecar (gitignored caches, snapshots regenerated on demand).
* Corrupt JSON and version mismatch warn to stderr when
  `silent=False`. The `component_name` prefix matches the
  pre-extraction stderr prefixes (`[altmetric_tracker_cache]`,
  `[sync]`, etc.).
* Returns `dict | None`. None means caller should use empty initial
  state; never raises on recoverable problems.
* Raises `OSError` only on a real disk/permission failure for an
  existing file (the path exists but can't be read). Callers that
  need to swallow this wrap with their own try/except — see
  `CitationCache._load_from_disk` for the rename pattern.

OSError handling is INTENTIONALLY asymmetric across the four
callers — record what each does so future-you can navigate the
divergence without re-deriving it:

  | Caller                          | OSError policy                |
  |---------------------------------|-------------------------------|
  | `TrackerCache._load`            | Catches → stderr warn + empty |
  | `CitationCache._load_from_disk` | Catches → silent + empty      |
  | `citation_counts.load_snapshot` | Catches → silent + empty      |
  | `pubmed_sync.load_sidecar`      | Propagates to caller          |

The propagate-to-caller pattern in `pubmed_sync` is load-bearing:
the CLI's main() wraps with its own error reporting, and the editor
route uses a separate `except Exception` to flash. Don't wrap OSError
inside `load_versioned` itself — leave the per-caller policy intact.

(The 5-line "if raw is None: return empty" suffix at every call site
LOOKS like duplication ripe for a second helper layer. Resist —
each caller's "rest" (CacheEntry build vs typed-dataclass unpack vs
counts-dict normalization vs rename-on-corrupt sibling) is genuinely
caller-specific. A factory would need callbacks for each unpacker —
net code growth.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_versioned(
    path: Path,
    expected_version: int,
    *,
    component_name: str | None = None,
    silent: bool = False,
) -> dict | None:
    """Load and validate a versioned JSON sidecar.

    Returns the parsed body dict on success, None on (a) missing file,
    (b) corrupt JSON, or (c) version mismatch. Stderr-warns on (b)/(c)
    unless `silent=True`. Missing-file is always silent.

    Raises OSError only on a real read failure for an existing path
    (permission denied, I/O error). JSON-decode errors and
    type-shape errors are caught and reported via stderr.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        if not silent:
            prefix = f"[{component_name}] " if component_name else ""
            print(
                f"{prefix}WARNING: {p.name} is corrupted ({e}); starting from empty state.",
                file=sys.stderr,
            )
        return None
    if not isinstance(raw, dict):
        if not silent:
            prefix = f"[{component_name}] " if component_name else ""
            print(
                f"{prefix}WARNING: {p.name} did not parse to a JSON "
                f"object (got {type(raw).__name__}); starting from "
                f"empty state.",
                file=sys.stderr,
            )
        return None
    on_disk_version = raw.get("version")
    if on_disk_version != expected_version:
        if not silent:
            prefix = f"[{component_name}] " if component_name else ""
            print(
                f"{prefix}WARNING: {p.name} version "
                f"{on_disk_version!r} does not match expected "
                f"{expected_version}; starting from empty state.",
                file=sys.stderr,
            )
        return None
    return raw
