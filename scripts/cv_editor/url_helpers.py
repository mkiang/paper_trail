"""Pure URL helpers shared between the editor's Jinja filters and any
script (e.g. verify_urls.py). No Flask imports — importable from anywhere."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# Hosts whose URLs are redirect trackers (need resolution before storage
# in a CV is useful). Single source of truth — `app.py:inject_helpers`
# injects this into every template context so JS reads it via
# `{{ tracker_hosts | tojson }}`. Adding a new tracker domain: edit
# THIS frozenset; the JS side picks it up automatically.
TRACKER_HOSTS: frozenset[str] = frozenset({"ct.moreover.com"})


def is_tracker_url(url) -> bool:
    """True iff `url` parses to one of the known tracker hosts.

    Uses `urlsplit(...).hostname` (canonical accessor) rather than
    `.netloc.split(":")[0]` so `user@host` userinfo and `:port`
    suffixes are stripped consistently. Mirror of how
    `verify_urls.py:_is_publisher_redirect` reads hosts.
    """
    if not isinstance(url, str):
        return False
    s = url.strip()
    if not s:
        return False
    try:
        parsed = urlsplit(s)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in TRACKER_HOSTS


def is_safe_fetch_url(url) -> bool:
    """V13 scheme guard: only absolute http/https URLs are allowed for
    server-side proxy fetches.

    Rejects empty, non-string, file://, ftp://, javascript:, data:,
    and any relative URL. The editor is local-only so SSRF risk is low,
    but a scheme allow-list is cheap defense against the user pasting
    a stray URI by mistake.
    """
    if not isinstance(url, str):
        return False
    s = url.strip()
    if not s:
        return False
    try:
        parsed = urlsplit(s)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# SSRF guard (M1, 2026-05-29)
# ---------------------------------------------------------------------------
# The editor fetches user-pasted URLs server-side (press-URL title fetch,
# Altmetric tracker resolution). `is_safe_fetch_url` above is only a scheme
# guard; it does NOT stop a paste (or a redirect) pointing at an internal
# address (127.0.0.1, 10/8, 192.168/16, the 169.254.169.254 cloud-metadata
# endpoint, ::1, ...). The helpers below resolve the host and reject any
# non-public address, and re-validate every redirect hop.
#
# Residual gap (accepted for a loopback-only single-user tool): a TOCTOU
# DNS-rebind between getaddrinfo() and connect() is not closed (urllib
# resolves again on connect). Pinning the validated IP would need a custom
# connection class; disproportionate here. A determined local attacker is
# already out of scope for a 127.0.0.1-bound editor.


# Extra v4 ranges not flagged by ipaddress' is_private on some Python
# versions (CGNAT 100.64.0.0/10 is carrier/internal but is_private=False
# on 3.13). Listed explicitly so the block set is auditable.
_EXTRA_BLOCKED_V4 = (ipaddress.ip_network("100.64.0.0/10"),)


def _ip_is_blocked(ip_str: str) -> bool:
    """True if `ip_str` is any non-publicly-routable address (or unparseable)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if isinstance(ip, ipaddress.IPv4Address) and any(ip in net for net in _EXTRA_BLOCKED_V4):
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_public(host, port=None) -> bool:
    """Resolve `host` and return True only if EVERY resolved address is a
    public, routable IP. Blank/unresolvable hosts return False.

    Catches the SSRF targets: loopback (127/8, ::1), private (10/8,
    172.16/12, 192.168/16, fc00::/7), link-local incl. the
    169.254.169.254 cloud-metadata endpoint, reserved, multicast, and
    the 0.0.0.0 unspecified address.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return False
    if not infos:
        return False
    return all(not _ip_is_blocked(sockaddr[0]) for *_, sockaddr in infos)


def is_safe_fetch_target(url) -> bool:
    """Stronger than `is_safe_fetch_url`: scheme + netloc + DNS/IP check.

    Call this immediately before opening a server-side connection to a
    user-supplied URL. Pair it with `safe_opener()` so redirects are
    validated too.
    """
    if not is_safe_fetch_url(url):
        return False
    try:
        parsed = urlsplit(url.strip())
        # `.port` is a property that raises ValueError on a malformed port
        # (e.g. ":99999", ":abc"); keep it inside the guard so this stays
        # `-> bool` and the redirect handler raises URLError, not ValueError.
        return host_is_public(parsed.hostname, parsed.port)
    except (ValueError, AttributeError):
        return False


class _SSRFSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate each redirect hop so a public URL can't bounce the fetch
    to an internal address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_fetch_target(newurl):
            raise urllib.error.URLError(f"blocked redirect to non-public address: {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener() -> urllib.request.OpenerDirector:
    """An opener whose redirects are SSRF-validated. The INITIAL URL must
    still be pre-checked with `is_safe_fetch_target` by the caller (the
    opener only sees the request once it is already being made)."""
    return urllib.request.build_opener(_SSRFSafeRedirectHandler)


def safe_urlopen(req, *, timeout):
    """SSRF-safe replacement for `urllib.request.urlopen`. Pre-checks the
    target host, then opens via an opener that re-validates every redirect.

    The intended single network seam for the editor's server-side fetchers.
    Adopted so far by the press-URL title fetch (`url_title_fetcher`) and
    the resolved-URL HEAD probe (`head_probe`). The Altmetric tracker
    resolver (`altmetric_client`) and URL verifier (`verify_urls`) still
    use raw urllib and migrate to this seam in M2 (they need the network
    DI/test-isolation refactor to convert their existing urlopen mocks
    cleanly). Point tests at THIS function (`monkeypatch.setattr(
    url_helpers, "safe_urlopen", ...)`) rather than at module `urlopen`.

    `req` is a `urllib.request.Request` or a URL string. Raises
    `urllib.error.URLError` if the target (or any redirect hop) is not a
    public address.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    if not is_safe_fetch_target(url):
        raise urllib.error.URLError(f"blocked non-public fetch target: {url!r}")
    return safe_opener().open(req, timeout=timeout)


def id_url(value, kind: str) -> str:
    """Canonical URL for an academic identifier. Returns '' for unknown
    kinds or empty values so callers can branch on truthiness.

    Kinds supported: doi, preprint_doi, pmid, pmcid.
    """
    if not value:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    if kind in ("doi", "preprint_doi"):
        return f"https://doi.org/{v}"
    if kind == "pmid":
        return f"https://pubmed.ncbi.nlm.nih.gov/{v}/"
    if kind == "pmcid":
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{v}/"
    return ""


def altmetric_url(title, author: str = "") -> str:
    """Altmetric **Explorer** deep-link URL — searches Explorer for a
    quoted article title (optionally plus an author keyword). Requires
    the user to be signed into Explorer, which lands them on the search
    with the full mentions panel: news, blogs, X, policy, patents.

    Returns '' for empty titles so templates can branch on truthiness.

    Why title-based (changed 2026-05-16): DOI-based search via the
    Explorer `?q=` parameter stopped returning hits after the 10 Nov 2025
    Altmetric API pivot (the public web UI's keyword search doesn't index
    DOIs reliably). Title-as-quoted-phrase has proven to be a stable
    Explorer query.

    The query is `"<title>"`, with an optional ` <author>` keyword
    appended when a non-empty `author` is passed (default: none). Internal
    `"` characters and Typst markup markers (`*`, `_`) are stripped from
    the title before wrapping so they don't break the phrase quoting.

    No API key involved (the JSON Details Page API became key-gated
    on 10 Nov 2025 and not every institution licenses the Explorer API;
    the Explorer **web UI** is unaffected and freely usable by
    institutionally-affiliated users via SSO).
    """
    if not title:
        return ""
    raw = str(title).strip()
    if not raw:
        return ""
    # Strip Typst-markup markers and embedded quotes so the wrapped
    # phrase survives the URL encode without breaking quoting.
    cleaned = (
        raw.replace("*", "")
        .replace("_", "")
        .replace("“", "")  # curly left quote
        .replace("”", "")  # curly right quote
        .replace('"', "")
    ).strip()
    if not cleaned:
        return ""
    # Subtitles after ": " often prevent Explorer from finding the article;
    # search on the main title only. Use ": " (colon-space) to avoid
    # mangling ratios/timestamps/model names that contain a bare colon.
    if ": " in cleaned:
        main = cleaned.split(": ", 1)[0].strip()
        if main:
            cleaned = main
    parts = [f'"{cleaned}"']
    if author:
        author_s = str(author).strip()
        if author_s:
            parts.append(author_s)
    q = " ".join(parts)
    from urllib.parse import quote

    return f"https://www.altmetric.com/explorer/highlights?q={quote(q, safe='')}"


# V20-cleanup T4 (2026-05-18): HEAD-probe helper used by the TrackerCache
# resolved-URL TTL verification sweep. Pure stdlib; no external deps.
# Default UA is project-name only (no PII per global rule + gotcha #14).
def head_probe(url: str, *, timeout: int = 10) -> bool:
    """Return True iff `url` responds with a 2xx or 3xx status.

    False on 4xx/5xx, network errors, decode errors, or timeout.
    No body is downloaded. UA is `cv-editor/1.0` per the no-PII rule.

    Used by `/publications/trackers/verify_resolved` (V20 D3) to
    re-check stale `resolved` cache entries. Test-time callers
    redirect via `app.config["_VERIFY_HEAD_PROBE"]` — see
    `tests/test_v20_resolved_ttl.py`.
    """
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "cv-editor/1.0"},
    )
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False
