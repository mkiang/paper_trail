"""M5-5d CP8: first-run onboarding card + entry_save None-body hardening.

Write-free: the save test monkeypatches yaml_io.load (to simulate a
comments-only file) AND yaml_io.write_with_backup (to capture instead of
write); the onboarding tests are GET-only with module-attr monkeypatches.
SEQUENTIAL pytest (gotcha #70).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import scaffold, sections, yaml_io
from cv_editor.app import create_app
from ruamel.yaml.comments import CommentedSeq


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


# ---------- onboarding card ----------


def _zero_cards(monkeypatch):
    """Force every index card count to 0 (the cheap pre-filter)."""
    monkeypatch.setattr(sections, "flatten", lambda data, structure: iter(()))


def test_card_shows_when_predicate_says_empty(client, monkeypatch):
    _zero_cards(monkeypatch)
    monkeypatch.setattr(scaffold, "corpus_is_empty", lambda *a, **k: True)
    body = client.get("/").get_data(as_text=True)
    assert "No CV data yet" in body
    assert "/reset?mode=example" in body
    assert "mode=blank" not in body  # dead affordance on an empty corpus
    # rebuild bar suppressed in the empty state
    assert "rebuild needed" not in body and "Run ./build.sh now" not in body


def test_card_parity_fail_closed(client, monkeypatch):
    """Zero cards but the authoritative predicate says NOT empty (e.g. a
    parse failure) -> no card; the banner may never advertise a reset the
    server would then refuse without a phrase."""
    _zero_cards(monkeypatch)
    monkeypatch.setattr(scaffold, "corpus_is_empty", lambda *a, **k: False)
    body = client.get("/").get_data(as_text=True)
    assert "No CV data yet" not in body


def test_real_corpus_shows_no_card(client):
    body = client.get("/").get_data(as_text=True)
    assert "No CV data yet" not in body


def test_tools_nav_carries_reset_entry(client):
    """Pins the base.html Tools-menu link — the /reset endpoint is guarded
    by the url-map baseline, but the nav entry could be dropped with a
    green suite without this."""
    body = client.get("/").get_data(as_text=True)
    assert "Reset CV data" in body
    assert 'href="/reset"' in body


# ---------- entry_save None-body guard ----------


def test_entry_save_accepts_first_entry_into_comments_only_file(client, monkeypatch):
    real_load = yaml_io.load

    def fake_load(path: Path):
        if path.name == "honors.yml":
            return "# header only\n", None  # comments-only file
        return real_load(path)

    captured = {}

    def fake_write(path, header, data, expected_mtime_ns=None, **kwargs):
        captured["data"] = data
        return Path("/fake/honors.yml.1.bak")

    monkeypatch.setattr(yaml_io, "load", fake_load)
    monkeypatch.setattr(yaml_io, "write_with_backup", fake_write)
    resp = client.post(
        "/honors/save",
        data={
            "mode": "new",
            "date": "2026",
            "award": "First award into an empty file",
            "institution": "Example Org",
            "mtime_ns": "1",
        },
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    assert isinstance(captured["data"], CommentedSeq)
    assert captured["data"][0]["award"] == "First award into an empty file"
