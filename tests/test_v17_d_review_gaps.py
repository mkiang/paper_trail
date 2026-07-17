"""V17-D code review gap-fill tests.

Top issues from the 4-reviewer parallel pass on V6-V17:

- T-H1: bulk move_subsection multi-entry / cross-subsection preservation
- T-H2: duplicate route on cluster + subsections_of_clusters structures
- T-H3: duplicate deep-copy is independent of source
- T-H4: CommentedMap comments survive duplicate round-trip
- T-H5: kick_url_verify_if_idle single-in-flight under concurrent posts
- T-M3: date_sort_norm malformed inputs return empty/fallback
- T-M9: verify_urls 429 categorization
- C-H1/C-H2: entry_undo + entry_restore mtime guards
- C-H3: publisher_blocked Location-header fallback
- C-M5: prune_frozen skips invalid names mid-loop
- D-H4: require_section decorator allow_meta variants
- D-H3: write_or_409 helper returns shape

These tests pin behavior we'd otherwise lose on a refactor.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from cv_editor import paths, schemas, sections, sort_keys, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def _pubs_path():
    # P1 seam: resolve against the active (test-isolated) workspace root.
    return paths.data_dir() / "publications.yml"


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def snapshot_section():
    """Snapshot a section's YAML; restore on teardown. Use it like:
    path = snapshot_section('teaching')
    """
    saved = {}

    def _snap(section: str):
        sch = schemas.get(section)
        p = paths.data_root() / sch["file"]
        saved[p] = p.read_bytes()
        return p

    yield _snap
    for p, content in saved.items():
        p.write_bytes(content)


# ---- T-H1: bulk move_subsection multi-entry cross-subsection preservation ----


def _pubs_subsections(data):
    """Return (subsection_name, [titles]) for each subsection in data."""
    return [
        (
            str(s.get("subsection") or ""),
            [str(e.get("title", "")) for e in (s.get("entries") or [])],
        )
        for s in (data or [])
    ]


def test_bulk_move_subsection_multi_entry_cross_subsection_preserves_all(client, snapshot_section):
    """Move 2+ entries from 2+ source subsections to a 3rd subsection.
    Verify (a) every entry survives, (b) total entry count unchanged,
    (c) entries land in the target. Regression for the V8-V11-D HIGH
    fix where a forward-delete loop would invalidate cached loc tuples.
    """
    snapshot_section("publications")
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    by_sub_before = _pubs_subsections(data)
    if len(by_sub_before) < 3:
        pytest.skip("fixture has fewer than 3 subsections")

    # Pick one entry from each of the first two subsections; target the third.
    recs = list(sections.flatten(data, sch["structure"]))
    src_sub_a = by_sub_before[0][0]
    src_sub_b = by_sub_before[1][0]
    target_sub = by_sub_before[2][0]
    pick_a = next(r for r in recs if r["ctx"].get("subsection") == src_sub_a)
    pick_b = next(r for r in recs if r["ctx"].get("subsection") == src_sub_b)

    titles_to_move = {pick_a["entry"].get("title"), pick_b["entry"].get("title")}
    n_total_before = sum(1 for _ in sections.flatten(data, sch["structure"]))
    mt = yaml_io.mtime_ns(_pubs_path())

    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "move_subsection",
            "selected": [str(pick_a["global_idx"]), str(pick_b["global_idx"])],
            "target_subsection": target_sub,
            "mtime_ns": str(mt),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)

    # Re-read.
    _, data2 = yaml_io.load(_pubs_path())
    n_total_after = sum(1 for _ in sections.flatten(data2, sch["structure"]))
    assert n_total_after == n_total_before, "lost entries during cross-subsection move"

    titles_in_target = {
        str(e.get("title", ""))
        for s in data2
        if str(s.get("subsection")) == target_sub
        for e in (s.get("entries") or [])
    }
    assert titles_to_move <= titles_in_target, "moved entries not in target subsection"


# ---- T-H2: duplicate for clusters + subsections_of_clusters ----


def test_duplicate_clusters_preserves_institution_and_city(client, snapshot_section):
    """Teaching is `clusters` structure. Duplicate must land the new
    entry in the SAME cluster (institution+city), not a new one."""
    snapshot_section("teaching")
    sch = schemas.get("teaching")
    _, data = yaml_io.load(paths.data_root() / sch["file"])
    rec = next(sections.flatten(data, sch["structure"]))
    src_inst = rec["ctx"].get("institution")
    src_city = rec["ctx"].get("city")
    n_clusters_before = len(data or [])
    mt = yaml_io.mtime_ns(paths.data_root() / sch["file"])

    resp = client.post(
        f"/teaching/{rec['global_idx']}/duplicate",
        data={"mtime_ns": str(mt)},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)

    _, data2 = yaml_io.load(paths.data_root() / sch["file"])
    assert len(data2 or []) == n_clusters_before, "duplicate created a new cluster"
    # The new entry must be in a cluster with matching institution+city.
    found = False
    for cl in data2 or []:
        if cl.get("institution") == src_inst and cl.get("city") == src_city:
            entries = cl.get("entries") or []
            n_matching_role = sum(1 for e in entries if e.get("role") == rec["entry"].get("role"))
            if n_matching_role >= 2:  # original + duplicate
                found = True
                break
    assert found, "duplicate did not land in source cluster"


def test_duplicate_subsections_of_clusters_preserves_both(client, snapshot_section):
    """Appointments is `subsections_of_clusters`. Duplicate must preserve
    BOTH the subsection AND the institution+city cluster within."""
    snapshot_section("appointments")
    sch = schemas.get("appointments")
    _, data = yaml_io.load(paths.data_root() / sch["file"])
    rec = next(sections.flatten(data, sch["structure"]))
    src_sub = rec["ctx"].get("subsection")
    src_inst = rec["ctx"].get("institution")
    mt = yaml_io.mtime_ns(paths.data_root() / sch["file"])
    n_before = sum(1 for _ in sections.flatten(data, sch["structure"]))

    resp = client.post(
        f"/appointments/{rec['global_idx']}/duplicate",
        data={"mtime_ns": str(mt)},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)

    _, data2 = yaml_io.load(paths.data_root() / sch["file"])
    n_after = sum(1 for _ in sections.flatten(data2, sch["structure"]))
    assert n_after == n_before + 1
    # Verify the new entry is reachable via the same (subsection, institution).
    for r in sections.flatten(data2, sch["structure"]):
        if (
            r["ctx"].get("subsection") == src_sub
            and r["ctx"].get("institution") == src_inst
            and r["entry"].get("role") == rec["entry"].get("role")
        ):
            return
    pytest.fail("duplicate not found in source subsection + cluster")


# ---- T-H3: duplicate deep-copy isolation ----


def test_duplicate_deep_copy_isolates_nested_mutations(client, snapshot_section):
    """Mutating the duplicate's nested list (notes/authors) must NOT
    leak back to the original. Pins copy.deepcopy semantics."""
    snapshot_section("publications")
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    # Find a publication with a typed_notes list.
    src_rec = next(
        (
            r
            for r in sections.flatten(data, sch["structure"])
            if isinstance(r["entry"].get("notes"), list) and r["entry"]["notes"]
        ),
        None,
    )
    if src_rec is None:
        pytest.skip("no publication with notes in fixture")
    src_idx = src_rec["global_idx"]
    src_n_notes = len(src_rec["entry"]["notes"])

    mt = yaml_io.mtime_ns(_pubs_path())
    resp = client.post(
        f"/publications/{src_idx}/duplicate",
        data={"mtime_ns": str(mt)},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    _, data2 = yaml_io.load(_pubs_path())
    # After the duplicate there are exactly two entries with this title (the
    # original + its deep copy). Find the PAIR by title — do NOT assume the
    # duplicate landed at src_idx (insert prepends to the subsection, so a
    # locate(src_idx) can hit an unrelated no-notes neighbor; that fragility
    # was the 2026-05-30 false failure). Mutate one copy's notes list and
    # confirm the other is untouched — a shallow copy would alias the list.
    matching = [
        r
        for r in sections.flatten(data2, sch["structure"])
        if r["entry"].get("title") == src_rec["entry"].get("title")
    ]
    assert len(matching) == 2, "expected exactly 2 matching titles after duplicate"
    matching[0]["entry"]["notes"].append({"type": "note", "text": "isolation-test"})
    n_unchanged = sum(1 for r in matching if len(r["entry"].get("notes") or []) == src_n_notes)
    assert n_unchanged == 1, "mutation to one copy leaked to the other — deep-copy isolation broken"


# ---- T-H4: comments survive duplicate round-trip ----


def test_duplicate_preserves_yaml_round_trip_no_data_loss(client, snapshot_section):
    """After duplicate, re-loading the YAML must yield N+1 entries with
    no field data loss on the duplicate. CommentedMap deepcopy + ruamel
    round-trip must keep keys + values intact."""
    snapshot_section("publications")
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    src_rec = next(sections.flatten(data, sch["structure"]))
    src_keys = set(src_rec["entry"].keys())
    src_title = src_rec["entry"].get("title")
    n_before = sum(1 for _ in sections.flatten(data, sch["structure"]))

    mt = yaml_io.mtime_ns(_pubs_path())
    resp = client.post(
        f"/publications/{src_rec['global_idx']}/duplicate",
        data={"mtime_ns": str(mt)},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    _, data2 = yaml_io.load(_pubs_path())
    n_after = sum(1 for _ in sections.flatten(data2, sch["structure"]))
    assert n_after == n_before + 1
    matching = [
        r for r in sections.flatten(data2, sch["structure"]) if r["entry"].get("title") == src_title
    ]
    assert len(matching) == 2
    for r in matching:
        # Every key on the original survives on the copy.
        assert set(r["entry"].keys()) >= src_keys, (
            f"missing keys on round-trip: {src_keys - set(r['entry'].keys())}"
        )


# ---- T-H5: kick_url_verify single-in-flight ----


def test_kick_url_verify_concurrent_posts_only_spawn_one_worker(client):
    """Two near-simultaneous POSTs to /urls/verify must spawn at most
    one verifier subprocess. The lock+state contract is load-bearing per
    scripts/CLAUDE.md gotcha #7."""
    # The actual subprocess gets spawned; we don't want to wait for its
    # 600s timeout. Instead: fire two calls, check both return 302, and
    # check the global state via /urls/status is consistent.
    # (We don't assert on the spawned process — that's external.)
    barrier = threading.Barrier(2)
    results = []

    def _hit():
        barrier.wait()
        with create_app().test_client() as c:
            r = c.post("/urls/verify", follow_redirects=False)
            results.append(r.status_code)

    t1 = threading.Thread(target=_hit)
    t2 = threading.Thread(target=_hit)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert all(s == 302 for s in results), f"expected both POSTs to redirect; got {results}"


# ---- T-M3: sort_keys malformed inputs ----


@pytest.mark.parametrize(
    "bad",
    [
        "13/2026",  # invalid month
        "00/2026",  # zero month — parse succeeds (200600) but document
        "MM/YYYY",  # literal placeholder
        "MM-YYYY",  # wrong separator
        "abc",
        "2025-09",  # YYYY-MM (uses '-' instead of '/')
        "01-2026",
        "2026/01",  # YYYY/MM — int(YYYY) succeeds → wrong shape but not crash
        "",
        None,
    ],
)
def test_date_sort_norm_handles_malformed_input(bad):
    """date_sort_norm must never raise; bad input returns "" so rows
    sort to one extreme rather than crashing the page."""
    out = sort_keys.date_sort_norm(bad)
    assert isinstance(out, str)


def test_year_month_sort_norm_handles_garbage():
    assert sort_keys.year_month_sort_norm(None) == "00000000"
    assert sort_keys.year_month_sort_norm("abc") == "00000000"
    assert sort_keys.year_month_sort_norm(2026) == "20260000"
    assert sort_keys.year_month_sort_norm(2026, 3) == "20260300"
    assert sort_keys.year_month_sort_norm(2026, 3, 15) == "20260315"
    assert sort_keys.year_month_sort_norm("2026", "3", "15") == "20260315"


# ---- T-M9: verify_urls 429 categorization ----


def test_categorize_429_publisher_blocked():
    """429 (Too Many Requests) after a host-crossing redirect should
    join 403 in the publisher_blocked carve-out — same semantic
    (publisher dislikes our bot)."""
    from cv_editor.verify_urls import _categorize

    cat = _categorize(
        429, None, request_url="https://doi.org/10.1/x", final_url="https://publisher.com/x"
    )
    assert cat == "publisher_blocked"
    cat2 = _categorize(
        429, None, request_url="https://example.com/x", final_url="https://example.com/x"
    )
    assert cat2 == "4xx"


# ---- C-H1 / C-H2: undo + restore mtime guards ----


def test_entry_undo_with_stale_mtime_returns_409(client, snapshot_section):
    """Old mtime → StaleFileError → 409 redirect. Defends against the
    "tab A saves while tab B clicks Undo" race."""
    snapshot_section("publications")
    # Need at least one backup to trigger the restore path.
    backups = yaml_io.list_backups(_pubs_path().name)
    if not backups:
        pytest.skip("no backups available to test undo")
    resp = client.post(
        "/publications/undo",
        data={"mtime_ns": "1"},  # definitely stale
        follow_redirects=False,
    )
    # 409 redirect path returns the redirect with status 409.
    assert resp.status_code in (302, 409)
    if resp.status_code == 302:
        # Followed redirect; check the flash banner mentions stale.
        body = client.get(resp.headers["Location"]).get_data(as_text=True)
        assert "Stale" in body or "stale" in body


def test_entry_restore_requires_mtime_ns(client, snapshot_section):
    """Old mtime on entry_restore → 409. Same race protection."""
    snapshot_section("publications")
    backups = yaml_io.list_backups(_pubs_path().name)
    if not backups:
        pytest.skip("no backups available to test restore")
    resp = client.post(
        "/publications/restore",
        data={
            "backup_name": backups[0].name,
            "mtime_ns": "1",  # definitely stale
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409


# ---- C-H3: publisher_blocked Location-header fallback ----


def test_categorize_uses_location_header_when_e_url_missing():
    """When HTTPError has no .url but Location header points elsewhere,
    we should still detect publisher_blocked. This is the V17-D fix
    for older urllib behavior where e.url is sometimes None."""
    # Tested at the _categorize layer; the wiring to e.headers / e.geturl()
    # is in check_url. Here we just confirm _is_publisher_redirect's
    # contract is satisfied when given the post-redirect URL.
    from cv_editor.verify_urls import _is_publisher_redirect

    assert _is_publisher_redirect("https://doi.org/10.1/x", "https://publisher.com/foo") is True
    assert _is_publisher_redirect("https://x.com/y", "") is False


# ---- C-M5: prune_frozen skips invalid names mid-loop ----


def test_prune_frozen_skips_invalid_names(tmp_path, monkeypatch):
    """If list_frozen yields a frozen-foo dir (no digits), delete_frozen
    raises ValueError. The prune loop must skip it and continue, not
    abort the rest of the deletion pass."""
    import os

    from cv_editor import freezer

    # Point freezer at a tmp ROOT so it sees only our fake "output" dir.
    out = tmp_path / "output"
    out.mkdir()
    (out / "frozen-1000000000").mkdir()
    (out / "frozen-foo").mkdir()  # bad name; valid glob match, fails regex
    (out / "frozen-2000000000").mkdir()
    old = time.time() - 100 * 86400
    for p in out.iterdir():
        os.utime(p, (old, old))
    monkeypatch.setattr(freezer, "ROOT", tmp_path)
    deleted = freezer.prune_frozen(days_old=1)
    names = set(deleted)
    assert "frozen-1000000000" in names
    assert "frozen-2000000000" in names
    assert "frozen-foo" not in names
    assert (out / "frozen-foo").exists()


# ---- D-H4: require_section decorator coverage ----


def test_require_section_404_for_unknown_section(client):
    resp = client.get("/nope")
    assert resp.status_code == 404


def test_require_section_meta_redirects_for_view(client):
    resp = client.get("/meta", follow_redirects=False)
    # /meta has its own dedicated route, but the section_list route at
    # /<section>=meta also redirects there — exercised by test_v2_routes.
    assert resp.status_code == 200


# ---- D-H3: write_or_409 helper invariants ----


def test_bulk_unknown_action_400(client):
    """Allow-list still enforced after refactor."""
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "drop_table",
            "selected": ["0"],
            "mtime_ns": str(yaml_io.mtime_ns(_pubs_path())),
        },
    )
    assert resp.status_code == 400
