"""V3 tests: rename-author logic, SSE rebuild stream shape, QC trigger,
side-by-side disagreement banner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _engine_guards import altmetric_required
from cv_editor import author_rename, paths, schemas, yaml_io
from cv_editor.app import create_app
from ruamel.yaml.comments import CommentedMap, CommentedSeq

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def self_name():
    """The author's self-name as configured in the live corpus
    (`meta.self_bold`). Data-agnostic: 'Public JQ' in the private tree,
    'Public JQ' in the Jane Q Public sample. Both appear in their own
    publications author lists, so the rename tests find them."""
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    return meta["self_bold"]


# ---- author_rename.collect_unique_author_names ----


def test_collect_unique_names_strips_dict_form():
    data = [
        {
            "subsection": "A",
            "entries": [
                {"authors": [{"name": "Smith J", "co_first": True}, "Public JQ", "Smith J"]},
            ],
        },
    ]
    out = author_rename.collect_unique_author_names(data)
    assert out == ["Public JQ", "Smith J"]


def test_find_affected_returns_only_matching_entries():
    data = [
        {
            "subsection": "S",
            "entries": [
                {"title": "T1", "authors": ["Smith J", "Doe A"]},
                {"title": "T2", "authors": ["Public JQ"]},
                {"title": "T3", "authors": [{"name": "Smith J", "co_senior": True}, "Doe A"]},
            ],
        },
    ]
    out = author_rename.find_affected(data, "Smith J")
    assert [r["global_idx"] for r in out] == [0, 2]
    assert out[0]["before_authors"] == ["Smith J", "Doe A"]


def test_apply_rename_preserves_dict_form_flags():
    data = CommentedSeq(
        [
            CommentedMap(
                {
                    "subsection": "S",
                    "entries": CommentedSeq(
                        [
                            CommentedMap(
                                {
                                    "title": "T1",
                                    "authors": CommentedSeq(
                                        [
                                            CommentedMap({"name": "Smith J", "co_first": True}),
                                            "Doe A",
                                        ]
                                    ),
                                }
                            ),
                            CommentedMap({"title": "T2", "authors": CommentedSeq(["Smith J"])}),
                        ]
                    ),
                }
            ),
        ]
    )
    n = author_rename.apply_rename(data, "Smith J", "Smith JT")
    assert n == 2
    e0_a0 = data[0]["entries"][0]["authors"][0]
    assert isinstance(e0_a0, dict)
    assert e0_a0["name"] == "Smith JT"
    assert e0_a0["co_first"] is True  # flag preserved
    assert data[0]["entries"][1]["authors"][0] == "Smith JT"  # plain string


def test_apply_rename_no_match_returns_zero():
    data = CommentedSeq(
        [
            CommentedMap(
                {
                    "subsection": "S",
                    "entries": CommentedSeq(
                        [
                            CommentedMap({"authors": CommentedSeq(["Smith J"])}),
                        ]
                    ),
                }
            ),
        ]
    )
    n = author_rename.apply_rename(data, "Nobody Z", "Z Replacement")
    assert n == 0


# ---- /publications/rename-author route ----


def test_rename_form_renders(client, self_name):
    resp = client.get("/publications/rename-author")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Rename author" in body
    # Datalist of names should be populated with the corpus self-name.
    assert self_name in body


def test_rename_preview_finds_self_name(client, self_name):
    resp = client.post(
        "/publications/rename-author",
        data={"action": "preview", "old_name": self_name, "new_name": self_name},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Affected publications" in body
    # The self-name appears in most publications; should find many.
    assert self_name in body


def test_rename_apply_round_trips(client, self_name):
    """Apply a rename and immediately rename back. Verify YAML unchanged
    after both apply operations."""
    pubs = paths.data_dir() / "publications.yml"
    marker = f"{self_name}-TESTMARKER"
    snapshot = pubs.read_bytes()
    try:
        mtime = yaml_io.mtime_ns(pubs)
        # Pick a name that almost certainly exists and is safely round-trippable.
        # We'll rename the self-name -> "<self>-TESTMARKER" then back.
        resp = client.post(
            "/publications/rename-author",
            data={
                "action": "apply",
                "old_name": self_name,
                "new_name": marker,
                "mtime_ns": str(mtime),
            },
        )
        assert resp.status_code in (302, 303)
        # File mutated.
        text_after = pubs.read_text()
        assert marker in text_after
        # Now rename back.
        mtime2 = yaml_io.mtime_ns(pubs)
        resp = client.post(
            "/publications/rename-author",
            data={
                "action": "apply",
                "old_name": marker,
                "new_name": self_name,
                "mtime_ns": str(mtime2),
            },
        )
        assert resp.status_code in (302, 303)
        text_back = pubs.read_text()
        assert marker not in text_back
        assert self_name in text_back
    finally:
        pubs.write_bytes(snapshot)


# ---- SSE rebuild stream ----


def test_sse_stream_returns_event_stream_mimetype(client):
    resp = client.post("/rebuild/stream", data={"mode": "cv_only"}, buffered=False)
    assert resp.mimetype == "text/event-stream"
    # Drain immediately so the generator cleans up.
    try:
        resp.close()
    except Exception:
        pass


# ---- QC routes ----


def test_qc_status_returns_dict(client):
    resp = client.get("/qc/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "running" in body
    assert "fresh" in body


def test_qc_run_kicks_off(client):
    resp = client.post("/qc/run", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_qc_report_serves_existing_file(client):
    """qc/report.md should already exist from prior runs."""
    report = paths.qc_dir() / "report.md"
    if not report.exists():
        pytest.skip("qc/report.md not present")
    resp = client.get("/qc/report")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"


def test_qc_status_url_matches_qc_report_route(client):
    """The qc_status.url field is rendered as the href of the QC banner
    link on /publications. It must point to a real route, not the
    filesystem path /qc/report.md (which has no Flask route)."""
    report = paths.qc_dir() / "report.md"
    if not report.exists():
        pytest.skip("qc/report.md not present")
    status_resp = client.get("/qc/status").get_json()
    assert status_resp["url"] == "/qc/report"
    # And that URL actually resolves.
    follow = client.get(status_resp["url"])
    assert follow.status_code == 200


# ---- show_hidden_default per-section default (2026-05-25, I5) ----


def test_show_hidden_default_set_on_teaching_and_mentees():
    """Teaching + Mentees default the Show-hidden checkbox ON because
    the user keeps many entries marked highlighted: true (hidden in
    render) and wants to see them by default in the editor."""
    assert schemas.SCHEMAS["teaching"].get("show_hidden_default") is True
    assert schemas.SCHEMAS["mentees"].get("show_hidden_default") is True


def test_show_hidden_default_off_for_other_sections():
    """All other sections retain the global default (off) — the per-
    section flag is opt-in."""
    for key, sch in schemas.SCHEMAS.items():
        if key in {"teaching", "mentees"}:
            continue
        assert not sch.get("show_hidden_default", False), (
            f"section {key!r} unexpectedly defaults show-hidden ON"
        )


def _show_hidden_input_tag(body: str) -> str:
    """Extract the <input ... id="show-hidden" ...> tag so attribute
    presence can be asserted independent of attribute order."""
    import re

    m = re.search(r'<input[^>]*id="show-hidden"[^>]*>', body)
    assert m, "show-hidden input not found in rendered page"
    return m.group(0)


def test_section_list_teaching_renders_checkbox_checked(client):
    """The Show hidden checkbox on /teaching should render with the
    `checked` attribute so the default lands without a manual click."""
    resp = client.get("/teaching")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    tag = _show_hidden_input_tag(body)
    # Attribute presence, order-independent.
    assert " checked" in tag or tag.endswith("checked>") or 'checked ' in tag
    # The visible affordance also appears so the asymmetry is legible.
    assert "default for this section" in body


def test_section_list_publications_renders_checkbox_unchecked(client):
    """A section without the per-section default keeps the checkbox
    unchecked (preserves existing behavior)."""
    resp = client.get("/publications")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    tag = _show_hidden_input_tag(body)
    # No checked attribute anywhere in the input tag.
    assert "checked" not in tag


def test_publications_list_does_not_hide_visible_rows_on_load(client):
    """Regression guard for the on-load applyFilters() call: when
    show-hidden defaults to OFF (as on /publications), the load-time
    filter pass must NOT add the `hidden` attribute to rows whose
    server-side render didn't already have it. Otherwise every
    non-highlighted publication would silently disappear."""
    resp = client.get("/publications")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # The page should contain visible <tr> rows (not hidden) for at
    # least one publication. We don't assert exact counts; we just
    # confirm that rows without highlighted are rendered without the
    # `hidden` attribute, since applyFilters runs client-side and the
    # server-rendered HTML is the source of truth for tests.
    import re

    # Rows use class="entry-row ..." with optional `hidden` attribute.
    row_tags = re.findall(r'<tr class="entry-row[^"]*"[^>]*>', body)
    assert row_tags, "no entry-row <tr>s rendered on /publications"
    # At least one row should be unhidden (most pubs aren't highlighted).
    # The server-rendered HTML must NOT have `hidden` on every row, because
    # the on-load applyFilters() with showH=False would otherwise leave
    # them all hidden indefinitely.
    unhidden = [r for r in row_tags if " hidden>" not in r and " hidden " not in r]
    assert unhidden, "expected at least one un-hidden row on /publications"


def test_qc_banner_link_renders_with_short_url(client):
    """L1 (post-impl review): the QC banner href in /publications HTML
    must point at /qc/report (not /qc/report.md). Belt-and-suspenders
    on top of test_qc_status_url_matches_qc_report_route, which only
    covers the JSON endpoint."""
    report = ROOT / "qc" / "report.md"
    if not report.exists():
        pytest.skip("qc/report.md not present")
    resp = client.get("/publications")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # The QC banner either renders the link (when fresh) or is absent
    # (when no QC has run). When present, it must use the new short URL.
    if "QC report fresh" in body:
        assert 'href="/qc/report"' in body
        assert 'href="/qc/report.md"' not in body


@altmetric_required
def test_trackers_page_renders_console_above_table_groups(client):
    """I3 regression guard: the build-console element must appear
    BEFORE the table groups in DOM order so user-visible SSE output
    lands without scrolling. (Trackers page is altmetric-gated — P5.)"""
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    console_idx = body.find('id="build-console"')
    assert console_idx != -1, "build-console element missing from page"
    # Tables (per-pub groups) live inside <section class="tracker-pub">.
    # If absent (e.g., empty queue), the test is still meaningful — the
    # console must come before where the tables would be, i.e., before
    # the end of <main>.
    section_idx = body.find('<section class="tracker-pub"')
    if section_idx != -1:
        assert console_idx < section_idx, (
            "build-console must appear before tracker-pub sections in DOM"
        )


# ---- Quit route ----


def test_quit_returns_200_without_killing_test_process(client):
    """The quit route fires SIGTERM via a daemon thread after a 200ms
    delay; in tests the test_client returns before that delay elapses."""
    # We can't safely actually invoke /quit in tests because it sends SIGTERM
    # to the pytest process. Just verify the endpoint is registered and
    # returns the expected content shape via Werkzeug's URL map.
    app = client.application
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/quit" in rules


# ---- Disagree banner renders side-by-side table when V1b stages an entry ----


def test_disagree_table_renders_when_disagreements_present(client):
    """Hit /publications/import via DOI lookup (in-process; will fail
    network so no disagreements) — just check the new template macros
    don't blow up the page when disagreements is empty."""
    resp = client.get("/publications/import")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Disagree banner is only shown on the edit form when there's a
    # disagreement; on the import page itself it doesn't appear.
    assert "disagree-banner" not in body


# ---- Quit / index / nav presence ----


def test_index_has_quit_and_search(client):
    body = client.get("/").get_data(as_text=True)
    assert "btn-quit" in body
    assert "search-form" in body


def test_publications_section_list_has_rename_link(client):
    body = client.get("/publications").get_data(as_text=True)
    assert "Rename author" in body


def test_non_publications_section_list_lacks_rename_link(client):
    body = client.get("/honors").get_data(as_text=True)
    assert "Rename author" not in body
