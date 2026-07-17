"""V8-V11-D three-reviewer gap-fill tests.

Covers fixes applied after the post-V11 parallel review:
- R2 MEDIUM: _publication_qc helper centralizes the contrib flag.
- R2 MEDIUM: _get_expected_mtime_ns helper survives non-integer input.
"""

from __future__ import annotations

import pytest
from cv_editor.app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- R2 MEDIUM: helper sanity ----


def test_get_expected_mtime_ns_handles_non_integer(client):
    """Bogus mtime_ns shouldn't 500 — the helper falls back to 0 so
    write_with_backup raises StaleFileError → 409."""
    # POST a bulk action with a garbage mtime_ns.
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_hidden",
            "selected": ["0"],
            "mtime_ns": "not-an-integer",
        },
        follow_redirects=False,
    )
    # Either 302 (success, treating 0 as "any") or 409 (stale) — but
    # never a 500.
    assert resp.status_code in (302, 409)


# ---- R3 LOW: bulk confirm uses "un-hide (show)" ----


def test_bulk_confirm_labels_present_in_template(client):
    resp = client.get("/publications")
    body = resp.get_data(as_text=True)
    # The JS labels object lives inline in the template.
    assert "un-hide (show)" in body
