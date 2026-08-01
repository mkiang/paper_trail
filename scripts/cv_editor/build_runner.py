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

import os
import queue
import signal
import subprocess
import threading
import time
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


def _kill_process_group(proc: subprocess.Popen, *, grace_s: float = 3.0) -> None:
    """SIGTERM then SIGKILL the child's whole process group.

    WHY NOT `proc.kill()`. Every build here is `make`/`build.sh` — a shell that forks
    its own children. SIGKILL cannot be trapped, so the shell CANNOT forward it to an
    already-forked `typst`, `python export_site.py` or `cp`; those are orphaned and keep
    running (and keep WRITING) after the caller has reported "killed" and released the
    build lock. With `start_new_session=True` the child is a session leader whose pgid
    equals its pid and whose descendants inherit it, so one `killpg` reaches all of them.

    REFUSES TO SIGNAL ITS OWN PROCESS GROUP, and that guard is not theoretical. A child
    spawned WITHOUT `start_new_session=True` stays in the caller's group, so
    `os.getpgid(child)` returns OUR pgid and the `killpg` below would SIGTERM the editor
    — or, when this ran under pytest with the flag mutated away to check the test was
    honest, the whole test session (it died on the spot). Any future caller that forgets
    the flag now degrades to a plain single-process kill instead of suicide.

    Safe on a child that already exited, and never raises.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:  # already reaped
        pgid = None
    if pgid is not None and pgid == os.getpgid(0):
        pgid = None  # same group as us — fall back to killing just the child
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            return
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue


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
    # 2026-05-28: tell child build.sh that its lock probe should skip —
    # the editor (this process) already holds the lock. Without this,
    # build.sh's first step (build_lock_check.py) self-deadlocks.
    child_env = {**os.environ, "CV_EDITOR_INTERNAL_BUILD": "1"}
    try:
        with FileLock(str(LOCK), timeout=0):
            OUTPUT.mkdir(exist_ok=True)
            t0 = time.time()
            # Popen + communicate rather than `subprocess.run(timeout=...)`, which kills
            # only the direct child on timeout and then BLOCKS in communicate() until
            # every inherited pipe closes — so one orphaned grandchild holding stdout
            # hangs the request thread outright. See `_kill_process_group`.
            proc = subprocess.Popen(
                cmd,
                cwd=str(_ENGINE),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=BUILD_TIMEOUT_S)
                return BuildResult(
                    ok=(proc.returncode == 0),
                    cmd=cmd_str,
                    duration_s=time.time() - t0,
                    stdout_tail=_tail(out or ""),
                    stderr_tail=_tail(err or ""),
                    returncode=proc.returncode,
                )
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                # The group is dead, so this drains the pipes rather than waiting on a
                # writer that will never close them.
                try:
                    out, _ = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - group is dead
                    out = ""
                return BuildResult(
                    ok=False,
                    cmd=cmd_str,
                    duration_s=time.time() - t0,
                    stdout_tail=_tail(out or ""),
                    stderr_tail=f"build timed out after {BUILD_TIMEOUT_S}s",
                    returncode=-1,
                )
    except Timeout:
        raise


def stream_subprocess(argv: list[str], cmd_str: str | None = None, timeout_s: int | None = None):
    """Stream any subprocess as (kind, payload) tuples, acquiring the
    shared build lock and merging stderr into stdout.

    Yields:
        ("line", "stdout/stderr line")  per output line
        ("done", {"ok": bool, "returncode": int, "duration_s": float, "cmd": str})
        ("error", "lock-busy message") if the lock can't be acquired

    Cleanup hierarchy on finally:
        kill the process GROUP if still running, close pipe, release lock —
        each independently try-guarded so a wedged subprocess can't
        strand the lock.

    Used by both `stream_rebuild` (full rebuild) and the per-variant
    `style_build_stream` route. Extracted in V5-D dedup pass (H1).

    TWO DEFECTS FIXED IN 1.2.4, both of which only bite when the child forks:

    1. THE KILL REACHED ONLY `make`. Spawning without `start_new_session` meant
       `proc.kill()` SIGKILLed the shell alone, and an untrappable signal cannot be
       forwarded to an already-forked `typst` / `export_site.py` / `cp`. Those kept
       running — and kept WRITING — after this generator reported "killed" and the
       `finally` released the lock, so a user who retried raced two writers over the
       same outputs with no lock held. `GeneratorExit` (the reader closing the tab or
       navigating away) is the ordinary trigger, not the timeout.
    2. THE DEADLINE COULD NOT FIRE ON A SILENT CHILD. It was evaluated only just after
       `readline` returned, and `readline` blocks forever on a child that says nothing,
       so a hung build was never capped at all. A reader thread feeding a queue lets the
       deadline be checked on a timer, and it is checked BEFORE each read so a
       continuously CHATTY child cannot starve it either.

    `timeout_s` defaults to `BUILD_TIMEOUT_S`; pass a larger cap for jobs that legitimately
    run longer than a PDF build (a full export + sync, say).
    """
    cmd_str = cmd_str or " ".join(argv)
    cap = BUILD_TIMEOUT_S if timeout_s is None else timeout_s
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
    child_env = {**os.environ, "CV_EDITOR_INTERNAL_BUILD": "1"}
    proc = subprocess.Popen(
        argv,
        cwd=str(_ENGINE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=child_env,
        start_new_session=True,
    )
    rc = -1
    killed = False
    try:
        q: queue.Queue = queue.Queue()

        def _reader(pipe, out):
            try:
                for line in iter(pipe.readline, ""):
                    out.put(line)
            except (OSError, ValueError):
                pass
            finally:
                out.put(None)

        threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

        while True:
            if time.time() - t0 > cap:
                _kill_process_group(proc)
                yield ("line", f"[build_runner] killed after {cap}s timeout (process group)")
                killed = True
                break
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            yield ("line", item.rstrip("\n"))

        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            rc = -1
        dt = time.time() - t0
        yield (
            "done",
            {
                "ok": rc == 0 and not killed,
                "returncode": rc,
                "duration_s": dt,
                "cmd": cmd_str,
            },
        )
    except GeneratorExit:
        _kill_process_group(proc)
        raise
    finally:
        try:
            _kill_process_group(proc)
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
