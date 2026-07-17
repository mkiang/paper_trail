"""V15 quick-add tests (2026-05-17).

Each non-meta section card on the index page exposes a `+` quick-add
button linking straight to that section's `new` form. Meta is a
`single_record` so it gets no quick-add (no "new entry" concept).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from cv_editor import schemas  # noqa: E402


@pytest.fixture
def client():
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


_NON_META_SECTIONS = [k for k in schemas.all_sections() if k != "meta"]


def test_index_renders_quick_add_for_every_non_meta_section(client):
    body = client.get("/").get_data(as_text=True)
    for key in _NON_META_SECTIONS:
        href = f'href="/{key}/new"'
        assert href in body, f"missing quick-add link for /{key}/new"


def test_index_omits_quick_add_for_meta(client):
    body = client.get("/").get_data(as_text=True)
    # No /meta/new link should appear; the meta card is a single_record,
    # there's no "new entry" route for it.
    assert 'href="/meta/new"' not in body


def test_quick_add_button_targets_entry_new_route(client):
    """The link points to the same URL as the existing "+ Add entry" link
    on the section list page — proves V15 is purely a shortcut."""
    body = client.get("/").get_data(as_text=True)
    for key in _NON_META_SECTIONS:
        list_body = client.get(f"/{key}").get_data(as_text=True)
        # Section list page has a "+ Add entry" or "Manual entry" link.
        # Both forms point at the same URL pattern.
        assert f'/{key}/new' in list_body
        # Index page exposes the same target via the quick-add overlay.
        assert f'href="/{key}/new"' in body


def test_quick_add_has_aria_label_and_title(client):
    body = client.get("/").get_data(as_text=True)
    # At least one quick-add link has both aria-label and title attributes.
    assert 'aria-label="Add new entry to' in body
    assert 'title="Add a new' in body


def test_quick_add_button_uses_btn_quick_add_class(client):
    body = client.get("/").get_data(as_text=True)
    assert 'class="btn-quick-add"' in body


def test_quick_add_url_links_to_blank_form(client):
    """Following the quick-add URL renders the new-entry form (200) with
    the section-specific schema."""
    for key in _NON_META_SECTIONS:
        resp = client.get(f"/{key}/new")
        assert resp.status_code == 200, f"/{key}/new returned {resp.status_code}"
        body = resp.get_data(as_text=True)
        # The new-entry form has an empty mode="new" hidden input.
        assert 'name="mode" value="new"' in body or 'value="new"' in body
