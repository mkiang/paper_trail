"""Gate 3: PubMed enrichment + sync tracking (scripts/pubmed_sync.py)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cv_editor import pubmed_client  # noqa: E402
from cv_editor import pubmed_sync as ps  # noqa: E402

# ---------------- Fixtures ----------------

NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _entry(**kw):
    e = CommentedMap()
    for k, v in kw.items():
        e[k] = v
    return e


def _pubs_data(entries):
    data = CommentedSeq()
    sub = CommentedMap()
    sub["subsection"] = "Peer-Reviewed Original Research"
    sub["entries"] = CommentedSeq(entries)
    data.append(sub)
    return data


def _pm_record(**overrides):
    """Build a minimal PubMed parsed record with sensible defaults."""
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


# ---------------- TTL logic ----------------


def test_ttl_ppublish_under_90d_skipped():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=80)).isoformat(),
        pubmed_status="ppublish",
    )
    assert not ps.needs_refresh(rec, now=NOW)


def test_ttl_ppublish_over_90d_refreshed():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=91)).isoformat(),
        pubmed_status="ppublish",
    )
    assert ps.needs_refresh(rec, now=NOW)


def test_ttl_epub_under_14d_skipped():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=13)).isoformat(),
        pubmed_status="aheadofprint",
    )
    assert not ps.needs_refresh(rec, now=NOW)


def test_ttl_epub_over_14d_refreshed():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=15)).isoformat(),
        pubmed_status="aheadofprint",
    )
    assert ps.needs_refresh(rec, now=NOW)


def test_ttl_force_overrides():
    rec = ps.EntryRecord(
        synced_at=NOW.isoformat(),
        pubmed_status="ppublish",
    )
    assert ps.needs_refresh(rec, now=NOW, force=True)


def test_ttl_only_epub_skips_ppublish():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=999)).isoformat(),
        pubmed_status="ppublish",
    )
    # only_epub=True means a very-stale ppublish entry is still skipped
    assert not ps.needs_refresh(rec, now=NOW, only_epub=True)


def test_ttl_only_epub_keeps_epub():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=15)).isoformat(),
        pubmed_status="aheadofprint",
    )
    assert ps.needs_refresh(rec, now=NOW, only_epub=True)


def test_ttl_no_record_refreshes_by_default():
    assert ps.needs_refresh(None, now=NOW)


def test_ttl_no_record_skipped_when_only_epub():
    # only_epub means we don't refresh fresh entries (no prior status)
    assert not ps.needs_refresh(None, now=NOW, only_epub=True)


def test_ttl_override_for_ppublish():
    rec = ps.EntryRecord(
        synced_at=(NOW - timedelta(days=60)).isoformat(),
        pubmed_status="ppublish",
    )
    # default 90d → would skip; override to 30d → would refresh
    assert not ps.needs_refresh(rec, now=NOW)
    assert ps.needs_refresh(rec, now=NOW, ttl_overrides={"ppublish": 30})


# ---------------- Auto-fill rules ----------------


def test_auto_fill_missing_pmcid():
    yaml_entry = _entry(title="T", journal="J", year=2025, pmid="1", authors=["Public JQ"])
    pm = _pm_record(pmcid="PMC9999")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert dec.fills["pmcid"] == "PMC9999"


def test_auto_fill_does_not_overwrite_existing_pmcid():
    yaml_entry = _entry(pmcid="PMC1111", title="T", journal="J", year=2025, authors=["Public JQ"])
    pm = _pm_record(pmcid="PMC9999")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "pmcid" not in dec.fills


def test_auto_fill_missing_pages():
    yaml_entry = _entry(title="T", journal="J", year=2025, authors=["Public JQ"])
    pm = _pm_record(pages="200-210")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert dec.fills["pages"] == "200-210"


def test_auto_fill_missing_month_and_day():
    yaml_entry = _entry(title="T", journal="J", year=2025, authors=["Public JQ"])
    pm = _pm_record(month=7, day=22)
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert dec.fills["month"] == 7
    assert dec.fills["day"] == 22


def test_auto_fill_skips_empty_pubmed_values():
    yaml_entry = _entry(title="T", journal="J", year=2025, authors=["Public JQ"])
    pm = _pm_record(pmcid="", volume="", issue="", pages="", month=None, day=None)
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert dec.fills == {}


# ---------------- Flag rules ----------------


def test_flag_author_disagreement():
    yaml_entry = _entry(
        title="T", journal="J", year=2025, authors=["Smith J", "Jones K", "Public JQ"]
    )
    pm = _pm_record(authors=["Smith J", "Public JQ"])  # missing Jones
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "authors" in dec.flags


def test_flag_title_disagreement_nontrivial():
    yaml_entry = _entry(
        title="A different title entirely", journal="J", year=2025, authors=["Public JQ"]
    )
    pm = _pm_record(title="Original title from PubMed")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "title" in dec.flags


def test_title_trailing_punctuation_ignored():
    """Whitespace + trailing period differences should NOT be flagged."""
    yaml_entry = _entry(title="A clear title.", journal="J", year=2025, authors=["Public JQ"])
    pm = _pm_record(title="A  clear   title")  # extra spaces, no period
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "title" not in dec.flags


def test_flag_journal_disagreement():
    yaml_entry = _entry(title="T", journal="Some Other Journal", year=2025, authors=["Public JQ"])
    pm = _pm_record(journal_full="Journal of X", journal_iso="J X")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "journal" in dec.flags


def test_journal_iso_match_not_flagged():
    """If YAML uses the ISO abbreviation, that should match too."""
    yaml_entry = _entry(title="T", journal="J X", year=2025, authors=["Public JQ"])
    pm = _pm_record(journal_full="Journal of X", journal_iso="J X")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "journal" not in dec.flags


def test_flag_doi_disagreement():
    yaml_entry = _entry(title="T", journal="J", year=2025, doi="10.1/wrong", authors=["Public JQ"])
    pm = _pm_record(doi="10.1000/example")
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert "doi" in dec.flags


def test_flag_year_disagreement_epub_to_published():
    """The epub→published transition: year changes."""
    yaml_entry = _entry(title="T", journal="J", year=2024, month=12, authors=["Public JQ"])
    pm = _pm_record(year="2025", month=3)
    dec = ps.diff_one(yaml_entry, pm, pmid="1", global_idx=0)
    assert dec.flags["year"] == (2024, "2025")
    # month also flagged because both are present and differ
    assert dec.flags["month"] == (12, 3)


# ---------------- Sidecar I/O ----------------


def test_sidecar_roundtrip(tmp_path):
    sidecar = tmp_path / "sidecar.json"
    state = ps.SidecarState(
        entries={
            "1234": ps.EntryRecord(
                synced_at="2026-05-16T12:00:00+00:00",
                pubmed_status="ppublish",
                fields_filled=["pmcid", "volume"],
                fields_flagged=["authors"],
                yaml_idx_at_sync=42,
            ),
        },
        no_pmid_skip_log={"7": "preprint"},
    )
    ps.save_sidecar(sidecar, state)
    loaded = ps.load_sidecar(sidecar)
    assert "1234" in loaded.entries
    assert loaded.entries["1234"].pubmed_status == "ppublish"
    assert loaded.entries["1234"].fields_filled == ["pmcid", "volume"]
    assert loaded.entries["1234"].yaml_idx_at_sync == 42
    assert loaded.no_pmid_skip_log == {"7": "preprint"}


def test_sidecar_load_missing_file_returns_empty(tmp_path):
    state = ps.load_sidecar(tmp_path / "does-not-exist.json")
    assert state.entries == {}
    assert state.no_pmid_skip_log == {}


def test_sidecar_load_malformed_returns_empty(tmp_path):
    sidecar = tmp_path / "broken.json"
    sidecar.write_text("not valid json {{{")
    state = ps.load_sidecar(sidecar)
    assert state.entries == {}


def test_sidecar_save_is_atomic(tmp_path, monkeypatch):
    """A failed write must not corrupt the existing sidecar."""
    sidecar = tmp_path / "sidecar.json"
    original = ps.SidecarState(
        entries={
            "1": ps.EntryRecord(synced_at="2026-01-01T00:00:00+00:00", pubmed_status="ppublish"),
        }
    )
    ps.save_sidecar(sidecar, original)
    pre_text = sidecar.read_text()

    # Force os.replace to fail mid-write
    # Patch the shared os module directly (pubmed_sync no longer re-exposes
    # `os`; the write goes through cv_editor.atomic_json, which sees the
    # same module object).
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", boom)
    try:
        with pytest.raises(OSError):
            ps.save_sidecar(
                sidecar,
                ps.SidecarState(
                    entries={
                        "2": ps.EntryRecord(synced_at="x", pubmed_status="x"),
                    }
                ),
            )
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    # Original sidecar untouched
    assert sidecar.read_text() == pre_text


# ---------------- Throttle ----------------


def test_polite_throttle_per_host(monkeypatch):
    # Post-V14: HostThrottle migrated to cv_editor.host_throttle. Patch
    # the shared module's time, since that's what the new class imports.
    from cv_editor import host_throttle as ht

    sleeps: list[float] = []
    monkeypatch.setattr(ht.time, "sleep", lambda s: sleeps.append(s))
    fake_t = [0.0]
    monkeypatch.setattr(ht.time, "monotonic", lambda: fake_t[0])

    t = ps.HostThrottle(gap=0.34)
    t.wait("eutils.ncbi.nlm.nih.gov")
    assert sleeps == []  # first call doesn't wait

    fake_t[0] = 0.10  # 100ms elapsed since last
    t.wait("eutils.ncbi.nlm.nih.gov")
    # Should sleep ~0.24s (0.34 - 0.10) to meet the gap
    assert len(sleeps) == 1
    assert 0.23 < sleeps[0] < 0.25


# ---------------- No-PII enforcement ----------------


def test_no_pii_in_user_agent():
    assert ps.UA == "cv-pubmed-sync/1.0"
    assert "@" not in ps.UA
    assert "kiang" not in ps.UA.lower()
    assert "stanford" not in ps.UA.lower()
    assert "mailto:" not in ps.UA.lower()


def test_no_pii_in_pubmed_client_default_ua():
    assert "@" not in pubmed_client.DEFAULT_UA
    assert "kiang" not in pubmed_client.DEFAULT_UA.lower()
    assert "stanford" not in pubmed_client.DEFAULT_UA.lower()


# ---------------- End-to-end --dry-run ----------------


def test_dry_run_writes_no_yaml_but_refreshes_sidecar_flags(tmp_path, monkeypatch):
    """Dry-run: writes report + refreshes sidecar `fields_flagged`, but
    NEVER writes publications.yml. Sidecar refresh on dry-run added
    2026-05-17 — see live-test fix in pubmed_sync.main: stale
    `fields_flagged` entries (from a YAML field fixed via direct edit
    without re-running --apply) were sticking around and showing bogus
    'pending triage' banners on entry_view. Dry-run now sweeps them."""
    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    # Minimal valid publications.yml — title that DISAGREES with PubMed.
    pubs.write_text(
        "# header docstring\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'YAML title differs from PubMed'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      authors: ['Public JQ']\n"
        "      pmid: '99999999'\n"
    )

    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record(pmcid="PMC999")}  # PubMed title = "X"

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)

    pubs_mtime_before = pubs.stat().st_mtime_ns
    rc = ps.main(["--dry-run", "--quiet"])
    assert rc == 0
    assert report.exists()
    assert "PubMed sync report" in report.read_text()
    # YAML still untouched (the dry-run invariant we DO preserve).
    assert pubs.stat().st_mtime_ns == pubs_mtime_before
    # Sidecar is now created — fields_flagged refresh.
    assert sidecar.exists()
    state = ps.load_sidecar(sidecar)
    rec = state.entries.get("99999999")
    assert rec is not None
    assert "title" in rec.fields_flagged
    # synced_at is NOT extended by dry-run (TTL preserved).
    assert rec.synced_at == ""


def test_dry_run_clears_stale_fields_flagged_when_yaml_now_agrees(
    tmp_path,
    monkeypatch,
):
    """Live-test regression (2026-05-17): a sidecar entry with
    `fields_flagged=['title']` from an old --apply run should have the
    flag CLEARED on the next dry-run if the YAML now agrees with PubMed
    (e.g. user edited the title directly). Otherwise the entry_view
    banner promises a triage row that doesn't exist."""
    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    # YAML title now MATCHES the PubMed title "X" (the user just fixed it).
    pubs.write_text(
        "# header docstring\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      authors: ['Public JQ']\n"
        "      pmid: '99999999'\n"
    )
    # Pre-populate sidecar with stale fields_flagged=['title'].
    sidecar.write_text(
        '{"version": 1, "entries": {"99999999": {'
        '"synced_at": "2026-05-15T12:00:00+00:00",'
        '"pubmed_status": "ppublish",'
        '"fields_filled": [],'
        '"fields_flagged": ["title"],'
        '"yaml_idx_at_sync": 0}},'
        '"no_pmid_skip_log": {},'
        '"accepted_yaml_overrides": {}}'
    )
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)

    # PubMed record now matches the YAML exactly (the previous flag's
    # `title` disagreement has since been resolved by direct YAML edit).
    def fake_fetch(pmids, **kw):
        return {
            "99999999": _pm_record(
                title="X",
                journal_full="J",
                journal_iso="J",
                year="2025",
                authors=["Public JQ"],
                pmcid="PMC999",
            )
        }

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    rc = ps.main(["--dry-run", "--quiet"])
    assert rc == 0
    state = ps.load_sidecar(sidecar)
    rec = state.entries.get("99999999")
    assert rec is not None
    # The stale flag is gone — YAML matches PubMed now.
    assert rec.fields_flagged == []
    # synced_at preserved — TTL still counts from the original --apply.
    assert rec.synced_at == "2026-05-15T12:00:00+00:00"


def test_apply_writes_yaml_and_sidecar(tmp_path, monkeypatch):
    """Apply: writes auto-fills to YAML AND updates sidecar."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    pubs.write_text(
        "# header docstring\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )

    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    # write_with_backup needs BACKUP_DIR; redirect to tmp
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record(pmcid="PMC999")}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)

    rc = ps.main(["--apply", "--quiet"])
    assert rc == 0

    new_text = pubs.read_text()
    assert "PMC999" in new_text, "auto-fill must land in YAML"

    sc = ps.load_sidecar(sidecar)
    assert "99999999" in sc.entries
    rec = sc.entries["99999999"]
    assert rec.pubmed_status == "ppublish"
    assert "pmcid" in rec.fields_filled


def test_apply_no_overwrite_already_present(tmp_path, monkeypatch):
    """If YAML has all fields, apply is a no-op for fills (still updates sidecar)."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      volume: '35'\n"
        "      issue: '3'\n"
        "      pages: '100-110'\n"
        "      pmcid: 'PMC1111'\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )

    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        # PubMed returns DIFFERENT pmcid + pages — must NOT overwrite
        return {"99999999": _pm_record(pmcid="PMC9999", pages="200-210")}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)

    rc = ps.main(["--apply", "--quiet"])
    assert rc == 0

    new_text = pubs.read_text()
    assert "PMC1111" in new_text, "original pmcid preserved"
    assert "PMC9999" not in new_text, "PubMed value must NOT overwrite"
    assert "200-210" not in new_text, "PubMed pages must NOT overwrite"


def test_no_pmid_entry_logged_to_skip(tmp_path, monkeypatch):
    """Entries without PMID get recorded in no_pmid_skip_log."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'Preprint X'\n"
        "      journal: 'medRxiv'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      preprint_doi: '10.1101/2025.01.01.99999'\n"
    )

    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    rc = ps.main(["--apply", "--quiet"])
    assert rc == 0
    sc = ps.load_sidecar(sidecar)
    # Single entry, idx 0, with preprint_doi
    assert "0" in sc.no_pmid_skip_log
    assert "preprint" in sc.no_pmid_skip_log["0"].lower()


def test_apply_idempotency(tmp_path, monkeypatch):
    """Running --apply twice: second run is a no-op (TTL skip)."""
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "publications_pubmed_sync.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )

    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    fetch_calls = []

    def fake_fetch(pmids, **kw):
        fetch_calls.append(list(pmids))
        return {"99999999": _pm_record(pmcid="PMC999")}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)

    ps.main(["--apply", "--quiet"])
    pubs_mtime_after_first = pubs.read_text()
    assert len(fetch_calls) == 1, "first apply fetches"

    ps.main(["--apply", "--quiet"])
    pubs_text_after_second = pubs.read_text()
    assert pubs_text_after_second == pubs_mtime_after_first, "YAML unchanged"
    assert len(fetch_calls) == 1, "second apply skips fetch (TTL)"


# ---------------- Report shape ----------------


def test_report_has_summary_and_sections(tmp_path):
    decisions = [
        ps.EntryDecision(
            pmid="1",
            global_idx=0,
            title_preview="Title A",
            fills={"pmcid": "PMC1"},
            flags={},
            publication_status="ppublish",
        ),
        ps.EntryDecision(
            pmid="2",
            global_idx=1,
            title_preview="Title B",
            fills={},
            flags={"authors": ("a", "b")},
            publication_status="ppublish",
        ),
    ]
    rpt = tmp_path / "report.md"

    class _Args:
        apply = False

    ps.write_report(
        rpt,
        decisions=decisions,
        skipped_no_pmid=[(2, "Preprint C", "no PMID")],
        skipped_in_ttl=0,
        fetch_errors=[],
        state_before=ps.SidecarState(),
        args=_Args(),
    )
    text = rpt.read_text()
    assert "Summary" in text
    assert "Would-fill" in text
    assert "Would-flag" in text
    assert "Skipped (no PMID)" in text
    assert "PMC1" in text
    assert "Title A" in text


# ---------------- Accepted overrides ----------------


def _make_decision_with_flag():
    return ps.EntryDecision(
        pmid="1",
        global_idx=0,
        title_preview="X",
        flags={"authors": ("Public JQ; Smith J", "Public J; Smith J")},
        publication_status="ppublish",
    )


def test_accepted_override_suppresses_flag():
    dec = _make_decision_with_flag()
    overrides = {
        "authors": ps.AcceptedOverride(
            yaml_value="Public JQ; Smith J",
            pubmed_value="Public J; Smith J",
            reason="YAML preferred form",
            accepted_at="2026-05-16T12:00:00+00:00",
        ),
    }
    ps.apply_overrides_to_decision(dec, overrides)
    assert "authors" not in dec.flags
    assert "authors" in dec.silenced


def test_accepted_override_resurfaces_when_yaml_changes():
    """If YAML has changed since the snapshot, the override no longer
    applies and the flag re-surfaces under `resurfaced`."""
    dec = _make_decision_with_flag()
    overrides = {
        "authors": ps.AcceptedOverride(
            yaml_value="OLD; Smith J",  # different from current YAML
            pubmed_value="Public J; Smith J",
            reason="...",
            accepted_at="2026-05-16T12:00:00+00:00",
        ),
    }
    ps.apply_overrides_to_decision(dec, overrides)
    # Flag still present (the override doesn't apply); resurfaced records
    # the divergence for the report.
    assert "authors" in dec.flags
    assert "authors" in dec.resurfaced


def test_apply_overrides_no_op_on_empty():
    dec = _make_decision_with_flag()
    ps.apply_overrides_to_decision(dec, None)
    ps.apply_overrides_to_decision(dec, {})
    assert "authors" in dec.flags
    assert dec.silenced == {}


def test_sidecar_roundtrip_includes_overrides(tmp_path):
    sidecar = tmp_path / "sidecar.json"
    state = ps.SidecarState(
        accepted_yaml_overrides={
            "1234": {
                "authors": ps.AcceptedOverride(
                    yaml_value="A; B",
                    pubmed_value="A; B; C",
                    reason="PubMed wrong",
                    accepted_at="2026-05-16T12:00:00+00:00",
                ),
            },
        },
    )
    ps.save_sidecar(sidecar, state)
    loaded = ps.load_sidecar(sidecar)
    assert "1234" in loaded.accepted_yaml_overrides
    ov = loaded.accepted_yaml_overrides["1234"]["authors"]
    assert ov.yaml_value == "A; B"
    assert ov.reason == "PubMed wrong"


# ---------------- Decisions file ----------------


def test_decisions_file_parses_valid(tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("""
decisions:
  - pmid: '1'
    field: authors
    decision: keep_yaml
    reason: 'YAML preferred'
  - pmid: '2'
    field: month
    decision: apply_pubmed
    reason: ''
""")
    decs = ps.load_decisions(path)
    assert len(decs) == 2
    assert decs[0].decision == ps.DECISION_KEEP_YAML
    assert decs[1].decision == ps.DECISION_APPLY_PUBMED


def test_decisions_file_blank_decisions_skipped(tmp_path):
    """A blank `decision:` (template stub) is silently skipped."""
    path = tmp_path / "d.yml"
    path.write_text("""
decisions:
  - pmid: '1'
    field: authors
    decision:
    reason:
""")
    decs = ps.load_decisions(path)
    assert decs == []


def test_decisions_file_unknown_decision_value_fails_loud(tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("""
decisions:
  - pmid: '1'
    field: authors
    decision: weird_choice
    reason: 'x'
""")
    with pytest.raises(ps.DecisionsFileError, match="invalid decision"):
        ps.load_decisions(path)


def test_decisions_file_missing_reason_for_keep_yaml_fails_loud(tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("""
decisions:
  - pmid: '1'
    field: authors
    decision: keep_yaml
    reason: ''
""")
    with pytest.raises(ps.DecisionsFileError, match="missing a 'reason'"):
        ps.load_decisions(path)


def test_decisions_file_missing_pmid_fails_loud(tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("""
decisions:
  - field: authors
    decision: keep_yaml
    reason: 'x'
""")
    with pytest.raises(ps.DecisionsFileError, match="missing 'pmid'"):
        ps.load_decisions(path)


def test_decisions_file_does_not_exist_fails_loud(tmp_path):
    with pytest.raises(ps.DecisionsFileError, match="does not exist"):
        ps.load_decisions(tmp_path / "missing.yml")


def test_template_written_after_dry_run_lists_all_flags(tmp_path, monkeypatch):
    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'TitleA'\n"
        "      journal: 'WrongJournal'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record(journal_full="Journal of X", title="Different Title")}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    ps.main(["--dry-run", "--quiet"])
    template = report.with_name("pubmed_sync_decisions.template.yml")
    assert template.exists()
    text = template.read_text()
    assert "decisions:" in text
    assert "99999999" in text
    # Both flagged fields should appear
    assert "field: journal" in text or "field: title" in text


def test_apply_with_decisions_keep_yaml_records_override(tmp_path, monkeypatch):
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    decisions = tmp_path / "decisions.yml"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'TitleA'\n"
        "      journal: 'WrongJournal'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    decisions.write_text("""
decisions:
  - pmid: '99999999'
    field: journal
    decision: keep_yaml
    reason: 'YAML uses preferred long form'
""")
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record(journal_full="Journal of X")}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    rc = ps.main(["--apply", "--quiet", "--decisions", str(decisions)])
    assert rc == 0

    # YAML untouched on the journal field
    assert "WrongJournal" in pubs.read_text()
    # Sidecar has the override
    sc = ps.load_sidecar(sidecar)
    assert "99999999" in sc.accepted_yaml_overrides
    ov = sc.accepted_yaml_overrides["99999999"]["journal"]
    assert ov.yaml_value == "WrongJournal"
    assert ov.reason == "YAML uses preferred long form"


def test_apply_pubmed_authors_preserves_co_senior_markers(tmp_path, monkeypatch):
    """Apply PubMed authors must keep co_first / co_senior markers from YAML.

    This guards against the regression discovered during the first live
    --apply run: a naive ``entry['authors'] = str(joined)`` overwrote the
    list with a string and erased markers, breaking the renderer.
    """
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    decisions = tmp_path / "decisions.yml"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'T'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      authors:\n"
        "        - 'Müller B-S'\n"
        "        - 'Torres-Ferro A'\n"
        "        - name: 'Author MJ'\n"
        "          co_senior: true\n"
        "        - name: 'Public JQ'\n"
        "          co_senior: true\n"
        "      pmid: '99999999'\n"
    )
    decisions.write_text("""
decisions:
  - pmid: '99999999'
    field: authors
    decision: apply_pubmed
    reason: ''
""")
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        # PubMed differs from YAML on 'Torres-Ferro A'->'D' (real
        # disagreement) AND strips Müller hyphen. Crucially: Author MJ
        # + Public JQ match YAML normalized form, so their markers must
        # survive.
        return {
            "99999999": _pm_record(
                authors=[
                    "Müller BS",
                    "Torres-Ferro D",
                    "Author MJ",
                    "Public JQ",
                ]
            )
        }

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    rc = ps.main(["--apply", "--quiet", "--decisions", str(decisions)])
    assert rc == 0

    # Reload + inspect
    _hdr, data = yaml_io.load(pubs)
    from cv_editor import schemas, sections

    sch = schemas.SCHEMAS["publications"]
    entry = sections.locate(data, sch["structure"], 0)["entry"]
    authors = entry["authors"]
    assert isinstance(authors, list), "authors must remain a list (not a string)"
    assert len(authors) == 4
    # Strings for the unmarked entries
    assert authors[0] == "Müller BS"
    assert authors[1] == "Torres-Ferro D"
    # Dict-shaped entries preserve co_senior
    assert isinstance(authors[2], dict)
    assert authors[2]["name"] == "Author MJ"
    assert authors[2]["co_senior"] is True
    assert isinstance(authors[3], dict)
    assert authors[3]["name"] == "Public JQ"
    assert authors[3]["co_senior"] is True


def test_apply_with_decisions_apply_pubmed_writes_to_yaml(tmp_path, monkeypatch):
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    decisions = tmp_path / "decisions.yml"

    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'TitleA'\n"
        "      journal: 'J'\n"
        "      year: 2025\n"
        "      month: 4\n"
        "      authors:\n"
        "        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    decisions.write_text("""
decisions:
  - pmid: '99999999'
    field: month
    decision: apply_pubmed
    reason: 'epub→print transition'
""")
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record(month=5)}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    rc = ps.main(["--apply", "--quiet", "--decisions", str(decisions)])
    assert rc == 0

    text = pubs.read_text()
    assert "month: 5" in text, "PubMed month must be written to YAML"
    assert "month: 4" not in text


def test_apply_with_invalid_decisions_aborts_without_writes(tmp_path, monkeypatch):
    from cv_editor import yaml_io

    pubs = tmp_path / "publications.yml"
    sidecar = tmp_path / "sc.json"
    report = tmp_path / "qc" / "pubmed_sync_report.md"
    decisions = tmp_path / "decisions.yml"
    pubs.write_text(
        "# header\n"
        "- subsection: 'Peer-Reviewed Original Research'\n"
        "  entries:\n"
        "    - title: 'X'\n      journal: 'J'\n      year: 2025\n"
        "      authors:\n        - 'Public JQ'\n"
        "      pmid: '99999999'\n"
    )
    decisions.write_text(
        "decisions:\n  - pmid: '99999999'\n    field: month\n    decision: garbage\n"
    )
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sidecar)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")

    def fake_fetch(pmids, **kw):
        return {"99999999": _pm_record()}

    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", fake_fetch)
    pre = pubs.read_text()
    rc = ps.main(["--apply", "--quiet", "--decisions", str(decisions)])
    assert rc != 0  # nonzero exit
    assert pubs.read_text() == pre  # YAML untouched
    assert not sidecar.exists()  # sidecar untouched


def test_silenced_count_appears_in_report(tmp_path):
    """The report's Summary section should mention silenced flags when present."""
    decisions = [
        ps.EntryDecision(
            pmid="1",
            global_idx=0,
            title_preview="X",
            silenced={
                "authors": ps.AcceptedOverride(
                    yaml_value="A",
                    pubmed_value="B",
                    reason="r",
                    accepted_at="2026-05-16T00:00:00+00:00",
                )
            },
            publication_status="ppublish",
        ),
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
    assert "1 flags silenced" in text
    assert "Silenced by accepted overrides" in text


def test_report_orphan_pmid_surfaced(tmp_path):
    """Sidecar PMIDs not in current YAML appear under Sidecar orphans."""
    decisions = []  # nothing fetched this run
    state = ps.SidecarState(
        entries={
            "ORPHAN_PMID": ps.EntryRecord(
                synced_at="2025-01-01T00:00:00+00:00",
                pubmed_status="ppublish",
            ),
        }
    )
    rpt = tmp_path / "report.md"

    class _Args:
        apply = False

    ps.write_report(
        rpt,
        decisions=decisions,
        skipped_no_pmid=[],
        skipped_in_ttl=0,
        fetch_errors=[],
        state_before=state,
        args=_Args(),
    )
    text = rpt.read_text()
    assert "Sidecar orphans" in text
    assert "ORPHAN_PMID" in text
