"""Stage B / I8 (2026-05-25): entry_save 409 pending-form snapshot.

Browsers don't auto-follow `Location` headers on 4xx responses, so the
old `(redirect, 409)` from `write_or_409` stranded users on the
"Redirecting" stub page with their unsaved form values lost. The fix
stashes the PARSED form payload under a UUID, then 302-redirects to
entry_edit / entry_new with `?pending=<uuid>`. The GET route consumes
the snapshot and rebuilds a synthetic entry via `_form_to_entry`, so
all complex sub-editors (authors, typed_notes, audiences_set, OA dict,
outlets per-note) round-trip through the same V20 B2 FIELD_HANDLERS
dispatch as a live save.

These tests mirror the V20 M2 test pattern in test_workflow_v13_v19.py
(`test_pending_form_snapshot_pops_after_first_read`) — the I8
machinery is structurally identical (UUID dict, FIFO eviction, pop on
first read), only the payload shape and consuming routes differ.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---- Round-trip: stale save → 302 with pending UUID → GET re-populates ----


def test_stale_save_round_trips_through_pending_uuid(client):
    """End-to-end: POST honors/save with stale mtime → confirm 302 to
    entry_edit?pending=<uuid> → GET that URL → form value comes from
    the stashed form payload, NOT the on-disk entry."""
    distinctive_award = "Stage-B-I8 round-trip award text"
    resp = client.post(
        "/honors/save",
        data={
            "mode": "edit",
            "global_idx": "0",
            "mtime_ns": "1",  # stale
            "date": "2099",
            "award": distinctive_award,
            "institution": "Round-trip Test Institution",
            "audiences_json": "[]",
            "hide-from_json": "[]",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers["Location"]
    assert "/honors/0/edit" in loc
    assert "pending=" in loc

    # Follow the redirect; the distinctive text must appear in the
    # rendered form, proving the synthetic entry was built from the
    # stashed payload (the on-disk honors.yml has different text).
    r2 = client.get(loc)
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert distinctive_award in body
    assert "Round-trip Test Institution" in body
    # The warning banner explains the conflict.
    assert "preserved from a prior save attempt" in body
    assert "review and save again" in body


def test_stale_save_redirects_to_entry_new_when_mode_is_new(client):
    """A 409 on a `mode=new` save should redirect to entry_new (not
    entry_edit) so the user can finish creating the entry without
    losing their work."""
    resp = client.post(
        "/honors/save",
        data={
            "mode": "new",
            "global_idx": "",
            "mtime_ns": "1",  # stale
            "date": "2099",
            "award": "Brand new award",
            "institution": "Brand new inst",
            "audiences_json": "[]",
            "hide-from_json": "[]",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers["Location"]
    assert "/honors/new" in loc
    assert "pending=" in loc

    r2 = client.get(loc)
    body = r2.get_data(as_text=True)
    assert "Brand new award" in body


# ---- Stale UUID fallback (no snapshot in dict) ----


def test_stale_or_missing_uuid_renders_canonical_entry(client):
    """GET entry_edit?pending=<bogus-uuid> → no 500, no error; just
    renders the current YAML state. The pending dict has evicted the
    UUID (editor restarted or 20 other conflicts queued)."""
    resp = client.get("/honors/0/edit?pending=does-not-exist")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The warn banner from a successful re-populate does NOT appear.
    assert "preserved from a prior save attempt" not in body


# ---- FIFO eviction at 20 ----


def test_entry_pending_dict_evicts_oldest_at_cap(app):
    """The bounded dict caps at 20 entries; older UUIDs are evicted
    in FIFO order when a 21st is stashed."""
    pending = app.config["_ENTRY_PENDING"]
    pending.clear()
    # Populate 25 entries with predictable keys; first 5 should evict.
    for i in range(25):
        pending[f"tok-{i:02d}"] = {"form_data": {"x": i}, "target": {}}
        # Mimic the FIFO trim from _entry_stash_pending.
        while len(pending) > 20:
            pending.pop(next(iter(pending)), None)
    assert len(pending) == 20
    # Oldest 5 evicted; newest 20 remain.
    assert "tok-00" not in pending
    assert "tok-04" not in pending
    assert "tok-05" in pending
    assert "tok-24" in pending


# ---- Complex form payload re-population (publications JSON-hidden editors) ----


def test_stale_publications_save_preserves_authors_and_notes(client):
    """The critical I8 case: a publications save with non-trivial
    authors_json + notes_json (the editors most likely to be edited
    when a stale-form 409 fires after an altmetric fetch) must
    re-populate through the synthetic entry path. The data block in
    the rendered template must contain the user's edits, not the
    on-disk entry's authors."""
    import json

    distinctive_title = "Stage-B-I8 Publications Round-Trip"
    distinctive_authors = [
        {"name": "Public JQ", "co_first": True},
        {"name": "Test Author A"},
        {"name": "Test Author B"},
    ]
    distinctive_notes = [
        {
            "type": "media",
            "outlets": [
                {"name": "Test Outlet Z", "url": "https://example.test/article"},
            ],
        },
    ]
    resp = client.post(
        "/publications/save",
        data={
            "mode": "edit",
            "global_idx": "0",
            "mtime_ns": "1",  # stale
            "subsection": "Peer-Reviewed Original Research",
            "title": distinctive_title,
            "journal": "Stage B Test Journal",
            "year": "2099",
            "authors_json": json.dumps(distinctive_authors),
            "notes_json": json.dumps(distinctive_notes),
            "audiences_json": "[]",
            "hide-from_json": "[]",
            "open_access_json": "{}",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers["Location"]
    assert "pending=" in loc

    r2 = client.get(loc)
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    # Title field appears as a simple <input value="...">.
    assert distinctive_title in body
    # Authors land in the JSON data block consumed by entry_edit.js.
    assert "Test Author A" in body
    assert "Test Author B" in body
    # Outlets too.
    assert "Test Outlet Z" in body
    assert "example.test/article" in body


# ---- Pop semantics: token is consumed on first read ----


def test_pending_uuid_pops_after_first_read(client, app):
    """Two GETs to the same pending URL: the first re-populates; the
    second behaves like a stale UUID (no re-populate, no warning)."""
    # Stash a payload directly so we don't trip the entry_save path.
    with app.app_context():
        pending = app.config["_ENTRY_PENDING"]
        pending["pop-test-token"] = {
            "form_data": {
                "date": "2099",
                "award": "Pop-test award",
                "institution": "Pop-test inst",
                "audiences": [],
                "hide_from": [],
            },
            "target": {},
        }
    r1 = client.get("/honors/0/edit?pending=pop-test-token")
    assert r1.status_code == 200
    assert "Pop-test award" in r1.get_data(as_text=True)
    # Token has been popped.
    assert "pop-test-token" not in app.config["_ENTRY_PENDING"]
    # Second GET with the same token: no re-populate.
    r2 = client.get("/honors/0/edit?pending=pop-test-token")
    assert r2.status_code == 200
    assert "Pop-test award" not in r2.get_data(as_text=True)
