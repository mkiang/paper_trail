"""Gate 3-D: gap-fill tests from the 3-agent code review (2026-05-16).

Covers HIGH + cheap MEDIUM findings applied during reconciliation:
  - C-H3 (partial): warning when decision references a not-flagged field
  - C-H2: defensive int() guard on apply_pubmed for date fields
  - C-M2: warning emitted on sidecar JSON corruption
  - U-H1: --help epilog mentions exit codes
  - U-H2: DecisionsFileError message includes 'Fix the file' hint
  - U-H3: dry-run report next-steps mentions decisions file workflow
  - U-H4: zero-write apply log line uses the new phrasing
  - U-M1: author list previews use [+N more] instead of char-truncation
  - U-M3: decisions template header includes a filled example
  - U-M4: report summary surfaces 'Decisions needed: N' when flaggable > 0
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cv_editor import pubmed_client  # noqa: E402
from cv_editor import pubmed_sync as ps  # noqa: E402


# Reused helpers
def _pm_record(**overrides):
    base = dict(
        title="A clear title.",
        journal_full="Journal of X",
        journal_iso="J X",
        volume="35",
        issue="3",
        pages="100-110",
        year="2025",
        month=3,
        day=15,
        authors=["Smith J", "Public JQ"],
        doi="10.1000/example",
        pmcid="PMC1234567",
        publication_status="ppublish",
    )
    base.update(overrides)
    return base


# ---------------- C-H3 partial: warn on unmatched decision ----------------


def test_warn_when_keep_yaml_decision_refs_unfetched_pmid(capsys):
    """C-H3: on_skip callback fires + WARNING printed for a decision whose
    PMID wasn't fetched this run (typo, in-TTL skip, deleted entry)."""
    state = ps.SidecarState()
    decisions = []  # nothing fetched
    keep = [ps.Decision(pmid="999", field="authors", decision="keep_yaml", reason="x")]
    skipped = []
    n = ps.record_keep_yaml_overrides(
        state,
        decisions,
        keep,
        iso_now="2026-05-16T00:00:00+00:00",
        on_skip=lambda d, reason: skipped.append((d, reason)),
    )
    assert n == 0
    assert len(skipped) == 1
    assert "not fetched this run" in skipped[0][1]


def test_warn_when_keep_yaml_decision_refs_unflagged_field(capsys):
    """C-H3: a decision for a field that wasn't flagged surfaces a hint."""
    dec = ps.EntryDecision(
        pmid="123",
        global_idx=0,
        title_preview="t",
        flags={"title": ("a", "b")},
    )
    keep = [ps.Decision(pmid="123", field="authors", decision="keep_yaml", reason="x")]
    state = ps.SidecarState()
    skipped = []
    n = ps.record_keep_yaml_overrides(
        state,
        [dec],
        keep,
        iso_now="2026-05-16T00:00:00+00:00",
        on_skip=lambda d, reason: skipped.append((d, reason)),
    )
    assert n == 0
    assert len(skipped) == 1
    assert "not flagged this run" in skipped[0][1]


# ---------------- C-H2: defensive int() guard ----------------


def test_apply_pubmed_skips_non_int_coercible_date_field(tmp_path, monkeypatch, capsys):
    """C-H2: a hand-built EntryDecision with malformed raw_pubmed for
    a date field doesn't crash apply_pubmed — it skips + warns."""
    from cv_editor import schemas

    # Build a tiny in-memory data structure mimicking publications.yml
    sub = CommentedMap()
    sub["subsection"] = "Peer-Reviewed Original Research"
    entry = CommentedMap()
    entry["title"] = "T"
    entry["journal"] = "J"
    entry["year"] = 2025
    entry["authors"] = ["Public JQ"]
    entry["pmid"] = "1"
    sub["entries"] = CommentedSeq([entry])
    data = CommentedSeq([sub])
    sch = schemas.SCHEMAS["publications"]

    dec = ps.EntryDecision(
        pmid="1",
        global_idx=0,
        title_preview="t",
        flags={"month": (4, "abc")},
        raw_yaml={"month": 4},
        raw_pubmed={"month": "abc"},  # not int-coercible
    )
    apply_decs = [ps.Decision(pmid="1", field="month", decision="apply_pubmed", reason="")]
    n = ps.apply_pubmed_decisions(data, sch, [dec], apply_decs)
    assert n == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "not int-coercible" in err
    # Critical: data tree must NOT be mutated (entry stayed untouched —
    # 'month' was never in entry to begin with; the corrupting bug
    # would have inserted entry['month'] = 'abc' before the fix)
    assert "month" not in entry or entry["month"] != "abc"


# ---------------- C-M2: sidecar corruption warning ----------------


def test_load_sidecar_corrupt_json_emits_warning(tmp_path, capsys):
    """C-M2: silent fallback was hiding a real bug; now we warn."""
    sidecar = tmp_path / "corrupt.json"
    sidecar.write_text("not valid json {{{")
    state = ps.load_sidecar(sidecar)
    assert state.entries == {}
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "corrupted" in err.lower() or "corrupt" in err.lower()


def test_load_sidecar_missing_file_no_warning(tmp_path, capsys):
    """Missing file is the normal first-run case — no warning."""
    state = ps.load_sidecar(tmp_path / "fresh.json")
    assert state.entries == {}
    assert capsys.readouterr().err == ""


# ---------------- U-H1: --help mentions exit codes ----------------


def test_help_text_documents_exit_codes(capsys):
    with pytest.raises(SystemExit):
        ps.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "Exit codes" in out
    assert "PubMed fetch" in out
    assert "decisions" in out.lower()


def test_help_text_explains_dry_run_default(capsys):
    with pytest.raises(SystemExit):
        ps.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "DEFAULT" in out or "default" in out
    # Two-phase contract referenced in description
    assert "--apply" in out


# ---------------- U-H2: DecisionsFileError actionable hint ----------------


def test_decisions_file_error_main_includes_fix_hint(tmp_path, monkeypatch, capsys):
    """U-H2: the main() error path tells the user to edit the file + re-run."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    decisions = tmp_path / "bad_decisions.yml"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n      journal: 'J'\n      year: 2025\n"
        "      authors:\n        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    decisions.write_text("not_a_mapping: true")
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {"99999999": _pm_record()},
    )
    rc = ps.main(["--apply", "--quiet", "--decisions", str(decisions)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "FATAL" in err
    assert "Fix the file" in err


# ---------------- U-H3: report Next steps mentions decisions file ----------------


def test_dry_run_report_next_steps_mentions_decisions_workflow(tmp_path):
    decisions = [
        ps.EntryDecision(
            pmid="1",
            global_idx=0,
            title_preview="t",
            flags={"journal": ("A", "B")},
        )
    ]
    rpt = tmp_path / "report.md"

    class _Args:
        apply = False

    ps.write_report(
        rpt,
        decisions=decisions,
        skipped_no_pmid=[],
        skipped_in_ttl=0,
        fetch_errors=[],
        state_before=ps.SidecarState(),
        args=_Args(),
    )
    text = rpt.read_text()
    assert "pubmed_sync_decisions.yml" in text
    assert "pubmed_sync_decisions.template.yml" in text
    assert "keep_yaml" in text
    assert "apply_pubmed" in text


def test_dry_run_report_summary_shows_decisions_needed(tmp_path):
    """U-M4: when there are flaggable items, the summary calls out the count."""
    decisions = [
        ps.EntryDecision(
            pmid=str(i), global_idx=i, title_preview="t", flags={"journal": ("A", "B")}
        )
        for i in range(3)
    ]
    rpt = tmp_path / "report.md"

    class _Args:
        apply = False

    ps.write_report(
        rpt,
        decisions=decisions,
        skipped_no_pmid=[],
        skipped_in_ttl=0,
        fetch_errors=[],
        state_before=ps.SidecarState(),
        args=_Args(),
    )
    text = rpt.read_text()
    assert "Decisions needed: 3" in text


# ---------------- U-M1: author preview ----------------


def test_author_preview_truncates_at_semantic_boundary():
    """U-M1: 10-author lists show first 4 + [+N more] instead of mid-name cut."""
    authors = "; ".join(f"Author{i} X" for i in range(10))
    out = ps._author_list_preview(authors, max_authors=4)
    assert out.startswith("Author0 X; Author1 X; Author2 X; Author3 X")
    assert "[+6 more]" in out
    # Critical: every shown author appears in full (no mid-name truncation)
    for i in range(4):
        assert f"Author{i} X" in out
    # And no name from positions 4-9 leaks (they're collapsed into [+N more])
    for i in range(4, 10):
        assert f"Author{i} X" not in out


def test_author_preview_below_threshold_returns_full_list():
    out = ps._author_list_preview("Smith J; Jones M", max_authors=4)
    assert out == "Smith J; Jones M"
    assert "[+" not in out


def test_author_preview_empty_returns_empty():
    assert ps._author_list_preview("") == ""
    assert ps._author_list_preview(None) == ""  # type: ignore


def test_decisions_template_uses_author_preview_for_authors(tmp_path):
    """End-to-end: an authors flag in the template uses semantic preview."""
    dec = ps.EntryDecision(
        pmid="1",
        global_idx=0,
        title_preview="t",
        flags={
            "authors": (
                "; ".join(f"YamlAuthor{i} X" for i in range(8)),
                "; ".join(f"PubmedAuthor{i} X" for i in range(8)),
            )
        },
    )
    out = tmp_path / "tmpl.yml"
    ps.write_decisions_template(out, [dec])
    text = out.read_text()
    assert "[+4 more]" in text


# ---------------- U-M3: template example ----------------


def test_decisions_template_header_includes_filled_example(tmp_path):
    """U-M3: header shows a complete example with both decision types."""
    out = tmp_path / "tmpl.yml"
    ps.write_decisions_template(out, [])  # empty decisions still gets header
    text = out.read_text()
    assert "Example" in text
    assert "12345678" in text  # example pmid
    assert "decision: keep_yaml" in text
    assert "decision: apply_pubmed" in text


# ---------------- U-H4: zero-write apply log line ----------------


def test_apply_idempotent_log_line_uses_new_phrasing(tmp_path, monkeypatch, capsys):
    """U-H4: 'no new YAML writes needed' replaces 'no fills/overrides'."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n      journal: 'J'\n      year: 2025\n"
        "      pmcid: 'PMC1'\n      volume: '1'\n      issue: '1'\n"
        "      pages: '1'\n      month: 3\n      day: 15\n"
        "      authors:\n        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")
    # Mock returns SAME values as YAML → no fills, no flags
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {
            "99999999": _pm_record(
                title="X",
                journal_full="J",
                year="2025",
                volume="1",
                issue="1",
                pages="1",
                month=3,
                day=15,
                pmcid="PMC1",
                authors=["Public JQ"],
                doi="",
            )
        },
    )
    rc = ps.main(["--apply"])  # not --quiet so we capture stderr
    assert rc == 0
    err = capsys.readouterr().err
    assert "no new YAML writes needed" in err
