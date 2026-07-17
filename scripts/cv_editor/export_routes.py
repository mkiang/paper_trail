"""Whole-CV export routes (M5 5b). Tools-nav links that render the entire CV
to a downloadable document in a non-PDF target.

Unlike CSV export (per-section, `/<section>/export.csv`), a web/markdown CV is a
WHOLE-document artifact, so these are top-level `/export/*` links, not per-section
buttons. The logic lives in `export_core` (the target-agnostic Document model +
`build_model`) + `export_emit` (the dumb per-target emitters); the routes are thin
wrappers — build the model once, emit, return an attachment. Read-only: they never
touch `data/`.

The export view defaults to the public default variant (the first in meta.yml;
audited so a published web CV can't leak `hide-from` / `highlighted` /
review-draft entries — see
`plans/m5-5b-exports-strategy.md`). `?variant=` overrides it for parity with the
CLI (`scripts/export_markdown.py --variant`); the user can already build any variant
from the Style page, so this is not a new disclosure surface on a local single-user
tool.

Registered via `register_export_routes(app, ExportDeps)` — the same bind-don't-move
seam as the other M2 route modules (gotcha #70). The data dir is `deps.root/data`,
captured at registration. `app.config["EXPORT_DATA_DIR"]` is a TEST-ONLY override
(points route tests at the frozen fixture corpus). It is deliberately NOT a `_cfg_path`
registry entry (gotcha #65): that registry is for individual sidecar/snapshot FILE
paths, while this is a whole data DIRECTORY redirect with a nullable default — a
distinct concern. Production never sets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flask import Flask, Response, current_app, flash, redirect, request, url_for

from cv_editor import export_core, export_emit

# Forced-download responses; nosniff stops a browser from MIME-sniffing the bytes
# into something executable if a future change ever serves them inline.
_DL_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@dataclass
class ExportDeps:
    """DI surface for the whole-CV export routes. `root` is the project root
    (typst/); the data dir is `root/data` (overridable in tests via the
    `EXPORT_DATA_DIR` config knob — see the module docstring)."""

    root: Path
    logger: object


def register_export_routes(app: Flask, deps: ExportDeps) -> None:
    data_root = Path(deps.root)

    def _data_dir() -> Path:
        override = current_app.config.get("EXPORT_DATA_DIR")
        return Path(override) if override else data_root / "data"

    @app.route("/export/markdown")
    def export_markdown():
        """Render the whole CV to a Markdown document and download it."""
        variant = (request.args.get("variant") or "").strip() or None
        try:
            doc = export_core.build_model(_data_dir(), target=export_core.MD, variant=variant)
            text = export_emit.render_markdown(doc)
        except Exception as e:
            deps.logger.warning("export_markdown failed: %s: %s", type(e).__name__, e)
            flash(f"Markdown export failed: {type(e).__name__}: {e}", "warn")
            return redirect(url_for("index")), 500
        return Response(
            text,
            mimetype="text/markdown; charset=utf-8",
            headers={**_DL_HEADERS, "Content-Disposition": 'attachment; filename="cv.md"'},
        )

    @app.route("/export/html")
    def export_html():
        """Render the whole CV to a standalone HTML page and download it. The page
        is self-contained (inline CSS, no external assets) so it drops straight onto
        a static host (e.g. a personal website)."""
        variant = (request.args.get("variant") or "").strip() or None
        try:
            doc = export_core.build_model(_data_dir(), target=export_core.HTML, variant=variant)
            page = export_emit.render_html(doc, variant=variant)
        except Exception as e:
            deps.logger.warning("export_html failed: %s: %s", type(e).__name__, e)
            flash(f"HTML export failed: {type(e).__name__}: {e}", "warn")
            return redirect(url_for("index")), 500
        return Response(
            page,
            mimetype="text/html; charset=utf-8",
            headers={**_DL_HEADERS, "Content-Disposition": 'attachment; filename="cv.html"'},
        )
