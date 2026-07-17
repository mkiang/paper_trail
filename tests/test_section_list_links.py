"""Regression test for the section-list link gap (2026-05-26).

The user reported that `/mentees` rows had nothing clickable, leaving
the section effectively read-only despite the schema + routes being
fully wired. Root cause: `templates/section_list.html` linked
`role`/`award`/`name` cells only when `loop.index == 1` (the first
column). Sections whose `list_columns[0]` is `date` (mentees, service,
honors, appointments) had no clickable cell anywhere. Fixed by
dropping the `loop.index == 1` guard.

Tests in this file assert every editable section's list page emits at
least one `<a href="/<section>/<int>">` link per visible row. Catches
any future regression in the per-column linking logic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cv_editor import schemas
from cv_editor.app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# Sections that the 2026-05-26 fix specifically unblocks. Mentees was
# the user-reported case; the other three had the same bug pattern.
PREVIOUSLY_BROKEN = ("mentees", "service", "honors", "appointments")

# Sections that should ALWAYS have linked rows (positive controls).
ALWAYS_WORKED = ("publications", "presentations", "teaching", "education")


def _entry_link_count(body: bytes, section: str) -> int:
    """Count anchor tags pointing into the section's entry view route.
    Matches `<a href="/<section>/<int>">` (no trailing slash, no `/edit`,
    no query string) which is the canonical view_url shape from
    `_row_for_listing`.
    """
    text = body.decode("utf-8")
    # Avoid matching `/section/new` or `/section/<int>/edit`. The
    # view_url is always `/<section>/<idx>` with no trailing slash.
    pat = re.compile(rf'<a\s+[^>]*href="/{re.escape(section)}/(\d+)"')
    return len(pat.findall(text))


def _entry_count_for(section: str) -> int:
    """How many entries the YAML file actually has — used as a sanity
    bound on the expected link count."""
    from cv_editor import sections as sec_mod
    from cv_editor import yaml_io

    sch = schemas.get(section)
    path = Path(__file__).resolve().parent.parent / sch["file"]
    _, data = yaml_io.load(path)
    return sum(1 for _ in sec_mod.flatten(data, sch["structure"]))


@pytest.mark.parametrize("section", PREVIOUSLY_BROKEN)
def test_previously_broken_section_rows_now_link_to_view(client, section):
    """The 2026-05-26 fix: rows on /mentees /service /honors /appointments
    now carry at least one anchor per row pointing at the entry view."""
    resp = client.get(f"/{section}")
    assert resp.status_code == 200, f"GET /{section} returned {resp.status_code}"
    link_count = _entry_link_count(resp.data, section)
    entry_count = _entry_count_for(section)
    assert entry_count > 0, f"fixture sanity: {section} has no entries"
    # At least one link per entry. Allow for >= because the template
    # may emit multiple links (e.g. publications has title + role).
    assert link_count >= entry_count, (
        f"/{section} has {entry_count} entries but only {link_count} "
        f"view-url links — row navigability regressed (2026-05-26 fix)."
    )


@pytest.mark.parametrize("section", ALWAYS_WORKED)
def test_positive_control_sections_still_link(client, section):
    """Sections that already worked (title/course/project/degree in
    list_columns) must keep at least one link per row after the fix.
    Guards against the wider template change accidentally regressing
    them."""
    resp = client.get(f"/{section}")
    assert resp.status_code == 200
    link_count = _entry_link_count(resp.data, section)
    entry_count = _entry_count_for(section)
    assert entry_count > 0
    assert link_count >= entry_count


def test_mentees_specifically_links_name_and_role(client):
    """The user-reported case: mentees rows should now have BOTH the
    role and name cells linked (template change linked both column
    types unconditionally)."""
    resp = client.get("/mentees")
    assert resp.status_code == 200
    # Mentees list_columns = [date, role, name, institution]. Look for
    # at least one tr containing two distinct anchors to the same idx.
    # Easier: total link count should be ≥ 2 × entry count.
    link_count = _entry_link_count(resp.data, "mentees")
    entry_count = _entry_count_for("mentees")
    assert entry_count > 0
    assert link_count >= 2 * entry_count, (
        f"Expected at least 2 links per mentee (role + name); got "
        f"{link_count} links for {entry_count} entries."
    )


def test_section_list_link_audit_all_sections(client):
    """Whole-system audit: every section registered in schemas.SCHEMAS
    that has list_columns must render at least one navigable link per
    entry. Catches a future section schema (e.g., a new 'awards'
    section with list_columns=['date', 'venue']) that would hit the
    same gap."""
    for section in schemas.all_sections():
        if section == "meta":
            continue  # single-record section, separate route
        sch = schemas.get(section)
        if not sch.get("list_columns"):
            continue
        resp = client.get(f"/{section}")
        assert resp.status_code == 200, f"GET /{section} returned {resp.status_code}"
        link_count = _entry_link_count(resp.data, section)
        entry_count = _entry_count_for(section)
        if entry_count == 0:
            continue  # empty section, nothing to assert
        assert link_count >= entry_count, (
            f"/{section} has {entry_count} entries but only {link_count} "
            f"view-url links per row. If this fires for a NEW section, "
            f"check its list_columns include a navigable identifier "
            f"(title/course/project/role/award/name/degree). The "
            f"section_list.html template only auto-links those types."
        )
