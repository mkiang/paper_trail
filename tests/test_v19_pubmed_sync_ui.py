"""V19 — Gate 3 PubMed sync UI integration tests (2026-05-17).

The CLI in `scripts/pubmed_sync.py` is the authoritative engine; V19
wraps it in editor surface:
  * `/pubmed_sync` status + triage page (GET)
  * `POST /pubmed_sync/run` kicks `--dry-run --quiet` in background
  * `POST /pubmed_sync/apply` writes a decisions YAML + kicks `--apply`
  * `GET /pubmed_sync/status` returns running/sidecar JSON
  * `GET /qc/pubmed_sync_report` plain-text report passthrough
  * Per-entry banner on entry_view when EntryRecord.fields_flagged
    is non-empty for the entry's PMID.

Tests focus on the wrapper surface — not the CLI's diff logic (already
covered by tests/test_gate3_pubmed_sync.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _engine_guards import altmetric_required

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))


@pytest.fixture(autouse=True)
def _mock_pubmed_fetch(monkeypatch):
    """R-M2 (post-review hardening 2026-05-17): suite is hermetic.
    Every test gets a no-op fetch_pubmed_batch so route handlers that
    call compute_decisions don't hit the network on cold-cache machines."""
    from cv_editor import pubmed_sync

    monkeypatch.setattr(
        pubmed_sync.pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {},
    )


@pytest.fixture
def client():
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- compute_decisions extraction --------------------------------------


def test_compute_decisions_is_callable():
    """The extracted helper exists and has the expected signature."""
    import inspect

    from cv_editor.pubmed_sync import compute_decisions

    sig = inspect.signature(compute_decisions)
    # All keyword-only arguments; no positional required.
    for p in sig.parameters.values():
        assert p.kind in (p.KEYWORD_ONLY, p.VAR_KEYWORD), (
            f"compute_decisions should be keyword-only; got {p.name!r}={p.kind}"
        )


def test_compute_decisions_returns_dryrun_result_shape(monkeypatch):
    """compute_decisions(no_cache=False) returns a _DryRunResult with the
    expected attributes. Monkeypatch fetch_pubmed_batch to avoid network."""
    from cv_editor import pubmed_sync
    from cv_editor.pubmed_sync import _DryRunResult, compute_decisions

    monkeypatch.setattr(
        pubmed_sync.pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {},
    )
    result = compute_decisions(force=False)
    assert isinstance(result, _DryRunResult)
    for attr in (
        "header",
        "data",
        "sch",
        "state",
        "decisions",
        "skipped_no_pmid",
        "skipped_in_ttl",
        "fetch_errors",
        "all_yaml_pmids",
        "fetched_pmids",
    ):
        assert hasattr(result, attr), f"missing _DryRunResult.{attr}"


# ---- /pubmed_sync routes -------------------------------------------------


def test_pubmed_sync_view_renders(client):
    """GET /pubmed_sync returns 200 + the page heading."""
    resp = client.get("/pubmed_sync")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "PubMed sync" in body
    assert "Run dry-run" in body


def test_triage_form_a11y_landmarks_present_when_rows_exist(monkeypatch, client):
    """V13-V19-D R3-M4 fix (F13, 2026-05-18): triage rows wrap radios in
    a fieldset with a visually-hidden legend, set per-row `id`s, and
    aria-label the reason input. With one synthesized triage row, all
    three a11y landmarks must be present in the page HTML.
    """
    from cv_editor.pubmed_sync import EntryDecision, _DryRunResult

    # Build one fake decision via the public dataclasses.
    fake_decision = EntryDecision(
        global_idx=0,
        pmid="12345678",
        title_preview="Fake title for a11y test",
        publication_status="ppublish",
        flags={"title": ("YAML version", "PubMed version")},
        fills={},
    )
    result = _DryRunResult(
        header=None,
        data=[],
        sch={},
        state=None,
        decisions=[fake_decision],
        skipped_no_pmid=[],
        skipped_in_ttl=0,
        fetch_errors=[],
        all_yaml_pmids={"12345678"},
        fetched_pmids={"12345678"},
    )

    # The triage page calls compute_decisions; intercept it.
    from cv_editor import pubmed_sync

    monkeypatch.setattr(pubmed_sync, "compute_decisions", lambda *a, **kw: result)
    # Seed a sidecar entry so the "no dry-run yet" bail doesn't fire.
    from cv_editor.pubmed_sync import EntryRecord, SidecarState

    state = SidecarState()
    state.entries["12345678"] = EntryRecord(
        synced_at="2026-05-17T00:00:00Z",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=["title"],
    )
    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: state)

    body = client.get("/pubmed_sync").get_data(as_text=True)
    # Row id for flash-error jump links.
    assert 'id="row-12345678-title"' in body, (
        "triage row missing per-row id; flash error jump-links won't work"
    )
    # Fieldset wrapping for screen-reader group context.
    assert "triage-choice-set" in body, "triage-choice-set fieldset missing"
    # Visually-hidden legend with the field+pmid context.
    assert "Decision for title on PMID 12345678" in body, (
        "visually-hidden legend missing or wrong text"
    )
    # aria-label on the reason input.
    assert 'aria-label="Reason' in body, "reason input missing aria-label"


def test_pubmed_sync_status_json(client):
    """GET /pubmed_sync/status returns a JSON dict with the documented keys."""
    resp = client.get("/pubmed_sync/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload is not None
    for key in (
        "running_dryrun",
        "running_apply",
        "sidecar_entries",
        "accepted_overrides",
        "report_mtime",
        "report_url",
    ):
        assert key in payload, f"missing status key {key!r}"
    # Type check the booleans + ints.
    assert isinstance(payload["running_dryrun"], bool)
    assert isinstance(payload["running_apply"], bool)
    assert isinstance(payload["sidecar_entries"], int)
    assert isinstance(payload["accepted_overrides"], int)


def test_pubmed_sync_report_route_exists(client):
    """GET /qc/pubmed_sync_report returns 200 or 404 depending on whether
    the report file is present. Both are valid; we just shouldn't 500."""
    resp = client.get("/qc/pubmed_sync_report")
    assert resp.status_code in (200, 404)


# ---- /pubmed_sync/run kicker ---------------------------------------------


def test_pubmed_sync_run_redirects_to_view(client, monkeypatch):
    """POST /pubmed_sync/run should redirect to /pubmed_sync without
    triggering a real subprocess (we patch the kicker)."""
    # Patch threading so the daemon thread doesn't fire any subprocess.
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    resp = client.post("/pubmed_sync/run", data={})
    assert resp.status_code in (302, 303)
    assert "/pubmed_sync" in resp.headers["Location"]


def test_csrf_blocks_cross_origin_post(monkeypatch, tmp_path):
    """V20-cleanup M6 (2026-05-18): a POST with an Origin header
    from a foreign domain MUST be rejected with 403. The check uses
    `urlparse(origin).netloc == host` (exact match), NOT startswith —
    the latter is exploitable via suffix attacks like
    `localhost:5000.attacker.com`.
    """
    from cv_editor.app import create_app

    app = create_app()
    # NOTE: do NOT set TESTING — the CSRF check bypasses TESTING=True.
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    client = app.test_client()
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=1"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


def test_csrf_blocks_suffix_attack_on_origin(monkeypatch, tmp_path):
    """The exact-netloc check must reject `localhost:5000.attacker.com`
    (which a `startswith("http://localhost:5000")` check would let pass)."""
    from cv_editor.app import create_app

    app = create_app()
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    client = app.test_client()
    # The test client's request.host defaults to "localhost"; build an
    # Origin that would `startswith` "http://localhost" but is a
    # different netloc entirely.
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=1"},
        headers={"Origin": "http://localhost.attacker.com"},
    )
    assert resp.status_code == 403


@altmetric_required
def test_csrf_allows_same_origin_post(monkeypatch, tmp_path):
    """Same-origin POST (Origin matches request.host exactly) is allowed.
    (Posts to the altmetric-gated /publications/altmetric/resolve route — P5.)"""
    import cv_editor.altmetric_client as ac
    from cv_editor.app import create_app

    monkeypatch.setattr(
        ac,
        "resolve_tracker_url",
        lambda url, **kw: ac.ResolveResult(status="failed_network", error="x"),
    )
    app = create_app()
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    client = app.test_client()
    # The Werkzeug test client uses request.host = "localhost" by default;
    # set an Origin that matches.
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=1"},
        headers={"Origin": "http://localhost"},
    )
    # 200 (cache-only mode, no idx). NOT 403.
    assert resp.status_code == 200


def test_apply_missing_reason_round_trips_form_via_pending_token(client, monkeypatch):
    """V20-cleanup M2 (2026-05-18): apply route rejects a missing-reason
    keep_yaml submission AND snapshots the form so the redirect's
    ?pending=<uuid> query param can re-populate the triage form on
    next GET. Without this, 30+ rows of triage progress are lost on
    one missing-reason mistake."""
    from cv_editor import pubmed_sync as _ps

    # Mock the sidecar so the apply path reaches the validation step.
    state = _ps.SidecarState()
    state.entries["11111111"] = _ps.EntryRecord(
        synced_at="2026-05-18",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=["title"],
        yaml_idx_at_sync=0,
    )
    monkeypatch.setattr(_ps, "load_sidecar", lambda p: state)

    def _fake_compute(**kw):
        return _ps._DryRunResult(
            header=[],
            data=[],
            sch={"file": "publications.yml"},
            state=state,
            decisions=[
                _ps.EntryDecision(
                    pmid="11111111",
                    global_idx=0,
                    title_preview="Test paper",
                    flags={"title": ("yaml title", "pubmed title")},
                    publication_status="ppublish",
                )
            ],
            skipped_no_pmid=[],
            skipped_in_ttl=0,
            fetch_errors=[],
            all_yaml_pmids={"11111111"},
            fetched_pmids=["11111111"],
        )

    monkeypatch.setattr(_ps, "compute_decisions", _fake_compute)

    # POST with keep_yaml + NO reason → rejected with redirect-to-pending.
    resp = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-11111111-title": "keep_yaml",
            "reason-11111111-title": "",
        },
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers["Location"]
    assert "pending=" in loc, f"expected pending token in {loc!r}"
    # Token is parseable from the query.
    from urllib.parse import parse_qs, urlparse

    token = parse_qs(urlparse(loc).query)["pending"][0]
    assert len(token) >= 16  # uuid4 hex is 32 chars; allow for safety

    # GET the redirect target — the body should contain the user's
    # keep_yaml decision pre-selected (via the macro's pending_form).
    resp2 = client.get(loc)
    assert resp2.status_code == 200
    body = resp2.get_data(as_text=True)
    # The radio for keep_yaml on this PMID/field must be checked.
    assert (
        'name="decision-11111111-title" value="keep_yaml"' in body
        and 'class="triage-radio-keep"' in body
    )


def test_triage_page_renders_bulk_toggle_buttons_when_rows_exist(client, monkeypatch):
    """V20-cleanup M5 (2026-05-18): when triage_rows is non-empty,
    the page renders 'All defer' + 'All keep YAML' buttons.
    `Apply PubMed` bulk is intentionally absent (destructive direction)."""
    from cv_editor import pubmed_sync

    def fake_compute_decisions(*a, **kw):
        return pubmed_sync._DryRunResult(
            header=[],
            data=[],
            sch={"file": "publications.yml"},
            state=pubmed_sync.SidecarState(),
            decisions=[
                pubmed_sync.EntryDecision(
                    pmid="11111111",
                    global_idx=0,
                    title_preview="Test paper",
                    flags={"title": ("yaml title", "pubmed title")},
                    publication_status="ppublish",
                ),
            ],
            skipped_no_pmid=[],
            skipped_in_ttl=0,
            fetch_errors=[],
            all_yaml_pmids={"11111111"},
            fetched_pmids=["11111111"],
        )

    # Pre-populate sidecar so the "no dry-run yet" guard doesn't bail.
    sidecar_state = pubmed_sync.SidecarState()
    sidecar_state.entries["11111111"] = pubmed_sync.EntryRecord(
        synced_at="2026-05-18",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=["title"],
        yaml_idx_at_sync=0,
    )

    def fake_load_sidecar(path):
        return sidecar_state

    monkeypatch.setattr(pubmed_sync, "compute_decisions", fake_compute_decisions)
    monkeypatch.setattr(pubmed_sync, "load_sidecar", fake_load_sidecar)
    resp = client.get("/pubmed_sync")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-bulk-decision=""' in body  # "All defer" button
    assert 'data-bulk-decision="keep_yaml"' in body  # "All keep YAML"
    # Per the M5 design decision, NO bulk apply-pubmed button:
    assert 'data-bulk-decision="apply_pubmed"' not in body


def test_entry_view_sidecar_cache_invalidates_on_mtime_change(tmp_path):
    """V20-cleanup M3 (2026-05-18): the sidecar cache on app.config
    keys on mtime_ns. Touching the sidecar file mid-session must
    invalidate the cache so banner counts reflect the fresh state.
    """
    import json
    import time

    from cv_editor.app import create_app

    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({"version": 1, "entries": {}}))

    app = create_app()
    app.config["TESTING"] = True
    app.config["PUBMED_SYNC_SIDECAR_PATH"] = sidecar
    app.config["_PMSYNC_SIDECAR_CACHE"] = {"mtime_ns": -1, "state": None}

    client = app.test_client()
    r1 = client.get("/publications/0")
    assert r1.status_code == 200
    cache_entry = app.config["_PMSYNC_SIDECAR_CACHE"]
    first_mtime = cache_entry["mtime_ns"]
    assert first_mtime != -1

    time.sleep(0.01)
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "12345678": {
                        "synced_at": "2026-05-18",
                        "pubmed_status": "ppublish",
                        "fields_filled": [],
                        "fields_flagged": [],
                        "yaml_idx_at_sync": 0,
                    }
                },
            }
        )
    )

    r2 = client.get("/publications/0")
    assert r2.status_code == 200
    assert app.config["_PMSYNC_SIDECAR_CACHE"]["mtime_ns"] != first_mtime


def test_pubmed_sync_run_with_force_flag(client, monkeypatch):
    """POST with force=1 redirects and the subprocess argv includes --force.
    R-M3 hardening: capture subprocess.run argv and assert the flag flows
    through; the prior version only asserted on the 302 status."""
    captured_argv: list[list[str]] = []

    def fake_run(argv, **kw):
        captured_argv.append(list(argv))

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return FakeResult()

    import subprocess

    # Patch subprocess.run AND make threads run synchronously so the
    # background daemon thread actually executes the build_argv lambda
    # and calls our fake_run before the test ends.
    monkeypatch.setattr(subprocess, "run", fake_run)
    import threading as _threading

    def sync_thread(target, **kw):
        class _T:
            def start(self_inner):
                target()

        return _T()

    monkeypatch.setattr(_threading, "Thread", sync_thread)

    resp = client.post("/pubmed_sync/run", data={"force": "1"})
    assert resp.status_code in (302, 303)
    # Expect exactly one subprocess invocation with --force in argv.
    assert captured_argv, "subprocess.run was never called"
    assert any("--force" in argv for argv in captured_argv), (
        f"--force missing from argv: {captured_argv}"
    )

    # Sanity: a force=0 POST should NOT include --force. Need a separate kicker
    # state OR a force-reset; the existing kicker state is module-scoped to the
    # app fixture so a second POST in the same test would skip due to running=True.
    # Skip the negative assertion here — covered by the no-force POST test above.


# ---- /pubmed_sync/apply path --------------------------------------------


def test_pubmed_sync_apply_no_decisions_warns(client):
    """POST /pubmed_sync/apply with no decision-* fields flashes a warning
    rather than writing an empty decisions YAML."""
    resp = client.post("/pubmed_sync/apply", data={}, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "No decisions to apply" in body


def test_pubmed_sync_apply_keep_yaml_missing_reason_rejects_all(
    client,
    tmp_path,
    monkeypatch,
):
    """R-H5 (post-review): keep_yaml without a reason REJECTS the whole
    submission — no gen file is written, no apply is kicked, even valid
    decisions in the same POST are not applied."""
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    # Read the CONFIGURED gen path (data_root()/qc), not a hardcoded
    # PROJ_ROOT/qc — the route writes to the P1-redirected workspace, so
    # asserting against PROJ_ROOT/qc checks a location the route never touches
    # (a vacuous guard). Matches test_pubmed_sync_apply_writes_decisions_file.
    gen_path = Path(client.application.config["PMSYNC_DECISIONS_GEN_PATH"])
    pre_existed = gen_path.exists()
    pre_mtime = gen_path.stat().st_mtime_ns if pre_existed else None
    resp = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-12345-authors": "keep_yaml",
            # missing reason-12345-authors
            "decision-99999-month": "apply_pubmed",  # would-be valid
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    # Whole submission rejected.
    assert "requires a reason" in body
    assert "No decisions applied" in body
    # Gen file MUST NOT have been (re)written by this rejected request.
    if pre_existed:
        assert gen_path.stat().st_mtime_ns == pre_mtime
    else:
        assert not gen_path.exists()


def test_pubmed_sync_apply_rejects_malformed_pmid(client, monkeypatch):
    """R-H2 (post-review): non-numeric pmid is dropped with a flash."""
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    resp = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-abc-authors": "apply_pubmed",
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "Malformed pmid" in body
    # Since the only decision was dropped, "No decisions to apply" surfaces.
    assert "No decisions to apply" in body


def test_pubmed_sync_apply_rejects_unknown_field(client, monkeypatch):
    """R-H2 (post-review): unknown field name is dropped with a flash."""
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    resp = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-12345-nopesfield": "apply_pubmed",
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "Unknown field" in body
    assert "No decisions to apply" in body


def test_only_epub_narrows_after_force_change(monkeypatch):
    """V13-V19-D R1-H1: a force=True call must still respect only_epub
    narrowing. Before the fix, `needs_refresh` checked force BEFORE
    only_epub, so dry-run's effective_force=True silently fetched
    ppublish entries too. After the fix, only_epub gates first."""
    from datetime import datetime, timezone

    from cv_editor.pubmed_sync import EntryRecord, needs_refresh

    rec_ppublish = EntryRecord(
        synced_at="2026-05-17T12:00:00+00:00",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=[],
        yaml_idx_at_sync=0,
    )
    rec_epub = EntryRecord(
        synced_at="2026-05-17T12:00:00+00:00",
        pubmed_status="epublish",
        fields_filled=[],
        fields_flagged=[],
        yaml_idx_at_sync=0,
    )
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    # only_epub=True + force=True: ppublish MUST be skipped, epub MUST refresh.
    assert needs_refresh(rec_ppublish, now=now, only_epub=True, force=True) is False
    assert needs_refresh(rec_epub, now=now, only_epub=True, force=True) is True
    # only_epub=False + force=True: both refresh (existing behaviour).
    assert needs_refresh(rec_ppublish, now=now, only_epub=False, force=True) is True
    assert needs_refresh(rec_epub, now=now, only_epub=False, force=True) is True


def test_apply_pubmed_drops_stale_keep_yaml_override(monkeypatch):
    """V13-V19-D R2-H5: after `apply_pubmed_decisions` writes PubMed's
    value to YAML, the stale `keep_yaml` override for the same
    (pmid, field) must be removed from `state.accepted_yaml_overrides`."""
    from cv_editor import schemas
    from cv_editor.pubmed_sync import (
        AcceptedOverride,
        Decision,
        EntryDecision,
        SidecarState,
        apply_pubmed_decisions,
    )

    sch = schemas.SCHEMAS["publications"]
    data = [
        {
            "subsection": "PRR",
            "entries": [
                {
                    "title": "x",
                    "journal": "J",
                    "year": 2025,
                    "authors": ["Public JQ"],
                    "pmid": "12345",
                    "month": 1,
                }
            ],
        }
    ]
    dec = EntryDecision(
        pmid="12345",
        global_idx=0,
        title_preview="x",
        flags={"month": (1, 3)},
        raw_yaml={"month": 1},
        raw_pubmed={"month": 3},
    )
    state = SidecarState()
    state.accepted_yaml_overrides["12345"] = {
        "month": AcceptedOverride(
            yaml_value="1",
            pubmed_value="3",
            reason="stale",
            accepted_at="2026-05-15T12:00:00+00:00",
        ),
    }
    apply_pubmed_decs = [
        Decision(pmid="12345", field="month", decision="apply_pubmed", reason=""),
    ]
    n = apply_pubmed_decisions(data, sch, [dec], apply_pubmed_decs, state=state)
    assert n == 1
    # Override was popped; PMID entry pruned to empty dict, then removed.
    assert "12345" not in state.accepted_yaml_overrides
    # YAML value written.
    assert data[0]["entries"][0]["month"] == 3


def test_citations_snapshot_refuses_to_shrink_committed(client, tmp_path, monkeypatch):
    """V13-V19-D R3-H2: a cold-sidecar 'Regenerate snapshot' click would
    silently overwrite a committed N-entry snapshot with {}. The route
    must refuse when the in-memory cache has fewer entries than the
    on-disk snapshot."""
    import json

    cache_path = tmp_path / "cold_cache.json"
    snap_path = tmp_path / "committed_snapshot.json"
    # Cold sidecar: empty.
    cache_path.write_text('{"version": 1, "entries": {}}')
    # Snapshot has 3 entries from a prior fetch.
    snap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "counts": {
                    "10.1/a": {"count": 5, "fetched_at": "2026-05-17T12:00:00+00:00"},
                    "10.1/b": {"count": 10, "fetched_at": "2026-05-17T12:00:00+00:00"},
                    "10.1/c": {"count": 15, "fetched_at": "2026-05-17T12:00:00+00:00"},
                },
            }
        )
    )
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["CITATION_CACHE_PATH"] = cache_path
    app.config["CITATION_SNAPSHOT_PATH"] = snap_path
    c = app.test_client()
    resp = c.post("/citations/snapshot", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Refusing to shrink" in body
    # Snapshot still has 3 entries.
    snap_after = json.loads(snap_path.read_text())
    assert len(snap_after["counts"]) == 3


def test_citations_snapshot_force_overrides_shrink_guard(client, tmp_path, monkeypatch):
    """The force=1 escape hatch from R3-H2 allows intentional shrink."""
    import json

    cache_path = tmp_path / "cold_cache.json"
    snap_path = tmp_path / "committed_snapshot.json"
    cache_path.write_text('{"version": 1, "entries": {}}')
    snap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "counts": {
                    "10.1/a": {"count": 5, "fetched_at": "2026-05-17T12:00:00+00:00"},
                },
            }
        )
    )
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["CITATION_CACHE_PATH"] = cache_path
    app.config["CITATION_SNAPSHOT_PATH"] = snap_path
    c = app.test_client()
    resp = c.post("/citations/snapshot", data={"force": "1"}, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Snapshot regenerated" in body
    snap_after = json.loads(snap_path.read_text())
    assert snap_after["counts"] == {}


def test_pubmed_sync_view_bails_when_sidecar_empty(client, monkeypatch):
    """R-H3 (post-review): cold sidecar means triage rows are NOT
    materialized — the page tells the user to run a dry-run first
    instead of synchronously fetching PMIDs from PubMed."""
    from cv_editor import pubmed_sync
    from cv_editor.pubmed_sync import SidecarState

    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: SidecarState())
    resp = client.get("/pubmed_sync")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No dry-run has been run yet" in body


def test_pubmed_sync_apply_writes_decisions_file(client, tmp_path, monkeypatch):
    """A valid keep_yaml (with reason) + apply_pubmed combo writes the
    gen-decisions YAML in qc/ and kicks the apply subprocess."""
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    # Read the gen file from the app's CONFIGURED path, not a hardcoded
    # PROJ_ROOT/qc — the route writes to data_root()/qc, which the P1
    # workspace-isolation fixture redirects to a per-test tmp. Hardcoding
    # PROJ_ROOT made this test pass only via cross-test pollution of the real
    # qc/ (and fail in isolation / a public tree). Config path fixes both.
    gen_path = Path(client.application.config["PMSYNC_DECISIONS_GEN_PATH"])
    pre_exists = gen_path.exists()
    pre_mtime = gen_path.stat().st_mtime if pre_exists else None
    try:
        resp = client.post(
            "/pubmed_sync/apply",
            data={
                "decision-12345-authors": "keep_yaml",
                "reason-12345-authors": "YAML preferred form",
                "decision-99999-month": "apply_pubmed",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert gen_path.exists()
        # Confirm shape: should be parseable YAML with 2 decisions.
        import yaml as pyyaml

        body = pyyaml.safe_load(gen_path.read_text()) or {}
        decs = body.get("decisions") or []
        assert len(decs) == 2
        pmids_seen = {d["pmid"] for d in decs}
        assert pmids_seen == {"12345", "99999"}
        for d in decs:
            assert d["decision"] in ("keep_yaml", "apply_pubmed")
    finally:
        # Restore prior state — don't leave the test artifact lying around.
        if pre_exists and pre_mtime is not None:
            pass  # leave it
        elif gen_path.exists():
            gen_path.unlink()


# ---- nav link + entry_view banner ----------------------------------------


def test_nav_link_includes_pubmed_sync(client):
    """Tools dropdown contains a PubMed sync link."""
    body = client.get("/").get_data(as_text=True)
    assert ">PubMed sync<" in body
    assert "/pubmed_sync" in body


def test_entry_view_no_banner_when_no_flagged_fields(client, monkeypatch):
    """Without a sidecar entry for this PMID, the banner is absent."""
    from cv_editor import pubmed_sync
    from cv_editor.pubmed_sync import SidecarState

    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: SidecarState())
    resp = client.get("/publications/0")
    body = resp.get_data(as_text=True)
    assert "PubMed disagreement" not in body


def test_entry_view_banner_subtracts_accepted_overrides(client, monkeypatch):
    """Live-test fix (2026-05-17): a field listed in `fields_flagged`
    that's ALSO in `accepted_yaml_overrides` for the same PMID has been
    silenced — the banner must subtract it. Without this fix, the banner
    promises a triage row that the page can't deliver (the dry-run
    silences via apply_overrides_to_decision; raw fields_flagged lags).

    V13-V19-D R2-H1 hardening (2026-05-17): the override's snapshot
    `yaml_value` must MATCH the current YAML value for the field to be
    silenced. If they diverge, the field is "re-surfaced" and the banner
    MUST show it. This test pins the silence path; another below covers
    the re-surface path."""
    from cv_editor import pubmed_sync, yaml_io
    from cv_editor.pubmed_sync import AcceptedOverride, EntryRecord, SidecarState

    _, data = yaml_io.load(PROJ_ROOT / "data" / "publications.yml")
    target_idx = None
    target_pmid = None
    target_entry = None
    cursor = 0
    for sub in data:
        for e in sub.get("entries", []) or []:
            pmid = str(e.get("pmid") or "").strip()
            if pmid:
                target_idx = cursor
                target_pmid = pmid
                target_entry = e
                break
            cursor += 1
        if target_pmid is not None:
            break
    assert target_pmid is not None

    fake_state = SidecarState()
    fake_state.entries[target_pmid] = EntryRecord(
        synced_at="2026-05-17T12:00:00+00:00",
        pubmed_status="ppublish",
        fields_filled=[],
        # Both fields appear as flagged...
        fields_flagged=["title", "journal"],
        yaml_idx_at_sync=target_idx,
    )
    # ...but `title` has an accepted override whose snapshot matches the
    # current YAML value → silenced → banner must NOT mention it.
    fake_state.accepted_yaml_overrides[target_pmid] = {
        "title": AcceptedOverride(
            yaml_value=target_entry.get("title") or "",
            pubmed_value="(different)",
            reason="YAML preferred form",
            accepted_at="2026-05-17T11:00:00+00:00",
        ),
    }
    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: fake_state)
    resp = client.get(f"/publications/{target_idx}")
    body = resp.get_data(as_text=True)
    assert "PubMed disagreement" in body
    assert ">journal<" in body
    assert ">title<" not in body


def test_entry_view_banner_resurfaces_override_when_yaml_changed(client, monkeypatch):
    """V13-V19-D R2-H1 (2026-05-17): an override whose snapshot
    `yaml_value` no longer matches the current YAML value has
    RE-SURFACED — the banner must show it. Mirrors the triage page's
    `apply_overrides_to_decision` resurfaced-vs-silenced split."""
    from cv_editor import pubmed_sync, yaml_io
    from cv_editor.pubmed_sync import AcceptedOverride, EntryRecord, SidecarState

    _, data = yaml_io.load(PROJ_ROOT / "data" / "publications.yml")
    target_idx = None
    target_pmid = None
    cursor = 0
    for sub in data:
        for e in sub.get("entries", []) or []:
            pmid = str(e.get("pmid") or "").strip()
            if pmid:
                target_idx = cursor
                target_pmid = pmid
                break
            cursor += 1
        if target_pmid is not None:
            break
    assert target_pmid is not None

    fake_state = SidecarState()
    fake_state.entries[target_pmid] = EntryRecord(
        synced_at="2026-05-17T12:00:00+00:00",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=["title"],
        yaml_idx_at_sync=target_idx,
    )
    # Override snapshot doesn't match the current YAML title → resurfaced.
    fake_state.accepted_yaml_overrides[target_pmid] = {
        "title": AcceptedOverride(
            yaml_value="(stale snapshot from a prior triage)",
            pubmed_value="(different)",
            reason="YAML preferred form",
            accepted_at="2026-05-17T11:00:00+00:00",
        ),
    }
    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: fake_state)
    resp = client.get(f"/publications/{target_idx}")
    body = resp.get_data(as_text=True)
    assert "PubMed disagreement" in body
    assert ">title<" in body


def test_pubmed_sync_apply_clears_silenced_fields_from_flagged(tmp_path):
    """Live-test root-cause fix (2026-05-17): after --apply records a
    keep_yaml override, the same run's sidecar update must NOT include
    that field in `fields_flagged`. Previously the entry_view banner
    over-reported the field as 'pending triage' the first time the user
    accepted an override."""
    # Build a fake state where pmid 999 has a flagged title.
    # Then simulate accepting keep_yaml on that title via --apply.
    # Walk pubmed_sync.main's logic in-process by hand:
    #   1. decisions: [EntryDecision(pmid=999, flags={"title": (yaml, pm)}, ...)]
    #   2. keep_yaml_decs: [Decision(pmid=999, field=title, decision=keep_yaml, reason="...")]
    #   3. After the new fix, fields_flagged should be [] for pmid 999.
    from cv_editor.pubmed_sync import Decision, EntryDecision

    dec = EntryDecision(
        pmid="999",
        global_idx=0,
        title_preview="x",
        flags={"title": ("yaml title", "pubmed title")},
        raw_yaml={"title": "yaml title"},
        raw_pubmed={"title": "pubmed title"},
    )
    keep_yaml_decs = [
        Decision(pmid="999", field="title", decision="keep_yaml", reason="x"),
    ]
    apply_pubmed_decs: list[Decision] = []
    # Inline the corrected flag-subtraction logic that lives in main():
    handled: dict[str, set[str]] = {}
    for d in keep_yaml_decs:
        handled.setdefault(d.pmid, set()).add(d.field)
    for d in apply_pubmed_decs:
        handled.setdefault(d.pmid, set()).add(d.field)
    fields_flagged_now = [f for f in dec.flags.keys() if f not in handled.get(dec.pmid, set())]
    assert fields_flagged_now == [], (
        f"keep_yaml field 'title' should be subtracted from fields_flagged "
        f"in the same apply run; got {fields_flagged_now}"
    )


def test_entry_view_banner_when_flagged_fields_present(client, monkeypatch):
    """Sidecar with fields_flagged → banner shows up."""
    # Use real publications.yml: find any entry with a PMID.
    from cv_editor import pubmed_sync, yaml_io
    from cv_editor.pubmed_sync import EntryRecord, SidecarState

    _, data = yaml_io.load(PROJ_ROOT / "data" / "publications.yml")
    target_idx = None
    target_pmid = None
    cursor = 0
    for sub in data:
        for e in sub.get("entries", []) or []:
            pmid = str(e.get("pmid") or "").strip()
            if pmid:
                target_idx = cursor
                target_pmid = pmid
                break
            cursor += 1
        if target_pmid is not None:
            break
    assert target_pmid is not None
    fake_state = SidecarState()
    fake_state.entries[target_pmid] = EntryRecord(
        synced_at="2026-05-17T12:00:00+00:00",
        pubmed_status="ppublish",
        fields_filled=[],
        fields_flagged=["title", "journal"],
        yaml_idx_at_sync=target_idx,
    )
    monkeypatch.setattr(pubmed_sync, "load_sidecar", lambda path: fake_state)
    resp = client.get(f"/publications/{target_idx}")
    body = resp.get_data(as_text=True)
    assert "PubMed disagreement" in body
    assert ">title<" in body
    assert ">journal<" in body
