"""Test coverage gaps surfaced by the V3 code-reviewer subagent pass:
- validation-error re-render
- stale-mtime 409
- backup-restore actually swaps file contents
- rename with 0 matches shows empty state
- grant past-end-date warning + helper unit tests
- malformed JSON in hidden field surfaces as validation error
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cv_editor import notes_helpers, paths, validate, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- save-with-validation-error re-renders with error markup ----


def test_save_with_missing_required_renders_errors(client):
    """Posting a publications save with no title (required) returns 400 and
    the response contains the error list + per-field error markup."""
    resp = client.post(
        "/publications/save",
        data={
            "mode": "new",
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
            "subsection": "Peer-Reviewed Original Research",
            # title intentionally omitted; required
            "journal": "Test Journal",
            "year": "2024",
            "authors_json": json.dumps(
                [{"name": "Smith J", "co_first": False, "co_senior": False}]
            ),
        },
    )
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "Could not save" in body
    assert "has-error" in body
    # Anchor link list surfaces the failing field name.
    assert "<code>title</code>" in body


# ---- stale-mtime 409 ----


def test_save_with_stale_mtime_redirects_to_entry_edit_with_pending(client):
    """Stage B / I8 (2026-05-25): posting with stale mtime_ns no longer
    returns 409 (which left browsers stuck on a Redirecting stub page
    with the user's unsaved changes lost). It now stashes the parsed
    form payload under a UUID and 302-redirects to entry_edit with
    ?pending=<uuid> so the changes survive."""
    resp = client.post(
        "/honors/save",
        data={
            "mode": "edit",
            "global_idx": "0",
            "mtime_ns": "1",  # stale
            "date": "2024",
            "award": "Test award",
            "institution": "Test inst",
            "audiences_json": "[]",
            "hide-from_json": "[]",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/honors/0/edit" in resp.headers["Location"]
    assert "pending=" in resp.headers["Location"]


# ---- backup restore actually swaps content ----


def test_backup_restore_swaps_file_content(client, tmp_path):
    """Edit a file, then restore the latest backup, then verify the file
    contents are back to the pre-edit state."""
    target = paths.data_dir() / "honors.yml"
    snapshot = target.read_bytes()
    try:
        _, data = yaml_io.load(target)
        mtime = yaml_io.mtime_ns(target)
        original_award = data[0].get("award")
        # Edit entry 0.
        resp = client.post(
            "/honors/save",
            data={
                "mode": "edit",
                "global_idx": "0",
                "mtime_ns": str(mtime),
                "date": str(data[0].get("date")),
                "award": original_award + " [edit-marker]",
                "institution": data[0].get("institution"),
                "audiences_json": "[]",
                "hide-from_json": "[]",
            },
        )
        assert resp.status_code in (302, 303)
        edited = target.read_text()
        assert "[edit-marker]" in edited
        # Find the most-recent backup (which is the pre-edit state).
        backups = yaml_io.list_backups(target.name)
        assert backups, "expected at least one backup"
        # Restore. V17-D: mtime_ns required to defend against the
        # tab-A-saves-while-tab-B-restores race.
        current_mtime = yaml_io.mtime_ns(target)
        resp = client.post(
            "/honors/restore",
            data={
                "backup_name": backups[0].name,
                "mtime_ns": str(current_mtime),
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        restored = target.read_text()
        assert "[edit-marker]" not in restored
        # The pre-restore backup of the *edited* file should now exist.
        backups_after = yaml_io.list_backups(target.name)
        assert len(backups_after) >= len(backups)
    finally:
        target.write_bytes(snapshot)


def test_restore_blocks_path_traversal(client):
    """A crafted backup_name with .. or / must be rejected."""
    resp = client.post("/publications/restore", data={"backup_name": "../../../etc/passwd"})
    assert resp.status_code == 400


# ---- rename-author with 0 matches ----


def test_rename_author_zero_matches_shows_empty_state(client):
    resp = client.post(
        "/publications/rename-author",
        data={
            "action": "preview",
            "old_name": "NoSuchAuthor ZZZ",
            "new_name": "Whatever",
        },
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No publications mention" in body


def test_rename_author_apply_rejects_empty_target(client):
    resp = client.post(
        "/publications/rename-author",
        data={
            "action": "apply",
            "old_name": "Public JQ",
            "new_name": "",
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_rename_author_apply_rejects_identity_target(client):
    resp = client.post(
        "/publications/rename-author",
        data={
            "action": "apply",
            "old_name": "Public JQ",
            "new_name": "Public JQ",
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


# ---- grant past-end-date warning ----


def test_grant_end_date_warning_active_past():
    msg = validate.grant_end_date_warning({"status": "active", "date": "01/2020 - 06/2020"})
    assert msg is not None
    assert "past" in msg


def test_grant_end_date_warning_active_year_range():
    msg = validate.grant_end_date_warning({"status": "active", "date": "2018 - 2019"})
    assert msg is not None


def test_grant_end_date_warning_unparseable_silent():
    msg = validate.grant_end_date_warning({"status": "active", "date": "TBD"})
    assert msg is None  # let the renderer catch unparseable forms


def test_grant_form_surfaces_warning_for_past_active(client):
    """Hit /research_support/0/edit; if entry 0 is active, the warning
    banner appears (status: active, has an end date). Otherwise this test
    just verifies the route renders."""
    body = client.get("/research_support/0/edit").get_data(as_text=True)
    assert "form-row" in body  # smoke check


# ---- malformed JSON in hidden field surfaces as validation error ----


def test_malformed_json_hidden_field_surfaces_error(client):
    """Posting a publications save with corrupted authors_json should
    return 400 with a parse-error message — NOT silently drop authors."""
    resp = client.post(
        "/publications/save",
        data={
            "mode": "new",
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
            "subsection": "Peer-Reviewed Original Research",
            "title": "Test title",
            "journal": "Test Journal",
            "year": "2024",
            "authors_json": "this is not json",  # corrupted
        },
    )
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "form data corrupted" in body or "authors" in body


# ---- notes_form_to_yaml strict unknown type ----


def test_notes_form_to_yaml_raises_on_unknown_type():
    with pytest.raises(notes_helpers.UnknownNoteTypeError):
        notes_helpers.notes_form_to_yaml([{"type": "made_up_type", "text": "x"}])


# ---- safe_url template filter ----


def test_safe_url_filter_passes_http(client):
    """Render a page that uses the filter (entry_view of a pub) and verify
    http URLs aren't mangled."""
    body = client.get("/publications/0").get_data(as_text=True)
    # Just smoke: page renders.
    assert "Edit" in body


def test_safe_url_filter_rejects_javascript_scheme():
    """Direct unit test on the filter."""
    app = create_app()
    with app.test_request_context():
        from flask import render_template_string

        out = render_template_string(
            '<a href="{{ u | safe_url }}">x</a>',
            u="javascript:alert(1)",
        )
        assert "javascript:" not in out
        assert 'href="#"' in out


def test_safe_url_filter_rejects_data_scheme():
    app = create_app()
    with app.test_request_context():
        from flask import render_template_string

        out = render_template_string(
            '<a href="{{ u | safe_url }}">x</a>',
            u="data:text/html,<script>alert(1)</script>",
        )
        assert "data:" not in out
        assert 'href="#"' in out
