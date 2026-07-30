"""v1.2.3: a reset handles EVERY corpus file, or names the ones it didn't.

Before this, both tree writers looped over `schemas.all_sections()`, so a host
data file the engine has no schema for was snapshotted by phase 1 and then left
on disk carrying the OLD corpus's contents — while the page reported a clean
slate. Measured against a real corpus before the fix: three such files survived
a reset byte-for-byte, in both modes.

The example corpus is now the list of what a corpus contains. Two consequences
are tested here: the tree writers cover a host `.yml`, and phase 3 removes any
`data/*.json` phase 2 did not write (a corpus-derived cache left behind lets one
click repopulate the fresh corpus from the old one — the same argument that had
the pubmed sidecar deleted by name). What neither can cover — a `.yml` with no
example counterpart — is REPORTED rather than guessed at.

Write-shaped tests redirect `yaml_io.BACKUP_DIR` (gotcha #78): without it,
tmp-tree writes deposit `.bak`s in the user's real recovery dir.
"""

from __future__ import annotations

import json as _json
import shutil
from pathlib import Path

import pytest
from cv_editor import scaffold, schemas, yaml_io

# A host curation file: mapping-rooted, which is the case `[]` would break.
HOST_YML = "hostcuration"
HOST_YML_HEADER = "# Host curation file, read only by the host's own exporter.\n"
HOST_YML_BODY = "alpha: {}\nbeta: {}\n"
# A host sidecar: a cache keyed to the old corpus, like an OpenAlex id dump.
HOST_JSON = "host_ids.json"


@pytest.fixture
def backups(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", bdir)
    return bdir


@pytest.fixture
def example_dir(tmp_path):
    """The bundled example corpus plus one file the engine has no schema for."""
    d = tmp_path / "example"
    shutil.copytree(scaffold.EXAMPLE_DATA, d)
    (d / f"{HOST_YML}.yml").write_text(HOST_YML_HEADER + HOST_YML_BODY, encoding="utf-8")
    return d


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def _reset(mode, tmp_path, data_dir, example_dir, backups):
    return scaffold.reset(
        mode,
        data_dir=data_dir,
        qc_dir=tmp_path / "qc",
        cache_dir=tmp_path / "cache",
        backup_dir=backups,
        example_dir=example_dir,
    )


# ---------- the file list widens to the example corpus ----------


def test_corpus_yml_names_covers_the_host_file_and_every_schema_section(example_dir):
    names = scaffold._corpus_yml_names(example_dir)
    assert HOST_YML in names
    assert set(schemas.all_sections()) <= set(names)


def test_corpus_yml_names_raises_when_a_schema_section_has_no_example(example_dir):
    """Negative control. Deriving the list from a directory means a deleted
    example file would silently drop that section from every reset, leaving the
    old corpus's entries under a header the page claims it rewrote."""
    (example_dir / "honors.yml").unlink()
    with pytest.raises(FileNotFoundError, match="honors"):
        scaffold._corpus_yml_names(example_dir)


def test_blank_body_mirrors_the_example_files_own_root_container(example_dir):
    assert scaffold._blank_body(HOST_YML, example_dir) == {}
    assert scaffold._blank_body("honors", example_dir) == []
    (example_dir / "headeronly.yml").write_text("# nothing but a header\n", encoding="utf-8")
    assert scaffold._blank_body("headeronly", example_dir) == []
    assert scaffold._blank_body("meta", example_dir)["name"] == scaffold.BLANK_NAME_PLACEHOLDER


def test_blank_tree_writes_the_host_yml_as_an_empty_mapping(data_dir, example_dir, backups):
    written = scaffold.blank_tree(data_dir, example_dir=example_dir)
    assert written[f"{HOST_YML}.yml"] == "created"
    header, body = yaml_io.load(data_dir / f"{HOST_YML}.yml")
    assert body == {}, "a mapping-rooted host file must blank to {}, not []"
    assert header == HOST_YML_HEADER, "the host file's schema prose must survive"
    # the widening must not disturb the ten schema sections
    assert yaml_io.load(data_dir / "honors.yml")[1] == []


def test_example_tree_writes_the_host_yml_body_from_the_example(data_dir, example_dir, backups):
    scaffold.example_tree(data_dir, example_dir=example_dir)
    header, body = yaml_io.load(data_dir / f"{HOST_YML}.yml")
    assert dict(body) == {"alpha": {}, "beta": {}}
    assert header == HOST_YML_HEADER


def test_reset_overwrites_a_host_yml_that_held_real_data(tmp_path, data_dir, example_dir, backups):
    """The headline regression: this file used to survive byte-for-byte."""
    scaffold.example_tree(data_dir, example_dir=example_dir)
    real = HOST_YML_HEADER + "alpha:\n  Someone Real: Someone R Real\n"
    (data_dir / f"{HOST_YML}.yml").write_text(real, encoding="utf-8")

    manifest = _reset("blank", tmp_path, data_dir, example_dir, backups)

    assert "Someone Real" not in (data_dir / f"{HOST_YML}.yml").read_text()
    assert manifest["phases"]["sections"][f"{HOST_YML}.yml"] == "overwrote"
    # still recoverable: phase 1 snapshotted it before phase 2 rewrote it
    snap = Path(manifest["snapshot_dir"])
    assert "Someone Real" in (snap / "data" / f"{HOST_YML}.yml").read_text()


# ---------- phase 3 sweeps every .json it did not write ----------


def test_reset_deletes_every_json_sidecar_it_did_not_write(
    tmp_path, data_dir, example_dir, backups
):
    scaffold.example_tree(data_dir, example_dir=example_dir)
    (data_dir / HOST_JSON).write_text('{"works": {"10.1/x": ["someone"]}}', encoding="utf-8")
    (data_dir / scaffold.PUBMED_SIDECAR).write_text('{"version": 3}', encoding="utf-8")

    manifest = _reset("blank", tmp_path, data_dir, example_dir, backups)
    snap = Path(manifest["snapshot_dir"])

    for name in (HOST_JSON, scaffold.PUBMED_SIDECAR):
        assert not (data_dir / name).exists(), f"{name} survived the reset"
        assert (snap / "data" / name).exists(), f"{name} was deleted without a snapshot"
        assert manifest["phases"]["sidecars"][name] == "deleted (snapshotted)"


def test_reset_keeps_the_citation_snapshot_phase_2_wrote(tmp_path, data_dir, example_dir, backups):
    """The sweep is 'every .json phase 2 did not write' — off by one file and it
    deletes the empty snapshot phase 2 just created, so a later
    POST /citations/snapshot has nothing to compare against."""
    manifest = _reset("blank", tmp_path, data_dir, example_dir, backups)
    path = data_dir / scaffold.CITATION_SNAPSHOT
    assert path.exists()
    assert _json.loads(path.read_text()) == scaffold.EMPTY_CITATION_SNAPSHOT
    assert scaffold.CITATION_SNAPSHOT not in manifest["phases"]["sidecars"]


# ---------- phase 5 reports what no phase could handle ----------


def test_reset_reports_a_yml_with_no_example_counterpart_and_leaves_it_alone(
    tmp_path, data_dir, example_dir, backups
):
    scaffold.example_tree(data_dir, example_dir=example_dir)
    hand_written = "# hand-written, no example counterpart\n- keep me\n"
    (data_dir / "notes.yml").write_text(hand_written, encoding="utf-8")

    manifest = _reset("blank", tmp_path, data_dir, example_dir, backups)

    assert manifest["phases"]["unmanaged"] == ["notes.yml"]
    assert (data_dir / "notes.yml").read_text() == hand_written, "reported, not deleted"


def test_nothing_is_unmanaged_when_the_example_corpus_declares_everything(
    tmp_path, data_dir, example_dir, backups
):
    scaffold.example_tree(data_dir, example_dir=example_dir)
    (data_dir / HOST_JSON).write_text("{}", encoding="utf-8")
    manifest = _reset("example", tmp_path, data_dir, example_dir, backups)
    assert manifest["phases"]["unmanaged"] == []


# ---------- the page reports both, or the reset is silent again ----------

FAKE_MANIFEST = {
    "version": 1,
    "mode": "blank",
    "snapshot_dir": "/fake/.cv_editor_backups/reset-123",
    "completed": True,
    "phases": {
        "snapshot": ["data/publications.yml"],
        "sections": {
            "publications.yml": "overwrote",
            f"{HOST_YML}.yml": "overwrote",
            "citation_counts.json": "written",
        },
        "sidecars": {HOST_JSON: "deleted (snapshotted)"},
        "qc_moved": [],
        "unmanaged": ["notes.yml"],
    },
}


@pytest.fixture
def client(monkeypatch):
    """Same construction as test_m5_reset_route.py: the write layer is patched
    to raise, so a missed seam explodes instead of rewriting the real CV."""

    def explode(*a, **k):
        raise AssertionError("route test reached a real write path")

    monkeypatch.setattr(yaml_io, "write_with_backup", explode)
    monkeypatch.setattr(yaml_io, "write_new", explode)
    monkeypatch.setattr(scaffold, "corpus_is_empty", lambda *a, **k: False)
    monkeypatch.setattr(scaffold, "reset", lambda mode, **k: dict(FAKE_MANIFEST, mode=mode))
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _post(client):
    resp = client.post("/reset", data={"mode": "blank", "confirm_phrase": "reset to blank"})
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_the_page_lists_a_rewritten_host_file_and_warns_about_an_unmanaged_one(client):
    body = _post(client)

    listed = body.split("Other corpus files rewritten", 1)[1].split("</ul>", 1)[0]
    assert f"{HOST_YML}.yml" in listed
    assert "publications.yml" not in listed, "a schema section belongs in the Backups list"

    warned = body.split("Left untouched", 1)[1].split("</div>", 1)[0]
    assert "notes.yml" in warned
    assert "banner-warn" in body

    # the sidecar sweep is already rendered by the existing Sidecars block
    assert HOST_JSON in body


def test_the_page_omits_both_blocks_when_there_is_nothing_to_report(client, monkeypatch):
    """Vacuity guard: a block that renders unconditionally would read as
    coverage on every reset while proving nothing about either code path."""
    phases = {**FAKE_MANIFEST["phases"], "sections": {"publications.yml": "overwrote"}}
    phases["unmanaged"] = []
    monkeypatch.setattr(
        scaffold, "reset", lambda mode, **k: {**FAKE_MANIFEST, "phases": phases, "mode": mode}
    )
    body = _post(client)
    assert "Other corpus files rewritten" not in body
    assert "Left untouched" not in body


def test_the_bundled_corpus_declares_every_schema_section():
    """The shipped example_data must satisfy _corpus_yml_names on its own —
    otherwise `make init` and POST /reset raise for a fresh user."""
    assert set(schemas.all_sections()) <= set(scaffold._corpus_yml_names(scaffold.EXAMPLE_DATA))
