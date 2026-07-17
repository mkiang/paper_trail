"""M1 (2026-05-29) stabilize fixes:

- field_handlers._apply_int: bad input is dropped, not raised (no 500 in
  non-validated callers like the pending-form re-render).
- Flask secret_key: per-process random key (stable only when the launcher
  sets CV_EDITOR_SECRET_KEY).
- QC apply route: FAILS CLOSED when the sidecar lacks
  publications_yml_mtime_ns (the task-#42 data-loss vector).

(SSRF guard has its own file: test_m1_ssrf_guard.py.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from cv_editor import field_handlers  # noqa: E402

# ----- _apply_int defensive --------------------------------------------


def test_apply_int_drops_bad_value_without_raising():
    from ruamel.yaml.comments import CommentedMap

    entry = CommentedMap()
    field = {"name": "year", "type": "int"}
    # Must not raise; bad value is simply not set.
    field_handlers._apply_int({"year": "not-a-number"}, field, entry)
    assert "year" not in entry


def test_apply_int_sets_valid_value():
    from ruamel.yaml.comments import CommentedMap

    entry = CommentedMap()
    field_handlers._apply_int({"year": "2026"}, {"name": "year"}, entry)
    assert entry["year"] == 2026


# ----- Flask secret_key per-process ------------------------------------


def test_secret_key_is_random_per_process_without_env(monkeypatch):
    monkeypatch.delenv("CV_EDITOR_SECRET_KEY", raising=False)
    from cv_editor.app import create_app

    a, b = create_app(), create_app()
    assert a.secret_key and b.secret_key
    assert a.secret_key != b.secret_key
    assert a.secret_key != "local-only-cv-editor"


def test_secret_key_stable_when_launcher_sets_env(monkeypatch):
    monkeypatch.setenv("CV_EDITOR_SECRET_KEY", "fixed-launch-key-abc")
    from cv_editor.app import create_app

    a, b = create_app(), create_app()
    assert a.secret_key == "fixed-launch-key-abc" == b.secret_key


# ----- QC apply route fails closed without sidecar mtime ---------------


@pytest.fixture
def app_missing_mtime(tmp_path, monkeypatch):
    """QC sidecar that OMITS publications_yml_mtime_ns. An `apply` on a
    real entry must be refused before any write (the task-#42 vector)."""
    sidecar = tmp_path / "report.json"
    decisions = tmp_path / "qc_decisions.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-05-29T12:00:00+00:00",
                "qc_script_version": "1.0",
                # NOTE: publications_yml_mtime_ns deliberately absent.
                "cache_key_version": 1,
                "summary": {"totals": {}, "total_findings": 1},
                "findings": {
                    "mismatches": [
                        {
                            "id": "MM:pubmed:test111:journal:aaaa1111",
                            "type": "MISMATCH",
                            "global_idx": 0,
                            "subsection": "PRR",
                            "entry_index": 1,
                            "pmid": "test111",
                            "doi": None,
                            "title_preview": "Test Paper",
                            "field": "journal",
                            "yaml_value": "Old",
                            "canonical_value": "New",
                            "source": "pubmed",
                        },
                    ],
                    "variants": [],
                    "id_enrichments": [],
                    "pmid_mismatches": [],
                    "self_absent": [],
                    "author_name_variants": [],
                    "journal_name_variants": [],
                    "missing_ids": [],
                },
            }
        )
    )
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", sidecar)
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["QC_DECISIONS_PATH"] = decisions
    return app


def test_qc_apply_fails_closed_when_sidecar_missing_mtime(app_missing_mtime):
    """Posting `apply` with no sidecar mtime must NOT write (the corruption
    canary would catch a write); it redirects with a clear warning."""
    client = app_missing_mtime.test_client()
    resp = client.post(
        "/qc/apply",
        data={"decision-MM:pubmed:test111:journal:aaaa1111": "apply"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "modification time" in body and "Re-run the QC sweep" in body
