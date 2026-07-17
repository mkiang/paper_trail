"""M5-5d CP3: yaml_io additions (new_header + write_new) + canary width.

Every write test runs on tmp_path AND redirects yaml_io.BACKUP_DIR — without
the redirect, tmp-tree writes deposit .bak files into the user's REAL
.cv_editor_backups/ and _prune_backups(keep=50) evicts genuine recovery
backups (pre-impl review HIGH). CP4 extends this file with the scaffold
core tests.
"""

from __future__ import annotations

import json as _json
import shutil
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE
from cv_editor import data_check, scaffold, schemas, yaml_io
from cv_editor.yaml_io import (
    CorruptedShapeError,
    StaleFileError,
    load,
    split_header,
    write_new,
    write_with_backup,
)

HEADER_A = "# Original header line one.\n# Line two.\n"
HEADER_B = "# Replacement header from the example corpus.\n"
BODY = "- date: '2024'\n  award: Some award\n  institution: Some org\n"


@pytest.fixture
def backups(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", bdir)
    return bdir


def _mk(tmp_path: Path, name: str = "honors.yml", text: str = HEADER_A + BODY) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------- write_with_backup(new_header=) ----------


def test_default_none_preserves_disk_header(tmp_path, backups):
    """The pre-M5-5d contract: the passed header arg is ignored; the header
    is re-read from disk inside the lock."""
    p = _mk(tmp_path)
    _, data = load(p)
    write_with_backup(p, "# NOT this header\n", data)
    header, _ = split_header(p.read_text())
    assert header == HEADER_A


def test_new_header_replaces_header_and_keeps_body(tmp_path, backups):
    p = _mk(tmp_path)
    _, data = load(p)
    write_with_backup(p, HEADER_A, data, new_header=HEADER_B)
    header, body = split_header(p.read_text())
    assert header == HEADER_B
    assert "Some award" in body


def test_new_header_still_makes_backup(tmp_path, backups):
    p = _mk(tmp_path)
    _, data = load(p)
    bk = write_with_backup(p, HEADER_A, data, new_header=HEADER_B)
    assert bk.exists()
    assert bk.read_text().startswith(HEADER_A)  # backup is the PRE-write file
    assert bk.parent == backups


def test_new_header_mtime_guard_still_fires(tmp_path, backups):
    p = _mk(tmp_path)
    _, data = load(p)
    with pytest.raises(StaleFileError):
        write_with_backup(p, HEADER_A, data, expected_mtime_ns=1, new_header=HEADER_B)
    header, _ = split_header(p.read_text())
    assert header == HEADER_A  # nothing written


def test_new_header_shape_guard_still_fires(tmp_path, backups):
    p = _mk(tmp_path, name="publications.yml", text=HEADER_A + "- subsection: X\n  entries: []\n")
    bad = [{"subsection": "X", "entries": [{"title": "t", "authors": "a; b; c; d"}]}]
    with pytest.raises(CorruptedShapeError):
        write_with_backup(p, HEADER_A, bad, new_header=HEADER_B)


# ---------- write_new ----------


def test_write_new_creates_and_round_trips(tmp_path, backups):
    p = tmp_path / "honors.yml"
    write_new(p, HEADER_A, [{"date": "2024", "award": "A", "institution": "B"}])
    header, data = load(p)
    assert header == HEADER_A
    assert data[0]["award"] == "A"


def test_write_new_empty_list_body(tmp_path, backups):
    """The blank-scaffold shape: header + [] body (never an empty body —
    split_header on a comment-only file returns header='' on the NEXT
    save, silently dropping the schema docs)."""
    p = tmp_path / "honors.yml"
    write_new(p, HEADER_A, [])
    header, body = split_header(p.read_text())
    assert header == HEADER_A
    assert body.strip() == "[]"
    _, data = load(p)
    assert data == []


def test_write_new_refuses_existing(tmp_path, backups):
    p = _mk(tmp_path)
    before = p.read_bytes()
    with pytest.raises(FileExistsError):
        write_new(p, HEADER_B, [])
    assert p.read_bytes() == before


def test_write_new_is_yaml_only(tmp_path, backups):
    with pytest.raises(ValueError, match="YAML-only"):
        write_new(tmp_path / "citation_counts.json", "", {})


def test_write_new_runs_publications_shape_guard(tmp_path, backups):
    bad = [{"subsection": "X", "entries": [{"title": "t", "authors": "a; b"}]}]
    p = tmp_path / "publications.yml"
    with pytest.raises(CorruptedShapeError):
        write_new(p, HEADER_A, bad)
    assert not p.exists()


def test_write_new_makes_no_backup(tmp_path, backups):
    write_new(tmp_path / "honors.yml", HEADER_A, [])
    assert not backups.exists() or not list(backups.iterdir())


# ---------- CP4: scaffold core ----------


def test_corpus_counts_truth_table(tmp_path, backups):
    # missing dir / all files missing -> all zero, empty
    counts = scaffold.corpus_entry_counts(tmp_path)
    assert all(c == 0 for c in counts.values())
    assert scaffold.corpus_is_empty(tmp_path)

    # blank tree -> still empty
    scaffold.blank_tree(tmp_path)
    assert scaffold.corpus_is_empty(tmp_path)

    # one entry -> not empty
    p = tmp_path / "honors.yml"
    hdr, _ = load(p)
    write_with_backup(p, hdr, [{"date": "2024", "award": "A", "institution": "B"}])
    assert scaffold.corpus_entry_counts(tmp_path)["honors"] == 1
    assert not scaffold.corpus_is_empty(tmp_path)


def test_corpus_is_empty_fail_closed_on_parse_failure(tmp_path, backups):
    """A corrupt corpus must NEVER count as empty — that would waive the
    reset confirmation exactly when the guard matters most (pre-impl
    review HIGH #1)."""
    scaffold.blank_tree(tmp_path)
    (tmp_path / "honors.yml").write_text("###\n- [unclosed\n", encoding="utf-8")
    assert scaffold.corpus_entry_counts(tmp_path)["honors"] is None
    assert not scaffold.corpus_is_empty(tmp_path)


def test_corpus_is_empty_fail_closed_on_personalized_meta(tmp_path, backups):
    """A fresh user who typed their name into Meta must get the phrase
    prompt before reset clobbers it (pre-impl review HIGH #2)."""
    scaffold.blank_tree(tmp_path)
    assert not scaffold.meta_is_personalized(tmp_path)
    meta_p = tmp_path / "meta.yml"
    hdr, meta = load(meta_p)
    meta["name"] = "Someone Real"
    write_with_backup(meta_p, hdr, meta)
    assert scaffold.meta_is_personalized(tmp_path)
    assert not scaffold.corpus_is_empty(tmp_path)


def test_meta_unreadable_counts_as_personalized(tmp_path, backups):
    scaffold.blank_tree(tmp_path)
    (tmp_path / "meta.yml").write_text("name: [unclosed\n", encoding="utf-8")
    assert scaffold.meta_is_personalized(tmp_path)
    assert not scaffold.corpus_is_empty(tmp_path)


def test_blank_tree_is_check_data_clean_with_example_headers(tmp_path, backups):
    scaffold.blank_tree(tmp_path)
    issues = data_check.check_data(tmp_path)
    assert issues == [], [(i.severity, i.section, i.message) for i in issues]
    for name in ("publications", "meta", "honors"):
        blank_hdr, body = split_header((tmp_path / f"{name}.yml").read_text(encoding="utf-8"))
        example_hdr, _ = split_header(
            (scaffold.EXAMPLE_DATA / f"{name}.yml").read_text(encoding="utf-8")
        )
        assert blank_hdr == example_hdr, f"{name} header not single-sourced"
        if name != "meta":
            assert body.strip() == "[]", f"{name} blank body must be literal []"
    snap = _json.loads((tmp_path / "citation_counts.json").read_text())
    assert snap == scaffold.EMPTY_CITATION_SNAPSHOT


def test_example_tree_matches_example_corpus(tmp_path, backups):
    import yaml as pyyaml

    scaffold.example_tree(tmp_path)
    for name in ("publications", "meta", "research_support"):
        got = pyyaml.safe_load((tmp_path / f"{name}.yml").read_text())
        want = pyyaml.safe_load((scaffold.EXAMPLE_DATA / f"{name}.yml").read_text())
        assert got == want, f"{name} example round-trip drifted"
    snap = _json.loads((tmp_path / "citation_counts.json").read_text())
    want_snap = _json.loads((scaffold.EXAMPLE_DATA / "citation_counts.json").read_text())
    assert snap == want_snap


def test_snapshot_tree_copies_and_is_prune_immune(tmp_path, backups):
    scaffold.example_tree(tmp_path)
    snap = scaffold.snapshot_tree(
        data_dir=tmp_path, cache_dir=tmp_path / "nocache", backup_dir=backups, mode="example"
    )
    assert (snap / "data" / "publications.yml").exists()
    assert (snap / "data" / "citation_counts.json").exists()
    manifest = _json.loads((snap / "manifest.json").read_text())
    assert manifest["completed"] is False  # snapshot phase only
    assert "data/publications.yml" in manifest["phases"]["snapshot"]
    # prune immunity: root-level .bak pruning must not touch reset-*/
    for i in range(60):
        (backups / f"honors.yml.{1000 + i}.bak").write_text("x")
    yaml_io._prune_backups("honors.yml")
    assert snap.exists() and (snap / "manifest.json").exists()


def _populate_full_tree(tmp_path):
    """Example tree + fake sidecars + fake qc artifacts + citation cache."""
    scaffold.example_tree(tmp_path)
    (tmp_path / scaffold.PUBMED_SIDECAR).write_text('{"version": 3}')
    qc = tmp_path / "qc"
    qc.mkdir()
    for name in scaffold.CORPUS_QC_FILES:
        (qc / name).write_text(f"old-corpus {name}\n")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / scaffold.CITATION_SNAPSHOT).write_text('{"version": 1}')
    return qc, cache


def test_reset_blank_full_lifecycle(tmp_path, backups):
    qc, cache = _populate_full_tree(tmp_path)
    manifest = scaffold.reset(
        "blank", data_dir=tmp_path, qc_dir=qc, cache_dir=cache, backup_dir=backups
    )
    snap = Path(manifest["snapshot_dir"])
    assert manifest["completed"] is True
    # sections replaced with blank
    assert scaffold.corpus_is_empty(tmp_path)
    # pubmed sidecar: snapshotted then deleted
    assert not (tmp_path / scaffold.PUBMED_SIDECAR).exists()
    assert (snap / "data" / scaffold.PUBMED_SIDECAR).exists()
    # citation cache deleted (snapshotted); data snapshot rewritten empty
    assert not (cache / scaffold.CITATION_SNAPSHOT).exists()
    assert (snap / ".cache" / scaffold.CITATION_SNAPSHOT).exists()
    assert (
        _json.loads((tmp_path / "citation_counts.json").read_text())
        == scaffold.EMPTY_CITATION_SNAPSHOT
    )
    # qc artifacts moved (gone from qc/, present in snapshot)
    for name in scaffold.CORPUS_QC_FILES:
        assert not (qc / name).exists()
        assert (snap / "qc" / name).read_text() == f"old-corpus {name}\n"
    # per-section .baks exist so /backups restore still works
    assert list(backups.glob("publications.yml.*.bak"))


def test_reset_example_on_partially_missing_tree(tmp_path, backups):
    """The first-run case: some files exist, some don't — the write loop
    must dispatch exists->write_with_backup / missing->write_new
    (pre-impl review HIGH: write_with_backup raises on missing targets)."""
    scaffold.blank_tree(tmp_path)
    (tmp_path / "honors.yml").unlink()
    (tmp_path / "service.yml").unlink()
    manifest = scaffold.reset(
        "example",
        data_dir=tmp_path,
        qc_dir=tmp_path / "qc",
        cache_dir=tmp_path / "c",
        backup_dir=backups,
    )
    assert manifest["completed"] is True
    sections_phase = manifest["phases"]["sections"]
    assert sections_phase["honors.yml"] == "created"
    assert sections_phase["publications.yml"] == "overwrote"
    assert not scaffold.corpus_is_empty(tmp_path)  # example data present


def test_reset_rejects_unknown_mode(tmp_path, backups):
    with pytest.raises(ValueError, match="unknown reset mode"):
        scaffold.reset("nuke", data_dir=tmp_path, backup_dir=backups)


def test_pubmed_sidecar_gone_reads_as_empty_state(tmp_path):
    """Post-reset the sidecar is absent; the loader must return empty (so
    the index banner counts 0, not phantom flags for the old corpus)."""
    from cv_editor import pubmed_sync

    state = pubmed_sync.load_sidecar(tmp_path / "publications_pubmed_sync.json")
    assert state.entries == {}


@pytest.mark.skipif(
    shutil.which("typst") is None or not HAS_BESPOKE,
    reason="needs typst + bespoke template + fonts/",
)
def test_blank_tree_builds(tmp_path, backups):
    """The blank scaffold must COMPILE (header + empty sections skeleton)."""
    from cv_editor import example_build

    scaffold.blank_tree(tmp_path / "corpus")
    results = example_build.build_corpus(
        tmp_path / "corpus", tmp_path / "out", stage_dir=tmp_path / "stage"
    )
    assert len(results) == 1  # blank meta pins a single `cv` variant
    assert results[0].returncode == 0, results[0].stderr
    assert results[0].pdf_path.stat().st_size > 5_000


# ---------- canary width (CP3c drift guard) ----------


def test_canary_watches_every_section_file_and_sidecars():
    """A future 11th section whose file isn't watched would reopen the
    silent-corruption window (gotcha #77's warning). Derive from schemas so
    adding a section forces the conftest decision."""
    from tests.conftest import WATCHED_DATA_FILES

    schema_files = {Path(schemas.get(name)["file"]).name for name in schemas.all_sections()}
    watched = set(WATCHED_DATA_FILES)
    assert schema_files <= watched, f"unwatched section files: {schema_files - watched}"
    assert {"publications_pubmed_sync.json", "citation_counts.json"} <= watched
