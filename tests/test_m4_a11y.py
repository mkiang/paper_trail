"""M4 CP2-CP5: accessibility markup, GET-only rendered-HTML assertions.

These guard the STATIC attributes so a template refactor can't silently drop
them. JS-driven behavior (Enter/Space sort, aria-sort flip, skip-link focus
reveal, tab aria-selected toggle) is verified MANUALLY (the Flask test_client
has no DOM/JS engine). No data write — read-only GETs.
"""

from __future__ import annotations

import pytest
from cv_editor.app import create_app


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


# ---------- CP2: skip link + labeled search ----------


def test_skip_link_and_main_landmark(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="#main"' in body
    assert 'class="skip-link"' in body
    assert 'id="main"' in body
    assert 'aria-label="Search all sections"' in body  # header search input


def test_search_page_input_labeled(client):
    body = client.get("/search").get_data(as_text=True)
    assert 'for="search-q"' in body
    assert 'id="search-q"' in body


# ---------- CP3: build-console live region (footer only) ----------


def test_build_console_footer_is_live_region(client):
    # _build_console.html is included on the index (SSE rebuild) page.
    body = client.get("/").get_data(as_text=True)
    assert 'id="build-console-footer"' in body
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body
    # The streaming <pre> is keyboard-scrollable but NOT a live region (per-line
    # announcement spam). Assert its attrs individually (order-independent).
    assert 'id="build-console-body"' in body
    assert 'aria-label="Build log output"' in body


# ---------- CP4: keyboard-operable sortable headers + aria-sort ----------


def test_sortable_headers_keyboard_and_aria_sort(client):
    body = client.get("/publications").get_data(as_text=True)
    assert 'aria-sort="none"' in body
    assert '<button type="button" class="th-sort-btn"' in body
    # the th keeps its sort hooks (class + data-col) so the click handler is unchanged
    assert 'th class="sortable-col"' in body
