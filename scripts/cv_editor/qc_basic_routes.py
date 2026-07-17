"""Basic-QC + URL-verify routes (extracted from app.py 2026-05-29, M2).

The non-triage QC surface (predates V23-B) plus the V7 URL-verifier:
report/status passthroughs + the two background kickers the user fires
from the UI. Follows the `register_qc_triage_routes(app, deps)` pattern
(gotcha #69) so endpoint names stay flat.

Routes registered here:
  - GET  /qc/report        — qc_report (plain-text qc/report.md)
  - GET  /qc/status        — qc_status_json
  - POST /qc/run           — qc_run (kick qc_publications.py)
  - GET  /qc/urls_report   — urls_report_text
  - GET  /urls/verify      — urls_verify_view
  - POST /urls/verify      — urls_verify_run (kick verify_urls.py)
  - GET  /urls/status      — urls_status_json

`_qc_status` stays a create_app() closure (entry_view shares it) and is
handed in via deps; `_url_status` is local to the URL routes so it moves
here. Both background kickers stay in create_app() (the basic-QC kicker is
shared with entry_save/trackers/promote) and are passed BY REFERENCE.
Behaviour-identical (M2 fingerprint guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask, Response, flash, redirect, render_template, request, url_for


@dataclass
class QCBasicDeps:
    """DI surface for the basic-QC + URL-verify routes. All read-only from
    the module (the kickers mutate their own state internally; `_url_status`
    only reads `url_state["running"]`)."""

    root: Path
    qc_status: Callable[[], dict]  # shared _qc_status (also used by entry_view)
    qc_kick: Callable[..., None]  # shared _kick_qc_if_idle
    url_state: dict  # _url_state (read .["running"])
    url_kick: Callable[..., None]  # _kick_url_verify_if_idle


def register_qc_basic_routes(app: Flask, deps: QCBasicDeps) -> None:
    ROOT = deps.root

    def _url_status():
        """Return verifier state + freshness of qc/urls_report.md."""
        pubs = ROOT / "data" / "publications.yml"
        report = ROOT / "qc" / "urls_report.md"
        if not report.exists():
            return {
                "running": deps.url_state["running"],
                "fresh": False,
                "exists": False,
                "url": "/qc/urls_report",
                "mtime": None,
                "failed": None,
                "total": None,
            }
        fresh = report.stat().st_mtime >= pubs.stat().st_mtime if pubs.exists() else True
        failed = None
        total = None
        try:
            text = report.read_text()
            for line in text.splitlines():
                if line.startswith("- Total unique URLs:"):
                    total = int(line.split(":", 1)[1].strip())
                elif line.startswith("- OK:"):
                    parts = line.split("·")
                    for p in parts:
                        p = p.strip()
                        if p.startswith("Failing:"):
                            failed = int(p.split(":", 1)[1].strip())
                            break
        except (OSError, ValueError):
            pass
        return {
            "running": deps.url_state["running"],
            "fresh": fresh,
            "exists": True,
            "url": "/qc/urls_report",
            "mtime": datetime.fromtimestamp(report.stat().st_mtime).isoformat(timespec="seconds"),
            "failed": failed,
            "total": total,
        }

    @app.route("/qc/report")
    def qc_report():
        report = ROOT / "qc" / "report.md"
        if not report.exists():
            return "qc/report.md not present yet — run a QC pass first.", 404
        return Response(report.read_text(), mimetype="text/plain")

    @app.route("/qc/status")
    def qc_status_json():
        return deps.qc_status()

    @app.route("/qc/run", methods=["POST"])
    def qc_run():
        deps.qc_kick()
        flash("QC pass kicked off in the background. Refresh in ~20s to see results.", "ok")
        return redirect(url_for("section_list", section="publications"))

    # ----- V7: URL verification surface -----

    @app.route("/qc/urls_report")
    def urls_report_text():
        p = ROOT / "qc" / "urls_report.md"
        if not p.exists():
            return "qc/urls_report.md not present yet — run the URL verifier first.", 404
        return Response(p.read_text(), mimetype="text/plain")

    @app.route("/urls/verify", methods=["GET"])
    def urls_verify_view():
        status = _url_status()
        report_text = None
        p = ROOT / "qc" / "urls_report.md"
        if p.exists():
            try:
                report_text = p.read_text()
            except OSError:
                report_text = None
        return render_template(
            "urls_verify.html",
            status=status,
            report_text=report_text,
        )

    @app.route("/urls/verify", methods=["POST"])
    def urls_verify_run():
        force = bool(request.form.get("force"))
        deps.url_kick(force=force)
        if force:
            flash(
                "URL verifier kicked off with --force (ignoring the 30-day cache). "
                "This re-checks every URL; expect ~2–3 minutes. "
                "Refresh this page to see the updated report.",
                "ok",
            )
        else:
            flash(
                "URL verifier kicked off in the background. "
                "Cached URLs are skipped; only stale/uncached/previously-failing URLs are checked. "
                "Refresh this page to see the report.",
                "ok",
            )
        return redirect(url_for("urls_verify_view"))

    @app.route("/urls/status")
    def urls_status_json():
        return _url_status()
