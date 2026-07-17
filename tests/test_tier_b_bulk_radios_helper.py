"""Tier B / B6 (2026-05-27) — extracted bulk-radio JS helper.

The actual two-step armed/commit state machine + 200ms guard +
aria-pressed lives in static/bulk_radios.js. Browser-side behavior
isn't exercisable without a JS runtime; these tests assert on
SERVER-rendered template structure to guarantee the wire-up is
intact and the data-* attribute contract from gotcha #51 +
gotcha #57 is preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


_PUBMED_SYNC_TEMPLATE = ROOT / "scripts" / "cv_editor" / "templates" / "pubmed_sync.html"
_QC_TRIAGE_TEMPLATE = ROOT / "scripts" / "cv_editor" / "templates" / "qc_triage.html"


def test_pubmed_sync_template_loads_bulk_radios_js():
    """The pubmed_sync triage template must include the bulk_radios.js
    script tag (B6 wire-up). Test the TEMPLATE SOURCE rather than the
    rendered output because the script lives inside a
    `{% if triage_rows %}` block — when the live sidecar happens to
    have zero pending flags, the rendered page omits the script even
    though the wire-up is correct."""
    src = _PUBMED_SYNC_TEMPLATE.read_text(encoding="utf-8")
    assert "bulk_radios.js" in src, (
        "pubmed_sync.html must reference static/bulk_radios.js inside the triage_rows block"
    )


def test_qc_triage_template_loads_bulk_radios_js():
    src = _QC_TRIAGE_TEMPLATE.read_text(encoding="utf-8")
    assert "bulk_radios.js" in src


def test_pubmed_sync_template_keeps_distinct_apply_attribute():
    """gotcha #51 critical contract: the apply button uses
    `data-bulk-apply` (NOT `data-bulk-decision`). The B6 extraction
    must preserve this — if the helper somehow collapsed them, the
    M5 handlers would match the apply button and fire on the first
    click, bypassing the two-step confirm. Source-level check so the
    test is robust against empty-sidecar render states."""
    src = _PUBMED_SYNC_TEMPLATE.read_text(encoding="utf-8")
    assert "data-bulk-apply" in src
    assert "All apply PubMed" in src
    assert "data-bulk-decision" in src  # the M5 family still uses it


def test_qc_triage_template_keeps_distinct_attributes():
    """Same contract on the per-section qc_triage template:
    `data-qc-bulk-apply` is the apply button; `data-qc-bulk-decision`
    is the defer/keep_yaml buttons. They must never be confused."""
    src = _QC_TRIAGE_TEMPLATE.read_text(encoding="utf-8")
    assert "data-qc-bulk-apply" in src
    assert "data-qc-bulk-decision" in src


def test_bulk_radios_js_file_exists():
    """File-level guard. If anyone moves or deletes static/bulk_radios.js
    without updating the templates, the wire-up tests above would still
    pass (Flask serves any extant static file). This guard fails
    explicitly."""
    path = ROOT / "scripts" / "cv_editor" / "static" / "bulk_radios.js"
    assert path.exists(), "static/bulk_radios.js is missing — B6 extraction broken"
    content = path.read_text()
    # Public API surface must be intact.
    assert "wireBulkDecision" in content
    assert "wireBulkApplyTwoStep" in content
    # Hard-coded a11y contract (gotcha #57 U-H1 / U-H2).
    assert "aria-pressed" in content
    assert "MIN_CONFIRM_MS" in content
    # Hard-coded "! " glyph (color-blind a11y).
    assert "'! '" in content or '"! "' in content
