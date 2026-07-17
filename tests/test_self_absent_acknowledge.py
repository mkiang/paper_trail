"""Self-absent QC acknowledge/dismiss (2026-06-08).

The /qc/triage page lists publications where the configured author isn't
detected in the author list. These are often legitimate (forewords,
committee reports), so the user can Acknowledge one to dismiss it. An
acknowledged finding is suppressed from `effective_findings` (and thus the
banner counts) and reappears via Un-acknowledge.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))


# ---- unit: suppression predicate ----


def test_predicate_none_decision_not_silenced():
    from cv_editor.qc_decisions import is_silenced_self_absent

    assert is_silenced_self_absent({"id": "SA:1"}, None) is False


def test_predicate_keep_yaml_silences():
    from cv_editor.qc_decisions import Decision, is_silenced_self_absent

    dec = Decision(
        decision="keep_yaml", finding_type="SELF_ABSENT", decided_at="2026-06-08T00:00:00+00:00"
    )
    assert is_silenced_self_absent({"id": "SA:1"}, dec) is True


def test_predicate_defer_never_silences():
    from cv_editor.qc_decisions import Decision, is_silenced_self_absent

    dec = Decision(
        decision="defer", finding_type="SELF_ABSENT", decided_at="2026-06-08T00:00:00+00:00"
    )
    assert is_silenced_self_absent({"id": "SA:1"}, dec) is False


# ---- unit: effective_findings filters acknowledged rows ----


def test_effective_findings_filters_acknowledged_self_absent():
    from cv_editor import qc_decisions, qc_sync

    sidecar = {
        "findings": {
            "self_absent": [
                {
                    "id": "SA:111",
                    "type": "SELF_ABSENT",
                    "global_idx": 0,
                    "subsection": "OSW",
                    "entry_index": 1,
                    "title_preview": "Paper A",
                },
                {
                    "id": "SA:222",
                    "type": "SELF_ABSENT",
                    "global_idx": 1,
                    "subsection": "OSW",
                    "entry_index": 2,
                    "title_preview": "Paper B",
                },
            ],
        }
    }
    decisions = qc_decisions.Decisions.empty()
    decisions.set("SA:111", decision="keep_yaml", finding_type="SELF_ABSENT")
    eff = qc_sync.effective_findings(sidecar, decisions)
    ids = [f["id"] for f in eff["self_absent"]]
    assert ids == ["SA:222"], "acknowledged SA:111 should be filtered out"


def test_effective_total_drops_acknowledged_self_absent():
    from cv_editor import qc_decisions, qc_sync

    sidecar = {
        "findings": {
            "self_absent": [
                {
                    "id": "SA:111",
                    "type": "SELF_ABSENT",
                    "global_idx": 0,
                    "subsection": "OSW",
                    "entry_index": 1,
                    "title_preview": "A",
                },
            ]
        }
    }
    empty = qc_decisions.Decisions.empty()
    assert qc_sync.effective_total(qc_sync.effective_findings(sidecar, empty)) == 1
    acked = qc_decisions.Decisions.empty()
    acked.set("SA:111", decision="keep_yaml", finding_type="SELF_ABSENT")
    assert qc_sync.effective_total(qc_sync.effective_findings(sidecar, acked)) == 0


# ---- route: acknowledge / un-acknowledge ----


@pytest.fixture
def app_and_paths(tmp_path, monkeypatch):
    sidecar = tmp_path / "report.json"
    decisions = tmp_path / "qc_decisions.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-06-08T12:00:00+00:00",
                "qc_script_version": "1.0",
                "publications_yml_mtime_ns": 1,
                "cache_key_version": 1,
                "summary": {"totals": {"self_absent": 1}, "total_findings": 1},
                "findings": {
                    "mismatches": [],
                    "variants": [],
                    "id_enrichments": [],
                    "pmid_mismatches": [],
                    "self_absent": [
                        {
                            "id": "SA:90000033",
                            "type": "SELF_ABSENT",
                            "subsection": "OSW",
                            "entry_index": 1,
                            "global_idx": 0,
                            "pmid": "90000033",
                            "doi": None,
                            "title_preview": "A committee report without the author",
                        },
                    ],
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
    return app, sidecar, decisions


@pytest.fixture
def client(app_and_paths):
    return app_and_paths[0].test_client()


def _load_decisions(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def test_acknowledge_writes_keep_yaml_decision(client, app_and_paths):
    _, _, decisions = app_and_paths
    resp = client.post("/qc/self_absent/acknowledge", data={"finding_id": "SA:90000033"})
    assert resp.status_code in (302, 303)
    saved = _load_decisions(decisions)
    rec = saved["decisions"]["SA:90000033"]
    assert rec["decision"] == "keep_yaml"
    assert rec["finding_type"] == "SELF_ABSENT"


def test_acknowledged_row_suppressed_then_in_collapsible(client):
    # Before: active in the triage page.
    body = client.get("/qc/triage").get_data(as_text=True)
    assert "A committee report without the author" in body
    assert "Acknowledge" in body  # the per-row button label

    client.post("/qc/self_absent/acknowledge", data={"finding_id": "SA:90000033"})

    body2 = client.get("/qc/triage").get_data(as_text=True)
    # Still on the page, but now in the "Acknowledged (1)" collapsible
    # with an Un-acknowledge control.
    assert "Acknowledged (1)" in body2
    assert "Un-acknowledge" in body2


def test_undo_removes_decision(client, app_and_paths):
    _, _, decisions = app_and_paths
    client.post("/qc/self_absent/acknowledge", data={"finding_id": "SA:90000033"})
    assert "SA:90000033" in _load_decisions(decisions)["decisions"]
    client.post("/qc/self_absent/acknowledge", data={"finding_id": "SA:90000033", "undo": "1"})
    saved = _load_decisions(decisions)
    assert "SA:90000033" not in saved["decisions"]
    # Tombstoned, not deleted outright.
    assert "SA:90000033" in saved["tombstones"]


def test_rejects_non_self_absent_finding_id(client, app_and_paths):
    _, _, decisions = app_and_paths
    resp = client.post(
        "/qc/self_absent/acknowledge", data={"finding_id": "MM:pubmed:111:journal:aaaa"}
    )
    assert resp.status_code in (302, 303)
    # No decision written.
    assert not decisions.exists() or "MM:pubmed:111:journal:aaaa" not in _load_decisions(
        decisions
    ).get("decisions", {})
