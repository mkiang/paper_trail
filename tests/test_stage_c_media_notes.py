"""Stage C / I4 + I7 (2026-05-25): media-note placement + outlet dedup.

I4: the "View on Altmetric" button moved from the general entry-edit
form (above the notes editor) into the FIRST media note's toolbar
area (rendered by entry_edit.js:renderAltmetricExplorerBar). The
entry_view detail page button is unchanged. Title-input listener
re-renders notes reactively on entry_new.

I7: outlets within a single media note are deduped by normalized URL
(case-insensitive, trailing-slash-ignoring) at save-time. First
occurrence wins; URL-less rows are preserved. Different notes can
both have the same URL (independent dedup scope).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _engine_guards import altmetric_required
from cv_editor import notes_helpers
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


# ---- I7: dedup_outlets_by_url helper ----


def test_dedup_outlets_drops_exact_duplicate_url():
    outlets = [
        {"name": "CNN", "url": "https://cnn.com/x"},
        {"name": "AP", "url": "https://cnn.com/x"},
    ]
    kept = notes_helpers.dedup_outlets_by_url(outlets)
    assert len(kept) == 1
    # First occurrence wins — CNN's name + position preserved.
    assert kept[0]["name"] == "CNN"


def test_dedup_outlets_ignores_trailing_slash():
    outlets = [
        {"name": "CNN", "url": "https://cnn.com/x"},
        {"name": "AP", "url": "https://cnn.com/x/"},
    ]
    assert len(notes_helpers.dedup_outlets_by_url(outlets)) == 1


def test_dedup_outlets_ignores_case():
    outlets = [
        {"name": "CNN", "url": "https://CNN.com/X"},
        {"name": "AP", "url": "https://cnn.com/x"},
    ]
    assert len(notes_helpers.dedup_outlets_by_url(outlets)) == 1


def test_dedup_outlets_keeps_different_urls():
    """Different URLs (genuinely different articles, even same outlet)
    must both survive — the user pasted both intentionally."""
    outlets = [
        {"name": "NPR", "url": "https://npr.org/a"},
        {"name": "NPR", "url": "https://npr.org/b"},
        {"name": "NPR", "url": "https://npr.org/c"},
    ]
    assert len(notes_helpers.dedup_outlets_by_url(outlets)) == 3


def test_dedup_outlets_preserves_rows_with_empty_url():
    """URL-less rows (outlet name only) aren't duplicates of anything
    — preserve them all even if there are several."""
    outlets = [
        {"name": "CNN", "url": ""},
        {"name": "AP", "url": ""},
        {"name": "BBC", "url": ""},
    ]
    assert len(notes_helpers.dedup_outlets_by_url(outlets)) == 3


def test_dedup_outlets_does_not_strip_query_string():
    """Different `?utm=...` may point to different campaigns/articles
    for the user's purposes; only case + trailing slash are normalized.
    Pins the scope so a future reviewer doesn't broaden it."""
    outlets = [
        {"name": "CNN", "url": "https://cnn.com/x?utm=a"},
        {"name": "AP", "url": "https://cnn.com/x?utm=b"},
    ]
    assert len(notes_helpers.dedup_outlets_by_url(outlets)) == 2


def test_dedup_outlets_preserves_order():
    outlets = [
        {"name": "First", "url": "https://example.com/1"},
        {"name": "Second", "url": "https://example.com/2"},
        {"name": "Third", "url": "https://example.com/1"},  # dup of First
        {"name": "Fourth", "url": "https://example.com/3"},
    ]
    kept = notes_helpers.dedup_outlets_by_url(outlets)
    assert [o["name"] for o in kept] == ["First", "Second", "Fourth"]


# ---- I7: dedup flows through form_note_to_yaml ----


def test_form_note_to_yaml_dedupes_media_outlets_by_url():
    """The save-time dedup in form_note_to_yaml runs before the
    CommentedMap is built, so the YAML list only carries survivors."""
    note_form = {
        "type": "media",
        "outlets": [
            {"name": "CNN", "url": "https://cnn.com/x"},
            {"name": "Dup1", "url": "https://cnn.com/x"},
            {"name": "BBC", "url": "https://bbc.co.uk/y"},
            {"name": "Dup2", "url": "https://cnn.com/x/"},  # trailing-slash dup
        ],
    }
    cm = notes_helpers.form_note_to_yaml(note_form)
    outlets = cm.get("outlets") or []
    assert len(outlets) == 2
    # First-CNN wins; BBC kept; both Dup* dropped.
    names = [o.get("name") if isinstance(o, dict) else o for o in outlets]
    assert names == ["CNN", "BBC"]


def test_notes_form_to_yaml_dedupes_within_each_note_first():
    """Per-note dedup (Stage C / I7) still runs first. Verify the within-
    note pass collapses duplicates BEFORE the cross-note pass (V23-A) sees
    them — the two layers compose. See test_v23_per_pub_outlet_dedup.py
    for the cross-note behavior."""
    notes_form = [
        {
            "type": "media",
            "outlets": [
                {"name": "CNN", "url": "https://cnn.com/x"},
                {"name": "Dup", "url": "https://cnn.com/x"},
            ],
        },
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 1
    n1_outlets = out[0].get("outlets") or []
    assert len(n1_outlets) == 1
    assert n1_outlets[0].get("name") == "CNN"


# ---- I4: general-form Altmetric link removed from entry_edit ----


def test_entry_edit_general_form_omits_altmetric_link(client):
    """The `<p class="altmetric-hint">` block above the notes editor
    is gone. The Altmetric Explorer link now renders inside the first
    media note (entry_edit.js)."""
    resp = client.get("/publications/0/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert '<p class="altmetric-hint">' not in body


@altmetric_required
def test_entry_view_altmetric_link_unchanged(client):
    """The detail page button must NOT be affected by I4."""
    resp = client.get("/publications/0")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # The entry_view button uses class="altmetric-link" with target=_blank.
    assert 'class="altmetric-link"' in body
    assert "View on Altmetric" in body


def test_entry_edit_data_block_carries_section_key_publications(client):
    """The JS-side renderAltmetricExplorerBar gates on
    SECTION_KEY === 'publications'. Confirm the data block carries
    that key so the bar will only render on publications entries."""
    resp = client.get("/publications/0/edit")
    body = resp.data.decode("utf-8")
    # The data block is injected as a JSON-typed script tag.
    assert 'id="entry-edit-data"' in body
    assert '"section_key": "publications"' in body


def test_entry_edit_other_sections_have_no_altmetric_link(client):
    """Non-publications sections (e.g. teaching) should not render the
    Altmetric link anywhere. The JS gate (SECTION_KEY !== 'publications'
    returns "") is the second line of defense; this is a smoke test
    that the template removal also extends to all sections."""
    resp = client.get("/teaching/0/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert '<p class="altmetric-hint">' not in body
    # The new in-JS bar lives in entry_edit.js, not the template, so
    # it won't appear in the rendered HTML at all on this page.
    assert "outlet-altmetric-explorer" not in body


# ---- I4: JS-Python parity baseline for altmetricExplorerUrl ----


def test_altmetric_url_parity_baseline_for_js_port():
    """Drift guard for the JS port (`entry_edit.js:altmetricExplorerUrl`).

    If you change `url_helpers.altmetric_url` (e.g., add a new strip
    rule, change the author name, broaden the subtitle separator),
    THIS test will fail loudly — and so should the JS port. The JS
    function has a comment pointing at this test ID. When you update
    Python, update both this test's expectations AND the JS port so
    the two stay in sync.

    This is a Python-only test (no JS runtime in pytest); it pins the
    canonical contract so a future JS edit has something to mirror.
    """
    from urllib.parse import unquote

    from cv_editor.url_helpers import altmetric_url

    # Each case: (input title, expected query string after URL-decode)
    cases = [
        ("Plain article", '"Plain article"'),
        ("Title: Subtitle stripped", '"Title"'),
        ("Mg:Ca preserved (bare colon)", '"Mg:Ca preserved (bare colon)"'),
        ('Markup *bold* _italic_ "quote" cleaned', '"Markup bold italic quote cleaned"'),
        (": only leading colon fallback", '": only leading colon fallback"'),
    ]
    for title, expected_q in cases:
        url = altmetric_url(title)
        assert url, f"altmetric_url({title!r}) returned empty"
        q = unquote(url.split("?q=", 1)[1])
        assert q == expected_q, (
            f"altmetric_url({title!r}): "
            f"expected query {expected_q!r}, got {q!r}. "
            "If you changed the Python implementation, also update "
            "entry_edit.js:altmetricExplorerUrl to match."
        )
