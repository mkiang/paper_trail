"""V14 citation-counts routes (extracted from app.py 2026-05-29, M2).

First leaf extraction of the M2 app.py decomposition. Follows the shipped
`register_qc_triage_routes(app, deps)` pattern (gotcha #69): routes are
defined with `@app.route` on the SAME `app`, so endpoint names stay flat
(`citations_view`, `citations_fetch`, ...) and every template `url_for`
is unchanged. The shared helpers (`cfg_path`, `load_section`) are NOT
moved — they stay as `create_app()` closures and are handed in via
`CitationsDeps`, so their behaviour is provably unchanged.

Routes registered here:
  - GET  /citations               — citations_view
  - POST /citations/fetch         — citations_fetch
  - POST /citations/snapshot      — citations_snapshot
  - GET  /citations/status        — citations_status_json
  - GET  /qc/citations_report     — citations_report_text

Behaviour-identical to the pre-extraction code (M2 fingerprint guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from cv_editor import yaml_io


@dataclass
class CitationsDeps:
    """DI surface for the citations routes. Every field is a name
    `create_app()` used to close over implicitly. All read-only from the
    module's perspective (the kicker mutates `cit_state` internally; the
    routes only read `cit_state["running"]`)."""

    root: Path
    cfg_path: Callable[[str], Path]
    load_section: Callable[[str], tuple]
    logger: object  # app.logger
    cit_kick: Callable[..., None]  # _kick_citation_fetch_if_idle
    cit_state: dict  # _cit_state (read .["running"])


def register_citations_routes(app: Flask, deps: CitationsDeps) -> None:
    ROOT = deps.root
    _cfg_path = deps.cfg_path
    _load_section = deps.load_section

    def _citation_status():
        """Status payload for /citations: cache state + drift detection."""
        from cv_editor.citation_counts import CitationCache, snapshot_drift

        cache_path = _cfg_path("CITATION_CACHE_PATH")
        snap_path = _cfg_path("CITATION_SNAPSHOT_PATH")
        cache = CitationCache.load(cache_path)
        stats = cache.stats()
        # V13-V19-D tail R1-M5 (2026-05-18): pass valid_dois so drift's
        # sidecar_count matches the post-F7 write_snapshot scoping.
        # Without this, an orphan DOI in the sidecar inflates the count
        # and triggers `stale: True` on every refresh.
        try:
            _, _, _, _pub_data_drift = _load_section("publications")
            _valid_drift: set[str] | None = {
                (e.get("doi") or "").strip().lower()
                for sub in _pub_data_drift
                for e in (sub.get("entries") or [])
                if e.get("doi")
            }
        except Exception:
            _valid_drift = None
        drift = snapshot_drift(cache, snap_path, valid_dois=_valid_drift)
        report = ROOT / "qc" / "citations_report.md"
        # Three buckets per R2-H3:
        #   has_counts = fetched + count > 0
        #   fetched_zero = fetched + count == 0
        #   never_attempted = DOIs in publications.yml not in cache + failed states
        from cv_editor.citation_counts import CountStatus

        has_counts = 0
        fetched_zero = 0
        failed = 0
        for e in cache.all().values():
            if e.status == CountStatus.FETCHED:
                if (e.count or 0) > 0:
                    has_counts += 1
                else:
                    fetched_zero += 1
            else:
                failed += 1
        # Count DOIs in publications.yml not in cache at all.
        pubs_path = ROOT / "data" / "publications.yml"
        never_attempted = 0
        if pubs_path.exists():
            try:
                _, data = yaml_io.load(pubs_path)
                from cv_editor.citation_counts import _doi_key

                yaml_dois = set()
                for sub in data:
                    for ent in sub.get("entries", []) or []:
                        d = ent.get("doi")
                        if d:
                            yaml_dois.add(_doi_key(str(d)))
                cached = set(cache.all().keys())
                never_attempted = len(yaml_dois - cached)
            except Exception:
                pass
        return {
            "running": deps.cit_state["running"],
            "stats": stats,
            "has_counts": has_counts,
            "fetched_zero": fetched_zero,
            "failed_count": failed,
            "never_attempted": never_attempted,
            "snapshot_count": drift["snapshot_count"],
            "sidecar_count": drift["sidecar_count"],
            "stale": drift["stale"],
            "snapshot_mtime": (
                datetime.fromtimestamp(drift["snapshot_mtime"]).isoformat(timespec="seconds")
                if drift["snapshot_mtime"]
                else None
            ),
            "sidecar_mtime": (
                datetime.fromtimestamp(drift["sidecar_mtime"]).isoformat(timespec="seconds")
                if drift["sidecar_mtime"]
                else None
            ),
            "report_url": "/qc/citations_report" if report.exists() else None,
        }

    @app.route("/citations", methods=["GET"])
    def citations_view():
        from cv_editor.citation_counts import CitationCache, CountStatus

        cache = CitationCache.load(_cfg_path("CITATION_CACHE_PATH"))
        status = _citation_status()
        # Top 10 most-cited (sidecar-derived)
        most_cited = sorted(
            (
                (doi, e.count)
                for doi, e in cache.all().items()
                if e.status == CountStatus.FETCHED and e.count and e.count > 0
            ),
            key=lambda t: t[1],
            reverse=True,
        )[:10]
        # Failed list (browsable per R2-M1)
        failed_rows = sorted(
            ((doi, e) for doi, e in cache.all().items() if e.status != CountStatus.FETCHED),
            key=lambda t: t[0],
        )
        return render_template(
            "citations_status.html",
            status=status,
            most_cited=most_cited,
            failed_rows=failed_rows,
        )

    @app.route("/citations/fetch", methods=["POST"])
    def citations_fetch():
        force = bool(request.form.get("force"))
        deps.cit_kick(force=force)
        if force:
            flash(
                "Citation-count fetcher kicked off with --force (ignoring TTL). "
                "Re-fetches every DOI from Crossref; expect ~30s.",
                "ok",
            )
        else:
            flash(
                "Citation-count fetcher kicked off in the background. "
                "Cached DOIs within their TTL are skipped.",
                "ok",
            )
        return redirect(url_for("citations_view"))

    @app.route("/citations/snapshot", methods=["POST"])
    def citations_snapshot():
        """Regenerate `data/citation_counts.json` from the sidecar (no network).

        V13-V19-D R3-H2 guard: on a fresh clone the gitignored sidecar
        is empty but the committed snapshot has ~95 entries. Without
        the size check below, this route would silently wipe the
        committed snapshot down to {} — invisible until the next build
        renders zero citation counts."""
        from cv_editor.citation_counts import CitationCache, load_snapshot, write_snapshot

        cache_path = _cfg_path("CITATION_CACHE_PATH")
        snap_path = _cfg_path("CITATION_SNAPSHOT_PATH")
        cache = CitationCache.load(cache_path)
        # V13-V19-D tail R1-M1 fix (2026-05-18): scope `new_count` to
        # DOIs we'd actually write. Without this, the shrink guard
        # passed when sidecar=95 + snapshot=95 + 2 orphan DOIs in
        # publications.yml → write_snapshot wrote 93 → committed
        # snapshot silently shrunk by 2 with no warn flash. Now we
        # pre-filter by the same `valid_dois` set we pass to
        # write_snapshot, so the guard's count matches the write's count.
        try:
            _, _, _, pub_data_for_count = _load_section("publications")
            valid_for_count: set[str] | None = set()
            for sub in pub_data_for_count:
                for e in sub.get("entries") or []:
                    d = (e.get("doi") or "").strip().lower()
                    if d:
                        valid_for_count.add(d)
        except Exception:
            valid_for_count = None
        # Count what would be written (FETCHED + count > 0 — same filter
        # used by write_snapshot internally — AND in valid_dois if known).
        from cv_editor.citation_counts import CountStatus

        new_count = sum(
            1
            for doi, e in cache.all().items()
            if e.status == CountStatus.FETCHED
            and e.count
            and e.count > 0
            and (valid_for_count is None or doi in valid_for_count)
        )
        prior_count = 0
        if snap_path.exists():
            try:
                prior_snap = load_snapshot(snap_path)
                prior_count = len(prior_snap.get("counts") or {})
            except Exception:
                prior_count = 0
        force = (request.form.get("force") or "").strip() in ("1", "true", "yes", "on")
        if new_count < prior_count and not force:
            flash(
                f"Refusing to shrink snapshot from {prior_count} → {new_count} "
                f"entries — looks like the sidecar is missing data (cold clone? "
                f"deleted .cache/?). Click 'Fetch (use cache)' first to "
                f"repopulate the sidecar, OR re-submit with force=1 if you "
                "really mean to shrink the committed snapshot.",
                "warn",
            )
            return redirect(url_for("citations_view"))
        # V13-V19-D R2-M1 / R2-M8 (2026-05-18): scope the snapshot to
        # DOIs currently in publications.yml. Stops the regen from re-
        # introducing orphan DOIs that the fetcher previously pruned.
        try:
            _, _, _, pub_data = _load_section("publications")
            valid_dois: set[str] | None = set()
            for sub in pub_data:
                for e in sub.get("entries") or []:
                    doi = (e.get("doi") or "").strip().lower()
                    if doi:
                        valid_dois.add(doi)
        except Exception as exc:
            deps.logger.warning(
                "citations_snapshot: could not load publications.yml for "
                "valid-DOI scoping (%s); falling back to unscoped write.",
                exc,
            )
            valid_dois = None
        body = write_snapshot(cache, snap_path, valid_dois=valid_dois)
        n = len(body.get('counts') or {})
        flash(
            f"Snapshot regenerated from sidecar: {n} entr{'y' if n == 1 else 'ies'}.",
            "ok",
        )
        return redirect(url_for("citations_view"))

    @app.route("/citations/status")
    def citations_status_json():
        return _citation_status()

    @app.route("/qc/citations_report")
    def citations_report_text():
        p = ROOT / "qc" / "citations_report.md"
        if not p.exists():
            return "qc/citations_report.md not present yet — fetch citation counts first.", 404
        return Response(p.read_text(), mimetype="text/plain")
