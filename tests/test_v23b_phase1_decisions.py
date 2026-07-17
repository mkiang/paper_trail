"""V23-B Phase 1: qc_decisions sidecar + effective_findings tests (2026-05-25).

Pins the re-surface contract per finding type. The triage page, index
banner, and entry_view banner ALL call effective_findings — if the
predicates change, all three must agree (V13-V19-D R2-H1 invariant).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from cv_editor import qc_decisions, qc_sync  # noqa: E402

# ---- Decision dataclass round-trip ----


def test_decision_round_trip_minimal():
    d = qc_decisions.Decision(
        decision="defer",
        finding_type="MISMATCH",
        decided_at="2026-05-25T12:00:00+00:00",
    )
    out = qc_decisions.Decision.from_json(d.to_json())
    assert out.decision == "defer"
    assert out.finding_type == "MISMATCH"
    assert out.reason is None


def test_decision_round_trip_with_snapshots():
    d = qc_decisions.Decision(
        decision="keep_yaml",
        finding_type="MISMATCH",
        decided_at="2026-05-25T12:00:00+00:00",
        reason="JAMA is what we use",
        yaml_value_at_decision="JAMA",
        canonical_value_at_decision="JAMA: The Journal of...",
    )
    out = qc_decisions.Decision.from_json(d.to_json())
    assert out.reason == "JAMA is what we use"
    assert out.yaml_value_at_decision == "JAMA"
    assert out.canonical_value_at_decision == "JAMA: The Journal of..."


def test_decision_round_trip_id_enrichment_snapshot():
    """C-H5 fix: ID_ENRICHMENT keep_yaml stores suggested_value_at_decision."""
    d = qc_decisions.Decision(
        decision="keep_yaml",
        finding_type="ID_ENRICHMENT",
        decided_at="2026-05-25T12:00:00+00:00",
        suggested_value_at_decision="10.1234/old",
    )
    out = qc_decisions.Decision.from_json(d.to_json())
    assert out.suggested_value_at_decision == "10.1234/old"


# ---- Decisions container ----


def test_decisions_set_and_get():
    d = qc_decisions.Decisions.empty()
    d.set("X:1", decision="apply", finding_type="MISMATCH", canonical_value_at_decision="canon")
    got = d.get("X:1")
    assert got.decision == "apply"
    assert got.canonical_value_at_decision == "canon"


def test_decisions_set_stamps_decided_at_automatically():
    d = qc_decisions.Decisions.empty()
    d.set("X:1", decision="defer", finding_type="MISMATCH")
    got = d.get("X:1")
    # Should be a parseable ISO-8601 timestamp with timezone.
    parsed = datetime.fromisoformat(got.decided_at)
    assert parsed.tzinfo is not None


def test_decisions_get_returns_none_when_absent():
    d = qc_decisions.Decisions.empty()
    assert d.get("NOT-PRESENT") is None


def test_decisions_remove_tombstones_not_deletes():
    d = qc_decisions.Decisions.empty()
    d.set("X:1", decision="apply", finding_type="MISMATCH")
    assert "X:1" in d.decisions
    d.remove("X:1")
    assert "X:1" not in d.decisions
    assert "X:1" in d.tombstones
    assert d.tombstones["X:1"].decision["decision"] == "apply"


def test_decisions_remove_noop_when_absent():
    d = qc_decisions.Decisions.empty()
    d.remove("MISSING")  # no exception
    assert d.tombstones == {}


# ---- Tombstone TTL pruning ----


def test_prune_expired_tombstones_drops_old():
    d = qc_decisions.Decisions.empty()
    # Insert a tombstone with pruned_at 40 days ago (past TTL).
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    d.tombstones["OLD"] = qc_decisions.Tombstone(
        pruned_at=old,
        decision={"decision": "apply", "finding_type": "MISMATCH"},
    )
    # And one 10 days ago (within TTL).
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    d.tombstones["RECENT"] = qc_decisions.Tombstone(
        pruned_at=recent,
        decision={"decision": "defer", "finding_type": "VARIANT"},
    )
    pruned = d.prune_expired_tombstones()
    assert pruned == 1
    assert "OLD" not in d.tombstones
    assert "RECENT" in d.tombstones


def test_prune_expired_tombstones_drops_malformed():
    d = qc_decisions.Decisions.empty()
    d.tombstones["BAD"] = qc_decisions.Tombstone(
        pruned_at="not-a-date",
        decision={},
    )
    pruned = d.prune_expired_tombstones()
    assert pruned == 1


# ---- Load + save round-trip ----


def test_save_and_load_round_trip(tmp_path):
    p = tmp_path / "qc_decisions.json"
    d = qc_decisions.Decisions.empty()
    d.set(
        "X:1",
        decision="keep_yaml",
        finding_type="MISMATCH",
        reason="r",
        yaml_value_at_decision="y",
        canonical_value_at_decision="c",
    )
    d.save_atomic(p)
    loaded = qc_decisions.load(p)
    got = loaded.get("X:1")
    assert got.decision == "keep_yaml"
    assert got.reason == "r"
    assert got.yaml_value_at_decision == "y"


def test_load_missing_returns_empty(tmp_path):
    out = qc_decisions.load(tmp_path / "missing.json", silent=True)
    assert isinstance(out, qc_decisions.Decisions)
    assert out.decisions == {}
    assert out.tombstones == {}


def test_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("not json {{")
    out = qc_decisions.load(p, silent=True)
    assert out.decisions == {}


def test_load_version_mismatch_returns_empty(tmp_path):
    p = tmp_path / "v999.json"
    p.write_text(json.dumps({"version": 999, "decisions": {}}))
    out = qc_decisions.load(p, silent=True)
    assert out.decisions == {}


def test_load_skips_malformed_decision_entries(tmp_path):
    """One bad decision in the sidecar must not poison the rest."""
    p = tmp_path / "mixed.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "decisions": {
                    "GOOD:1": {
                        "decision": "defer",
                        "finding_type": "MISMATCH",
                        "decided_at": "2026-05-25T12:00:00+00:00",
                    },
                    "BAD:1": {"missing_decision_field": True},
                },
                "tombstones": {},
            }
        )
    )
    out = qc_decisions.load(p, silent=True)
    assert "GOOD:1" in out.decisions
    assert "BAD:1" not in out.decisions


# ---- Re-surface predicates: MISMATCH ----


def _mm_finding(canonical="JAMA: The Journal", yaml="JAMA"):
    return {
        "id": "MM:pubmed:111:journal:aaaa1111",
        "type": "MISMATCH",
        "global_idx": 0,
        "field": "journal",
        "yaml_value": yaml,
        "canonical_value": canonical,
    }


def _mm_decision(kind, *, yaml_at="JAMA", canon_at="JAMA: The Journal"):
    return qc_decisions.Decision(
        decision=kind,
        finding_type="MISMATCH",
        decided_at="2026-05-25T12:00:00+00:00",
        yaml_value_at_decision=yaml_at,
        canonical_value_at_decision=canon_at,
    )


def test_mismatch_no_decision_surfaces():
    assert qc_decisions.is_silenced_mismatch(_mm_finding(), None) is False


def test_mismatch_apply_silences_when_canonical_unchanged():
    d = _mm_decision("apply")
    assert qc_decisions.is_silenced_mismatch(_mm_finding(), d) is True


def test_mismatch_apply_resurfaces_on_canonical_drift():
    """Canonical changed since decision -> new finding ID (per Phase 0).
    BUT also re-surface predicate catches it for the same ID case
    (defensive)."""
    d = _mm_decision("apply", canon_at="OLD CANONICAL")
    assert qc_decisions.is_silenced_mismatch(_mm_finding(canonical="NEW CANONICAL"), d) is False


def test_mismatch_keep_yaml_silences_when_yaml_unchanged():
    d = _mm_decision("keep_yaml", yaml_at="JAMA")
    out = qc_decisions.is_silenced_mismatch(_mm_finding(yaml="JAMA"), d, current_yaml_value="JAMA")
    assert out is True


def test_mismatch_keep_yaml_resurfaces_when_yaml_drifts():
    d = _mm_decision("keep_yaml", yaml_at="JAMA")
    out = qc_decisions.is_silenced_mismatch(_mm_finding(), d, current_yaml_value="Different Now")
    assert out is False


def test_mismatch_keep_yaml_falls_back_to_finding_yaml_value():
    """If current_yaml_value is None (no live re-read), use finding['yaml_value']."""
    d = _mm_decision("keep_yaml", yaml_at="JAMA")
    out = qc_decisions.is_silenced_mismatch(_mm_finding(yaml="JAMA"), d)
    assert out is True


def test_mismatch_defer_always_surfaces():
    d = _mm_decision("defer")
    assert qc_decisions.is_silenced_mismatch(_mm_finding(), d) is False


# ---- Re-surface predicates: VARIANT (same as MISMATCH) ----


def test_variant_uses_same_predicate_as_mismatch():
    d = _mm_decision("keep_yaml", yaml_at="x")
    f = _mm_finding(yaml="x")
    assert qc_decisions.is_silenced_variant(f, d, current_yaml_value="x") is True
    assert qc_decisions.is_silenced_variant(f, d, current_yaml_value="y") is False


# ---- Re-surface predicates: ID_ENRICHMENT (the C-H5 fix) ----


def _ie_finding(suggested="10.1234/abc", field="doi"):
    return {
        "id": f"ID:111:{field}",
        "type": "ID_ENRICHMENT",
        "global_idx": 0,
        "have": {"pmid": "111"},
        "suggested_field": field,
        "suggested_value": suggested,
    }


def _ie_decision(kind, *, suggested_at="10.1234/abc"):
    return qc_decisions.Decision(
        decision=kind,
        finding_type="ID_ENRICHMENT",
        decided_at="2026-05-25T12:00:00+00:00",
        suggested_value_at_decision=suggested_at,
    )


def test_id_enrichment_no_decision_surfaces():
    assert qc_decisions.is_silenced_id_enrichment(_ie_finding(), None) is False


def test_id_enrichment_apply_silences_when_suggested_value_unchanged():
    d = _ie_decision("apply", suggested_at="10.1234/abc")
    assert qc_decisions.is_silenced_id_enrichment(_ie_finding(), d) is True


def test_id_enrichment_apply_resurfaces_when_suggested_value_drifts():
    """C-H5 fix: PubMed offers a DIFFERENT suggested DOI for the same
    entity. Old apply decision must NOT silence the new suggestion."""
    d = _ie_decision("apply", suggested_at="10.1234/OLD")
    out = qc_decisions.is_silenced_id_enrichment(
        _ie_finding(suggested="10.1234/NEW"),
        d,
    )
    assert out is False


def test_id_enrichment_keep_yaml_silences_when_suggested_unchanged():
    d = _ie_decision("keep_yaml", suggested_at="10.1234/abc")
    assert qc_decisions.is_silenced_id_enrichment(_ie_finding(), d) is True


def test_id_enrichment_keep_yaml_resurfaces_when_suggested_changes():
    """C-H5 fix: user keep_yaml'd one suggested DOI; PubMed now offers
    a different one — must re-surface so user can re-decide."""
    d = _ie_decision("keep_yaml", suggested_at="10.1234/OLD")
    out = qc_decisions.is_silenced_id_enrichment(
        _ie_finding(suggested="10.1234/NEW"),
        d,
    )
    assert out is False


def test_id_enrichment_defer_always_surfaces():
    d = _ie_decision("defer")
    assert qc_decisions.is_silenced_id_enrichment(_ie_finding(), d) is False


# ---- effective_findings (the public API) ----


def _sidecar(
    *, mismatches=None, variants=None, id_enrichments=None, pmid_mismatches=None, self_absent=None
):
    return {
        "version": 1,
        "summary": {"totals": {}, "total_findings": 0},
        "findings": {
            "mismatches": mismatches or [],
            "variants": variants or [],
            "pmid_mismatches": pmid_mismatches or [],
            "id_enrichments": id_enrichments or [],
            "author_name_variants": [],
            "journal_name_variants": [],
            "self_absent": self_absent or [],
            "missing_ids": [],
        },
    }


def test_effective_findings_empty_sidecar():
    out = qc_sync.effective_findings(None, qc_decisions.Decisions.empty())
    assert out["mismatches"] == []
    assert out["variants"] == []
    assert qc_sync.effective_total(out) == 0


def test_effective_findings_no_decisions_passes_all_through():
    sc = _sidecar(mismatches=[_mm_finding()])
    out = qc_sync.effective_findings(sc, qc_decisions.Decisions.empty())
    assert len(out["mismatches"]) == 1


def test_effective_findings_silences_silenced_mismatch():
    """Decision exists + canonical unchanged -> finding silenced."""
    f = _mm_finding()
    sc = _sidecar(mismatches=[f])
    d = qc_decisions.Decisions.empty()
    d.set(
        f["id"],
        decision="apply",
        finding_type="MISMATCH",
        canonical_value_at_decision=f["canonical_value"],
    )
    out = qc_sync.effective_findings(sc, d)
    assert out["mismatches"] == []


def test_effective_findings_resurfaces_keep_yaml_with_drifted_yaml():
    """Live YAML drifted from yaml_value_at_decision -> re-surface."""
    f = _mm_finding(yaml="JAMA")
    sc = _sidecar(mismatches=[f])
    d = qc_decisions.Decisions.empty()
    d.set(f["id"], decision="keep_yaml", finding_type="MISMATCH", yaml_value_at_decision="JAMA")
    # Simulate live YAML carrying a different value now.
    yaml_by_idx = {0: {"journal": "Different Now"}}
    out = qc_sync.effective_findings(sc, d, current_yaml_by_global_idx=yaml_by_idx)
    assert len(out["mismatches"]) == 1


def test_effective_findings_phase_3_types_pass_through_unfiltered():
    sa = {"id": "SA:111", "type": "SELF_ABSENT", "global_idx": 0, "title_preview": "T"}
    sc = _sidecar(self_absent=[sa])
    out = qc_sync.effective_findings(sc, qc_decisions.Decisions.empty())
    assert out["self_absent"] == [sa]
    assert out["pmid_mismatches"] == []


def test_effective_total_sums_across_types():
    sc = _sidecar(mismatches=[_mm_finding()], variants=[_mm_finding()])
    out = qc_sync.effective_findings(sc, qc_decisions.Decisions.empty())
    # Two distinct findings with the same content -> both count.
    assert qc_sync.effective_total(out) == 2


def test_effective_for_entry_filters_by_global_idx():
    f1 = _mm_finding()  # global_idx=0
    f2 = dict(f1)
    f2["global_idx"] = 1
    f2["id"] = "MM:pubmed:222:year:bbbb2222"
    sc = _sidecar(mismatches=[f1, f2])
    out = qc_sync.effective_for_entry(
        sc,
        qc_decisions.Decisions.empty(),
        global_idx=0,
    )
    assert len(out) == 1
    assert out[0]["id"] == f1["id"]


def test_effective_for_entry_returns_empty_when_no_findings():
    out = qc_sync.effective_for_entry(
        _sidecar(),
        qc_decisions.Decisions.empty(),
        global_idx=42,
    )
    assert out == []


# ---- Banner parity invariant ----


def test_banner_parity_index_matches_triage_total():
    """V13-V19-D R2-H1: index banner count == sum of triage-page findings.
    Both call effective_findings, so they're equal by construction.

    V23-B Phase 1.5 update (2026-05-26): effective_findings now returns a
    `cross_silenced` dict alongside the per-section lists. Both surfaces
    must consume `effective_total` (which excludes cross_silenced) to
    stay in sync."""
    sc = _sidecar(mismatches=[_mm_finding()], variants=[_mm_finding()])
    d = qc_decisions.Decisions.empty()
    # Index banner reads:
    out_index = qc_sync.effective_total(qc_sync.effective_findings(sc, d))
    # Triage page reads:
    out_triage_lists = qc_sync.effective_findings(sc, d)
    triage_total = sum(
        len(v) for k, v in out_triage_lists.items() if k != "cross_silenced" and isinstance(v, list)
    )
    assert out_index == triage_total


def test_banner_parity_index_silences_match_triage():
    """Apply a decision; both index banner and triage page see the silenced
    finding the same way."""
    f = _mm_finding()
    sc = _sidecar(mismatches=[f])
    d = qc_decisions.Decisions.empty()
    d.set(
        f["id"],
        decision="apply",
        finding_type="MISMATCH",
        canonical_value_at_decision=f["canonical_value"],
    )
    out = qc_sync.effective_findings(sc, d)
    assert qc_sync.effective_total(out) == 0


# ---- entry_edit anchor helper for Phase 1 types ----


def test_entry_edit_anchor_for_mismatch_uses_field():
    f = {"type": "MISMATCH", "field": "journal"}
    assert qc_sync.entry_edit_anchor(f) == "field-journal"


def test_entry_edit_anchor_for_variant_uses_field():
    f = {"type": "VARIANT", "field": "pages"}
    assert qc_sync.entry_edit_anchor(f) == "field-pages"


def test_entry_edit_anchor_for_id_enrichment_uses_suggested_field():
    f = {"type": "ID_ENRICHMENT", "suggested_field": "doi"}
    assert qc_sync.entry_edit_anchor(f) == "field-doi"
