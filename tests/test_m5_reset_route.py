"""M5-5d CP7: the /reset route (guarded whole-corpus reset).

DATA-SAFE BY CONSTRUCTION: every test monkeypatches `scaffold.reset` and
`scaffold.corpus_is_empty` at the module-attribute seam core_routes calls
through, AND (defense-in-depth, pre-impl review HIGH) `yaml_io.write_with_backup`
+ `yaml_io.write_new` are patched to raise — if a dispatch monkeypatch ever
silently misses, the write layer explodes instead of rewriting the real CV,
and the widened conftest canary is the final backstop. SEQUENTIAL pytest
(gotcha #70).
"""

from __future__ import annotations

import pytest
from cv_editor import scaffold, yaml_io
from cv_editor.app import create_app

FAKE_MANIFEST = {
    "version": 1,
    "mode": "example",
    "snapshot_dir": "/fake/.cv_editor_backups/reset-123",
    "completed": True,
    "phases": {
        "snapshot": ["data/publications.yml"],
        "sections": {
            "publications.yml": "overwrote",
            "honors.yml": "created",
            "meta.yml": "overwrote",
            "citation_counts.json": "written",
        },
        "sidecars": {"publications_pubmed_sync.json": "deleted (snapshotted)"},
        "qc_moved": ["qc/report.md"],
    },
}


@pytest.fixture
def client(monkeypatch):
    def explode(*a, **k):  # the write layer must be unreachable from tests
        raise AssertionError("route test reached a real write path")

    monkeypatch.setattr(yaml_io, "write_with_backup", explode)
    monkeypatch.setattr(yaml_io, "write_new", explode)
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


@pytest.fixture
def reset_spy(monkeypatch):
    calls = []

    def fake_reset(mode, **kwargs):
        calls.append(mode)
        return dict(FAKE_MANIFEST, mode=mode)

    monkeypatch.setattr(scaffold, "reset", fake_reset)
    return calls


def _nonempty(monkeypatch):
    monkeypatch.setattr(scaffold, "corpus_is_empty", lambda *a, **k: False)


def _empty(monkeypatch):
    monkeypatch.setattr(scaffold, "corpus_is_empty", lambda *a, **k: True)


# ---------- GET ----------


def test_get_renders_both_modes_with_phrase_on_nonempty(client, monkeypatch):
    _nonempty(monkeypatch)
    body = client.get("/reset").get_data(as_text=True)
    assert 'value="example"' in body and 'value="blank"' in body
    assert 'id="reset-confirm"' in body  # phrase input present
    assert "reset to example" in body  # default mode preselected phrase


def test_get_mode_query_preselects_blank(client, monkeypatch):
    _nonempty(monkeypatch)
    body = client.get("/reset?mode=blank").get_data(as_text=True)
    assert "reset to blank" in body


def test_get_bogus_mode_falls_back_to_example(client, monkeypatch):
    _nonempty(monkeypatch)
    body = client.get("/reset?mode=nuke").get_data(as_text=True)
    assert "reset to example" in body


def test_get_empty_corpus_waives_phrase(client, monkeypatch):
    _empty(monkeypatch)
    body = client.get("/reset").get_data(as_text=True)
    assert 'id="reset-confirm"' not in body
    assert "no confirmation phrase needed" in body


# ---------- POST guards ----------


def test_post_wrong_phrase_400_rerenders_form(client, monkeypatch, reset_spy):
    _nonempty(monkeypatch)
    resp = client.post("/reset", data={"mode": "example", "confirm_phrase": "reset to blank"})
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    # direct re-render, NOT a redirect stub (gotcha #46): form must be in the body
    assert 'id="reset-confirm"' in body and 'value="example"' in body
    assert "mismatch" in body
    assert reset_spy == []


def test_post_missing_phrase_400(client, monkeypatch, reset_spy):
    _nonempty(monkeypatch)
    resp = client.post("/reset", data={"mode": "blank"})
    assert resp.status_code == 400
    assert reset_spy == []


def test_post_bogus_mode_400(client, monkeypatch, reset_spy):
    _nonempty(monkeypatch)
    resp = client.post("/reset", data={"mode": "nuke", "confirm_phrase": "reset to nuke"})
    assert resp.status_code == 400
    assert reset_spy == []


def test_post_empty_to_nonempty_race_shows_phrase_input(client, monkeypatch, reset_spy):
    """GET rendered the waived variant, but by POST time the corpus is
    non-empty — the 400 re-render must SHOW the phrase input it previously
    omitted."""
    _nonempty(monkeypatch)
    resp = client.post("/reset", data={"mode": "example"})
    assert resp.status_code == 400
    assert 'id="reset-confirm"' in resp.get_data(as_text=True)
    assert reset_spy == []


# ---------- POST success ----------


def test_post_correct_phrase_runs_reset_and_renders_manifest(client, monkeypatch, reset_spy):
    _nonempty(monkeypatch)
    resp = client.post("/reset", data={"mode": "example", "confirm_phrase": "  Reset TO Example "})
    assert resp.status_code == 200  # case-insensitive, stripped
    assert reset_spy == ["example"]
    body = resp.get_data(as_text=True)
    assert "reset-123" in body  # snapshot dir surfaced
    assert "Backups" in body  # per-section restore links
    assert "deleted (snapshotted)" in body  # sidecar phase reported


def test_post_empty_corpus_needs_no_phrase(client, monkeypatch, reset_spy):
    _empty(monkeypatch)
    resp = client.post("/reset", data={"mode": "blank"})
    assert resp.status_code == 200
    assert reset_spy == ["blank"]


def test_post_reset_exception_renders_failed_page(client, monkeypatch, tmp_path):
    """Hermetic: BACKUP_DIR is redirected to an EMPTY tmp so the
    manifest-is-None branch renders (and the route never reads the user's
    real .cv_editor_backups)."""
    _empty(monkeypatch)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "empty-backups")

    def boom(mode, **kwargs):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(scaffold, "reset", boom)
    resp = client.post("/reset", data={"mode": "blank"})
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "FAILED mid-run" in body
    assert "manifest.json" in body  # points at the phase record
    assert "reset-*" in body  # the no-manifest fallback copy rendered


def test_manifest_snapshot_path_is_escaped(client, monkeypatch):
    """A hostile snapshot path must render escaped — pins autoescape so a
    future |safe on the manifest page fails loudly."""
    _empty(monkeypatch)
    hostile = dict(FAKE_MANIFEST, snapshot_dir='/fake/reset-1<script>alert(1)</script>')

    def fake_reset(mode, **kwargs):
        return dict(hostile, mode=mode)

    monkeypatch.setattr(scaffold, "reset", fake_reset)
    resp = client.post("/reset", data={"mode": "blank"})
    body = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
