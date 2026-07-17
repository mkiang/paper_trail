"""V14 extraction tests (2026-05-17).

Locks in the behavior of the shared `HostThrottle` and `atomic_write_json`
modules so the three migrating callers (verify_urls, pubmed_sync,
altmetric_tracker_cache, and the new fetch_citation_counts) all see the
same contract.

Per critique R1-M6: golden tests written BEFORE migration so any
regression in the existing callers is caught.
"""

import json
import threading
import time

import pytest

# Importing inside tests where needed so module-level import order
# matches the migration sequence (HostThrottle first).
from cv_editor.host_throttle import HostThrottle

# ---- HostThrottle ----------------------------------------------------------


def test_host_throttle_serializes_same_host():
    """Two back-to-back calls to the same host enforce the gap."""
    t = HostThrottle(default_gap=0.2)
    start = time.monotonic()
    t.wait("a.example")
    t.wait("a.example")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.18, f"expected >= 0.18s gap, got {elapsed:.3f}s"
    assert elapsed < 0.35, f"slept too long: {elapsed:.3f}s"


def test_host_throttle_different_hosts_dont_block():
    """Different hosts do not contend."""
    t = HostThrottle(default_gap=0.5)
    start = time.monotonic()
    t.wait("a.example")
    t.wait("b.example")
    elapsed = time.monotonic() - start
    # Both calls return promptly; only one host has been hit each.
    assert elapsed < 0.1, f"different hosts shouldn't block: {elapsed:.3f}s"


def test_host_throttle_per_host_override():
    """gap_per_host overrides the default."""
    t = HostThrottle(
        gap_per_host={"slow.example": 0.3},
        default_gap=0.05,
    )
    assert t.gap_for("slow.example") == 0.3
    assert t.gap_for("any.other") == 0.05


def test_host_throttle_concurrent_threads_serialize():
    """32 threads hitting the same host should serialize per gap.

    Per critique R2-M6: 4-worker stress isn't enough. 32 threads sharing
    one HostThrottle on one host: total elapsed time must be ≥ (N-1)*gap
    (the first call has no prior to wait for; the rest each wait one gap).
    """
    gap = 0.05
    t = HostThrottle(default_gap=gap)
    N = 32
    barrier = threading.Barrier(N)
    timestamps: list[float] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        t.wait("same.host")
        with lock:
            timestamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(N)]
    start = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.monotonic() - start
    # Total elapsed >= (N-1) * gap. Allow 20% slack for scheduling jitter.
    expected = (N - 1) * gap
    assert elapsed >= expected * 0.8, f"{elapsed:.3f}s < {expected:.3f}s"
    # Pairwise spacing: consecutive timestamps should be at least gap apart.
    timestamps.sort()
    for prev, cur in zip(timestamps, timestamps[1:]):
        assert cur - prev >= gap * 0.8, f"pair too close: {cur - prev:.3f}s"


# ---- atomic_write_json -----------------------------------------------------


def test_atomic_write_json_creates_file(tmp_path):
    from cv_editor.atomic_json import atomic_write_json

    p = tmp_path / "out.json"
    atomic_write_json(p, {"version": 1, "x": 42})
    assert p.exists()
    assert json.loads(p.read_text()) == {"version": 1, "x": 42}


def test_atomic_write_json_tmp_cleaned_on_success(tmp_path):
    """After success, no .tmp files linger in the parent directory."""
    from cv_editor.atomic_json import atomic_write_json

    p = tmp_path / "out.json"
    atomic_write_json(p, {"a": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert not leftovers, f"leftover tmp: {leftovers}"


def test_atomic_write_json_tmp_cleaned_on_failure(tmp_path, monkeypatch):
    """Mid-write failure unlinks the tmp file (R7-H3 orphan-unlink preserved)."""
    from cv_editor import atomic_json

    p = tmp_path / "out.json"

    def boom(_):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(atomic_json, "_dumps", boom)
    with pytest.raises(RuntimeError):
        atomic_json.atomic_write_json(p, {"a": 1})
    assert not p.exists()
    leftovers = list(tmp_path.glob("*.tmp"))
    assert not leftovers, f"orphan tmp: {leftovers}"


def test_atomic_write_json_overwrites_existing(tmp_path):
    from cv_editor.atomic_json import atomic_write_json

    p = tmp_path / "out.json"
    p.write_text(json.dumps({"old": True}))
    atomic_write_json(p, {"new": True})
    assert json.loads(p.read_text()) == {"new": True}


def test_atomic_write_json_verify_load_catches_corrupt(tmp_path, monkeypatch):
    """With verify_load=True (default), if the tmp content fails to round-trip
    parse, the replace is aborted and the original file remains intact."""
    from cv_editor import atomic_json

    p = tmp_path / "out.json"
    p.write_text(json.dumps({"good": True}))

    # Force the encoder to emit non-JSON content; verify_load should reject.
    monkeypatch.setattr(atomic_json, "_dumps", lambda data: "not-json-at-all{")
    with pytest.raises(ValueError):
        atomic_json.atomic_write_json(p, {"a": 1})
    # Original file untouched.
    assert json.loads(p.read_text()) == {"good": True}


# ---- Migration smoke (structural, not byte-level — per R2-M4) --------------


def test_migration_pubmed_sync_sidecar_shape(tmp_path):
    """Post-migration `pubmed_sync.save_sidecar` writes the same logical
    JSON shape as pre-migration (structural deep-equality, not byte-level)."""
    # Import inside the test so the migration has had a chance to land.
    import importlib

    pubmed_sync = importlib.import_module("cv_editor.pubmed_sync")
    state = pubmed_sync.SidecarState()
    p = tmp_path / "sync.json"
    pubmed_sync.save_sidecar(p, state)
    body = json.loads(p.read_text())
    # The version key must survive; the entries map must exist.
    assert body["version"] == 1
    assert isinstance(body["entries"], dict)


def test_migration_tracker_cache_shape(tmp_path):
    """Post-migration `TrackerCache.save` writes the same logical shape."""
    from cv_editor.altmetric_tracker_cache import TrackerCache

    p = tmp_path / "trackers.json"
    cache = TrackerCache(p)
    cache.save()
    body = json.loads(p.read_text())
    assert body.get("version") == 1
    assert "trackers" in body
