"""M5-5d CP5: scripts/init_cv.py CLI (in-process main(), tmp dirs only).

Every test monkeypatches yaml_io.BACKUP_DIR (scaffold's writes .bak into
it) — see tests/test_m5_scaffold.py's module docstring for why.
"""

from __future__ import annotations

import init_cv
import pytest
from cv_editor import scaffold, yaml_io
from filelock import FileLock


@pytest.fixture
def backups(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", bdir)
    return bdir


def test_fresh_dir_scaffolds_blank_no_snapshot(tmp_path, backups, capsys):
    target = tmp_path / "corpus"
    assert init_cv.main(["--data-dir", str(target)]) == 0
    assert scaffold.corpus_is_empty(target)
    assert (target / "publications.yml").exists()
    assert (target / "citation_counts.json").exists()
    out = capsys.readouterr().out
    assert "blank CV tree" in out
    assert "snapshot" not in out  # nothing existed -> no snapshot
    assert not list(backups.glob("reset-*")) if backups.exists() else True


def test_example_flag_seeds_example_corpus(tmp_path, backups):
    target = tmp_path / "corpus"
    assert init_cv.main(["--data-dir", str(target), "--example"]) == 0
    assert not scaffold.corpus_is_empty(target)  # example data present
    text = (target / "publications.yml").read_text()
    assert "Public JQ" in text


def test_nonempty_without_force_refuses_and_writes_nothing(tmp_path, backups, capsys):
    target = tmp_path / "corpus"
    init_cv.main(["--data-dir", str(target), "--example"])
    before = (target / "publications.yml").read_bytes()
    assert init_cv.main(["--data-dir", str(target)]) == 1
    assert (target / "publications.yml").read_bytes() == before
    assert "refusing" in capsys.readouterr().err


def test_force_snapshots_then_overwrites(tmp_path, backups, capsys):
    target = tmp_path / "corpus"
    init_cv.main(["--data-dir", str(target), "--example"])
    (target / scaffold.PUBMED_SIDECAR).write_text('{"version": 3}')
    assert init_cv.main(["--data-dir", str(target), "--force"]) == 0
    out = capsys.readouterr().out
    assert "snapshot: " in out
    snaps = list(backups.glob("reset-*"))
    assert len(snaps) == 1
    assert (snaps[0] / "data" / scaffold.PUBMED_SIDECAR).exists()
    assert not (target / scaffold.PUBMED_SIDECAR).exists()  # deleted post-write
    assert scaffold.corpus_is_empty(target)  # blank now


def test_midwrite_failure_exits_2_with_snapshot_path(tmp_path, backups, capsys, monkeypatch):
    target = tmp_path / "corpus"
    init_cv.main(["--data-dir", str(target), "--example"])

    def boom(*a, **k):
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr(scaffold, "blank_tree", boom)
    assert init_cv.main(["--data-dir", str(target), "--force"]) == 2
    err = capsys.readouterr().err
    assert "FAILED mid-write" in err
    assert "snapshot preserved" in err


def test_editor_lock_held_refuses_before_writing(tmp_path, backups, capsys):
    target = tmp_path / "corpus"
    init_cv.main(["--data-dir", str(target), "--example"])
    before = (target / "honors.yml").read_bytes()
    lock = FileLock(str(target / "honors.yml") + ".lock", timeout=0)
    with lock:
        rc = init_cv.main(["--data-dir", str(target), "--force"])
    assert rc == 1
    assert "holds the lock" in capsys.readouterr().err
    assert (target / "honors.yml").read_bytes() == before


def test_data_dir_pointing_at_file_refuses(tmp_path, backups, capsys):
    f = tmp_path / "notadir"
    f.write_text("x")
    assert init_cv.main(["--data-dir", str(f)]) == 1
    assert "not a directory" in capsys.readouterr().err
