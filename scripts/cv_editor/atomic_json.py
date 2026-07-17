"""Atomic JSON write (V14 extraction, 2026-05-17).

Consolidates three previously-separate implementations:
- `scripts/pubmed_sync.py:save_sidecar`
- `scripts/cv_editor/altmetric_tracker_cache.py:TrackerCache.save`
- `scripts/fetch_citation_counts.py` (NEW; via `cv_editor.citation_counts`)

Pattern (V3-H + R7-H3 hardening preserved):
1. Dump JSON to a `.tmp` sibling.
2. fsync(tmp) so disk has bytes.
3. (Default) Parse tmp back to verify round-trip; abort on parse failure.
4. os.replace(tmp, target) — atomic on POSIX.
5. fsync(parent dir) so the directory entry persists.
6. On any failure mid-stream, unlink the .tmp orphan.

API:

    from cv_editor.atomic_json import atomic_write_json
    atomic_write_json(path, {"version": 1, "data": {...}})

The caller is responsible for dataclass-to-dict conversion (e.g.,
`asdict(state)`). The function expects a JSON-serializable dict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _dumps(data: Any) -> str:
    """Indirection point so tests can monkeypatch it."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    verify_load: bool = True,
) -> None:
    """Write `data` to `path` atomically.

    Args:
        path: destination file. Parent directory must exist.
        data: JSON-serializable object.
        verify_load: if True (default), parse the tmp file back as JSON
            before replacing the target. Catches corrupt encoding bugs.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        body = _dumps(data)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        if verify_load:
            with open(tmp, "r", encoding="utf-8") as fh:
                json.loads(fh.read())  # raises on corrupt JSON
        os.replace(tmp, path)
        # fsync the parent directory so the rename hits disk.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        # R7-H3: orphan-unlink on any failure mid-stream.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
