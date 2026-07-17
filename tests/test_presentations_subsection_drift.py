"""Regression test for the presentations subsection drift bug (2026-05-30).

The presentations schema's `subsections` (the dropdown + validation list) had
drifted from the actual subsection names in data/presentations.yml. Two symptoms,
one cause: (a) the edit form couldn't pre-select an entry's real subsection — no
schema option matched, so the <select> fell back to its first option, "Invited
Talks"; (b) saving a dropdown option the data didn't have 400'd with "unknown
subsection". The fix realigns the schema to the data AND makes the schema the
single source of truth (the validator gates on it; insert_entry creates a group
on first use).

All tests here are WRITE-FREE: the GET is read-only, and the save tests stay on
the 400 error path, which returns BEFORE any write. (presentations.yml is not in
the conftest corruption canary's watched set, so a real-write test would clobber
it unnoticed — these deliberately never reach the write.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import schemas, sections, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


def _first_idx_in_subsection(target_sub):
    """(global_idx, entry) of the first presentation whose subsection == target_sub."""
    _, data = yaml_io.load(ROOT / "data" / "presentations.yml")
    for rec in sections.flatten(data, "list_of_subsections"):
        if rec["ctx"].get("subsection") == target_sub:
            return rec["global_idx"], rec["entry"]
    return None, None


def test_edit_form_preselects_entry_subsection(client):
    # Symptom #2: a talk in a NON-first subsection must pre-select ITS subsection,
    # not fall back to the first dropdown option.
    target = "Invited Presentations"
    assert target in schemas.get("presentations")["subsections"]
    idx, _ = _first_idx_in_subsection(target)
    assert idx is not None, "fixture: no 'Invited Presentations' entry in the corpus"
    body = client.get(f"/presentations/{idx}/edit").get_data(as_text=True)
    assert f'value="{target}" selected' in body  # the right option is pre-selected
    assert 'value="Invited Talks"' not in body  # the stale schema name is gone


def test_save_rejects_unknown_subsection_write_free(client):
    # Symptom #1 cause: a subsection not in the schema list is refused (400). The
    # error path returns before any write, so this can't create a junk subsection.
    r = client.post(
        "/presentations/save",
        data={
            "js_mounted": "1",
            "mode": "new",
            "subsection": "Totally Bogus Subsection",
            "date": "01/2026",
            "venue": "Some Venue",
        },
    )
    assert r.status_code == 400
    assert "unknown subsection" in r.get_data(as_text=True)


def test_save_accepts_schema_subsection_no_unknown_error(client):
    # A valid schema subsection must NOT trigger the subsection error. Force a
    # DIFFERENT validation error (omit required `venue`) so the request still 400s
    # on the error path (no write) while proving the subsection check passed.
    valid_sub = "National and Regional Meetings (Poster Presentations)"
    assert valid_sub in schemas.get("presentations")["subsections"]
    r = client.post(
        "/presentations/save",
        data={
            "js_mounted": "1",
            "mode": "new",
            "subsection": valid_sub,
            "date": "01/2026",  # venue omitted -> required-field error -> 400, no write
        },
    )
    assert r.status_code == 400
    assert "unknown subsection" not in r.get_data(as_text=True)
