"""`build_runner` must kill the whole process GROUP, and must be able to time out.

Both defects here only appear when the child FORKS, which every real build does:
`build.sh` and `make` are shells that spawn `typst`, `export_site.py`, `cp`. A test
whose fake child has no children passes whether or not the fix is present, so the
grandchild tests below are paired with a positive control that proves the marker really
does appear when nothing kills the group.

The timeout tests come in a silent/chatty pair on purpose. The pre-1.2.4 code evaluated
its deadline only just after `readline` returned, which fails BOTH ways — a silent child
blocks in `readline` forever, and a chatty child never lets a queue-starvation check run.
Each fake child self-terminates so a regression FAILS these tests rather than hanging
the suite.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from cv_editor import build_runner, paths

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups / killpg are POSIX-only")


def _script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def _forking(tmp_path: Path, marker: Path) -> str:
    """Announces itself, forks a grandchild that touches `marker` after 2s, then sleeps
    well past any deadline under test."""
    return _script(
        tmp_path,
        "forking.sh",
        f"( sleep 2; touch '{marker}' ) &\necho started\nsleep 20\n",
    )


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Keep the build lock and `output/` inside the tmp tree."""
    paths.configure(data_dir=tmp_path, project_root=tmp_path)
    yield


# --------------------------------------------------------------------------- #
# the control that makes the rest non-vacuous
# --------------------------------------------------------------------------- #


def test_a_grandchild_survives_when_nothing_kills_the_group(tmp_path):
    """POSITIVE CONTROL. Without this, a grandchild test passes for the wrong reason —
    e.g. the script never ran, or the marker path was wrong."""
    marker = tmp_path / "survived"
    proc = subprocess.Popen([_forking(tmp_path, marker)])
    time.sleep(3.5)
    proc.kill()
    proc.wait(timeout=5)
    assert marker.exists(), "the fake grandchild never ran; the tests below prove nothing"


# --------------------------------------------------------------------------- #
# the kill
# --------------------------------------------------------------------------- #


def test_stream_subprocess_kills_the_grandchild_on_generator_exit(tmp_path):
    """Closing the tab is the ordinary trigger. Pre-1.2.4 this SIGKILLed only the shell,
    orphaning a writer that kept running after the lock was released."""
    marker = tmp_path / "survived"
    gen = build_runner.stream_subprocess([_forking(tmp_path, marker)], "fake")
    assert next(gen) == ("line", "started")
    gen.close()
    time.sleep(3.5)
    assert not marker.exists(), "grandchild outlived the kill — process group not killed"


def test_stream_subprocess_kills_the_grandchild_on_timeout(tmp_path):
    marker = tmp_path / "survived"
    frames = list(build_runner.stream_subprocess([_forking(tmp_path, marker)], "fake", timeout_s=1))
    assert any("killed after 1s" in p for k, p in frames if k == "line")
    time.sleep(3.0)
    assert not marker.exists()


def test_run_kills_the_grandchild_on_timeout_and_does_not_hang(tmp_path, monkeypatch):
    """`subprocess.run(timeout=)` kills only the direct child and then blocks in
    communicate() until every inherited pipe closes — an orphaned grandchild holding
    stdout hangs the request thread outright."""
    marker = tmp_path / "survived"
    monkeypatch.setattr(build_runner, "BUILD_TIMEOUT_S", 1)
    t0 = time.time()
    result = build_runner._run([_forking(tmp_path, marker)], "fake")
    elapsed = time.time() - t0

    assert result.ok is False
    assert "timed out" in result.stderr_tail
    assert elapsed < 15, f"_run hung on the orphaned grandchild's pipe ({elapsed:.1f}s)"
    time.sleep(3.0)
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# the deadline, which has to survive both a mute child and a talkative one
# --------------------------------------------------------------------------- #


def test_timeout_fires_on_a_silent_child(tmp_path):
    """`readline` blocks forever on a child that says nothing, so the old post-read
    check could never run. Self-terminating so a regression fails instead of hanging."""
    script = _script(tmp_path, "silent.sh", "sleep 30\n")
    t0 = time.time()
    frames = list(build_runner.stream_subprocess([script], "silent", timeout_s=2))
    elapsed = time.time() - t0

    assert elapsed < 12, f"a silent child was never capped ({elapsed:.1f}s)"
    assert any("killed after 2s" in p for k, p in frames if k == "line")
    assert frames[-1][0] == "done" and frames[-1][1]["ok"] is False


def test_timeout_fires_on_a_chatty_child(tmp_path):
    """A child emitting faster than the 1s queue read never starves it, so a deadline
    checked only on starvation is never evaluated. It is checked before each read."""
    script = _script(
        tmp_path,
        "chatty.sh",
        "i=0\nwhile [ $i -lt 200 ]; do echo tick; sleep 0.05; i=$((i+1)); done\n",
    )
    t0 = time.time()
    frames = list(build_runner.stream_subprocess([script], "chatty", timeout_s=2))
    elapsed = time.time() - t0

    assert elapsed < 8, f"a chatty child was never capped ({elapsed:.1f}s)"
    assert any("killed after 2s" in p for k, p in frames if k == "line")
    assert frames[-1][0] == "done" and frames[-1][1]["ok"] is False


def test_a_killed_run_is_not_reported_ok(tmp_path):
    """`ok` used to be `rc == 0` alone. A killed child can exit 0 in the race between the
    kill and the wait, which would report a timed-out build as a success."""
    script = _script(
        tmp_path,
        "chatty2.sh",
        "i=0\nwhile [ $i -lt 200 ]; do echo tick; sleep 0.05; i=$((i+1)); done\n",
    )
    frames = list(build_runner.stream_subprocess([script], "chatty", timeout_s=2))
    assert frames[-1][1]["ok"] is False


# --------------------------------------------------------------------------- #
# the helper must not shoot the caller
# --------------------------------------------------------------------------- #


def test_kill_group_refuses_to_signal_our_own_process_group(tmp_path, monkeypatch):
    """A child spawned WITHOUT `start_new_session` shares our group, so `getpgid(child)`
    returns OUR pgid and an unguarded `killpg` SIGTERMs the editor itself.

    This is not hypothetical: it is what happened when `start_new_session=True` was
    mutated away to check the grandchild tests were honest — the killpg took down the
    entire pytest session rather than failing an assertion.

    Asserted by intercepting `killpg` rather than by letting it fire, because a test that
    kills its own runner on regression is not a test.
    """
    proc = subprocess.Popen([_script(tmp_path, "sleeper.sh", "sleep 10\n")])  # same group
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_runner.os, "killpg", lambda pg, sig: calls.append((pg, sig)))
    try:
        assert os.getpgid(proc.pid) == os.getpgid(0), "fixture failed to share our group"
        build_runner._kill_process_group(proc, grace_s=0.5)
        assert calls == [], f"killpg was called on our own group: {calls}"
        assert proc.poll() is not None, "the child should still have been killed directly"
    finally:
        if proc.poll() is None:  # pragma: no cover - only on regression
            proc.kill()
            proc.wait(timeout=5)


# --------------------------------------------------------------------------- #
# the ordinary path still works
# --------------------------------------------------------------------------- #


def test_a_normal_child_streams_its_lines_and_reports_ok(tmp_path):
    script = _script(tmp_path, "ok.sh", "echo one\necho two\n")
    frames = list(build_runner.stream_subprocess([script], "ok"))
    lines = [p for k, p in frames if k == "line"]
    assert lines == ["one", "two"]
    kind, payload = frames[-1]
    assert kind == "done" and payload["ok"] is True and payload["returncode"] == 0


def test_a_failing_child_reports_its_returncode(tmp_path):
    script = _script(tmp_path, "fail.sh", "echo nope\nexit 3\n")
    frames = list(build_runner.stream_subprocess([script], "fail"))
    kind, payload = frames[-1]
    assert kind == "done" and payload["ok"] is False and payload["returncode"] == 3


def test_the_lock_is_released_after_a_killed_stream(tmp_path):
    """The cleanup hierarchy's whole point: a wedged child must not strand the lock."""
    marker = tmp_path / "survived"
    gen = build_runner.stream_subprocess([_forking(tmp_path, marker)], "fake")
    next(gen)
    gen.close()
    from filelock import FileLock

    probe = FileLock(str(build_runner.LOCK), timeout=0)
    probe.acquire()  # raises filelock.Timeout if still held
    probe.release()
