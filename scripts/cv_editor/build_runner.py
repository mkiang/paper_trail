"""
Subprocess wrapper for rebuilds. Two modes:

- `cv_only`: compile the DEFAULT variant (the first build variant in
   meta.yml) to output/<name>.pdf (~3-4s) — a fast single-PDF preview.
   Inputs are read from that variant's meta.yml row so the quick build
   matches the real default. (Was a bare `cv.typ -> output/cv.pdf`
   before 2026-06-08.)
- `full`: invoke `./build.sh` which regenerates publications.bib AND
   every variant (~20s).

Both modes acquire `output/cv.pdf.lock` via filelock for the duration
of the build. `build.sh` PROBES the lock at start (via
`build_lock_check.py`) and refuses to start if the editor is mid-build,
but it does NOT hold the lock during the compile itself — humans rarely
run `./build.sh` while clicking Rebuild in the editor, so single-user
local-only cooperation is enough (R7-M1, 2026-05-16).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from filelock import FileLock, Timeout

from cv_editor import paths

# Two-field seam (P1): ROOT/OUTPUT are WORKSPACE (data/meta.yml reads +
# output/ staleness + the shared lock); _ENGINE is the PROJECT root (the
# subprocess cwd where cv.typ, build.sh, and --font-path fonts resolve).
# Byte-identical while the two roots coincide (private repo). The quick-
# build's relative `output/<name>.pdf` argv + build.sh's own output path
# are reconciled to the workspace in P1-b (build.sh/Makefile rewire).
ROOT = paths.data_root()
OUTPUT = paths.output_dir()
_ENGINE = paths.project_root()
# Generic fallback quick-rebuild target when meta.yml defines no variants;
# the live name is resolved via default_variant_name() (the FIRST build
# variant, so the gauge + quick Rebuild follow the corpus's own default).
DEFAULT_VARIANT = "cv"
# Fixed cross-process mutex name, shared with build.sh + build_lock_check.py.
# Not tied to a built PDF (the `cv` variant / output/cv.pdf no longer exist).
LOCK = OUTPUT / "cv.pdf.lock"

BUILD_TIMEOUT_S = 120  # safety cap; full build is usually ~20s


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, OUTPUT, _ENGINE, LOCK
    ROOT = paths.data_root()
    OUTPUT = paths.output_dir()
    _ENGINE = paths.project_root()
    LOCK = OUTPUT / "cv.pdf.lock"


@dataclass
class BuildResult:
    ok: bool
    cmd: str
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    returncode: int


def _tail(s: str, lines: int = 40) -> str:
    parts = s.splitlines()
    return "\n".join(parts[-lines:]) if len(parts) > lines else s


def default_variant_name() -> str:
    """The live quick-rebuild / staleness-gauge variant name — the FIRST build
    variant in meta.yml (delegated to build_variants.default_variant_name).
    Unreadable/empty meta falls back to DEFAULT_VARIANT (the pre-M5-5d index
    gauge never read meta, so it was immune to a broken meta; this must be
    too)."""
    try:
        from cv_editor import build_variants as bv
        from cv_editor import yaml_io

        _, meta = yaml_io.load(ROOT / "data" / "meta.yml")
        return bv.default_variant_name(meta or {})
    except Exception:
        return DEFAULT_VARIANT


def _default_variant_argv() -> tuple[list[str], str]:
    """typst argv for the default variant, read from meta.yml so the
    quick-rebuild stays in sync with the variant definition (inputs +
    output filename). Falls back to a bare `cv.typ -> output/<name>.pdf`
    compile if the variant row can't be read."""
    name = default_variant_name()
    try:
        from cv_editor import build_variants as bv
        from cv_editor import yaml_io

        _, meta = yaml_io.load(ROOT / "data" / "meta.yml")
        variants = (meta or {}).get("build_variants") or []
        variant = next(
            (v for v in variants if (v.get("filename") or "").strip() == name),
            None,
        )
        if variant is not None:
            argv = bv.variant_typst_argv(variant)
            return argv, " ".join(argv)
    except Exception:
        pass
    argv = [
        "typst",
        "compile",
        "--font-path",
        "fonts",
        "--ignore-system-fonts",
        "cv.typ",
        f"output/{name}.pdf",
    ]
    return argv, " ".join(argv)


def rebuild_cv_only() -> BuildResult:
    cmd, cmd_str = _default_variant_argv()
    return _run(cmd, cmd_str)


def rebuild_full() -> BuildResult:
    return _run(["./build.sh"], "./build.sh")


def _run(cmd: list[str], cmd_str: str) -> BuildResult:
    import os
    import time

    # 2026-05-28: tell child build.sh that its lock probe should skip —
    # the editor (this process) already holds the lock. Without this,
    # build.sh's first step (build_lock_check.py) self-deadlocks.
    child_env = {**os.environ, "CV_EDITOR_INTERNAL_BUILD": "1"}
    try:
        with FileLock(str(LOCK), timeout=0):
            OUTPUT.mkdir(exist_ok=True)
            t0 = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(_ENGINE),
                    capture_output=True,
                    text=True,
                    timeout=BUILD_TIMEOUT_S,
                    env=child_env,
                )
                dt = time.time() - t0
                return BuildResult(
                    ok=(proc.returncode == 0),
                    cmd=cmd_str,
                    duration_s=dt,
                    stdout_tail=_tail(proc.stdout or ""),
                    stderr_tail=_tail(proc.stderr or ""),
                    returncode=proc.returncode,
                )
            except subprocess.TimeoutExpired as e:
                return BuildResult(
                    ok=False,
                    cmd=cmd_str,
                    duration_s=time.time() - t0,
                    stdout_tail=_tail((e.stdout or b"").decode("utf-8", errors="replace")),
                    stderr_tail=f"build timed out after {BUILD_TIMEOUT_S}s",
                    returncode=-1,
                )
    except Timeout:
        raise


def stream_subprocess(argv: list[str], cmd_str: str | None = None):
    """Stream any subprocess as (kind, payload) tuples, acquiring the
    shared build lock and merging stderr into stdout.

    Yields:
        ("line", "stdout/stderr line")  per output line
        ("done", {"ok": bool, "returncode": int, "duration_s": float, "cmd": str})
        ("error", "lock-busy message") if the lock can't be acquired

    Cleanup hierarchy on finally:
        kill subprocess if still running, close pipe, release lock —
        each independently try-guarded so a wedged subprocess can't
        strand the lock.

    Used by both `stream_rebuild` (full rebuild) and the per-variant
    `style_build_stream` route. Extracted in V5-D dedup pass (H1).
    """
    import time

    cmd_str = cmd_str or " ".join(argv)
    try:
        lock = FileLock(str(LOCK), timeout=0)
        lock.acquire()
    except Timeout:
        yield ("error", "Another build is already running; wait for it to finish.")
        return

    OUTPUT.mkdir(exist_ok=True)
    t0 = time.time()
    # 2026-05-28: see _run() — the editor holds the lock; tell child
    # build.sh to skip its probe so it doesn't self-deadlock.
    import os as _os_env

    child_env = {**_os_env.environ, "CV_EDITOR_INTERNAL_BUILD": "1"}
    proc = subprocess.Popen(
        argv,
        cwd=str(_ENGINE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=child_env,
    )
    rc = -1
    try:
        for line in iter(proc.stdout.readline, ""):
            yield ("line", line.rstrip("\n"))
            if time.time() - t0 > BUILD_TIMEOUT_S:
                proc.kill()
                yield ("line", f"[build_runner] killed after {BUILD_TIMEOUT_S}s timeout")
                break
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -1
        dt = time.time() - t0
        yield (
            "done",
            {
                "ok": rc == 0,
                "returncode": rc,
                "duration_s": dt,
                "cmd": cmd_str,
            },
        )
    except GeneratorExit:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            lock.release()
        except Exception:
            pass


def stream_rebuild(mode: str):
    """Run the canonical rebuild with line-by-line streaming. Backward-
    compatible name; delegates to stream_subprocess."""
    if mode == "full":
        argv = ["./build.sh"]
        cmd_str = "./build.sh"
    else:
        argv, cmd_str = _default_variant_argv()
    yield from stream_subprocess(argv, cmd_str)


__all__ = [
    "BuildResult",
    "rebuild_cv_only",
    "rebuild_full",
    "stream_rebuild",
    "stream_subprocess",
    "LOCK",
]
