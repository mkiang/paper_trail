"""V12: Altmetric Explorer deep-link (no API).

The Altmetric Details Page API became key-gated on 10 November 2025
and not every institution licenses the Explorer API. Instead we deep-link the
Explorer web UI: the user (signed into Explorer) lands
on a search for the article and scans press mentions there, then
manually copies the relevant outlets into the notes.media.outlets list.

**2026-05-16 update:** DOI-based `?q=<doi>` search stopped returning
hits after the API pivot. The query now wraps the article title in
quotes (title-only; no author keyword by default).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from _engine_guards import altmetric_required
from cv_editor import schemas, sections, url_helpers, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- altmetric_url() pure function ----


def test_altmetric_url_basic_title():
    """Title is wrapped in double quotes as a title-only query (no author
    keyword by default)."""
    out = url_helpers.altmetric_url("A Study Of Things")
    assert out.startswith("https://www.altmetric.com/explorer/highlights?q=")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"A Study Of Things"'


def test_altmetric_url_strips_typst_markup_markers():
    """`*` and `_` are Typst markup markers; they'd otherwise pollute the
    Explorer search query."""
    out = url_helpers.altmetric_url("_Effects_ of *X* on Y")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"Effects of X on Y"'


def test_altmetric_url_strips_embedded_quotes():
    """Internal `"` characters break phrase quoting; strip them so the
    wrapped form stays valid."""
    out = url_helpers.altmetric_url('A "quoted" word in title')
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"A quoted word in title"'


def test_altmetric_url_strips_curly_quotes():
    """YAML titles sometimes carry curly quotes from copy-paste; strip
    those too."""
    out = url_helpers.altmetric_url("Smith’s “title” here")
    q = unquote(out.split("?q=", 1)[1])
    # curly left/right quotes removed; apostrophe (’) preserved
    # so the search still matches the YAML title.
    assert q == "\"Smith’s title here\""


def test_altmetric_url_strips_whitespace():
    out = url_helpers.altmetric_url("  Padded title  ")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"Padded title"'


def test_altmetric_url_empty_returns_empty():
    assert url_helpers.altmetric_url("") == ""
    assert url_helpers.altmetric_url(None) == ""
    assert url_helpers.altmetric_url("   ") == ""


def test_altmetric_url_strip_only_markup_returns_empty():
    """A title that's only markup chars collapses to empty after cleanup."""
    assert url_helpers.altmetric_url("***") == ""
    assert url_helpers.altmetric_url('""""') == ""


def test_altmetric_url_custom_author():
    """The author keyword is overridable for collaborators / shared
    rebuilds, though the editor passes no author keyword by default."""
    out = url_helpers.altmetric_url("X", author="Smith")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"X" Smith'


def test_altmetric_url_no_author_when_blank():
    """Falsy author keyword drops the author term entirely (no trailing
    space)."""
    out = url_helpers.altmetric_url("X", author="")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"X"'


def test_altmetric_url_encodes_reserved_chars():
    """quote(safe='') escapes every non-unreserved char, including the
    double quotes that wrap the title."""
    # Bare colon (no following space) is preserved by the subtitle-strip
    # rule, so the encoding-coverage assertion still applies.
    out = url_helpers.altmetric_url("Time 10:30 AM")
    # `:` -> %3A, `"` -> %22, space -> %20
    assert "%3A" in out
    assert "%22" in out
    assert "%20" in out


# ---- Subtitle stripping (2026-05-25) ----


def test_altmetric_url_strips_subtitle_after_colon_space():
    """Subtitles after ": " often prevent Explorer from finding the
    article; the URL should search on the main title only."""
    out = url_helpers.altmetric_url("Main Title: A Long Descriptive Subtitle")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"Main Title"'


def test_altmetric_url_preserves_bare_colon():
    """Bare colon (no space after) is NOT a subtitle separator and must
    survive: ratios, timestamps, model names, etc."""
    out = url_helpers.altmetric_url("Mg:Ca ratio in seawater")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"Mg:Ca ratio in seawater"'


def test_altmetric_url_subtitle_only_with_empty_main_falls_back():
    """Pathological title that splits to an empty main part (e.g.,
    starts with ': ') should preserve the original cleaned value
    rather than emit an empty quoted phrase."""
    out = url_helpers.altmetric_url(": leading colon only")
    q = unquote(out.split("?q=", 1)[1])
    # The split discards the leading-colon variant only when there's a
    # non-empty main part; here `main` is "" so we keep the cleaned form.
    assert q == '": leading colon only"'


# ---- Jinja filter wires through ----


def test_altmetric_filter_registered_on_app():
    app = create_app()
    assert "altmetric_url" in app.jinja_env.filters
    out = app.jinja_env.filters["altmetric_url"]("Filter Title")
    assert out.startswith("https://www.altmetric.com/explorer/highlights?q=")
    q = unquote(out.split("?q=", 1)[1])
    assert q == '"Filter Title"'


# ---- entry_view template renders the button when a DOI is present ----
# (The placement is unchanged — still adjacent to the DOI/preprint_doi
# field on entry_view — only the search query changed.)


def _find_publication_with_doi() -> int:
    sch = schemas.get("publications")
    _, data = yaml_io.load(ROOT / sch["file"])
    for rec in sections.flatten(data, sch["structure"]):
        if rec["entry"].get("doi"):
            return rec["global_idx"]
    raise AssertionError("no publication with a DOI found")


@altmetric_required
def test_entry_view_renders_altmetric_button(client):
    idx = _find_publication_with_doi()
    resp = client.get(f"/publications/{idx}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="altmetric-link"' in body
    assert "altmetric.com/explorer/highlights" in body
    assert "View on Altmetric" in body
    # The query contains a URL-encoded quoted title fragment (%22 = `"`).
    assert "%22" in body


def test_entry_view_altmetric_button_only_for_publications(client):
    """A research_support entry has a `project` field that's code-styled
    in the same elif branch — confirm we don't accidentally render the
    Altmetric button there."""
    sch = schemas.get("research_support")
    _, data = yaml_io.load(ROOT / sch["file"])
    rec = next(sections.flatten(data, sch["structure"]))
    resp = client.get(f"/research_support/{rec['global_idx']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "altmetric" not in body.lower()


def test_entry_view_no_altmetric_button_when_doi_absent(client):
    """Placement of the link on entry_view is still inside the
    DOI/preprint_doi field's render branch, so when neither ID is
    present the link is not surfaced on that page. The edit page is
    not subject to the same constraint (V13)."""
    sch = schemas.get("publications")
    _, data = yaml_io.load(ROOT / sch["file"])
    target_idx = None
    for rec in sections.flatten(data, sch["structure"]):
        if not rec["entry"].get("doi") and not rec["entry"].get("preprint_doi"):
            target_idx = rec["global_idx"]
            break
    if target_idx is None:
        pytest.skip("every publication has a DOI; can't test the absent branch")
    resp = client.get(f"/publications/{target_idx}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "altmetric-link" not in body
