#!/usr/bin/env python3
"""V14 citation-count fetcher (2026-05-17).

Walks `data/publications.yml` for DOIs and fetches citation counts from
Crossref's public REST API. Writes per-DOI state to the sidecar cache
at `.cache/citation_counts.json` and a trimmed snapshot at
`data/citation_counts.json` (read by the Typst renderer when
`--input show_citations=true`).

**No PII in outbound HTTP** (global rule). UA is `cv-citation-fetcher/1.0`;
no email, no mailto pool. Politeness comes from per-host throttle.

Crossref public-pool limits (verified 2026-05-17): 5 req/s, concurrency 1.
We use single-concurrency + 0.25s gap = ~4 req/s, safely under.

CLI:
    fetch_citation_counts.py                       # fetch missing/stale
    fetch_citation_counts.py --force               # ignore TTL
    fetch_citation_counts.py --snapshot-only       # no network; regen snapshot
    fetch_citation_counts.py --workers 1           # concurrency knob (kept for parity)
    fetch_citation_counts.py --ttl-days N          # informational; per-status TTLs hard-coded
    fetch_citation_counts.py --quiet               # suppress progress lines
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cv_editor import paths, yaml_io  # noqa: E402
from cv_editor.citation_counts import (  # noqa: E402
    CitationCache,
    CountStatus,
    write_snapshot,
)
from cv_editor.host_throttle import HostThrottle  # noqa: E402

ROOT = paths.data_root()  # workspace
PUB_YML = paths.data_dir() / "publications.yml"
CACHE_PATH = paths.cache_dir() / "citation_counts.json"
SNAPSHOT_PATH = paths.data_dir() / "citation_counts.json"
REPORT_PATH = paths.qc_dir() / "citations_report.md"


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, PUB_YML, CACHE_PATH, SNAPSHOT_PATH, REPORT_PATH
    ROOT = paths.data_root()
    PUB_YML = paths.data_dir() / "publications.yml"
    CACHE_PATH = paths.cache_dir() / "citation_counts.json"
    SNAPSHOT_PATH = paths.data_dir() / "citation_counts.json"
    REPORT_PATH = paths.qc_dir() / "citations_report.md"


UA = "cv-citation-fetcher/1.0"
TIMEOUT_S = 12.0
CROSSREF_HOST = "api.crossref.org"
CROSSREF_GAP_S = 0.25  # public-pool: 5 req/s + concurrency=1


def _iter_dois(data) -> list[str]:
    """Yield every `doi:` field from publications.yml (subsection structure)."""
    out: list[str] = []
    for sub in data:
        for e in sub.get("entries", []) or []:
            doi = e.get("doi")
            if doi:
                out.append(str(doi))
    return out


def fetch_count(doi: str, *, throttle: HostThrottle) -> tuple[int | None, str, str | None]:
    """Fetch citation count for one DOI from Crossref.

    Returns (count, status, error). count is None when status != FETCHED.
    """
    throttle.wait(CROSSREF_HOST)
    encoded = urllib.parse.quote(doi, safe="/")
    url = f"https://{CROSSREF_HOST}/works/{encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            status_code = resp.status
            if status_code != 200:
                return _classify_http(status_code, None)
            body = resp.read()
    except urllib.error.HTTPError as e:
        return _classify_http(e.code, str(e))
    except urllib.error.URLError as e:
        return (None, CountStatus.FAILED_NETWORK, str(e.reason))
    except (TimeoutError, OSError) as e:
        return (None, CountStatus.FAILED_NETWORK, str(e))
    # 200 OK: parse JSON, extract count.
    try:
        j = json.loads(body)
        n = j["message"]["is-referenced-by-count"]
        return (int(n), CountStatus.FETCHED, None)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        return (None, CountStatus.FAILED_OTHER, f"parse: {e}")


def _classify_http(status_code: int, err: str | None) -> tuple[int | None, str, str | None]:
    """Map an HTTP response code to a CountStatus."""
    if status_code == 404:
        return (None, CountStatus.FAILED_NOT_FOUND, err)
    if status_code == 429:
        return (None, CountStatus.FAILED_RATE_LIMIT, err)
    if 500 <= status_code < 600:
        return (None, CountStatus.FAILED_NETWORK, err or f"http {status_code}")
    return (None, CountStatus.FAILED_OTHER, err or f"http {status_code}")


def _status_str(s) -> str:
    """Normalize a status (str or CountStatus enum) to its string value.
    Python 3.11+ str-mixed enums return 'CountStatus.FOO' from f-strings;
    `.value` returns the plain 'foo' (R1-M3)."""
    return s.value if hasattr(s, "value") else str(s)


def write_report(cache: CitationCache, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = cache.stats()
    lines: list[str] = []
    lines.append("# Citation counts report")
    lines.append("")
    lines.append(f"- Total DOIs in cache: {sum(stats.values())}")
    for status, n in sorted(stats.items()):
        lines.append(f"  - {_status_str(status)}: {n}")
    # Top 10 most cited
    entries = [
        (d, e) for d, e in cache.all().items() if e.status == CountStatus.FETCHED and e.count
    ]
    entries.sort(key=lambda t: t[1].count or 0, reverse=True)
    lines.append("")
    lines.append("## Most cited")
    for doi, e in entries[:10]:
        lines.append(f"- {doi}: {e.count}")
    # Failed
    failed = [(d, e) for d, e in cache.all().items() if e.status != CountStatus.FETCHED]
    if failed:
        lines.append("")
        lines.append("## Failed")
        for doi, e in failed:
            lines.append(f"- {doi}: {_status_str(e.status)}{f' ({e.error})' if e.error else ''}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_fetch(*, force: bool, workers: int, quiet: bool) -> dict:
    """Walk publications.yml, fetch missing/stale counts, write sidecar + snapshot."""
    _, data = yaml_io.load(PUB_YML)
    dois = _iter_dois(data)
    cache = CitationCache.load(CACHE_PATH)
    throttle = HostThrottle(
        gap_per_host={CROSSREF_HOST: CROSSREF_GAP_S},
        default_gap=0.1,
    )

    # Respect Crossref's concurrency=1 directive: use one worker regardless
    # of the --workers flag (kept for CLI parity but clamped here).
    effective_workers = 1
    targets = [d for d in dois if cache.should_attempt(d, force=force)]
    if not quiet:
        print(f"[v14] {len(targets)} DOIs to fetch (of {len(dois)} total)", file=sys.stderr)

    def one(doi: str) -> tuple[str, int | None, str, str | None]:
        count, status, error = fetch_count(doi, throttle=throttle)
        return (doi, count, status, error)

    if targets:
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            for doi, count, status, error in ex.map(one, targets):
                cache.record(doi, count=count, source="crossref", status=status, error=error)
                if not quiet:
                    if status == CountStatus.FETCHED:
                        print(f"  {doi}: {count}", file=sys.stderr)
                    else:
                        print(f"  {doi}: {_status_str(status)}", file=sys.stderr)
        cache.save()

    # V13-V19-D R2-M1 / R2-M8 (2026-05-18): scope the snapshot to DOIs
    # currently present in publications.yml. Sidecar keeps full history;
    # the committed snapshot prunes orphans (preprint→published swaps,
    # typo fixes, entry deletions) so the renderer never sees a count
    # for a DOI that's no longer on the CV.
    valid_dois = {d.strip().lower() for d in dois if d}
    snapshot_body = write_snapshot(cache, SNAPSHOT_PATH, valid_dois=valid_dois)
    write_report(cache, REPORT_PATH)
    return {
        "total_dois": len(dois),
        "attempted": len(targets),
        "snapshot_count": len(snapshot_body.get("counts") or {}),
        "stats": cache.stats(),
    }


def run_snapshot_only() -> dict:
    """Regenerate `data/citation_counts.json` from the sidecar (no network).
    Also refreshes the Markdown report so it stays in sync."""
    cache = CitationCache.load(CACHE_PATH)
    # V13-V19-D R2-M1 / R2-M8: scope the snapshot to YAML-present DOIs.
    # Even on `--snapshot-only` we want orphan pruning — that's the route
    # the editor exposes for "regen without fetching."
    _, data = yaml_io.load(PUB_YML)
    valid_dois = {d.strip().lower() for d in _iter_dois(data) if d}
    body = write_snapshot(cache, SNAPSHOT_PATH, valid_dois=valid_dois)
    write_report(cache, REPORT_PATH)
    return {"snapshot_count": len(body.get("counts") or {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch citation counts from Crossref.")
    ap.add_argument("--force", action="store_true", help="Ignore TTL; re-fetch every DOI.")
    ap.add_argument(
        "--workers", type=int, default=1, help="Concurrency (clamped to 1 for Crossref)."
    )
    ap.add_argument(
        "--ttl-days", type=int, default=30, help="Informational; per-status TTLs are hard-coded."
    )
    ap.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Regenerate snapshot from sidecar without network.",
    )
    ap.add_argument("--quiet", action="store_true", help="Suppress per-DOI progress lines.")
    args = ap.parse_args(argv)

    if args.snapshot_only:
        result = run_snapshot_only()
        if not args.quiet:
            print(
                f"[v14] snapshot regenerated: {result['snapshot_count']} entries", file=sys.stderr
            )
        return 0

    result = run_fetch(force=args.force, workers=args.workers, quiet=args.quiet)
    if not args.quiet:
        print(
            f"[v14] {result['attempted']} attempted; "
            f"snapshot now has {result['snapshot_count']} entries",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
