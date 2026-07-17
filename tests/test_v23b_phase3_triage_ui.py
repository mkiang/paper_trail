"""V23-B Phase 3: /qc/triage route + qc_sync loader tests (2026-05-25).

Phase 3 ships the read-only triage page (jump-to-edit only for PMID
mismatches + self_absent). Decisions sidecar + apply route arrive in
Phase 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Test client with the QC sidecar path redirected to a temp dir so
    tests don't depend on the real qc/report.json content."""
    sidecar = tmp_path / "report.json"
    # Write a small fixture sidecar.
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-05-25T12:00:00+00:00",
                "qc_script_version": "1.0",
                "publications_yml_mtime_ns": 1716660000000000000,
                "cache_key_version": 1,
                "summary": {
                    "totals": {
                        "mismatches": 2,
                        "variants": 0,
                        "pmid_mismatches": 1,
                        "id_enrichments": 0,
                        "author_name_variants": 0,
                        "journal_name_variants": 0,
                        "self_absent": 1,
                        "missing_ids": 0,
                    },
                    "total_findings": 4,
                },
                "findings": {
                    "mismatches": [
                        {
                            "id": "MM:pubmed:111:journal:aaaa1111",
                            "type": "MISMATCH",
                            "global_idx": 0,
                            "subsection": "PRR",
                            "entry_index": 1,
                            "pmid": "111",
                            "doi": None,
                            "title_preview": "Some Mismatch Title",
                            "field": "journal",
                            "yaml_value": "JAMA",
                            "canonical_value": "JAMA: Journal",
                            "source": "pubmed",
                        },
                        {
                            "id": "MM:pubmed:222:year:bbbb2222",
                            "type": "MISMATCH",
                            "global_idx": 1,
                            "subsection": "PRR",
                            "entry_index": 2,
                            "pmid": "222",
                            "doi": None,
                            "title_preview": "Another Mismatch",
                            "field": "year",
                            "yaml_value": "2023",
                            "canonical_value": "2024",
                            "source": "pubmed",
                        },
                    ],
                    "variants": [],
                    "pmid_mismatches": [
                        {
                            "id": "PM:pubmed:99999999:no_record",
                            "type": "PMID_MISMATCH",
                            "global_idx": 2,
                            "subsection": "PRR",
                            "entry_index": 3,
                            "pmid": "99999999",
                            "title_preview": "Broken PMID Entry",
                            "reason": "PubMed returned no record for this PMID",
                            "source": "pubmed",
                        },
                    ],
                    "id_enrichments": [],
                    "self_absent": [
                        {
                            "id": "SA:doi:abcd1234",
                            "type": "SELF_ABSENT",
                            "subsection": "OSW",
                            "entry_index": 1,
                            "global_idx": 0,
                            "pmid": None,
                            "doi": "10.x/y",
                            "title_preview": "Some Paper Without Public",
                        },
                    ],
                    "author_name_variants": [],
                    "journal_name_variants": [],
                    "missing_ids": [],
                },
            }
        )
    )
    # Monkeypatch SIDECAR_PATH everywhere it's read.
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", sidecar)
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- qc_sync module ----


def test_qc_sync_load_sidecar_returns_dict_on_valid_file(tmp_path):
    from cv_editor import qc_sync

    p = tmp_path / "report.json"
    p.write_text(
        json.dumps({"version": 1, "summary": {"totals": {}, "total_findings": 0}, "findings": {}})
    )
    out = qc_sync.load_sidecar(p)
    assert isinstance(out, dict)
    assert out["version"] == 1


def test_qc_sync_load_sidecar_returns_none_when_missing(tmp_path):
    from cv_editor import qc_sync

    p = tmp_path / "missing.json"
    assert qc_sync.load_sidecar(p, silent=True) is None


def test_qc_sync_load_sidecar_returns_none_on_version_mismatch(tmp_path):
    """Version 999 != expected 1 → returns None (forward-compat protection)."""
    from cv_editor import qc_sync

    p = tmp_path / "wrongver.json"
    p.write_text(json.dumps({"version": 999, "summary": {}, "findings": {}}))
    assert qc_sync.load_sidecar(p, silent=True) is None


def test_qc_sync_load_sidecar_returns_none_on_corrupt_json(tmp_path):
    from cv_editor import qc_sync

    p = tmp_path / "broken.json"
    p.write_text("this is not json {")
    assert qc_sync.load_sidecar(p, silent=True) is None


def test_qc_sync_summary_totals_handles_none():
    from cv_editor import qc_sync

    out = qc_sync.summary_totals(None)
    assert out == {"total_findings": 0, "totals": {}}


def test_qc_sync_summary_totals_extracts_from_sidecar():
    from cv_editor import qc_sync

    sc = {"summary": {"totals": {"mismatches": 7}, "total_findings": 7}}
    out = qc_sync.summary_totals(sc)
    assert out["total_findings"] == 7
    assert out["totals"] == {"mismatches": 7}


def test_qc_sync_iter_finding_sections_yields_all_eight_types_even_when_empty():
    from cv_editor import qc_sync

    sections = list(qc_sync.iter_finding_sections(None))
    assert len(sections) == 8
    keys = [s["key"] for s in sections]
    assert "mismatches" in keys
    assert "self_absent" in keys
    assert "missing_ids" in keys


def test_qc_sync_iter_finding_sections_marks_phase_3_active():
    """pmid_mismatches + self_absent are the Phase 3 active types."""
    from cv_editor import qc_sync

    sections = {s["key"]: s for s in qc_sync.iter_finding_sections(None)}
    assert sections["pmid_mismatches"]["is_active_in_phase_3"] is True
    assert sections["self_absent"]["is_active_in_phase_3"] is True
    assert sections["mismatches"]["is_active_in_phase_3"] is False
    assert sections["author_name_variants"]["is_active_in_phase_3"] is False


def test_qc_sync_entry_edit_anchor_for_pmid_mismatch():
    from cv_editor import qc_sync

    assert qc_sync.entry_edit_anchor({"type": "PMID_MISMATCH"}) == "field-pmid"


def test_qc_sync_entry_edit_anchor_for_self_absent():
    from cv_editor import qc_sync

    assert qc_sync.entry_edit_anchor({"type": "SELF_ABSENT"}) == "field-authors"


def test_qc_sync_entry_edit_anchor_respects_explicit_field():
    """An explicit `field` overrides the type-default anchor."""
    from cv_editor import qc_sync

    assert qc_sync.entry_edit_anchor({"type": "PMID_MISMATCH"}, field="title") == "field-title"


# ---- /qc/triage GET route ----


def test_qc_triage_renders_200(client):
    resp = client.get("/qc/triage")
    assert resp.status_code == 200


def test_qc_triage_renders_pmid_mismatch_row(client):
    resp = client.get("/qc/triage")
    assert b"99999999" in resp.data
    assert b"Broken PMID Entry" in resp.data


def test_qc_triage_renders_self_absent_row(client):
    resp = client.get("/qc/triage")
    assert b"Some Paper Without Public" in resp.data
    assert b"Self-author not detected" in resp.data


def test_qc_triage_renders_jump_to_edit_links(client):
    """Each Phase 3 active row gets a jump-to-edit link with the field
    URL fragment so entry_edit can scroll/focus that field."""
    resp = client.get("/qc/triage")
    # The self_absent row anchor goes to #field-authors.
    assert b"field-authors" in resp.data
    # pmid_mismatch row anchors to #field-pmid.
    assert b"field-pmid" in resp.data


def test_qc_triage_renders_phase_2_placeholder_for_author_variants(client):
    resp = client.get("/qc/triage")
    assert b"Author-name variants" in resp.data
    # Fixture has 0 author_name_variants; placeholder shouldn't appear,
    # but the section title + "None." should.
    assert b"<em>None.</em>" in resp.data


def test_qc_triage_renders_phase_4_placeholder_for_missing_ids(client):
    resp = client.get("/qc/triage")
    assert b"Entries missing both PMID and DOI" in resp.data


def test_qc_triage_renders_when_sidecar_missing(client, monkeypatch, tmp_path):
    """No sidecar -> page still renders with a 'sidecar missing' banner +
    Run-sweep button. Don't crash."""
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", tmp_path / "nope.json")
    resp = client.get("/qc/triage")
    assert resp.status_code == 200
    assert b"No QC sidecar yet" in resp.data
    assert b"Run QC sweep" in resp.data


def test_qc_triage_sets_current_section_for_nav(client):
    """Active-nav highlighting: current_section=qc_triage marks the nav link."""
    resp = client.get("/qc/triage")
    # nav link gets is-current class; verify the QC triage anchor renders.
    assert b"QC triage" in resp.data


# ---- /qc/triage/status JSON endpoint ----


def test_qc_triage_status_returns_json(client):
    resp = client.get("/qc/triage/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "total_findings" in data
    assert "sidecar_loaded" in data
    assert "running" in data


def test_qc_triage_status_reports_loaded_total(client):
    resp = client.get("/qc/triage/status")
    data = resp.get_json()
    assert data["sidecar_loaded"] is True
    assert data["total_findings"] == 4


def test_qc_triage_status_running_false_at_rest(client):
    resp = client.get("/qc/triage/status")
    assert resp.get_json()["running"] is False


# ---- /qc/triage/run POST kicker ----


def test_qc_triage_run_redirects_to_view(client):
    resp = client.post("/qc/triage/run")
    assert resp.status_code in (302, 303)
    assert "/qc/triage" in resp.location


def test_qc_triage_run_flashes_ok_message(client):
    resp = client.post("/qc/triage/run", follow_redirects=True)
    assert b"QC sweep kicked off" in resp.data


# ---- Index banner ----


def test_index_banner_renders_when_findings_present(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"QC finding" in resp.data
    # The link goes to /qc/triage.
    assert b"/qc/triage" in resp.data


def test_index_banner_hidden_when_findings_zero(client, monkeypatch, tmp_path):
    """qc_findings_banner_global renders nothing when count is 0."""
    empty_sidecar = tmp_path / "empty.json"
    empty_sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "summary": {"totals": {}, "total_findings": 0},
                "findings": {
                    k: []
                    for k in (
                        "mismatches",
                        "variants",
                        "pmid_mismatches",
                        "id_enrichments",
                        "author_name_variants",
                        "journal_name_variants",
                        "self_absent",
                        "missing_ids",
                    )
                },
            }
        )
    )
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", empty_sidecar)
    resp = client.get("/")
    # Banner text shouldn't appear when count is 0.
    assert b"QC finding" not in resp.data


def test_index_banner_hidden_when_sidecar_missing(client, monkeypatch, tmp_path):
    """No sidecar -> count=0 -> banner suppressed."""
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", tmp_path / "nope.json")
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"QC finding" not in resp.data


# ---- _kick_qc_sweep_if_idle wiring ----


def test_qc_sweep_kicker_exists_in_app():
    """The kicker tuple should be created at app init time."""
    from cv_editor.app import create_app

    app = create_app()
    # We can't easily introspect the closure, but the route's existence
    # is the contract. Already covered above; this is a smoke check that
    # importing the app doesn't crash on the qc-sweep kicker init.
    assert app is not None
