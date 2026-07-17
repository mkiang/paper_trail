"""V13 press-URL title fetcher.

Pure synchronous helper for the /publications/fetch_title route.
Reads at most MAX_BYTES; tries og:title (cleaner on news sites) then
falls back to <title>. Returns None on any failure so callers can show
a "fill it in by hand" affordance instead of an error stack.

No cache (one-shot fetches), no PII in User-Agent (global rule).
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request

from cv_editor import url_helpers

UA = "cv-editor/1.0"  # matches enrichment.UA; no PII per global rule
TIMEOUT = 10  # seconds; matches verify_urls per-request budget
MAX_BYTES = 200 * 1024  # 200 KB body cap; <head> fits comfortably
MAX_TITLE_LEN = 500  # post-strip cap; defensive against pathological titles

# og:title may appear with property and content in either attribute order.
_OG_TITLE_PROP_FIRST_RE = re.compile(
    rb'<meta\b[^>]*?property\s*=\s*["\']og:title["\'][^>]*?content\s*=\s*'
    rb'["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_TITLE_CONTENT_FIRST_RE = re.compile(
    rb'<meta\b[^>]*?content\s*=\s*["\']([^"\']*)["\'][^>]*?property\s*=\s*'
    rb'["\']og:title["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(rb"<title\b[^>]*>([^<]*)</title>", re.IGNORECASE | re.DOTALL)
_CHARSET_HEADER_RE = re.compile(r"charset\s*=\s*([\w\-]+)", re.IGNORECASE)
_CHARSET_META_RE = re.compile(
    rb'<meta\b[^>]*?charset\s*=\s*["\']?([\w\-]+)',
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def fetch_title(url: str, *, timeout: int = TIMEOUT) -> str | None:
    """Return a cleaned title for `url`, or None on any failure.

    Prefers OpenGraph og:title (news sites curate it without the
    "| CNN" boilerplate that often pollutes <title>); falls back to
    <title> when og:title is absent.
    """
    # SSRF guard (M1): safe_urlopen rejects non-public targets before
    # connecting and re-validates every redirect hop.
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with url_helpers.safe_urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES)
            content_type = resp.headers.get("Content-Type", "") or ""
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

    title_bytes = _extract_title_bytes(body)
    if not title_bytes:
        return None

    charset = _guess_charset(content_type, body)
    try:
        text = title_bytes.decode(charset, errors="replace")
    except (LookupError, TypeError):
        text = title_bytes.decode("utf-8", errors="replace")

    return _clean(text)


def _extract_title_bytes(body: bytes) -> bytes | None:
    for pat in (_OG_TITLE_PROP_FIRST_RE, _OG_TITLE_CONTENT_FIRST_RE):
        m = pat.search(body)
        if m and m.group(1):
            return m.group(1)
    m = _TITLE_RE.search(body)
    if m and m.group(1):
        return m.group(1)
    return None


def _guess_charset(content_type: str, body: bytes) -> str:
    m = _CHARSET_HEADER_RE.search(content_type)
    if m:
        return m.group(1)
    m = _CHARSET_META_RE.search(body)
    if m:
        return m.group(1).decode("ascii", errors="ignore") or "utf-8"
    return "utf-8"


def _clean(text: str) -> str | None:
    text = html.unescape(text).strip()
    text = _WHITESPACE_RE.sub(" ", text)
    if not text:
        return None
    return text[:MAX_TITLE_LEN]
