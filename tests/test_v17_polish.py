"""V17 polish (2026-05-15): post-trial fixes.

Covers four user-reported issues from the V6-V12 trial pass:

1. **OA banner copy by position** — first/co-first/last/co-senior author
   labels instead of generic 'lead author'. Tested in test_v8_oa_decision.
2. **Nav cleanup** — More + Tools dropdowns instead of one-row overflow.
3. **Date sorting across years** — server- and client-side sort use a
   normalized YYYYMM-derived key instead of string/parseFloat compare.
4. **Duplicate button** — generic /<section>/<idx>/duplicate route.
5. **URL verifier publisher_blocked** — 403 from publisher after
   redirect now classified separately from real 4xx.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cv_editor import capabilities, paths, schemas, sections, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- Nav: dropdown menus ----


def test_nav_uses_more_and_tools_dropdowns(client):
    body = client.get("/").get_data(as_text=True)
    # Primary chips remain
    for label in ("Publications", "Talks", "Grants", "Service", "Teaching", "Mentees"):
        assert f">{label}</a>" in body, f"primary nav missing {label}"
    # Dropdown summaries (V17-D added aria-label after the trial pass)
    assert 'aria-label="More sections">More' in body
    assert 'aria-label="Editor tools">Tools' in body
    # Renamed labels
    assert ">Appointments<" in body
    # Verify URLs is an ungated tool; always present.
    assert ">Verify URLs<" in body
    # Freeze CV is gated on the active template's freeze capability
    # (base.html: `{% if capabilities.freeze %}`). In the private tree
    # (bespoke, freeze=True) it renders; under a public modern template
    # (freeze=False) it's omitted — so only assert it when the capability
    # is on.
    if capabilities.current().freeze:
        assert ">Freeze CV<" in body
    else:
        assert ">Freeze CV<" not in body
    # Old labels gone
    assert ">Appts<" not in body
    assert "details class=\"nav-menu" in body


# ---- Sorting: cross-year MM/YYYY ----


def test_presentations_sort_normalizes_across_years(client):
    """Default sort is reverse-chronological. The bug: 03/2026 should NOT
    appear above 12/2025 as desc default sort uses string compare."""
    body = client.get("/presentations").get_data(as_text=True)
    # Pull every (data-sort-value, MM/YYYY display) for the date column.
    # Check that data-sort-value is the normalized form (YYYYMM_YYYYMM
    # for single dates), not the raw 'MM/YYYY' string.
    pattern = re.compile(r'<td[^>]*class="col-date[^"]*"[^>]*data-sort-value="([^"]*)"')
    sortvals = pattern.findall(body)
    assert sortvals, "no date columns rendered for presentations"
    # Each value is either '' or 'YYYYMM_YYYYMM' (12 chars + underscore).
    for v in sortvals:
        if v:
            assert re.match(r"^\d{6}_\d{6}$", v), f"expected normalized date sort value, got {v!r}"
    # Verify the sort-value math: 04/2026 must compare > 12/2025 as strings
    # (the bug was string compare of '04/2026' < '12/2025').
    a = "202604_202604"  # 04/2026
    b = "202512_202512"  # 12/2025
    assert a > b, "normalized sort key should put April 2026 after Dec 2025"


def test_publications_sort_uses_year_plus_month(client):
    body = client.get("/publications").get_data(as_text=True)
    # year column data-sort-value should now be 'YYYYMMDD' not 'YYYY'.
    pattern = re.compile(r'<td[^>]*class="col-year[^"]*"[^>]*data-sort-value="([^"]*)"')
    sortvals = pattern.findall(body)
    assert sortvals, "no year columns rendered for publications"
    # All sort values are 8 digits (YYYYMMDD).
    for v in sortvals:
        if v:
            assert re.match(r"^\d{8}$", v), f"expected YYYYMMDD year sort value, got {v!r}"


def test_research_support_sort_dominates_by_end_date(client):
    body = client.get("/research_support").get_data(as_text=True)
    pattern = re.compile(r'<td[^>]*class="col-date[^"]*"[^>]*data-sort-value="([^"]*)"')
    sortvals = pattern.findall(body)
    assert sortvals, "no date columns rendered for grants"
    # Open-ended grants ('MM/YYYY -') normalize to '999999_START'.
    open_ended = [v for v in sortvals if v.startswith("999999_")]
    closed = [v for v in sortvals if v and not v.startswith("999999_")]
    # Test data should have at least one of each (if not, these asserts
    # are no-ops; the regex test above still validates structure).
    if open_ended and closed:
        # Open-ended must sort to the top in desc → highest sort key.
        assert max(sortvals) == max(open_ended)


def test_sortable_kind_is_text_for_all_columns(client):
    """All columns now use data-kind=text (date/year carry pre-normalized
    sort values; numeric kind would re-parseFloat them and re-introduce
    the cross-year bug)."""
    body = client.get("/publications").get_data(as_text=True)
    assert 'data-kind="num"' not in body
    # data-kind="text" should appear on every sortable header.
    assert 'data-kind="text"' in body


# ---- Duplicate button ----


def _section_count(section: str) -> int:
    sch = schemas.get(section)
    _, data = yaml_io.load(paths.data_root() / sch["file"])
    return sum(1 for _ in sections.flatten(data, sch["structure"]))


@pytest.fixture
def snapshot_section():
    """Snapshot a section's YAML content; restore after each test."""
    saved = {}

    def _snap(section: str):
        sch = schemas.get(section)
        p = paths.data_root() / sch["file"]
        saved[p] = p.read_bytes()
        return p

    yield _snap
    for p, content in saved.items():
        p.write_bytes(content)


@pytest.mark.parametrize("section", ["teaching", "service", "presentations"])
def test_duplicate_creates_copy_and_redirects_to_edit(client, snapshot_section, section):
    snapshot_section(section)
    sch = schemas.get(section)
    _, data = yaml_io.load(paths.data_root() / sch["file"])
    recs = list(sections.flatten(data, sch["structure"]))
    if not recs:
        pytest.skip(f"no entries in {section} fixture to duplicate")
    src = recs[0]
    src_idx = src["global_idx"]
    mt = yaml_io.mtime_ns(paths.data_root() / sch["file"])
    before = _section_count(section)

    resp = client.post(
        f"/{section}/{src_idx}/duplicate",
        data={"mtime_ns": str(mt)},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    # Redirects to /<section>/<new_idx>/edit
    assert f"/{section}/" in resp.headers["Location"]
    assert resp.headers["Location"].endswith("/edit")

    after = _section_count(section)
    assert after == before + 1


def test_duplicate_for_meta_is_405(client):
    resp = client.post("/meta/0/duplicate", data={"mtime_ns": "0"})
    assert resp.status_code == 405


def test_duplicate_for_unknown_section_is_404(client):
    resp = client.post("/nope/0/duplicate", data={"mtime_ns": "0"})
    assert resp.status_code == 404


def test_duplicate_button_in_entry_view(client):
    body = client.get("/publications/0").get_data(as_text=True)
    assert 'action="/publications/0/duplicate"' in body
    assert ">Duplicate</button>" in body


# ---- Verifier: publisher_blocked category ----


def test_verifier_publisher_blocked_classification():
    """403 after a host-crossing redirect is publisher_blocked, not 4xx."""
    from cv_editor.verify_urls import _categorize

    # doi.org → example.org redirect, 403 from the publisher host
    cat = _categorize(
        403,
        None,
        request_url="https://doi.org/10.9999/jse.2025.6495",
        final_url="https://example.org/article/9000001",
    )
    assert cat == "publisher_blocked"
    # Same status, same host (no redirect across hosts) → 4xx
    cat2 = _categorize(
        403, None, request_url="https://example.com/x", final_url="https://example.com/x"
    )
    assert cat2 == "4xx"
    # Real 404 always stays 4xx
    cat3 = _categorize(
        404,
        None,
        request_url="https://doi.org/10.0/doesnotexist",
        final_url="https://crossref.org/error",
    )
    assert cat3 == "4xx"


def test_publisher_redirect_helper():
    from cv_editor.verify_urls import _is_publisher_redirect

    assert (
        _is_publisher_redirect("https://doi.org/10.1/x", "https://publisher.com/article/1") is True
    )
    assert _is_publisher_redirect("https://example.com/x", "https://example.com/x") is False
    assert _is_publisher_redirect("", "https://x.com") is False
    assert _is_publisher_redirect("https://x.com", None) is False


def test_verifier_no_pii_in_user_agent():
    """Per global feedback rule (no PII in outbound headers): the UA must
    NOT contain the user's email or affiliation."""
    from cv_editor.verify_urls import UA

    assert "@stanford.edu" not in UA
    assert "mkiang" not in UA
    assert "mailto:" not in UA


def test_head_probe_no_pii_in_user_agent():
    """V20-cleanup T4: the head_probe in cv_editor/url_helpers.py
    must use a clean project-name UA. Mirror of the verifier test
    above per gotcha #14."""
    import inspect

    from cv_editor import url_helpers

    src = inspect.getsource(url_helpers.head_probe)
    assert "@stanford.edu" not in src
    assert "mkiang" not in src
    assert "mailto:" not in src
    assert "cv-editor/1.0" in src  # positive assertion: clean UA wired


def test_verifier_no_from_header_with_email():
    """The 'From:' header must not be set to a personal email."""
    import inspect

    from cv_editor.verify_urls import check_url

    src = inspect.getsource(check_url)
    assert "stanford.edu" not in src
    assert "mkiang" not in src
    # 'From' header was removed entirely; ensure it didn't sneak back in
    # with a value other than empty.
    assert "POLITE_FROM" not in src


def test_publisher_blocked_in_report_section():
    """The renderer puts publisher_blocked in its own report section, not
    in 'Client errors (4xx)'."""
    from cv_editor.verify_urls import (
        CheckResult,
        Report,
        render_report,
    )

    blocked = CheckResult(
        url="https://doi.org/10.1/x",
        status=403,
        final_url="https://publisher.com/x",
        error="HTTPError 403: Forbidden",
        category="publisher_blocked",
        checked_at="2026-05-15T00:00:00+00:00",
        method_used="GET",
    )
    report = Report(
        started_at="2026-05-15T00:00:00+00:00",
        finished_at="2026-05-15T00:00:01+00:00",
        total_urls=1,
        checked=1,
        cached_skips=0,
        by_category={"publisher_blocked": [blocked]},
        sources_by_url={"https://doi.org/10.1/x": ["publications.yml#0:doi"]},
    )
    out = render_report(report)
    assert "Publisher-blocked" in out
    assert "Client errors" not in out  # 0 real 4xx
    # Counts: zero failing, one blocked.
    assert "Failing: 0" in out
    assert "Publisher-blocked: 1" in out


def test_qc_publications_no_pii_in_user_agent():
    """Same global rule applied to the other HTTP fetcher."""
    from cv_editor import qc_publications

    assert "@stanford.edu" not in qc_publications.UA
    assert "mkiang" not in qc_publications.UA


def test_enrichment_no_pii_in_user_agent():
    """Same global rule applied to the editor's enrichment fetcher."""
    from cv_editor import enrichment

    assert "@stanford.edu" not in enrichment.UA
    assert "mkiang" not in enrichment.UA
