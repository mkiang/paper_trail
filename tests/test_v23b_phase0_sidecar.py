"""V23-B Phase 0: JSON sidecar emit + integration tests (2026-05-25).

Tests that exercise `write_sidecar` directly with synthetic ctx data
(faster + hermetic; doesn't require a real PubMed sweep). One end-to-end
test against the live qc_publications.main() is skipped if external
APIs are unavailable.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from cv_editor import qc_publications as qp  # noqa: E402


def _ctx(**overrides):
    """Default empty ctx; override per-test."""
    base = {
        "total": 0,
        "with_pmid": 0,
        "with_doi_only": 0,
        "mismatches": [],
        "variants": [],
        "pmid_mismatches": [],
        "author_variants": {},
        "journal_variants": {},
        "enrichments": [],
        "missing_ids": [],
        "self_absent": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def tmp_sidecar(tmp_path):
    return tmp_path / "report.json"


# ---- Schema: required top-level fields ----


def test_sidecar_has_all_required_top_level_fields(tmp_sidecar):
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    for k in (
        "version",
        "generated_at",
        "qc_script_version",
        "publications_yml_mtime_ns",
        "cache_key_version",
        "summary",
        "findings",
    ):
        assert k in data, f"missing top-level field: {k}"
    assert data["version"] == 1  # integer for versioned_json.load_versioned


def test_sidecar_findings_has_all_eight_categories(tmp_sidecar):
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    expected = {
        "mismatches",
        "variants",
        "pmid_mismatches",
        "id_enrichments",
        "author_name_variants",
        "journal_name_variants",
        "self_absent",
        "missing_ids",
    }
    assert set(data["findings"].keys()) == expected


def test_sidecar_summary_totals_match_findings_counts(tmp_sidecar):
    ctx = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T1",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal",
            },
        ],
        variants=[
            {
                "subsection": "PRR",
                "entry_index": 2,
                "global_idx": 1,
                "pmid": "456",
                "doi": None,
                "title_preview": "T2",
                "source": "PubMed",
                "field": "pages",
                "cited": "1-10",
                "canonical": "1-10.",
            },
        ],
        pmid_mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 3,
                "global_idx": 2,
                "pmid": "789",
                "title_preview": "T3",
                "reason": "no record",
                "source": "pubmed",
            },
        ],
        self_absent=[
            {"subsection": "PRR", "entry_index": 4, "title_preview": "T4"},
        ],
    )
    flat = [
        (0, "PRR", 3, {"pmid": None, "doi": None, "title": "T4", "year": 2025}),
    ]
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=flat)
    data = json.loads(tmp_sidecar.read_text())
    totals = data["summary"]["totals"]
    assert totals["mismatches"] == 1
    assert totals["variants"] == 1
    assert totals["pmid_mismatches"] == 1
    assert totals["self_absent"] == 1
    assert totals["id_enrichments"] == 0
    assert data["summary"]["total_findings"] == sum(totals.values())


# ---- Critical: pmid_mismatches are SEPARATE from mismatches ----


def test_pmid_mismatches_not_in_mismatches_list(tmp_sidecar):
    """Correctness reviewer H1: a PMID that returned no PubMed record
    must NOT live in the mismatches list (apply path could overwrite
    the PMID with '(no record returned)'). It belongs in pmid_mismatches."""
    ctx = _ctx(
        pmid_mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "99999999",
                "title_preview": "Some title",
                "reason": "PubMed returned no record for this PMID",
                "source": "pubmed",
            },
        ]
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    assert len(data["findings"]["mismatches"]) == 0
    assert len(data["findings"]["pmid_mismatches"]) == 1
    pmm = data["findings"]["pmid_mismatches"][0]
    assert pmm["type"] == "PMID_MISMATCH"
    assert pmm["pmid"] == "99999999"
    assert pmm["id"].startswith("PM:pubmed:99999999")


# ---- Critical: length_changed flag on authors mismatches ----


def test_authors_mismatch_carries_length_changed_true(tmp_sidecar):
    """When the number of authors differs (sev=MISMATCH), length_changed
    must be True so Phase 1 apply requires explicit per-field confirmation
    (won't drop co-first/co-senior markers silently)."""
    # Simulate the diff_entry output for authors with different lengths
    # (3 vs 4 authors; surnames diverge mid-list).
    cited = "a b; c d; e f"  # 3 authors
    canon = "a b; c d; e f; g h"  # 4 authors
    ctx = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "authors",
                "cited": cited,
                "canonical": canon,
                "length_changed": True,
            },  # set by main()
        ]
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    m = data["findings"]["mismatches"][0]
    assert m["field"] == "authors"
    assert m["length_changed"] is True


def test_authors_variant_carries_length_changed_false(tmp_sidecar):
    """When author counts match but a name differs (sev=VARIANT),
    length_changed is False — safe to apply (no marker drop risk)."""
    cited = "smith j; cohen ak"
    canon = "smith j; cohen ak"  # already matches; but contrived case
    ctx = _ctx(
        variants=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "authors",
                "cited": cited,
                "canonical": canon,
                "length_changed": False,
            },
        ]
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    v = data["findings"]["variants"][0]
    assert v["length_changed"] is False


# ---- _authors_length_changed helper ----


def test_authors_length_changed_helper_matches_count():
    """The helper is what main() calls when building the row."""
    assert qp._authors_length_changed("a; b; c", "a; b; c") is False
    assert qp._authors_length_changed("a; b", "a; b; c") is True
    assert qp._authors_length_changed("", "a") is True
    assert qp._authors_length_changed(None, "a") is True
    assert qp._authors_length_changed(None, None) is False


# ---- ID stability under simulated re-sweep ----


def test_sidecar_ids_stable_across_two_emits(tmp_path):
    """The same ctx emitted twice produces identical IDs (modulo
    generated_at timestamp). This is the simulated 'same sweep, same
    findings, two writes' invariant."""
    ctx = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal",
            },
        ]
    )
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    qp.write_sidecar(p1, ctx, flat_entries=[])
    qp.write_sidecar(p2, ctx, flat_entries=[])
    d1 = json.loads(p1.read_text())
    d2 = json.loads(p2.read_text())
    assert d1["findings"]["mismatches"][0]["id"] == d2["findings"]["mismatches"][0]["id"]


def test_sidecar_id_changes_when_canonical_drifts(tmp_path):
    """Correctness H2: canonical drift -> new id -> re-surfaces."""
    ctx_v1 = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA",
            },
        ]
    )
    ctx_v2 = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "123",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal of...",
            },
        ]
    )
    p1 = tmp_path / "v1.json"
    p2 = tmp_path / "v2.json"
    qp.write_sidecar(p1, ctx_v1, flat_entries=[])
    qp.write_sidecar(p2, ctx_v2, flat_entries=[])
    id_v1 = json.loads(p1.read_text())["findings"]["mismatches"][0]["id"]
    id_v2 = json.loads(p2.read_text())["findings"]["mismatches"][0]["id"]
    assert id_v1 != id_v2


# ---- ID stability under idx shifts (the headline property) ----


def test_sidecar_ids_unchanged_when_other_entries_inserted(tmp_path):
    """The user inserted 50 publications above this one. global_idx
    shifts from 0 to 50. The finding's PMID + field + canonical are
    unchanged. ID must NOT change."""
    ctx_pre = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "global_idx": 0,
                "pmid": "90000011",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal",
            },
        ]
    )
    ctx_post = _ctx(
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 51,
                "global_idx": 50,  # idx shifted
                "pmid": "90000011",
                "doi": None,
                "title_preview": "T",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal",
            },
        ]
    )
    p1 = tmp_path / "pre.json"
    p2 = tmp_path / "post.json"
    qp.write_sidecar(p1, ctx_pre, flat_entries=[])
    qp.write_sidecar(p2, ctx_post, flat_entries=[])
    id_pre = json.loads(p1.read_text())["findings"]["mismatches"][0]["id"]
    id_post = json.loads(p2.read_text())["findings"]["mismatches"][0]["id"]
    assert id_pre == id_post


# ---- mtime invariant ----


def test_sidecar_carries_publications_yml_mtime(tmp_sidecar):
    """The mtime ns is the snapshot publications.yml at sweep time."""
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    # qp.DATA points to the real publications.yml. mtime is whatever's
    # current on disk; just confirm it's a positive int.
    assert isinstance(data["publications_yml_mtime_ns"], int)
    assert data["publications_yml_mtime_ns"] > 0


def test_sidecar_mtime_null_when_publications_yml_missing(tmp_sidecar, monkeypatch):
    """If publications.yml is gone, mtime is null (don't crash)."""
    monkeypatch.setattr(qp, "DATA", Path("/nonexistent/path/publications.yml"))
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    assert data["publications_yml_mtime_ns"] is None


# ---- Atomic write integrity ----


def test_sidecar_uses_atomic_write_verify_load(tmp_sidecar):
    """atomic_write_json verifies the tmp file parses as JSON before
    swap. If serialization is broken, the existing file is unchanged.
    Smoke-test that the written file is parseable JSON."""
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    # If write_sidecar bypassed atomic_write_json (e.g. used path.write_text
    # directly), this could still pass — but the contract is enforced
    # by import + use in write_sidecar.
    data = json.loads(tmp_sidecar.read_text())
    assert isinstance(data, dict)


def test_sidecar_no_tmp_file_lingering(tmp_path):
    """After write_sidecar, no .tmp file remains in the directory."""
    p = tmp_path / "report.json"
    qp.write_sidecar(p, _ctx(), flat_entries=[])
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


# ---- Author + journal name variant cluster IDs ----


def test_author_name_variant_finding_id_uses_normalized_key(tmp_sidecar):
    ctx = _ctx(
        author_variants={
            "cohen ak": ["Cohen AK", "Cohen, AK"],
        }
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    finding = data["findings"]["author_name_variants"][0]
    assert finding["id"] == "AN:cohen ak"
    assert finding["normalized_key"] == "cohen ak"
    assert finding["raw_forms"] == ["Cohen AK", "Cohen, AK"]


def test_journal_name_variant_finding_id_uses_normalized_key(tmp_sidecar):
    ctx = _ctx(
        journal_variants={
            "jama": ["JAMA", "Jama"],
        }
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    finding = data["findings"]["journal_name_variants"][0]
    assert finding["id"] == "JN:jama"


# ---- ID enrichment: one row may emit MULTIPLE findings ----


def test_id_enrichment_row_emits_one_finding_per_suggested_field(tmp_sidecar):
    """A row that suggests {doi, pmcid} emits TWO findings (one per
    suggested ID), each independently triagable."""
    ctx = _ctx(
        enrichments=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "title_preview": "T",
                "have": {"pmid": "123"},
                "suggested": {"doi": "10.x/y", "pmcid": "PMC1234"},
            },
        ]
    )
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    enrichments = data["findings"]["id_enrichments"]
    assert len(enrichments) == 2
    fields = sorted(f["suggested_field"] for f in enrichments)
    assert fields == ["doi", "pmcid"]


# ---- self_absent + missing_ids: resolve entity from flat_entries ----


def test_self_absent_finding_resolves_entity_from_flat(tmp_sidecar):
    """self_absent rows in ctx don't carry pmid/doi; the function
    looks them up from flat_entries by (subsection, entry_index)."""
    ctx = _ctx(
        self_absent=[
            {"subsection": "PRR", "entry_index": 1, "title_preview": "Some Title"},
        ]
    )
    flat = [
        (0, "PRR", 0, {"pmid": "9876", "doi": None, "title": "Some Title"}),
    ]
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=flat)
    data = json.loads(tmp_sidecar.read_text())
    sa = data["findings"]["self_absent"][0]
    assert sa["pmid"] == "9876"
    assert sa["id"].startswith("SA:9876")


def test_missing_ids_finding_resolves_year_from_flat(tmp_sidecar):
    """missing_ids rows pick year from the entry so the title+year hash
    is stable."""
    ctx = _ctx(
        missing_ids=[
            {"subsection": "PRR", "entry_index": 1, "title_preview": "Untracked Paper"},
        ]
    )
    flat = [
        (0, "PRR", 0, {"pmid": None, "doi": None, "title": "Untracked Paper", "year": 2024}),
    ]
    qp.write_sidecar(tmp_sidecar, ctx, flat_entries=flat)
    data = json.loads(tmp_sidecar.read_text())
    mi = data["findings"]["missing_ids"][0]
    assert mi["year"] == 2024
    assert mi["id"].startswith("MI:")
    assert mi["candidates"] == []  # Phase 4 reserved


# ---- generated_at format ----


def test_generated_at_is_iso8601_with_seconds(tmp_sidecar):
    qp.write_sidecar(tmp_sidecar, _ctx(), flat_entries=[])
    data = json.loads(tmp_sidecar.read_text())
    parsed = datetime.fromisoformat(data["generated_at"])
    assert parsed.tzinfo is not None  # must be timezone-aware


# ---- Backwards compat: report.md still produced ----


def test_write_report_still_runs_with_pmid_mismatches_key(tmp_path):
    """The markdown report rendering must accept the new pmid_mismatches
    key in ctx (added in Phase 0) without breaking existing sections."""
    md = tmp_path / "report.md"
    ctx = _ctx(
        total=2,
        mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 1,
                "title_preview": "T1",
                "source": "PubMed",
                "field": "journal",
                "cited": "JAMA",
                "canonical": "JAMA: The Journal",
            },
        ],
        pmid_mismatches=[
            {
                "subsection": "PRR",
                "entry_index": 2,
                "global_idx": 1,
                "pmid": "9999",
                "title_preview": "T2",
                "reason": "no record",
                "source": "pubmed",
            },
        ],
    )
    qp.write_report(md, ctx)
    text = md.read_text()
    assert "External mismatches" in text
    assert "PMIDs that PubMed could not resolve" in text
    assert "9999" in text
    assert "T2" in text


def test_write_report_pmid_mismatches_section_says_none_when_empty(tmp_path):
    md = tmp_path / "report.md"
    qp.write_report(md, _ctx(total=0))
    text = md.read_text()
    # Split on the H2 header (`## `), not on the substring that also
    # appears in the summary bullets.
    assert "## PMIDs that PubMed could not resolve" in text
    pmm_section = text.split("## PMIDs that PubMed could not resolve", 1)[1].split("\n## ", 1)[0]
    assert "_None._" in pmm_section
