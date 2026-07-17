#!/usr/bin/env python3
"""
Probe `output/cv.pdf.lock` with timeout=0. Exit 0 if free, 1 if another
process holds it. Used by `build.sh` to cooperate with editor-triggered
builds via the same `filelock`-backed lock.

Note: this releases the lock immediately. It is NOT a real mutex
against concurrent CLI invocations — just a sanity gate so a CLI build
doesn't stomp on a running editor build. Single-user local use only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prefer the project venv if it exists; otherwise rely on the system
# python having `filelock`. The venv is created during V1a setup.
ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
# Re-exec under the venv interpreter if we're not already running under it.
# Comparing sys.prefix vs sys.base_prefix is the canonical venv detection
# (resolve() doesn't help because .venv/bin/python is a symlink to the
# system Python and resolves to the same path).
if VENV_PY.exists() and sys.prefix == sys.base_prefix:
    import os

    os.execv(str(VENV_PY), [str(VENV_PY), __file__, *sys.argv[1:]])

import os  # noqa: E402 — after the venv re-exec check; moving would import filelock under system Python

from filelock import FileLock, Timeout  # noqa: E402 — same reason as above

# 2026-05-28: when build.sh is invoked BY the editor (via
# build_runner._run / stream_subprocess), the editor already holds the
# lock from line 1 of that helper. The lock semantic is "prevent CLI
# build.sh from racing with editor build" — not "prevent the editor
# from racing with the build.sh it just spawned." build_runner sets
# CV_EDITOR_INTERNAL_BUILD=1 in the subprocess env so we skip the
# probe and avoid the self-deadlock that aborts every editor-driven
# build.sh invocation.
if os.environ.get("CV_EDITOR_INTERNAL_BUILD"):
    sys.exit(0)

lock_path = ROOT / "output" / "cv.pdf.lock"
(ROOT / "output").mkdir(exist_ok=True)

try:
    with FileLock(str(lock_path), timeout=0):
        pass  # acquired and released; lock is free
    sys.exit(0)
except Timeout:
    sys.stderr.write(
        "[build.sh] output/cv.pdf.lock is held by another process "
        "(editor build in progress?). Aborting.\n"
    )
    sys.exit(1)
