"""M4 CP0 + CP1: js_mounted sentinel + the save-route data-safety guard.

The entry-edit form's JS-driven hidden fields (authors/notes/open_access/
audiences/sections) submit EMPTY if JavaScript fails to mount, and an empty
`<field>_json` wipes the existing value (field_handlers pops the key). The guard
(sections_routes._js_unmounted_rejection) refuses the save when `js_mounted` is
PRESENT but != "1" — the page rendered but its JS didn't finish. A MISSING field
(test / non-form POST) is allowed, since the production template always renders
it. Every test here is WRITE-FREE: the rejection path returns before any write,
and the helper logic is exercised via request contexts — so no real data/*.yml
is mutated and the corruption canary is never armed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor.app import create_app
from cv_editor.sections_routes import _js_unmounted_rejection

ROOT = Path(__file__).resolve().parent.parent
PUBS = ROOT / "data" / "publications.yml"
META = ROOT / "data" / "meta.yml"


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---------- CP0: sentinel + noscript render ----------


def test_edit_form_ships_sentinel_and_noscript(client):
    resp = client.get("/publications/0/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="js_mounted"' in body
    assert "<noscript" in body


def test_meta_edit_form_ships_sentinel(client):
    # /meta/edit renders the SAME entry_edit.html (section_key=meta), so the
    # sentinel covers meta_save too.
    resp = client.get("/meta/edit")
    assert resp.status_code == 200
    assert 'name="js_mounted"' in resp.get_data(as_text=True)


# ---------- CP1: the guard rejects + writes nothing ----------


def test_entry_save_rejected_when_js_unmounted_no_write(client):
    before = PUBS.read_bytes()
    resp = client.post(
        "/publications/save",
        data={"js_mounted": "", "mode": "edit", "global_idx": "0"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)  # guard redirected
    assert PUBS.read_bytes() == before  # ...and wrote nothing


def test_meta_save_rejected_when_js_unmounted_no_write(client):
    before = META.read_bytes()
    resp = client.post(
        "/meta/save",
        data={"js_mounted": "", "mtime_ns": "0"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert META.read_bytes() == before


def test_guard_helper_three_states(app):
    # present-and-empty -> reject; "1" -> allow; absent -> allow.
    with app.test_request_context(method="POST", data={"js_mounted": ""}):
        assert _js_unmounted_rejection() is not None
    with app.test_request_context(method="POST", data={"js_mounted": "1"}):
        assert _js_unmounted_rejection() is None
    with app.test_request_context(method="POST", data={}):
        assert _js_unmounted_rejection() is None
