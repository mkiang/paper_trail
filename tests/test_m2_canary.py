"""Meta-test for the real-data corruption canary (conftest.py).

The canary is the PRIMARY guard against real-data clobbering during the
M2 refactor (DATA_DIR isolation is deferred). If its detect/restore logic
silently broke, a corrupting test would pass and the user would wake up to
broken data (the exact task-#42 incident). These tests pin the core logic
directly so it can't rot unnoticed.
"""

from __future__ import annotations

from conftest import (
    WATCHED_DATA_FILES,
    _restore_and_collect_changes,
    _snapshot_watched,
)


def test_canary_detects_and_restores_a_change(tmp_path):
    f = tmp_path / "data" / "publications.yml"
    f.parent.mkdir(parents=True)
    original = b"authors:\n  - name: Real Author\n"
    f.write_text(original.decode())

    snaps = _snapshot_watched(tmp_path, lambda m: False)
    assert snaps, "snapshot should include the existing watched file"

    # Simulate a misbehaving test corrupting the real file.
    f.write_text("authors: a; b; c; d\n")

    changed = _restore_and_collect_changes(snaps)
    assert "publications.yml" in changed
    # And it must RESTORE the file, not just report.
    assert f.read_bytes() == original


def test_canary_restores_a_deleted_file(tmp_path):
    """M5-5d post-impl review: scaffold.reset DELETES the pubmed sidecar —
    a rogue reset must not crash the restore loop (stranding later files);
    the deletion is restored from snapshot bytes and reported."""
    d = tmp_path / "data"
    d.mkdir(parents=True)
    sidecar = d / "publications_pubmed_sync.json"
    # citation_counts.json iterates AFTER the sidecar in WATCHED_DATA_FILES,
    # so a crash on the deletion would strand it unrestored.
    later = d / "citation_counts.json"
    original_sidecar = b'{"version": 3}\n'
    original_later = b'{"version": 1, "counts": {}}\n'
    sidecar.write_bytes(original_sidecar)
    later.write_bytes(original_later)

    snaps = _snapshot_watched(tmp_path, lambda m: False)
    # Simulate a rogue reset: sidecar deleted AND a later-watched file changed.
    sidecar.unlink()
    later.write_bytes(b'{"version": 1, "counts": {"clobbered": 1}}\n')

    changed = _restore_and_collect_changes(snaps)
    assert "publications_pubmed_sync.json (was deleted)" in changed
    assert "citation_counts.json" in changed  # loop continued past the deletion
    assert sidecar.read_bytes() == original_sidecar
    assert later.read_bytes() == original_later


def test_canary_clean_when_unchanged(tmp_path):
    f = tmp_path / "data" / "meta.yml"
    f.parent.mkdir(parents=True)
    f.write_text("self_bold: Public JQ\n")
    snaps = _snapshot_watched(tmp_path, lambda m: False)
    changed = _restore_and_collect_changes(snaps)
    assert changed == []


def test_canary_respects_opt_out_marker(tmp_path):
    f = tmp_path / "data" / "publications.yml"
    f.parent.mkdir(parents=True)
    f.write_text("x: 1\n")
    # marker active for publications -> file is NOT snapshotted/watched.
    snaps = _snapshot_watched(tmp_path, lambda m: m == "mutates_publications_yml")
    assert all(p.name != "publications.yml" for p, _ in snaps)


def test_watch_set_covers_generic_crud_write_targets():
    # The generic CRUD save path can write any section file; the canary
    # must at least cover the high-traffic ones the refactor touches.
    assert "publications.yml" in WATCHED_DATA_FILES
    assert "meta.yml" in WATCHED_DATA_FILES
    assert "honors.yml" in WATCHED_DATA_FILES
