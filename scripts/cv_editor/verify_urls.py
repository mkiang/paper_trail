#!/usr/bin/env python3
"""URL health-check for the CV data.

Walks `data/publications.yml` (and `data/meta.yml` for the contact site)
to collect every URL the CV publishes — canonical doi.org / PubMed / PMC
URLs derived from the entry's IDs, plus any literal URLs in
`open_access` values and `notes.outlets[].url`. HEAD-checks each one,
caches results for 30 days, writes a Markdown report to
`qc/urls_report.md`.

Usage:
    python3 scripts/verify_urls.py                # default
    python3 scripts/verify_urls.py --force        # ignore cache
    python3 scripts/verify_urls.py --ttl-days 7
    python3 scripts/verify_urls.py --workers 4
    python3 scripts/verify_urls.py --quiet

Designed to be run "every now and then" — manually or via cron/launchd.
NOT wired into ./build.sh because network calls don't belong on the
critical-path render.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from cv_editor import paths, schemas, sections, yaml_io  # noqa: E402
from cv_editor.host_throttle import HostThrottle as _HostThrottle  # noqa: E402
from cv_editor.url_helpers import id_url  # noqa: E402

ROOT = paths.data_root()  # workspace
CACHE_DIR = paths.cache_dir() / "url_verify"
REPORT_PATH = paths.qc_dir() / "urls_report.md"


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, CACHE_DIR, REPORT_PATH
    ROOT = paths.data_root()
    CACHE_DIR = paths.cache_dir() / "url_verify"
    REPORT_PATH = paths.qc_dir() / "urls_report.md"


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 12.0
DEFAULT_TTL_DAYS = 30
DEFAULT_WORKERS = 4

# Per-host minimum gap between requests (seconds). NCBI's anonymous
# limit is 3 req/sec, so 0.34s leaves headroom. Other hosts get a
# lighter politeness gap.
_NCBI_HOSTS = ("ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov")
_HOST_GAP_S = {h: 0.34 for h in _NCBI_HOSTS}
_DEFAULT_HOST_GAP_S = 0.10


# ---------------- data shapes ----------------


@dataclass(frozen=True)
class UrlEntry:
    """One URL + a human-readable label of where it came from."""

    url: str
    source: str  # e.g. "publications.yml#42:doi"


@dataclass
class CheckResult:
    url: str
    status: int  # 0 if no HTTP response (DNS, timeout, SSL, etc.)
    final_url: str | None
    error: str | None
    category: (
        str  # "ok" | "publisher_blocked" | "4xx" | "5xx" | "timeout" | "dns" | "ssl" | "other"
    )
    checked_at: str
    method_used: str  # "HEAD" | "GET" | "-"


@dataclass
class Report:
    started_at: str
    finished_at: str
    total_urls: int
    checked: int  # actually contacted the network
    cached_skips: int
    by_category: dict[str, list[CheckResult]] = field(default_factory=dict)
    sources_by_url: dict[str, list[str]] = field(default_factory=dict)


# ---------------- URL collection (pure) ----------------


def _is_url_string(v) -> bool:
    return isinstance(v, str) and v.strip().startswith(("http://", "https://"))


def collect_publication_urls(data, sch: dict) -> Iterator[UrlEntry]:
    """Yield every URL derivable from publications.yml."""
    for rec in sections.flatten(data, sch["structure"]):
        e = rec["entry"]
        gid = rec["global_idx"]
        # Canonical ID URLs.
        for kind in ("doi", "preprint_doi", "pmid", "pmcid"):
            v = e.get(kind)
            if not v:
                continue
            u = id_url(v, kind)
            if u:
                yield UrlEntry(u, f"publications.yml#{gid}:{kind}")
        # Open-access URLs (skip bare-true placeholders).
        oa = e.get("open_access") or {}
        if isinstance(oa, dict):
            for k in ("paper", "code", "data"):
                v = oa.get(k)
                if _is_url_string(v):
                    yield UrlEntry(v.strip(), f"publications.yml#{gid}:open_access.{k}")
        # Media-note outlet URLs.
        notes = e.get("notes") or []
        for ni, note in enumerate(notes):
            if not isinstance(note, dict) or note.get("type") != "media":
                continue
            for oi, out in enumerate(note.get("outlets") or []):
                if isinstance(out, dict) and _is_url_string(out.get("url")):
                    yield UrlEntry(
                        str(out["url"]).strip(),
                        f"publications.yml#{gid}:notes[{ni}].outlets[{oi}].url",
                    )


def collect_meta_urls(meta_path: Path | None = None) -> Iterator[UrlEntry]:
    """Yield URL-shaped fields from meta.yml's contacts."""
    p = meta_path or (ROOT / "data" / "meta.yml")
    if not p.exists():
        return
    _, meta = yaml_io.load(p)
    if not isinstance(meta, dict):
        return
    for ci, contact in enumerate(meta.get("contacts") or []):
        if not isinstance(contact, dict):
            continue
        text = contact.get("text")
        if _is_url_string(text):
            yield UrlEntry(str(text).strip(), f"meta.yml:contacts[{ci}].text")


def collect_all_urls(
    *, pubs_path: Path | None = None, meta_path: Path | None = None
) -> list[UrlEntry]:
    """Aggregate everything (deduped by URL, sources preserved separately)."""
    entries: list[UrlEntry] = []
    sch = schemas.get("publications")
    pp = pubs_path or (ROOT / sch["file"])
    _, data = yaml_io.load(pp)
    entries.extend(collect_publication_urls(data, sch))
    entries.extend(collect_meta_urls(meta_path))
    return entries


# ---------------- cache ----------------


class UrlCache:
    """SHA256-keyed on-disk cache. Stores only successful checks; the
    next run will re-check anything that failed last time so transient
    outages don't poison the cache."""

    def __init__(self, cache_dir: Path | None = None, ttl_days: int = DEFAULT_TTL_DAYS):
        # None-sentinel: resolve the workspace cache dir at call time so an
        # in-process caller after paths.configure() honors the active root
        # (a `= CACHE_DIR` default would freeze the import-time value).
        self.dir = CACHE_DIR if cache_dir is None else cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".json")

    def get_fresh(self, url: str) -> CheckResult | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            checked_at = datetime.fromisoformat(data["checked_at"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked_at > self.ttl:
            return None
        try:
            return CheckResult(**data)
        except TypeError:
            return None

    def set(self, result: CheckResult) -> None:
        if result.category != "ok":
            return  # don't cache failures — next run retries
        p = self._path(result.url)
        p.write_text(json.dumps(asdict(result), indent=2))


# ---------------- HTTP check ----------------

# Per-host throttle (migrated to cv_editor.host_throttle, V14 2026-05-17).
# Module-level singleton preserves the cross-thread-worker shared-state
# semantic — all workers in the ThreadPoolExecutor see the same per-host
# last-call timestamp.
_THROTTLE = _HostThrottle(gap_per_host=_HOST_GAP_S, default_gap=_DEFAULT_HOST_GAP_S)


def _polite(host: str) -> None:
    """Serialize requests to the same host with a per-host minimum gap."""
    _THROTTLE.wait(host)


def _categorize(
    status: int, error: str | None, *, request_url: str = "", final_url: str | None = None
) -> str:
    """Classify the outcome of a URL check.

    Note the `publisher_blocked` carve-out: when the request was for a
    DOI URL (or any URL that follows a redirect) and the publisher's
    page returned 403/429, we know the URL is fine for human users
    (the redirect chain succeeded) — only our bot was blocked. Those
    don't belong in the "Client errors" bucket alongside genuine 404s.
    """
    if error is None and 200 <= status < 400:
        return "ok"
    if error and "timeout" in error.lower():
        return "timeout"
    if error and "dns" in error.lower():
        return "dns"
    if error and "ssl" in error.lower():
        return "ssl"
    if status in (403, 429) and _is_publisher_redirect(request_url, final_url):
        return "publisher_blocked"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def _is_publisher_redirect(request_url: str, final_url: str | None) -> bool:
    """True iff the request crossed hosts before the failure — i.e., a
    DOI/PMID resolver redirected us to the publisher and the publisher
    is the one that blocked the bot."""
    if not request_url or not final_url:
        return False
    try:
        req_host = urllib.parse.urlparse(request_url).hostname or ""
        fin_host = urllib.parse.urlparse(final_url).hostname or ""
    except ValueError:
        return False
    return bool(req_host) and bool(fin_host) and req_host != fin_host


def check_url(url: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> CheckResult:
    """HEAD first; on 403/405/501 retry GET. Follows redirects (urllib
    default ~10 hops). Returns a typed result; never raises for HTTP
    or network failures."""
    host = urllib.parse.urlparse(url).hostname or ""
    _polite(host)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                final = resp.url or url
                return CheckResult(
                    url=url,
                    status=status,
                    final_url=final,
                    error=None,
                    category=_categorize(status, None, request_url=url, final_url=final),
                    checked_at=now_iso,
                    method_used=method,
                )
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 429, 405, 501):
                continue  # publisher dislikes HEAD; retry with GET
            # V17-D fix (C-H3): probe several places urllib stashes the
            # post-redirect URL. e.geturl() is the documented accessor;
            # e.url and e.filename are fallbacks. If the publisher set a
            # Location header on the error response (some 3xx-then-error
            # patterns), that's also useful evidence of a host change.
            final = (
                (e.geturl() if hasattr(e, "geturl") else None)
                or getattr(e, "url", None)
                or getattr(e, "filename", None)
                or (e.headers.get("Location") if getattr(e, "headers", None) else None)
                or url
            )
            return CheckResult(
                url=url,
                status=e.code,
                final_url=final,
                error=f"HTTPError {e.code}: {e.reason}",
                category=_categorize(e.code, None, request_url=url, final_url=final),
                checked_at=now_iso,
                method_used=method,
            )
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, socket.gaierror):
                msg, cat = f"DNS error: {reason}", "dns"
            elif isinstance(reason, (socket.timeout, TimeoutError)):
                msg, cat = "timeout", "timeout"
            elif isinstance(reason, ssl.SSLError):
                msg, cat = f"SSL error: {reason}", "ssl"
            else:
                msg, cat = f"URLError: {reason}", "other"
            return CheckResult(
                url=url,
                status=0,
                final_url=None,
                error=msg,
                category=cat,
                checked_at=now_iso,
                method_used=method,
            )
        except Exception as e:
            return CheckResult(
                url=url,
                status=0,
                final_url=None,
                error=f"{type(e).__name__}: {e}",
                category="other",
                checked_at=now_iso,
                method_used=method,
            )
    return CheckResult(
        url=url,
        status=0,
        final_url=None,
        error="exhausted",
        category="other",
        checked_at=now_iso,
        method_used="-",
    )


# ---------------- orchestrator ----------------


def verify_all(
    *,
    force: bool = False,
    ttl_days: int = DEFAULT_TTL_DAYS,
    max_workers: int = DEFAULT_WORKERS,
    check_fn: Callable[[str], CheckResult] = check_url,
    cache: UrlCache | None = None,
    pubs_path: Path | None = None,
    meta_path: Path | None = None,
    on_progress: Callable[[int, int, CheckResult], None] | None = None,
) -> Report:
    """Collect → dedupe → check (skipping cache hits) → return Report."""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = cache or UrlCache(ttl_days=ttl_days)
    entries = collect_all_urls(pubs_path=pubs_path, meta_path=meta_path)

    sources_by_url: dict[str, list[str]] = {}
    for e in entries:
        sources_by_url.setdefault(e.url, []).append(e.source)

    unique_urls = sorted(sources_by_url.keys())
    results: list[CheckResult] = []
    cached_skips = 0
    to_check: list[str] = []

    if not force:
        for u in unique_urls:
            hit = cache.get_fresh(u)
            if hit is not None:
                results.append(hit)
                cached_skips += 1
            else:
                to_check.append(u)
    else:
        to_check = list(unique_urls)

    total_to_check = len(to_check)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(check_fn, u): u for u in to_check}
        for fut in as_completed(futures):
            r = fut.result()
            cache.set(r)
            results.append(r)
            done += 1
            if on_progress is not None:
                on_progress(done, total_to_check, r)

    by_category: dict[str, list[CheckResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Report(
        started_at=started,
        finished_at=finished,
        total_urls=len(unique_urls),
        checked=total_to_check,
        cached_skips=cached_skips,
        by_category=by_category,
        sources_by_url=sources_by_url,
    )


# ---------------- report ----------------

_CATEGORY_ORDER = ("4xx", "5xx", "timeout", "dns", "ssl", "other", "publisher_blocked", "ok")
_CATEGORY_LABEL = {
    "ok": "OK",
    "4xx": "Client errors (4xx)",
    "5xx": "Server errors (5xx)",
    "timeout": "Timeouts",
    "dns": "DNS errors",
    "ssl": "SSL/TLS errors",
    "other": "Other failures",
    "publisher_blocked": "Publisher-blocked (likely fine for humans)",
}
# Categories that count as actionable failures. publisher_blocked is
# excluded — those URLs work for humans, only the bot was rejected.
_FAIL_CATEGORIES = ("4xx", "5xx", "timeout", "dns", "ssl", "other")


def render_report(report: Report) -> str:
    fail_count = sum(len(report.by_category.get(cat, [])) for cat in _FAIL_CATEGORIES)
    blocked_count = len(report.by_category.get("publisher_blocked", []))
    ok_count = len(report.by_category.get("ok", []))
    lines = []
    lines.append("# URL Verification Report")
    lines.append("")
    lines.append(f"- Started:  {report.started_at}")
    lines.append(f"- Finished: {report.finished_at}")
    lines.append(f"- Total unique URLs: {report.total_urls}")
    summary_bits = [
        f"OK: {ok_count}",
        f"Failing: {fail_count}",
    ]
    if blocked_count:
        summary_bits.append(f"Publisher-blocked: {blocked_count}")
    summary_bits.extend(
        [
            f"Checked this run: {report.checked}",
            f"Skipped via cache: {report.cached_skips}",
        ]
    )
    lines.append("- " + "  ·  ".join(summary_bits))
    lines.append("")

    if fail_count == 0:
        if blocked_count:
            lines.append(
                f"No actionable failures. {blocked_count} URL(s) returned 403/429 from "
                "the publisher after a successful redirect — those are typically fine "
                "for human users. See section below."
            )
        else:
            lines.append("All URLs healthy. No action required.")
        lines.append("")
    for cat in _CATEGORY_ORDER:
        if cat == "ok":
            continue
        items = report.by_category.get(cat, [])
        if not items:
            continue
        lines.append(f"## {_CATEGORY_LABEL[cat]} ({len(items)})")
        lines.append("")
        if cat == "publisher_blocked":
            lines.append(
                "These URLs returned 403/429 from the publisher AFTER the resolver "
                "(doi.org, etc.) successfully redirected. The destination page is "
                "almost certainly fine for humans — the publisher just blocks "
                "automated traffic. Spot-check by clicking through; bulk action only "
                "if a publisher genuinely changed URLs."
            )
            lines.append("")
        for r in sorted(items, key=lambda x: x.url):
            lines.append(f"- `{r.url}`")
            if r.status:
                lines.append(f"  - status: {r.status}")
            if r.error:
                lines.append(f"  - error: {r.error}")
            if r.final_url and r.final_url != r.url:
                lines.append(f"  - final: {r.final_url}")
            srcs = report.sources_by_url.get(r.url, [])
            for s in srcs:
                lines.append(f"  - source: {s}")
        lines.append("")

    # Possible format drift: OK URLs whose final URL host differs from
    # the requested one. Worth flagging — could mean the publisher
    # moved to a new domain.
    drift = []
    for r in report.by_category.get("ok", []):
        if not r.final_url:
            continue
        try:
            req_host = urllib.parse.urlparse(r.url).hostname or ""
            fin_host = urllib.parse.urlparse(r.final_url).hostname or ""
            if req_host and fin_host and req_host != fin_host:
                drift.append(r)
        except ValueError:
            pass
    if drift:
        lines.append(f"## Possible format drift ({len(drift)})")
        lines.append("")
        lines.append(
            "These URLs resolved OK but the final URL is on a different host than requested. Worth a glance."
        )
        lines.append("")
        for r in sorted(drift, key=lambda x: x.url):
            lines.append(f"- `{r.url}` → `{r.final_url}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report: Report, path: Path | None = None) -> None:
    path = REPORT_PATH if path is None else path  # call-time workspace resolve
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(report))


# ---------------- CLI ----------------


def _progress_printer(total: int):
    """Return a callback that prints '[N/total] url status' as checks finish."""

    def cb(done, _total, r: CheckResult):
        marker = "OK" if r.category == "ok" else r.category.upper()
        print(f"[{done}/{total}] {marker:>7}  {r.url}", flush=True)

    return cb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="URL health-check for the CV data.")
    parser.add_argument("--force", action="store_true", help="Ignore cache; re-check every URL.")
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=DEFAULT_TTL_DAYS,
        help=f"Cache TTL in days (default {DEFAULT_TTL_DAYS}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Concurrent workers (default {DEFAULT_WORKERS}).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress per-URL progress lines.")
    args = parser.parse_args(argv)

    # Pre-collect just to print total before any network calls.
    entries = collect_all_urls()
    total = len({e.url for e in entries})
    if not args.quiet:
        print(f"Collected {total} unique URLs.", flush=True)

    on_progress = None if args.quiet else _progress_printer(total)
    report = verify_all(
        force=args.force,
        ttl_days=args.ttl_days,
        max_workers=args.workers,
        on_progress=on_progress,
    )
    write_report(report)
    if not args.quiet:
        fail = sum(len(report.by_category.get(c, [])) for c in _FAIL_CATEGORIES)
        blocked = len(report.by_category.get("publisher_blocked", []))
        print(f"\nReport written to {REPORT_PATH.relative_to(ROOT)}")
        msg = f"OK: {len(report.by_category.get('ok', []))}  ·  Failing: {fail}"
        if blocked:
            msg += f"  ·  Publisher-blocked: {blocked}"
        print(msg)
    # Exit 0 unless there's a real failure. Publisher-blocked doesn't count
    # — those URLs work for humans, they just block the bot.
    return 0 if not any(report.by_category.get(c) for c in _FAIL_CATEGORIES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
