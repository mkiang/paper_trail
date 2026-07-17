"""V20 B3 — static JS extraction smoke tests (post-impl review HIGH).

The 735 LOC of inline JS in templates/entry_edit.html moved to
static/entry_edit.js + static/list_editor.js. The plan called out
three regression-guards that didn't initially land:

  1. The Jinja template no longer carries the big inline <script>.
  2. The static assets are served via Flask's static handler.
  3. The new <script id="entry-edit-data"> JSON block parses.

If these three break, the editor silently degrades (forms don't bind,
authors editor doesn't mount, etc.). Smoke-test them here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from cv_editor.app import create_app  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_template_no_longer_contains_inline_iife():
    """The big `(function () { ... })()` IIFE that ran 735 LOC inside
    the template is gone. A specific anchor: the inline block used to
    declare `const SECTION_KEY = {{ section_key | tojson | safe }}` —
    that line, if it reappears, means someone re-inlined the JS.
    """
    tpl = (PROJ_ROOT / "scripts" / "cv_editor" / "templates" / "entry_edit.html").read_text()
    assert "const SECTION_KEY = " not in tpl
    assert "function renderAuthors" not in tpl
    assert "function renderNotes" not in tpl
    # The two new <script src> imports are present:
    assert "list_editor.js" in tpl
    assert "entry_edit.js" in tpl


def test_static_entry_edit_js_is_served(client):
    """Flask's built-in static-file handler serves the extracted JS."""
    resp = client.get("/static/entry_edit.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "renderAuthors" in body
    assert "renderNotes" in body
    assert "outlet-altmetric" in body


def test_static_list_editor_js_is_served(client):
    """The ListEditor factory ships separately."""
    resp = client.get("/static/list_editor.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "window.ListEditor" in body
    assert "list-up" in body
    assert "list-down" in body
    assert "list-remove" in body


def test_entry_edit_renders_well_formed_json_data_block(client):
    """The `<script id="entry-edit-data" type="application/json">`
    block must contain valid JSON parseable by `JSON.parse()`. If the
    template's `{{ var | tojson }}` interpolation breaks (e.g.,
    Jinja's autoescape interaction with a context variable that's not
    a primitive), the JS payload silently corrupts and the editor
    fails to initialize.
    """
    resp = client.get("/publications/0/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Extract the JSON block.
    import re

    m = re.search(
        r'<script id="entry-edit-data"[^>]*>(.*?)</script>',
        body,
        flags=re.DOTALL,
    )
    assert m is not None, "JSON data block missing from rendered entry_edit.html"
    payload = m.group(1).strip()
    # Parse — raises ValueError if malformed.
    data = json.loads(payload)
    # The required keys for entry_edit.js to bootstrap:
    for key in (
        "section_key",
        "primary_note_types",
        "all_note_types",
        "note_type_label",
        "tracker_hosts",
        "routes",
    ):
        assert key in data, f"required key {key!r} missing from JSON-data block"
    # `fetch_title` is always present; `altmetric_resolve` is only wired
    # when the active template has the altmetric capability (P5) — under a
    # capless template (e.g. public `modern`) the route isn't registered, so
    # the template legitimately omits it. Gate the assertion on the cap.
    from cv_editor import capabilities

    required_routes = ["fetch_title"]
    if capabilities.current().altmetric:
        required_routes.append("altmetric_resolve")
    for route_key in required_routes:
        assert route_key in data["routes"], (
            f"required route {route_key!r} missing from routes object"
        )
    assert data["section_key"] == "publications"


def test_entry_edit_non_publications_section_has_no_crash(client):
    """The JSON-data block uses `(author_forms or [])` defaults so a
    non-publications section (teaching/service/etc.) still renders.
    Regression guard for the Jinja Undefined handling.
    """
    resp = client.get("/teaching/0/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    import re

    m = re.search(
        r'<script id="entry-edit-data"[^>]*>(.*?)</script>',
        body,
        flags=re.DOTALL,
    )
    assert m is not None
    data = json.loads(m.group(1).strip())
    # author_forms is empty (teaching has no authors field).
    assert data["author_forms"] == []
    assert data["section_key"] == "teaching"
