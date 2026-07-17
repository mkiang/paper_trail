"""V9: bulk operations on the publications list.

POST /publications/bulk with selected[] + action [+ target_subsection].
Tests rollback the publications.yml content after each case so the
user's data isn't perturbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import paths, schemas, sections, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def _pubs_path():
    # P1 seam: resolve against the active (test-isolated) workspace root so
    # helper reads match the tmp copy the app writes (conftest copies the
    # real corpus to tmp per-test — content is identical, asserts preserved).
    return paths.data_dir() / "publications.yml"


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _mtime_ns():
    return yaml_io.mtime_ns(_pubs_path())


def _first_two_global_idxs():
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    recs = list(sections.flatten(data, sch["structure"]))
    return recs[0]["global_idx"], recs[1]["global_idx"]


def _entry_at(idx: int):
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    rec = sections.locate(data, sch["structure"], idx)
    return rec["entry"]


def test_bulk_set_hidden(client):
    a, b = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "selected": [str(a), str(b)],
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _entry_at(a).get("highlighted") is True
    assert _entry_at(b).get("highlighted") is True


def test_bulk_unset_hidden(client):
    a, _ = _first_two_global_idxs()
    # First set it.
    client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "selected": [str(a)],
            "mtime_ns": str(_mtime_ns()),
        },
    )
    assert _entry_at(a).get("highlighted") is True
    # Then unset.
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "unset_hidden",
            "selected": [str(a)],
            "mtime_ns": str(_mtime_ns()),
        },
    )
    assert resp.status_code == 302
    assert "highlighted" not in _entry_at(a)


def test_bulk_move_subsection(client):
    # Pick the first entry (in the first subsection by definition).
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    rec = next(sections.flatten(data, sch["structure"]))
    src_subsection = rec["ctx"]["subsection"]
    title = str(rec["entry"]["title"])
    # Pick a different subsection from the schema.
    targets = [s for s in sch["subsections"] if s != src_subsection]
    target = targets[0]

    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "move_subsection",
            "selected": [str(rec["global_idx"])],
            "target_subsection": target,
            "mtime_ns": str(_mtime_ns()),
        },
    )
    assert resp.status_code == 302

    # Find the entry by title in the new state; confirm its subsection.
    _, data2 = yaml_io.load(_pubs_path())
    found = None
    for r in sections.flatten(data2, sch["structure"]):
        if str(r["entry"].get("title")) == title:
            found = r
            break
    assert found is not None
    assert found["ctx"]["subsection"] == target


def test_bulk_empty_selection_is_warning_not_error(client):
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # File unchanged.
    a, _ = _first_two_global_idxs()
    assert "highlighted" not in _entry_at(a)


def test_bulk_invalid_action_400s(client):
    a, _ = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "drop_table",
            "selected": [str(a)],
            "mtime_ns": str(_mtime_ns()),
        },
    )
    assert resp.status_code == 400


def test_bulk_missing_action_400s(client):
    a, _ = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "selected": [str(a)],
            "mtime_ns": str(_mtime_ns()),
        },
    )
    assert resp.status_code == 400


def test_bulk_non_integer_selected_ignored(client):
    """Selected values that aren't digits are dropped on the server."""
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "selected": ["abc", "../etc"],
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Nothing changed.
    a, _ = _first_two_global_idxs()
    assert "highlighted" not in _entry_at(a)


def test_bulk_stale_mtime_409(client):
    a, _ = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "selected": [str(a)],
            "mtime_ns": "12345",  # stale
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "highlighted" not in _entry_at(a)


def test_bulk_move_subsection_rejects_unknown_target(client):
    a, _ = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "move_subsection",
            "selected": [str(a)],
            "target_subsection": "Not A Real Subsection",
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_section_list_renders_checkbox_for_publications(client):
    resp = client.get("/publications")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="row-select"' in body
    assert 'id="bulk-toolbar"' in body
    assert 'id="select-all"' in body


def test_section_list_no_checkbox_for_other_sections(client):
    resp = client.get("/presentations")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="row-select"' not in body
    assert 'id="bulk-toolbar"' not in body
