"""V13: paste press URL → media outlet.

Covers:
- url_title_fetcher.fetch_title: og:title preference, fallback chain,
  charset handling, entity unescape, whitespace collapse, length cap,
  body cap, failure paths (404, timeout, no title).
- url_helpers.is_safe_fetch_url: scheme allow-list.
- /publications/fetch_title route: JSON shapes for success / 400 /
  fetch-failure.
- No-PII enforcement on the module's UA string.
"""

from __future__ import annotations

import io
import socket
import urllib.error

import pytest
from cv_editor import url_helpers, url_title_fetcher
from cv_editor.app import create_app

# ----- urlopen stub --------------------------------------------------


class _FakeResp:
    """Context-manager stub matching urllib.urlopen's interface."""

    def __init__(self, body: bytes, content_type: str = "text/html"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return self._body
        return self._body[:n]


def _patch_urlopen(monkeypatch, body=b"", content_type="text/html", exc: Exception | None = None):
    # M1 (2026-05-29): the fetchers now route through the SSRF-safe seam
    # url_helpers.safe_urlopen (host pre-check + redirect re-validation).
    # Mock at that seam so the fake `x.test` host bypasses the real DNS
    # check and no network is touched.
    def fake(req, timeout=None):
        if exc is not None:
            raise exc
        return _FakeResp(body, content_type=content_type)

    monkeypatch.setattr(url_helpers, "safe_urlopen", fake)


# ----- fetch_title: title-source preference --------------------------


def test_fetch_title_prefers_og_title(monkeypatch):
    body = (
        b'<html><head>'
        b'<meta property="og:title" content="OG Headline">'
        b'<title>Page Title | CNN</title>'
        b'</head></html>'
    )
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") == "OG Headline"


def test_fetch_title_og_title_content_first(monkeypatch):
    # property/content attribute order swapped.
    body = (
        b'<html><head><meta content="Swapped" property="og:title"><title>Page</title></head></html>'
    )
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") == "Swapped"


def test_fetch_title_falls_back_to_title_tag(monkeypatch):
    body = b"<html><head><title>Just A Title</title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") == "Just A Title"


def test_fetch_title_returns_none_when_no_title(monkeypatch):
    body = b"<html><head></head><body>no title here</body></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") is None


def test_fetch_title_returns_none_when_title_is_empty(monkeypatch):
    body = b"<html><head><title>   </title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") is None


# ----- fetch_title: cleaning -----------------------------------------


def test_fetch_title_unescapes_html_entities(monkeypatch):
    body = b"<html><head><title>Tom &amp; Jerry &#8217;s ride</title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    result = url_title_fetcher.fetch_title("https://x.test/a")
    assert result == "Tom & Jerry ’s ride"


def test_fetch_title_collapses_whitespace(monkeypatch):
    body = b"<html><head><title>\n  Multi\n\t  Line\n   Title  </title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") == "Multi Line Title"


def test_fetch_title_truncates_at_max_len(monkeypatch):
    long_title = b"X" * (url_title_fetcher.MAX_TITLE_LEN + 200)
    body = b"<html><head><title>" + long_title + b"</title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    result = url_title_fetcher.fetch_title("https://x.test/a")
    assert result is not None
    assert len(result) == url_title_fetcher.MAX_TITLE_LEN


# ----- fetch_title: charset handling ---------------------------------


def test_fetch_title_honors_content_type_charset(monkeypatch):
    # 0xe9 == 'é' in iso-8859-1.
    body = b"<html><head><title>caf\xe9</title></head></html>"
    _patch_urlopen(
        monkeypatch,
        body=body,
        content_type="text/html; charset=iso-8859-1",
    )
    assert url_title_fetcher.fetch_title("https://x.test/a") == "café"


def test_fetch_title_honors_meta_charset_when_header_missing(monkeypatch):
    body = b'<html><head><meta charset="iso-8859-1"><title>caf\xe9</title></head></html>'
    _patch_urlopen(monkeypatch, body=body, content_type="text/html")
    assert url_title_fetcher.fetch_title("https://x.test/a") == "café"


def test_fetch_title_unknown_charset_falls_back_to_utf8(monkeypatch):
    body = b"<html><head><title>Plain ASCII</title></head></html>"
    _patch_urlopen(
        monkeypatch,
        body=body,
        content_type="text/html; charset=zzz-not-a-thing",
    )
    assert url_title_fetcher.fetch_title("https://x.test/a") == "Plain ASCII"


# ----- fetch_title: body cap -----------------------------------------


def test_fetch_title_caps_body_at_max_bytes_title_past_cap(monkeypatch):
    # Pad to push <title> past MAX_BYTES.
    pad = b"<!-- " + (b"x" * (url_title_fetcher.MAX_BYTES + 100)) + b" -->"
    body = b"<html><head>" + pad + b"<title>Hidden</title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") is None


def test_fetch_title_caps_body_at_max_bytes_title_within_cap(monkeypatch):
    body = b"<html><head><title>Visible</title></head></html>"
    _patch_urlopen(monkeypatch, body=body)
    assert url_title_fetcher.fetch_title("https://x.test/a") == "Visible"


# ----- fetch_title: failure paths ------------------------------------


def test_fetch_title_returns_none_on_404(monkeypatch):
    exc = urllib.error.HTTPError(
        "https://x.test/missing",
        404,
        "Not Found",
        {},
        None,
    )
    _patch_urlopen(monkeypatch, exc=exc)
    assert url_title_fetcher.fetch_title("https://x.test/missing") is None


def test_fetch_title_returns_none_on_timeout(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        exc=urllib.error.URLError(socket.timeout("timed out")),
    )
    assert url_title_fetcher.fetch_title("https://x.test/slow") is None


def test_fetch_title_returns_none_on_dns_failure(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        exc=urllib.error.URLError(socket.gaierror(-2, "no such host")),
    )
    assert url_title_fetcher.fetch_title("https://nope.example/") is None


def test_fetch_title_returns_none_on_value_error(monkeypatch):
    # urllib raises ValueError on malformed URLs (e.g. "http://").
    _patch_urlopen(monkeypatch, exc=ValueError("unknown url"))
    assert url_title_fetcher.fetch_title("http://") is None


def test_fetch_title_sends_project_ua(monkeypatch):
    captured = {}

    def fake(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return _FakeResp(b"<title>x</title>", "text/html")

    monkeypatch.setattr(url_helpers, "safe_urlopen", fake)
    url_title_fetcher.fetch_title("https://x.test/a")
    assert captured["ua"] == "cv-editor/1.0"


# ----- is_safe_fetch_url ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/path?q=1",
        "HTTP://example.com",
        "  https://example.com/  ",
    ],
)
def test_is_safe_fetch_url_accepts_http_https(url):
    assert url_helpers.is_safe_fetch_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://x.test/",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ssh://x.test/",
        "//example.com/path",  # scheme-relative
        "/relative/path",
        "example.com",  # no scheme
    ],
)
def test_is_safe_fetch_url_rejects_non_http(url):
    assert not url_helpers.is_safe_fetch_url(url)


@pytest.mark.parametrize("url", ["", "   ", None, 42, [], {}])
def test_is_safe_fetch_url_rejects_empty_and_non_string(url):
    assert not url_helpers.is_safe_fetch_url(url)


def test_is_safe_fetch_url_rejects_http_without_host():
    assert not url_helpers.is_safe_fetch_url("http://")
    assert not url_helpers.is_safe_fetch_url("https:///path")


# ----- /publications/fetch_title route -------------------------------


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_route_fetch_title_returns_json_on_success(client, monkeypatch):
    monkeypatch.setattr(
        url_title_fetcher,
        "fetch_title",
        lambda url, **kw: "Real Headline",
    )
    resp = client.post(
        "/publications/fetch_title",
        data={"url": "https://example.com/article"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {
        "title": "Real Headline",
        "url": "https://example.com/article",
        "error": None,
    }


def test_route_fetch_title_returns_400_on_invalid_scheme(client):
    resp = client.post(
        "/publications/fetch_title",
        data={"url": "javascript:alert(1)"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["title"] is None
    assert body["error"] == "Invalid URL"


def test_route_fetch_title_returns_400_on_empty_url(client):
    resp = client.post("/publications/fetch_title", data={"url": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid URL"


def test_route_fetch_title_returns_400_on_missing_url(client):
    resp = client.post("/publications/fetch_title", data={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid URL"


def test_route_fetch_title_returns_200_with_error_on_fetch_failure(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        url_title_fetcher,
        "fetch_title",
        lambda url, **kw: None,
    )
    resp = client.post(
        "/publications/fetch_title",
        data={"url": "https://example.com/dead"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["title"] is None
    assert body["url"] == "https://example.com/dead"
    assert body["error"] == "Could not fetch title"


def test_route_fetch_title_strips_whitespace_in_url(client, monkeypatch):
    captured = {}

    def fake(url, **kw):
        captured["url"] = url
        return "OK"

    monkeypatch.setattr(url_title_fetcher, "fetch_title", fake)
    resp = client.post(
        "/publications/fetch_title",
        data={"url": "  https://example.com/x  "},
    )
    assert resp.status_code == 200
    assert captured["url"] == "https://example.com/x"


# ----- No-PII enforcement (global rule) ------------------------------


def test_url_title_fetcher_no_pii_in_user_agent():
    ua = url_title_fetcher.UA.lower()
    for forbidden in ("@", "mailto:", "public", "stanford", "mathew"):  # leak-allow
        assert forbidden not in ua, (
            f"V13 fetcher UA {ua!r} leaks {forbidden!r}; see ~/.claude/CLAUDE.md Privacy rule."
        )


# ----- Entry-edit template renders the new UI ------------------------


def test_entry_edit_template_includes_paste_url_widget():
    """V13 affordance must be present in the rendered media-note editor.

    After V20 B3 (2026-05-18), the JS lives in `static/entry_edit.js`
    and the template injects only the route URL via the JSON-data
    block. Check both surfaces.
    """
    pkg_root = url_title_fetcher.__file__.replace(
        "scripts/cv_editor/url_title_fetcher.py",
        "scripts/cv_editor/",
    )
    tpl_body = io.open(pkg_root + "templates/entry_edit.html", encoding="utf-8").read()
    js_body = io.open(pkg_root + "static/entry_edit.js", encoding="utf-8").read()
    assert "outlet-paste-url" in js_body
    assert "paste-url-input" in js_body
    assert "paste-url-fetch" in js_body
    # Route URL is injected via the JSON-data block:
    assert "publications_fetch_title" in tpl_body


def test_publication_edit_page_wires_altmetric_explorer_via_js(client):
    """Stage C / I4 (2026-05-25): the Altmetric Explorer link migrated
    from the general-form rendered HTML into entry_edit.js, where it
    renders client-side inside the FIRST media note. The static HTML
    no longer carries the literal `altmetric-link` string; instead it
    must (a) include the entry_edit.js script and (b) carry
    `section_key: "publications"` in the entry-edit-data block so the
    JS gate `SECTION_KEY === "publications"` lets the bar render.
    """
    body = client.get("/publications/0/edit").get_data(as_text=True)
    assert "entry_edit.js" in body, (
        "entry_edit.js must be loaded for the in-JS Altmetric bar to render"
    )
    assert '"section_key": "publications"' in body, (
        "JS gate requires section_key=publications to enable the Altmetric bar"
    )


def test_non_publication_edit_pages_omit_altmetric(client):
    """Altmetric is publication-specific; talks / service / etc. should not
    render the link in static HTML. (Post-Stage-C: publications also no
    longer render it statically — it's created by JS at runtime — but
    the JS gate ensures non-publications never get the bar even after
    JS runs.)"""
    for section in ("presentations", "service", "teaching"):
        body = client.get(f"/{section}/0/edit").get_data(as_text=True)
        assert "altmetric-link" not in body, f"{section} edit page should not show Altmetric link"
        # Confirm the data block ALSO doesn't claim section_key=publications
        # (defensive: if a future refactor broke section_key plumbing the
        # JS gate would silently leak the bar to other sections).
        assert '"section_key": "publications"' not in body, (
            f"{section} edit page data block should NOT carry section_key=publications"
        )


# ----- Presentations date column matches publications year column ----


def test_presentations_date_column_renders_year_with_muted_month(client):
    """Talks date column should mirror the publications year column:
    year prominent + muted ` · MM` sub-cell, not the raw `MM/YYYY`."""
    import re

    body = client.get("/presentations").get_data(as_text=True)
    # The new presentations date cell must:
    # (a) include the muted sort-sub span (the publications-style sub-cell)
    # (b) NOT render the raw 'MM/YYYY' string inside the date cell.
    cell_pat = re.compile(
        r'<td[^>]*class="col-date[^"]*"[^>]*data-sort-value="(\d{6})_\d{6}"[^>]*>'
        r'(.*?)</td>',
        re.DOTALL,
    )
    matches = cell_pat.findall(body)
    assert matches, "no date cells found for presentations"
    saw_styled = False
    for sortval, inner in matches:
        # The styled form shows the year (first 4 of sortval) followed by
        # a muted `· MM` sub-cell.
        year = sortval[:4]
        month = sortval[4:6]
        if month == "00":
            continue  # year-only dates fall back to raw display
        # Year must appear as visible text, raw MM/YYYY must NOT.
        assert year in inner, f"expected year {year} in cell, got {inner!r}"
        assert f"{month}/{year}" not in inner, (
            f"expected styled display, but raw {month}/{year} still rendered"
        )
        assert 'class="muted sort-sub"' in inner, f"expected muted sub-cell in {inner!r}"
        assert f"· {month}" in inner, f"expected `· {month}` muted suffix in {inner!r}"
        saw_styled = True
    assert saw_styled, "no presentation rows exercised the styled display"


def test_presentations_date_sort_values_still_normalized(client):
    """Display change must not regress the V17-D cross-year sort fix
    (data-sort-value must remain YYYYMM_YYYYMM, not raw MM/YYYY)."""
    import re

    body = client.get("/presentations").get_data(as_text=True)
    pattern = re.compile(r'<td[^>]*class="col-date[^"]*"[^>]*data-sort-value="([^"]*)"')
    sortvals = [v for v in pattern.findall(body) if v]
    assert sortvals, "no presentation date sort values rendered"
    for v in sortvals:
        assert re.match(r"^\d{6}_\d{6}$", v), f"sort key regressed away from YYYYMM_YYYYMM: {v!r}"
