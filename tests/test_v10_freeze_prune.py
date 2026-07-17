"""V10: garbage-collect old frozen workspaces.

Tests the prune_frozen() helper with a fake output/ directory of staged
frozen-<ns> dirs whose mtimes we backdate. Plus a route smoke test.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from _engine_guards import freeze_required
from cv_editor import freezer
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def fake_output(monkeypatch, tmp_path):
    """Redirect freezer.ROOT to a tmp dir so we don't touch real output/."""
    monkeypatch.setattr(freezer, "ROOT", tmp_path)
    (tmp_path / "output").mkdir()
    return tmp_path


def _stage_frozen(out_root: Path, name: str, days_ago: float):
    """Create a fake frozen workspace under output/ with mtime backdated."""
    d = out_root / "output" / name
    d.mkdir()
    (d / "cv.typ").write_text("// placeholder\n")
    ts = time.time() - days_ago * 86400
    os.utime(d, (ts, ts))
    return d


def test_prune_frozen_rejects_zero_days(fake_output):
    with pytest.raises(ValueError):
        freezer.prune_frozen(days_old=0)


def test_prune_frozen_rejects_negative(fake_output):
    with pytest.raises(ValueError):
        freezer.prune_frozen(days_old=-1)


def test_prune_frozen_keeps_recent(fake_output):
    _stage_frozen(fake_output, "frozen-100", days_ago=5)
    _stage_frozen(fake_output, "frozen-200", days_ago=1)
    deleted = freezer.prune_frozen(days_old=30)
    assert deleted == []
    assert (fake_output / "output" / "frozen-100").exists()
    assert (fake_output / "output" / "frozen-200").exists()


def test_prune_frozen_deletes_only_old(fake_output):
    _stage_frozen(fake_output, "frozen-100", days_ago=45)  # old
    _stage_frozen(fake_output, "frozen-200", days_ago=10)  # recent
    _stage_frozen(fake_output, "frozen-300", days_ago=400)  # very old
    deleted = freezer.prune_frozen(days_old=30)
    assert set(deleted) == {"frozen-100", "frozen-300"}
    assert not (fake_output / "output" / "frozen-100").exists()
    assert (fake_output / "output" / "frozen-200").exists()
    assert not (fake_output / "output" / "frozen-300").exists()


def test_prune_frozen_handles_empty(fake_output):
    deleted = freezer.prune_frozen(days_old=30)
    assert deleted == []


def test_prune_frozen_handles_missing_output_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(freezer, "ROOT", tmp_path)
    # No output/ created
    deleted = freezer.prune_frozen(days_old=30)
    assert deleted == []


# ---- route smoke ----


@freeze_required
def test_freeze_prune_route_with_bad_days(client):
    resp = client.post("/freeze/prune", data={"days_old": "abc"}, follow_redirects=False)
    assert resp.status_code == 400


@freeze_required
def test_freeze_prune_route_with_zero_days(client):
    resp = client.post("/freeze/prune", data={"days_old": "0"}, follow_redirects=False)
    assert resp.status_code == 400


@freeze_required
def test_freeze_prune_route_default_returns_302(client, monkeypatch):
    # Patch prune to avoid touching real output/.
    monkeypatch.setattr(freezer, "prune_frozen", lambda days_old=30: [])
    resp = client.post("/freeze/prune", data={"days_old": "30"}, follow_redirects=False)
    assert resp.status_code == 302


@freeze_required
def test_freeze_page_renders_prune_form(client):
    resp = client.get("/freeze")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="days_old"' in body
    assert 'Prune' in body
