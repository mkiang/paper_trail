"""Regression test for the 2026-05-28 build-lock self-deadlock.

The editor's `build_runner._run` / `stream_subprocess` acquire
`output/cv.pdf.lock` and THEN spawn `./build.sh`. `build.sh`'s first
step is `scripts/build_lock_check.py`, which tries to acquire the
SAME lock with timeout=0 — and used to fail because the parent
process (the editor) already held it.

Fix: build_runner exports `CV_EDITOR_INTERNAL_BUILD=1` in the child
env; `build_lock_check.py` skips the probe when that env var is set.

This test pins both halves of the contract:
1. The probe honors the env var (skip path).
2. The probe still works when the env var is absent (CLI users running
   `./build.sh` while the editor is mid-build still get the abort).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from filelock import FileLock

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
LOCK_PATH = ROOT / "output" / "cv.pdf.lock"
PROBE = ROOT / "scripts" / "build_lock_check.py"


def _run_probe(extra_env: dict | None = None) -> int:
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    py = str(VENV_PY) if VENV_PY.exists() else "python3"
    return subprocess.run(
        [py, str(PROBE)],
        env=env,
        capture_output=True,
    ).returncode


def test_probe_passes_when_lock_is_free():
    assert _run_probe() == 0


def test_probe_fails_when_lock_held_by_external_process():
    """A real CLI build.sh racing with an editor build (the lock-
    cooperation case the probe was originally built for) should still
    abort. Hold the lock from this Python process; probe must exit 1."""
    LOCK_PATH.parent.mkdir(exist_ok=True)
    with FileLock(str(LOCK_PATH), timeout=0):
        rc = _run_probe()
    assert rc == 1, f"probe should refuse while lock is held; got {rc}"


def test_probe_skips_when_internal_build_env_set_even_with_lock_held():
    """The 2026-05-28 fix: when the editor invokes ./build.sh itself,
    it holds the lock AND sets CV_EDITOR_INTERNAL_BUILD=1. The probe
    must skip rather than self-deadlock."""
    LOCK_PATH.parent.mkdir(exist_ok=True)
    with FileLock(str(LOCK_PATH), timeout=0):
        rc = _run_probe({"CV_EDITOR_INTERNAL_BUILD": "1"})
    assert rc == 0, "probe should skip the lock check when CV_EDITOR_INTERNAL_BUILD=1; got rc={rc}"


def test_build_runner_sets_internal_build_env():
    """Code-level guard: build_runner._run + stream_subprocess both
    inject CV_EDITOR_INTERNAL_BUILD=1 into the subprocess env. Without
    this, the self-deadlock bug returns."""
    src = (ROOT / "scripts" / "cv_editor" / "build_runner.py").read_text()
    # Both helpers must set the env var. Two distinct injections is
    # the current shape; if a future refactor consolidates into one
    # spot, update this guard to match.
    occurrences = src.count("CV_EDITOR_INTERNAL_BUILD")
    assert occurrences >= 2, (
        f"build_runner.py must set CV_EDITOR_INTERNAL_BUILD in both "
        f"_run and stream_subprocess; found {occurrences} reference(s)"
    )
