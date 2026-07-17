"""
YAML round-trip with comment preservation, atomic writes, and backups.

Load: split header (leading docstring) from body, ruamel-RT load body,
return (header, data). Header lives in memory; data is a CommentedMap
or CommentedSeq tree.

Write: ruamel dump body, prepend header, write to .tmp, normalize the
.tmp via scripts/normalize_yaml_quotes.py, parse-verify with PyYAML,
then os.replace(tmp, target). Backups of the prior file go to
.cv_editor_backups/<file>.<time_ns>.bak (size-checked).

Restore: parse-verify backup before clobbering target.

Per-YAML filelock guards every write. Editor and CLI cooperate by
acquiring the same lock from build.sh.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml as pyyaml
from filelock import FileLock, Timeout
from ruamel.yaml import YAML

from cv_editor import paths

# ROOT/DATA/BACKUP_DIR stay REAL module attributes rather than call-time
# accessors: ~18 test sites do `monkeypatch.setattr(yaml_io, "BACKUP_DIR",
# tmp)` and internal writes MUST honor the override (a module body does not
# route bare-name lookups through a PEP-562 __getattr__). The refresh hook
# recomputes them from the active root on every paths.configure()/reset(),
# so they track the seam while staying ordinary, monkeypatch-able globals.
# (The normalizer is now invoked as `-m cv_editor.normalize_yaml_quotes`,
# P1-b, so no NORMALIZER path is captured here.)
ROOT = paths.data_root()  # typst/ (workspace root)
DATA = paths.data_dir()
BACKUP_DIR = paths.backup_dir()
BACKUP_RETAIN = 50

PY = sys.executable


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, DATA, BACKUP_DIR
    ROOT = paths.data_root()
    DATA = paths.data_dir()
    BACKUP_DIR = paths.backup_dir()


# ----- ruamel helpers -----


def _new_yaml() -> YAML:
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=2, offset=0)
    return rt


def split_header(text: str) -> tuple[str, str]:
    """Split YAML on the first non-comment, non-blank line. Returns (header, body)."""
    lines = text.splitlines(keepends=True)
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            cut = i
            break
    return "".join(lines[:cut]), "".join(lines[cut:])


def load(path: Path) -> tuple[str, object]:
    """Return (header, data). Data is a ruamel CommentedMap/Seq tree."""
    raw = path.read_text()
    header, body = split_header(raw)
    data = _new_yaml().load(body) if body.strip() else None
    return header, data


def _dump_body(data) -> str:
    rt = _new_yaml()
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


# ----- backups -----


def _backup_path(target: Path) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    return BACKUP_DIR / f"{target.name}.{time.time_ns()}.bak"


def _make_backup(target: Path) -> Path:
    """Copy target to .cv_editor_backups/, size-checked. Reviewer-1
    MEDIUM V5-D: bump the ns timestamp on the vanishingly-rare collision
    so a second backup can't overwrite the first."""
    raw = target.read_bytes()
    bk = _backup_path(target)
    while bk.exists():
        bk = _backup_path(target)
    bk.write_bytes(raw)
    if bk.stat().st_size != len(raw):
        raise IOError(f"Backup size mismatch: {bk}")
    return bk


def _prune_backups(name: str, keep: int = BACKUP_RETAIN) -> None:
    pat = re.compile(rf"^{re.escape(name)}\.\d+\.bak$")
    backups = sorted(
        (p for p in BACKUP_DIR.glob(f"{name}.*.bak") if pat.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[keep:]:
        old.unlink(missing_ok=True)


def list_backups(name: str) -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    pat = re.compile(rf"^{re.escape(name)}\.\d+\.bak$")
    return sorted(
        (p for p in BACKUP_DIR.glob(f"{name}.*.bak") if pat.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )


# ----- writes -----


def _lock_for(path: Path) -> FileLock:
    """Per-YAML lock. Editor and CLI share it (CLI acquires via flock(1) in build.sh)."""
    return FileLock(str(path) + ".lock", timeout=0)


class StaleFileError(Exception):
    """Raised when the on-disk mtime_ns no longer matches the value the form posted."""


class CorruptedShapeError(Exception):
    """Raised when a write would persist a known-bad data shape.

    Defensive guard added for task #30 (2026-05-25): an unidentified
    code path persisted `authors:` as a bare YAML string instead of a
    list, breaking `./build.sh` and ~50 tests. Static analysis of every
    `entry["authors"] = ...` call site shows only lists / CommentedSeq
    being written, yet the corruption reproduced. Until the root cause
    is found, refuse the write so the corruption can never reach disk."""


def _author_display_name(a) -> str:
    """Surface the name string from either dict-form or plain-string author."""
    if isinstance(a, dict):
        return str(a.get("name", ""))
    return str(a)


def _validate_publications_data(data) -> None:
    """Pre-write shape guard for `data/publications.yml` (task #30,
    2026-05-25; tightened 2026-05-26 for task-#30 recurrence).

    Rejects three corruption patterns:
      1. `authors:` is not a list (bare YAML string `authors: a; b; c; d`)
         — task #30 original case.
      2. `authors:` is a list of suspiciously short single-letter names
         (`[a, b, c, d]`) — the stale-browser-tab recurrence (task #30
         takes 2). When a tab loaded with the bug #1 state was saved,
         the form's char-iterated authors_json survived the strip
         pass as N one-char "names." Almost certainly a corruption
         pattern; legitimate single-letter authors don't exist.
      3. `authors:` is an empty list (existing validator should have
         caught this; defense in depth).

    Raises `CorruptedShapeError` on any of the above so the write is
    aborted before the tmp file is renamed.
    """
    if not isinstance(data, list):
        return
    for s_idx, sub in enumerate(data):
        if not isinstance(sub, dict):
            continue
        entries = sub.get("entries") or []
        if not isinstance(entries, list):
            continue
        for e_idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if "authors" not in entry:
                continue
            authors = entry.get("authors")
            title = str(entry.get("title", "(no title)"))[:80]
            if not isinstance(authors, list):
                raise CorruptedShapeError(
                    f"publications.yml entry [{s_idx}].entries[{e_idx}] "
                    f"has authors of type {type(authors).__name__} "
                    f"(value: {authors!r}), expected a list. "
                    f"Title: {title!r}. Refusing write to prevent the "
                    f"task-#30 corruption pattern from reaching disk."
                )
            if len(authors) == 0:
                raise CorruptedShapeError(
                    f"publications.yml entry [{s_idx}].entries[{e_idx}] "
                    f"has authors=[] (empty list). "
                    f"Title: {title!r}. Refusing write."
                )
            # Task #30 recurrence guard (2026-05-26): catch the
            # `[a, b, c, d]` shape produced by saving a stale browser
            # tab that rendered a bare-string-authors entry char-by-
            # char into the form. Trigger if ≥3 authors AND every
            # name is ≤2 chars — that's overwhelmingly the
            # corruption pattern, not legitimate data.
            names = [_author_display_name(a).strip() for a in authors]
            short_names = [n for n in names if len(n) <= 2]
            if len(authors) >= 3 and len(short_names) == len(authors):
                raise CorruptedShapeError(
                    f"publications.yml entry [{s_idx}].entries[{e_idx}] "
                    f"has authors that look like a corruption pattern: "
                    f"{names!r} (all ≤2 chars, ≥3 entries). "
                    f"Title: {title!r}. This typically means a stale "
                    f"browser tab was saved after the entry's YAML was "
                    f"already in the `authors: a; b; c; d` bare-string "
                    f"state. Refusing write to prevent further corruption."
                )


def write_with_backup(
    path: Path,
    header: str,
    data,
    expected_mtime_ns: int | None = None,
    *,
    new_header: str | None = None,
) -> Path:
    """Atomic-tmp write with backup, normalization, parse-verification.

    Args:
        path: target YAML file. MUST already exist — this is never a
            creation path (the backup + header re-read both need the
            current file); use write_new() to create a file.
        header: leading docstring text. NOTE: on this write path the
            argument is effectively IGNORED — the header is re-read from
            disk inside the lock (see the V3-H comment below) so a
            concurrent hand-edit of the docstring isn't reverted. Kept in
            the signature for call-site symmetry with load(); do NOT
            "simplify" by merging it with new_header — their semantics
            are opposite (header = advisory, new_header = authoritative).
        data: ruamel-shaped data tree to dump as the body.
        expected_mtime_ns: if not None, check the file's mtime_ns
            matches before writing; raise StaleFileError if not.
        new_header: if not None, REPLACE the on-disk header with this
            text instead of preserving it (M5-5d: reset-to-example/blank
            rewrites section headers to the canonical example docs).
            Default None is byte-identical to the pre-M5-5d behavior.

    Returns:
        Path to the backup file.

    Raises:
        StaleFileError: if expected_mtime_ns mismatches.
        Timeout: if another writer holds the lock.
        CalledProcessError: if the normalizer rejects the result.
        yaml.YAMLError: if parse-verify fails.
        IOError: if backup fails its size check.
    """
    # Task #30 (2026-05-25): pre-write shape guard for publications.yml.
    # Runs OUTSIDE the lock so a malformed in-memory tree never reaches
    # the tmp file. The guard is publications-only; no other section has
    # the same author-shape invariant.
    if path.name == "publications.yml":
        _validate_publications_data(data)
    try:
        with _lock_for(path):
            if expected_mtime_ns is not None:
                actual = path.stat().st_mtime_ns
                if actual != expected_mtime_ns:
                    raise StaleFileError(
                        f"{path.name} changed under us: "
                        f"expected mtime_ns={expected_mtime_ns}, got {actual}"
                    )
            # Re-read header from disk inside the lock so a concurrent edit
            # of the leading docstring (e.g., a hand-edit during a long
            # editor session) isn't reverted on save. Body comes from the
            # in-memory ruamel tree. new_header (M5-5d) overrides — still
            # inside the lock, all other guarantees unchanged.
            if new_header is not None:
                current_header = new_header
            else:
                current_header, _ = split_header(path.read_text())
            backup = _make_backup(path)
            new_body = _dump_body(data) if data is not None else ""
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(current_header + new_body)
                    f.flush()
                    os.fsync(f.fileno())
                # T4.1: cap the normalizer at 60s so a hung subprocess
                # can't hold the filelock forever. TimeoutExpired falls
                # through to the bare `except Exception:` cleanup below
                # which unlinks tmp and releases the lock.
                subprocess.run(
                    [PY, "-m", "cv_editor.normalize_yaml_quotes", str(tmp)],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                pyyaml.safe_load(tmp.read_text())  # parse-verify
                os.replace(tmp, path)
                # fsync the directory so the rename itself is durable.
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            _prune_backups(path.name)
            return backup
    except Timeout as e:
        raise Timeout(f"another writer holds the lock for {path.name}") from e


def write_new(path: Path, header: str, data) -> None:
    """Atomically CREATE a YAML file (M5-5d scaffold path).

    The creation counterpart to write_with_backup: same atomic-tmp +
    normalize + parse-verify + os.replace + dir-fsync pipeline, but it
    REFUSES an existing target (overwrites go through write_with_backup so
    they get the backup + header-preservation + mtime guarantees). No
    backup is made — there is nothing to back up. YAML ONLY: the
    normalizer subprocess + comment header would corrupt strict JSON
    (sidecars go through cv_editor.atomic_json instead).

    Runs the publications authors-shape guard for publications.yml, same
    as write_with_backup (gotcha #58).

    Raises:
        FileExistsError: if path already exists.
        Timeout: if another writer holds the lock.
        CalledProcessError / yaml.YAMLError: normalizer / parse-verify.
    """
    if path.suffix != ".yml":
        raise ValueError(f"write_new is YAML-only, got {path.name}")
    if path.name == "publications.yml":
        _validate_publications_data(data)
    try:
        with _lock_for(path):
            if path.exists():
                raise FileExistsError(f"{path} already exists — use write_with_backup to overwrite")
            path.parent.mkdir(parents=True, exist_ok=True)
            new_body = _dump_body(data) if data is not None else ""
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(header + new_body)
                    f.flush()
                    os.fsync(f.fileno())
                subprocess.run(
                    [PY, "-m", "cv_editor.normalize_yaml_quotes", str(tmp)],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                pyyaml.safe_load(tmp.read_text())  # parse-verify
                os.replace(tmp, path)
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
    except Timeout as e:
        raise Timeout(f"another writer holds the lock for {path.name}") from e


def restore_from_backup(
    backup_path: Path,
    target_path: Path,
    *,
    expected_mtime_ns: int | None = None,
) -> None:
    """Restore target from a backup with atomic-tmp + fsync. Parse-verifies
    the backup first; takes a pre-restore backup of the current good file
    so the restore itself is reversible.

    Args:
        expected_mtime_ns: if not None, check the file's mtime_ns matches
            BEFORE replacing it. Raises StaleFileError on mismatch — same
            mtime guarantee write_with_backup provides. Without this guard,
            another tab's save between page-render and click-Undo gets
            silently clobbered by the older backup.
    """
    raw = backup_path.read_bytes()
    if len(raw) == 0:
        raise IOError(f"Backup is empty: {backup_path}")
    pyyaml.safe_load(raw)  # raises yaml.YAMLError if unparseable
    with _lock_for(target_path):
        if expected_mtime_ns is not None and target_path.exists():
            actual = target_path.stat().st_mtime_ns
            if actual != expected_mtime_ns:
                raise StaleFileError(
                    f"{target_path.name} changed under us: "
                    f"expected mtime_ns={expected_mtime_ns}, got {actual}"
                )
        _make_backup(target_path)  # snapshot the current good file
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target_path)
            dir_fd = os.open(str(target_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        _prune_backups(target_path.name)


def mtime_ns(path: Path) -> int:
    """Cheap accessor used to populate hidden form fields."""
    return path.stat().st_mtime_ns
