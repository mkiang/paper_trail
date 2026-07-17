"""Tests for cv_editor.versioned_json.load_versioned (V20, 2026-05-18)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cv_editor.versioned_json import load_versioned


def test_missing_file_returns_none_silently(tmp_path, capsys):
    p = tmp_path / "nope.json"
    assert load_versioned(p, expected_version=1) is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_valid_file_returns_body(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"version": 1, "foo": "bar"}))
    body = load_versioned(p, expected_version=1)
    assert body == {"version": 1, "foo": "bar"}


def test_corrupt_json_warns_and_returns_none(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert load_versioned(p, expected_version=1, component_name="testcache") is None
    captured = capsys.readouterr()
    assert "[testcache] WARNING" in captured.err
    assert "corrupted" in captured.err


def test_corrupt_json_silent_when_silent_true(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert load_versioned(p, expected_version=1, silent=True) is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_version_mismatch_warns_and_returns_none(tmp_path, capsys):
    p = tmp_path / "v2.json"
    p.write_text(json.dumps({"version": 2, "data": "x"}))
    assert load_versioned(p, expected_version=1, component_name="sync") is None
    captured = capsys.readouterr()
    assert "[sync] WARNING" in captured.err
    assert "version" in captured.err


def test_version_mismatch_silent_when_silent_true(tmp_path, capsys):
    p = tmp_path / "v2.json"
    p.write_text(json.dumps({"version": 2}))
    assert load_versioned(p, expected_version=1, silent=True) is None
    captured = capsys.readouterr()
    assert captured.err == ""


def test_non_dict_body_warns(tmp_path, capsys):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["a", "b"]))
    assert load_versioned(p, expected_version=1) is None
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


def test_no_component_prefix_when_none(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{")
    assert load_versioned(p, expected_version=1) is None
    captured = capsys.readouterr()
    assert "[" not in captured.err.split("WARNING")[0]


def test_missing_version_key_treated_as_mismatch(tmp_path, capsys):
    p = tmp_path / "no_version.json"
    p.write_text(json.dumps({"data": "x"}))
    assert load_versioned(p, expected_version=1, silent=True) is None


# ---- integration: real callers still load valid files correctly ----


def test_tracker_cache_load_real_file(tmp_path):
    """Sanity: TrackerCache._load reads a valid file through the helper."""
    from cv_editor.altmetric_tracker_cache import CACHE_VERSION, TrackerCache

    p = tmp_path / "trackers.json"
    p.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "trackers": {
                    "https://ct.moreover.com/abc": {
                        "tracker_url": "https://ct.moreover.com/abc",
                        "final_url": "https://example.com",
                        "status": "resolved",
                        "strategy": "head",
                        "first_seen_at": "2026-01-01T00:00:00+00:00",
                        "last_checked": "2026-01-01T00:00:00+00:00",
                        "attempt_count": 1,
                        "last_error": None,
                    },
                },
            }
        )
    )
    cache = TrackerCache(p)
    entry = cache.get("https://ct.moreover.com/abc")
    assert entry is not None
    assert entry.status == "resolved"
    assert entry.final_url == "https://example.com"


def test_tracker_cache_corrupt_file_starts_empty(tmp_path, capsys):
    from cv_editor.altmetric_tracker_cache import TrackerCache

    p = tmp_path / "trackers.json"
    p.write_text("not json at all")
    cache = TrackerCache(p)
    assert cache.get("anything") is None
    captured = capsys.readouterr()
    assert "altmetric_tracker_cache" in captured.err


def test_citation_cache_corrupt_renames_then_recovers(tmp_path):
    """CitationCache wraps load_versioned with rename-on-corrupt."""
    from cv_editor.citation_counts import CitationCache

    p = tmp_path / "citations.json"
    p.write_text("garbage")
    cache = CitationCache.load(p)
    # Starts empty
    assert cache.get("10.1234/foo") is None
    # Original path is gone, .corrupt-* sibling exists
    assert not p.exists()
    corrupt_siblings = list(tmp_path.glob("*corrupt*.json"))
    assert len(corrupt_siblings) == 1


def test_citation_cache_version_mismatch_renames(tmp_path):
    """Version mismatch also triggers the rename path."""
    from cv_editor.citation_counts import CitationCache

    p = tmp_path / "citations.json"
    p.write_text(json.dumps({"version": 999, "counts": {}}))
    cache = CitationCache.load(p)
    assert cache.get("10.1234/foo") is None
    assert not p.exists()
    corrupt_siblings = list(tmp_path.glob("*corrupt*.json"))
    assert len(corrupt_siblings) == 1


def test_pubmed_sync_load_sidecar_corrupt_warns(tmp_path, capsys):
    from cv_editor.pubmed_sync import SidecarState, load_sidecar

    p = tmp_path / "sidecar.json"
    p.write_text("nope")
    state = load_sidecar(p)
    assert isinstance(state, SidecarState)
    assert state.entries == {}
    captured = capsys.readouterr()
    assert "[sync]" in captured.err
