#!/usr/bin/env python3
"""Scaffold a blank (or example) CV data tree (M5-5d CP5).

Thin CLI shim over cv_editor.scaffold — fulfills the `make init` hint
doctor.py has printed since M0. Scope is deliberately the DATA DIR ONLY:
it snapshots + rewrites `<data-dir>/*.yml` + the two committed sidecars,
but never touches `qc/` reports or `.cache/` — the editor's /reset route
does the full corpus-state job. Refuses a non-empty corpus without
--force (exit 1, wrote nothing).

Exit codes: 0 ok / 1 refused-wrote-nothing / 2 failed after the refusal
checks passed (the snapshot path, if one was taken, is printed; section
files MAY have been rewritten — a snapshot-phase failure writes nothing).

Usage:
  .venv/bin/python scripts/init_cv.py                 # blank tree in data/
  .venv/bin/python scripts/init_cv.py --example       # example corpus
  .venv/bin/python scripts/init_cv.py --force         # overwrite non-empty
  .venv/bin/python scripts/init_cv.py --data-dir DIR  # scaffold elsewhere
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cv_editor import scaffold  # noqa: E402
from filelock import FileLock, Timeout  # noqa: E402


def _probe_locks(data_dir: Path) -> str | None:
    """Return the name of a lock-held target file, or None when clear.

    Mirrors build_lock_check.py's role for build.sh: refuse up-front with
    a friendly message instead of dying mid-loop when the editor is
    mid-save. Non-blocking probe; tiny probe->write race is acceptable
    (single-user local tool)."""
    for name in list(scaffold.schemas.all_sections()):
        p = data_dir / f"{name}.yml"
        if not p.exists():
            continue
        try:
            lock = FileLock(str(p) + ".lock", timeout=0)
            with lock:
                pass
        except Timeout:
            return p.name
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=scaffold.DATA_DIR,
        help="target corpus dir (default: data/)",
    )
    ap.add_argument(
        "--example", action="store_true", help="seed the example corpus instead of a blank tree"
    )
    ap.add_argument(
        "--force", action="store_true", help="overwrite a non-empty corpus (snapshot taken first)"
    )
    args = ap.parse_args(argv)
    data_dir = args.data_dir

    if data_dir.exists() and not data_dir.is_dir():
        print(f"error: {data_dir} is not a directory", file=sys.stderr)
        return 1

    if not scaffold.corpus_is_empty(data_dir) and not args.force:
        print(
            f"refusing: {data_dir} holds CV data (or a file that failed to "
            "parse). Re-run with --force to snapshot + overwrite, or use the "
            "editor's /reset page for the full guarded flow.",
            file=sys.stderr,
        )
        return 1

    held = _probe_locks(data_dir)
    if held is not None:
        print(
            f"refusing: another writer (the editor?) holds the lock for "
            f"{held}. Close the editor's in-flight save and re-run.",
            file=sys.stderr,
        )
        return 1

    snap = None
    has_existing = data_dir.is_dir() and (
        any(data_dir.glob("*.yml"))
        or (data_dir / scaffold.CITATION_SNAPSHOT).exists()
        or (data_dir / scaffold.PUBMED_SIDECAR).exists()
    )
    # Only fold the REAL .cache citation file into the snapshot when
    # scaffolding the real corpus — a foreign --data-dir snapshot must not
    # embed the owner's citation cache (post-impl review).
    cache_dir = (
        scaffold.CACHE_DIR
        if data_dir.resolve() == scaffold.DATA_DIR.resolve()
        else data_dir / ".cache"
    )
    try:
        if has_existing:
            snap = scaffold.snapshot_tree(
                data_dir=data_dir, cache_dir=cache_dir, mode="example" if args.example else "blank"
            )
            print(f"snapshot: {snap}")
        writer = scaffold.example_tree if args.example else scaffold.blank_tree
        written = writer(data_dir)
        pubmed = data_dir / scaffold.PUBMED_SIDECAR
        if pubmed.exists():
            pubmed.unlink()
            written[scaffold.PUBMED_SIDECAR] = "deleted (snapshotted)"
    except Exception as exc:  # partial failure: report + distinct exit code
        print(f"FAILED mid-write: {exc}", file=sys.stderr)
        if snap is not None:
            print(f"pre-write snapshot preserved at: {snap}", file=sys.stderr)
        print("re-running init_cv.py --force completes the job.", file=sys.stderr)
        return 2

    for fname, what in sorted(written.items()):
        print(f"  {what:>9}  {fname}")
    kind = "example corpus" if args.example else "blank CV tree"
    print(f"ok: {kind} in {data_dir}")
    if data_dir == scaffold.DATA_DIR:
        print(
            "note: qc/ reports and .cache/ were not touched — the editor's "
            "/reset page handles those."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
