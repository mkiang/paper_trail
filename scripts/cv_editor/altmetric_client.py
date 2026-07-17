"""Altmetric tracker URL resolver (V13 finish, 2026-05-16; API removed 2026-05-28).

Multi-strategy resolver for press-coverage tracker URLs (typically
`ct.moreover.com/?a=...`) that the user pastes into a publication's
media notes. The 4th strategy (unshorten.me) handles networks that
block tracker hosts locally.

The Altmetric Explorer JSON:API ingest workflow (paste-API-URL +
fetch_mentions + extract_mentions) was removed 2026-05-28 — the user
doesn't use it. The V12 Altmetric Explorer DEEP-LINK button in
`url_helpers.altmetric_url(title)` is unaffected (no API, no key).

Module surface
--------------
* `resolve_tracker_url(url, *, timeout=10)` — multi-strategy resolver
  (HEAD → GET browser-UA → meta-refresh → unshorten.me) returning a
  `ResolveResult`.
* `resolve_tracker_url_with_cache(url, cache, *, force=False)` —
  consults the persistent `TrackerCache` first; only attempts network
  when cache is missing or stale per the retry rules.
* `resolve_via_unshorten_me(url)` — strategy-4 helper, includes the
  10-min 429 circuit breaker (gotcha #53).

No PII in User-Agent (project global rule). UA is "cv-editor/1.0".
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from cv_editor.altmetric_tracker_cache import (
    ResolveResult,
    TrackerCache,
)

UA = "cv-editor/1.0"  # matches enrichment.UA; no PII
# Browser UA for tracker hosts that gate redirects on UA sniffing
# (ct.moreover.com is the main offender). Same string verify_urls.py uses.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
HEAD_TIMEOUT = 10  # seconds; tracker-resolve budget
RESOLVE_BODY_BYTES = 16 * 1024  # only need <head> for meta-refresh

UNSHORTEN_ME_ENDPOINT = "https://unshorten.me/json/{url}"
UNSHORTEN_ME_TIMEOUT = 15  # seconds; their service is slower than HEAD/GET

# 2026-05-25 (post-batch): in-memory circuit breaker for the unshorten.me
# free tier. Their published limit is ~10 requests per HOUR for new URLs
# (cached URLs are unlimited but we don't know in advance which is which).
# After Stage B / I9 dropped the failure-TTL, every Resolve click would
# re-attempt strategy 4 even right after a 429 — wasteful + makes the
# resolve-all sweep look broken on home-network conditions where
# strategies 1-3 fail and unshorten.me is the only path.
#
# Default cooldown is 10 minutes (rolling 10/hour window means a slot
# opens roughly every 6 min on average; 10 is a small safety margin).
# If the 429 response carries a `Retry-After` header (RFC 9110 §10.2.3),
# we honor that exactly (integer seconds OR HTTP-date).
#
# Module-level state intentionally — single Flask process, session-scoped
# (forgets on restart, which is fine: every restart is a clean slate).
UNSHORTEN_ME_DEFAULT_COOLDOWN_SECONDS = 600
_unshorten_me_cooldown_until: float = 0.0


def _unshorten_me_on_cooldown(*, now: float | None = None) -> bool:
    ref = now if now is not None else time.time()
    return ref < _unshorten_me_cooldown_until


def _unshorten_me_cooldown_remaining(*, now: float | None = None) -> int:
    ref = now if now is not None else time.time()
    return max(0, int(_unshorten_me_cooldown_until - ref))


def _trip_unshorten_me_cooldown(
    *,
    seconds: float | None = None,
    now: float | None = None,
) -> None:
    """Set the cooldown window. Pass `seconds` to override the default
    (e.g., parsed Retry-After). Pass `now` to control the clock in tests."""
    global _unshorten_me_cooldown_until
    ref = now if now is not None else time.time()
    duration = seconds if seconds is not None else UNSHORTEN_ME_DEFAULT_COOLDOWN_SECONDS
    _unshorten_me_cooldown_until = ref + duration


def _reset_unshorten_me_cooldown() -> None:
    """For test fixtures only — clear the circuit-breaker state so test
    order doesn't matter."""
    global _unshorten_me_cooldown_until
    _unshorten_me_cooldown_until = 0.0


def _parse_retry_after(raw: str | None, *, now: float | None = None) -> float | None:
    """Parse an RFC 9110 §10.2.3 Retry-After value. Accepts either:
      - non-negative integer seconds, e.g. "3600"
      - HTTP-date, e.g. "Wed, 21 Oct 2026 07:28:00 GMT"
    Returns seconds-from-now (float), or None if unparseable / negative.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        secs = int(raw)
        return secs if secs >= 0 else None
    except ValueError:
        pass
    # HTTP-date fallback.
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target is None:
            return None
        ref = (
            datetime.fromtimestamp(now, tz=target.tzinfo) if now else datetime.now(tz=target.tzinfo)
        )
        delta = (target - ref).total_seconds()
        return delta if delta >= 0 else None
    except (TypeError, ValueError):
        return None


# <meta http-equiv="refresh" content="0;url=https://final/article">
_META_REFRESH_RE = re.compile(
    rb'<meta\b[^>]*?http-equiv\s*=\s*["\']?refresh["\']?[^>]*?'
    rb'content\s*=\s*["\']\s*\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
    re.IGNORECASE,
)


def _try_head(url: str, *, ua: str, timeout: int) -> str | None:
    """HEAD request; returns the URL after redirects, or None on failure
    or when the server didn't redirect (resp.url == request url)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = str(getattr(resp, "url", None) or url)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return final if final and final != url else None


def _try_get(url: str, *, ua: str, timeout: int) -> tuple[str | None, bytes]:
    """GET with browser UA; returns (final_url_after_redirects, body_bytes).

    The body is capped at RESOLVE_BODY_BYTES so we can parse <meta refresh>
    without downloading the full page. final_url is None if the server
    didn't redirect away from the request URL.
    """
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = str(getattr(resp, "url", None) or url)
            body = resp.read(RESOLVE_BODY_BYTES)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, b""
    redirected = final if final and final != url else None
    return redirected, body


def _parse_meta_refresh(body: bytes, *, base_url: str) -> str | None:
    """Find a <meta http-equiv=refresh> redirect target in HTML body."""
    if not body:
        return None
    m = _META_REFRESH_RE.search(body)
    if not m:
        return None
    target = m.group(1).decode("utf-8", errors="replace").strip()
    if not target:
        return None
    # Resolve against the base URL in case the refresh is relative.
    try:
        return urllib.parse.urljoin(base_url, target)
    except ValueError:
        return target


def resolve_via_unshorten_me(
    url: str,
    *,
    timeout: int = UNSHORTEN_ME_TIMEOUT,
) -> ResolveResult:
    """Resolve via the free unshorten.me JSON API (V13 finish — 4th strategy).

    unshorten.me runs the redirect chain from its own servers, so the
    user's local network blocking ct.moreover.com doesn't matter here.
    Returned shape: `{success: bool, resolved_url: str, ...}` on success,
    `{success: false, error: ...}` on failure (rate limit, invalid input).

    Returns:
        ResolveResult with status one of resolved / failed_rate_limit /
        failed_network. Never raises.
    """
    if not isinstance(url, str) or not url.strip():
        return ResolveResult(status="failed_network", error="empty url")
    # Circuit breaker (2026-05-25 post-batch): when a recent call hit
    # unshorten.me's rate limit, skip the HTTP request and return a
    # synthetic failed_rate_limit so the SSE console shows what's
    # happening + we don't burn quota on a guaranteed-fail attempt.
    if _unshorten_me_on_cooldown():
        remaining = _unshorten_me_cooldown_remaining()
        return ResolveResult(
            status="failed_rate_limit",
            error=f"unshorten.me on cooldown ({remaining}s remaining); skipped HTTP call",
        )
    endpoint = UNSHORTEN_ME_ENDPOINT.format(url=urllib.parse.quote(url, safe=""))
    req = urllib.request.Request(endpoint, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # Honor Retry-After if present; otherwise default cooldown.
            retry_after_raw = None
            try:
                retry_after_raw = e.headers.get("Retry-After") if e.headers else None
            except AttributeError:
                pass
            secs = _parse_retry_after(retry_after_raw)
            _trip_unshorten_me_cooldown(seconds=secs)
            tripped = _unshorten_me_cooldown_remaining()
            return ResolveResult(
                status="failed_rate_limit",
                error=f"unshorten.me rate limit (HTTP 429); cooldown {tripped}s",
            )
        return ResolveResult(
            status="failed_network",
            error=f"unshorten.me HTTP {e.code}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return ResolveResult(
            status="failed_network",
            error=f"unshorten.me network error: {e}",
        )
    try:
        doc = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return ResolveResult(
            status="failed_network",
            error=f"unshorten.me bad JSON: {e}",
        )
    if not isinstance(doc, dict):
        return ResolveResult(
            status="failed_network",
            error="unshorten.me non-object payload",
        )
    success = bool(doc.get("success"))
    resolved = (doc.get("resolved_url") or "").strip()
    err = str(doc.get("error") or "").strip()
    if not success or not resolved:
        # Detect rate limit via the error string when the API still
        # returns HTTP 200 with a success:false payload. No Retry-After
        # available here, so fall back to the default cooldown.
        if err and "rate" in err.lower() and "limit" in err.lower():
            _trip_unshorten_me_cooldown()
            tripped = _unshorten_me_cooldown_remaining()
            return ResolveResult(
                status="failed_rate_limit",
                error=f"unshorten.me: {err}; cooldown {tripped}s",
            )
        return ResolveResult(
            status="failed_network",
            error=f"unshorten.me failed: {err or 'no resolved_url'}",
        )
    # Some responses return the original URL unchanged — treat as
    # failed_no_redirect so we don't loop on un-resolvable trackers.
    if resolved == url:
        return ResolveResult(
            final_url=None,
            strategy=None,
            status="failed_no_redirect",
            error="unshorten.me returned the same URL",
        )
    # T4.4: guard against tracker→tracker hops. If unshorten.me's
    # resolved_url is itself a known tracker host, accepting it would
    # pollute YAML with another tracker URL instead of a real article URL.
    from cv_editor.url_helpers import is_tracker_url  # noqa: WPS433

    if is_tracker_url(resolved):
        return ResolveResult(
            final_url=None,
            strategy=None,
            status="failed_no_redirect",
            error=f"unshorten.me returned another tracker URL: {resolved[:50]}",
        )
    return ResolveResult(
        final_url=resolved,
        strategy="unshorten_me",
        status="resolved",
    )


def resolve_tracker_url(
    url: str,
    *,
    timeout: int = HEAD_TIMEOUT,
) -> ResolveResult:
    """Resolve a tracker URL (ct.moreover.com/?a=...) to the final article URL.

    Returns a `ResolveResult` with one of these statuses:

      * resolved           one of the strategies produced a final URL
      * failed_rate_limit  unshorten.me rate-limited us (retry after 24 h)
      * failed_network     direct strategies failed AND unshorten.me errored
      * failed_no_redirect every strategy returned a non-redirect or the
                           original URL unchanged (tracker is non-functional)

    Strategy order:
      1. HEAD with cv-editor UA. Fast; works for cleanly-redirecting trackers.
      2. GET with a real browser UA. Many trackers gate redirects on UA
         and serve 200-no-redirect to non-browser clients.
      3. Parse <meta http-equiv="refresh"> from the GET body. Some
         trackers redirect via meta-refresh, not HTTP 30x.
      4. unshorten.me free public API. Side-steps local-network blocks
         (e.g., user's home network DNS-blocks ct.moreover.com) by
         delegating the redirect chain to a third-party server.

    Never raises. The caller persists the result via `TrackerCache.record`.
    """
    if not isinstance(url, str) or not url.strip():
        return ResolveResult(status="failed_network", error="empty url")
    raw = url.strip()

    # Defense-in-depth scheme guard. YAML-stored tracker URLs flow into this
    # function from `publications_trackers_resolve_all` without per-row scheme
    # validation; urllib defaults expose file:// / ftp:// to urlopen. Reject
    # anything but http(s) before we feed it to _try_head / _try_get.
    from cv_editor.url_helpers import is_safe_fetch_url  # noqa: WPS433

    if not is_safe_fetch_url(raw):
        return ResolveResult(status="failed_network", error="non-http(s) scheme")

    # Strategy 1: HEAD with project UA.
    head = _try_head(raw, ua=UA, timeout=timeout)
    if head:
        return ResolveResult(final_url=head, strategy="head", status="resolved")

    # Strategy 2: GET with browser UA (trackers often UA-sniff).
    get_final, body = _try_get(raw, ua=BROWSER_UA, timeout=timeout)
    if get_final:
        return ResolveResult(final_url=get_final, strategy="get", status="resolved")

    # Strategy 3: parse <meta refresh> from the body.
    meta = _parse_meta_refresh(body, base_url=raw)
    if meta:
        return ResolveResult(
            final_url=meta,
            strategy="meta_refresh",
            status="resolved",
        )

    # Strategy 4: unshorten.me (off-network fallback).
    return resolve_via_unshorten_me(raw)


def resolve_tracker_url_with_cache(
    url: str,
    cache: TrackerCache,
    *,
    timeout: int = HEAD_TIMEOUT,
    force: bool = False,
) -> ResolveResult:
    """Cache-aware resolution. Consults the sidecar first; only attempts
    network when the cache is missing the URL or holds a stale failure
    (per the retry rules in `altmetric_tracker_cache`).

    Persists every attempt back into the cache. The caller is responsible
    for calling `cache.save()` once a batch is done — this function does
    not save on every record() to keep batch resolutions fast.
    """
    if not isinstance(url, str) or not url.strip():
        return ResolveResult(status="failed_network", error="empty url")
    raw = url.strip()
    if not force and not cache.should_attempt(raw):
        cached = cache.get_result(raw)
        if cached is not None:
            # Stage B / I9 (2026-05-25): mark cache hits so the SSE
            # console can distinguish "kept [resolved]" from a fresh
            # "resolved [<strategy>]" attempt.
            cached.from_cache = True
            return cached
    result = resolve_tracker_url(raw, timeout=timeout)
    cache.record(raw, result)
    return result


def _cli():
    """CLI for direct testing + batch operations:

    python -m cv_editor.altmetric_client resolve <url>
    python -m cv_editor.altmetric_client parse <api-url>
    python -m cv_editor.altmetric_client resolve-all [--force]
    python -m cv_editor.altmetric_client cache-status
    """
    import sys

    args = sys.argv[1:]
    if not args:
        print(
            "usage:\n"
            "  python -m cv_editor.altmetric_client resolve <url>\n"
            "  python -m cv_editor.altmetric_client resolve-all [--force]\n"
            "  python -m cv_editor.altmetric_client cache-status",
            file=sys.stderr,
        )
        sys.exit(2)
    op = args[0]
    if op == "resolve":
        if len(args) != 2:
            print("usage: resolve <url>", file=sys.stderr)
            sys.exit(2)
        result = resolve_tracker_url(args[1], timeout=15)
        if not result.is_resolved:
            print(f"(no resolution; status={result.status}; error={result.error or '-'})")
            sys.exit(1)
        print(result.final_url)
        return
    if op == "resolve-all":
        force = "--force" in args[1:]
        _cli_resolve_all(force=force)
        return
    if op == "cache-status":
        _cli_cache_status()
        return
    print(f"unknown subcommand: {op!r}", file=sys.stderr)
    sys.exit(2)


def _cli_resolve_all(*, force: bool) -> None:
    """Sweep every tracker URL in data/publications.yml; persist cache;
    rewrite resolved URLs back to YAML via yaml_io.write_with_backup.

    Idempotent. Safe to run repeatedly from any network — failed URLs
    on one network simply persist as failed in the cache and re-attempt
    next time should_attempt returns True.
    """
    import sys

    # Late imports to keep altmetric_client importable in test contexts
    # that don't have ruamel/filelock installed.
    # P1 seam: resolve against the active workspace root (data_root).
    from cv_editor import (
        paths,
        yaml_io,  # noqa: WPS433
    )

    pubs_path = paths.data_dir() / "publications.yml"
    if not pubs_path.exists():
        print(f"error: {pubs_path} not found", file=sys.stderr)
        sys.exit(2)
    header, data = yaml_io.load(pubs_path)
    # T1.3: capture mtime_ns at LOAD time so a concurrent edit during
    # resolution surfaces as StaleFileError instead of silent clobber.
    load_mtime_ns = yaml_io.mtime_ns(pubs_path)
    cache = TrackerCache()

    trackers = list(_iter_pub_tracker_urls(data))
    if not trackers:
        print("no tracker URLs found in publications.yml")
        return
    print(f"found {len(trackers)} tracker URLs; resolving (force={force})…")

    resolved_count = 0
    failed_count = 0
    skipped_count = 0
    substitutions: dict[str, str] = {}
    for url in {t["url"] for t in trackers}:  # dedup across multiple occurrences
        if not force and not cache.should_attempt(url):
            cached = cache.get_result(url)
            if cached and cached.is_resolved:
                substitutions[url] = cached.final_url  # type: ignore[assignment]
            skipped_count += 1
            continue
        result = resolve_tracker_url_with_cache(url, cache, force=force)
        if result.is_resolved and result.final_url:
            substitutions[url] = result.final_url
            resolved_count += 1
            print(f"  resolved [{result.strategy:>13}] {url[:55]}…")
        else:
            failed_count += 1
            print(f"  failed   [{result.status:>13}] {url[:55]}… ({result.error})")

    # T1.3 invariant: YAML write FIRST, then cache.save(). If the YAML
    # write fails (stale-mtime, lock timeout), we abort BEFORE persisting
    # the cache so a re-run will retry resolution.
    if substitutions:
        _substitute_urls_in_publications(data, substitutions)
        try:
            yaml_io.write_with_backup(
                pubs_path,
                header,
                data,
                expected_mtime_ns=load_mtime_ns,
            )
        except yaml_io.StaleFileError as e:
            print(f"\nerror: YAML write aborted — {e}", file=sys.stderr)
            print(
                "(another editor save landed during the sweep; cache NOT persisted)",
                file=sys.stderr,
            )
            sys.exit(3)
    cache.save()

    stats = cache.stats()
    print(
        f"\ndone: {resolved_count} resolved, {failed_count} failed, "
        f"{skipped_count} skipped (cache hit / backoff)"
    )
    print(
        f"cache: {stats['total']} total, {stats['resolved']} resolved, "
        f"{stats['failed_network']} failed_network, "
        f"{stats['failed_rate_limit']} failed_rate_limit, "
        f"{stats['failed_no_redirect']} failed_no_redirect"
    )


def _cli_cache_status() -> None:
    """Print sidecar contents: counts by status + the sidecar path."""
    cache = TrackerCache()
    stats = cache.stats()
    print(f"cache file: {cache.path}")
    print(f"  total:              {stats['total']}")
    print(f"  resolved:           {stats['resolved']}")
    print(f"  failed_network:     {stats['failed_network']}")
    print(f"  failed_rate_limit:  {stats['failed_rate_limit']}")
    print(f"  failed_no_redirect: {stats['failed_no_redirect']}")


def _iter_pub_tracker_urls(data):
    """Back-compat wrapper. Yields dict rows for the CLI's progress print.

    Note (T3.1): pub_idx is now sections.flatten() global_idx, matching
    the editor's URL routes. Previously this used an ad-hoc recursive
    walk with a different counter.
    """
    from cv_editor import tracker_walk as _tw  # noqa: WPS433

    for ref in _tw.iter_tracker_outlets(data):
        yield ref.as_row()


def _substitute_urls_in_publications(data, substitutions):
    """Back-compat wrapper for the CLI."""
    from cv_editor import tracker_walk as _tw  # noqa: WPS433

    _tw.substitute_tracker_urls_in_publications(data, substitutions)


if __name__ == "__main__":
    _cli()
