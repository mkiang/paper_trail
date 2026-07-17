"""V20 D2 — per-row Altmetric Resolve atomic-write tests.

Verifies the cache+YAML invariant: a resolved tracker URL is committed
to YAML in the same POST that resolves the cache. The pre-V20 route
saved only the cache, leaving YAML stranded with the tracker URL.

Two-mode contract:
- cache-only (no idx)        — current behavior preserved
- atomic     (idx + mtime)   — cache + YAML committed together
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from _engine_guards import altmetric_required  # noqa: E402
from cv_editor import altmetric_client  # noqa: E402
from cv_editor.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    """Standard test client with tmp tracker-cache redirected."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    return app.test_client()


@pytest.fixture
def sandbox_client(tmp_path, monkeypatch):
    """Test client running against a tmp copy of `data/`. Returns
    (client, sandbox_path). Use when the test needs to inspect YAML
    after a write — keeps the real publications.yml untouched.
    """
    sandbox = tmp_path / "data"
    sandbox.mkdir()
    for src in (PROJ_ROOT / "data").iterdir():
        if src.is_file():
            shutil.copy2(src, sandbox / src.name)
    monkeypatch.chdir(tmp_path)
    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    return app.test_client(), sandbox


@altmetric_required
def test_cache_only_mode_no_idx_no_yaml_write(client, monkeypatch):
    """Without idx, the route returns cache-only — no YAML write,
    no atomic_written flag, current behavior preserved."""
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test/article",
            strategy="head",
            status="resolved",
        ),
    )
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=1"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["final_url"] == "https://final.test/article"
    assert body.get("atomic_written") is not True


@altmetric_required
def test_atomic_mode_requires_existing_entry(client, monkeypatch):
    """idx=99999 → 404, cache still saves but YAML untouched."""
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test/x",
            strategy="head",
            status="resolved",
        ),
    )
    resp = client.post(
        "/publications/altmetric/resolve",
        data={
            "url": "http://ct.moreover.com/?b=2",
            "idx": "99999",
            "mtime_ns": "0",
        },
    )
    assert resp.status_code == 404
    body = resp.get_json()
    assert "not found" in (body.get("error") or "").lower()


@altmetric_required
def test_atomic_mode_returns_409_on_stale_mtime(client, monkeypatch):
    """A bogus mtime_ns triggers write_or_409 → 409 with view_url."""
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test/y",
            strategy="head",
            status="resolved",
        ),
    )
    # Pick a real existing publication idx (0 should exist in any
    # non-empty fixture).
    resp = client.post(
        "/publications/altmetric/resolve",
        data={
            "url": "http://ct.moreover.com/?c=3",
            "idx": "0",
            "mtime_ns": "1",  # ancient — will conflict with real mtime
        },
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body.get("conflict") is True
    assert "view_url" in body


@altmetric_required
def test_atomic_mode_failure_does_not_write(client, monkeypatch):
    """When resolution fails, atomic mode returns 200 with the failure
    payload — no YAML write attempted (would write nothing useful).
    """
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            status="failed_network",
            error="boom",
        ),
    )
    resp = client.post(
        "/publications/altmetric/resolve",
        data={
            "url": "http://ct.moreover.com/?d=4",
            "idx": "0",
            "mtime_ns": "0",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["final_url"] is None
    assert body["status"] == "failed_network"
    assert body.get("atomic_written") is not True


@altmetric_required
def test_invalid_idx_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test/z",
            status="resolved",
        ),
    )
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://t.example/x", "idx": "not-an-int"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "invalid idx" in (body.get("error") or "").lower()


def test_tracker_walk_substitution_contract():
    """Post-impl-review HIGH (Reviewer C): D2's atomic-write path uses
    tracker_walk.substitute_tracker_urls_on_entry to rewrite the outlet
    URL in the loaded YAML before write_or_409 commits. The route-level
    success path returns 200/atomic_written=True; to prove the actual
    YAML substitution works, assert the contract at the unit level
    here (route-level YAML-inspection is deferred to a future
    `app.config["DATA_DIR"]` knob — see scripts/CLAUDE.md deferred-
    refactor entry).
    """
    from cv_editor import tracker_walk

    entry = {
        "title": "fake",
        "notes": [
            {
                "type": "media",
                "outlets": [
                    {"name": "Example", "url": "http://ct.moreover.com/?a=1"},
                ],
            },
        ],
    }
    tracker_walk.substitute_tracker_urls_on_entry(
        entry,
        {"http://ct.moreover.com/?a=1": "https://final.test/article"},
    )
    assert entry["notes"][0]["outlets"][0]["url"] == "https://final.test/article"


@altmetric_required
def test_atomic_mode_409_response_shape(client, monkeypatch):
    """Post-impl-review HIGH (Reviewer A): on 409 the JSON contract is
    `{conflict: true, view_url, error}` — this is what the trackers.html
    JS branches on. The in-memory cache-rollback behaviour is documented
    in app.py's post-impl-fix comment; this test pins the response
    shape so a regression on JSON keys would fail loudly.
    """
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test/x",
            strategy="head",
            status="resolved",
        ),
    )
    resp = client.post(
        "/publications/altmetric/resolve",
        data={
            "url": "http://ct.moreover.com/?bouncetest=1",
            "idx": "0",
            "mtime_ns": "1",  # stale; will conflict
        },
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body.get("conflict") is True
    assert "view_url" in body
    assert body.get("final_url") == "https://final.test/x"
    assert "stale" in (body.get("error") or "").lower()


@altmetric_required
def test_trackers_page_passes_pub_idx_and_mtime(client, tmp_path):
    """The trackers page renders pub_idx + pub_mtime_ns into the
    Resolve buttons so per-row POST has what atomic mode needs."""
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    # Either the queue is empty (the "all done" banner is shown) or
    # buttons have the new data attributes. Either way the template
    # renders without error.
    # The atomic-mode wiring is also asserted by template inspection:
    from cv_editor import app as app_mod

    tpl_path = Path(app_mod.__file__).parent / "templates" / "trackers.html"
    tpl = tpl_path.read_text()
    assert "data-idx" in tpl
    assert "data-mtime" in tpl
    assert "pub_mtime_ns" in tpl
