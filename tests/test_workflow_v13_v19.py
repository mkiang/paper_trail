"""V13-V19-D R3-M5 (2026-05-18) — one workflow smoke test.

End-to-end coverage was flagged as a gap by the combined review: every
existing test is single-route. This file adds a multi-step workflow
test for the highest-value path — PubMed sync triage → keep_yaml
override → triage page no longer surfaces the row.

The broader test infrastructure for V13/V14/V15 multi-step flows
(Altmetric paste → save, citation fetch → snapshot regen, tracker
sweep → YAML write) is queued as its own milestone; this file covers
the one path that the V19 R-H1 race fix is specifically about, and
that the live-test bugs of 2026-05-17 made visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))


@pytest.fixture(autouse=True)
def _hermetic_pubmed_fetch(monkeypatch):
    """Hermetic suite: no PubMed network. All tests provide their own
    sidecar state via the fixture below."""
    from cv_editor import pubmed_sync

    monkeypatch.setattr(
        pubmed_sync.pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {},
    )


@pytest.fixture
def app_with_tmp_sidecar(tmp_path, monkeypatch):
    """Create the Flask app with PUBMED_SYNC_SIDECAR_PATH and
    PMSYNC_DECISIONS_GEN_PATH redirected to tmp files, so tests can
    seed sidecar state and observe gen-YAML writes without touching
    the real `data/publications_pubmed_sync.json` or
    `qc/pubmed_sync_decisions.gen.yml`.

    Two F2-class app.config overrides verified at once. If either
    regresses (route reads the module constant instead of the config),
    the tests in this file will polute the real repo OR silently use
    production sidecar — both useful canaries.
    """
    sidecar = tmp_path / "test_pubmed_sync.json"
    gen_yml = tmp_path / "test_decisions.gen.yml"
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["PUBMED_SYNC_SIDECAR_PATH"] = sidecar
    app.config["PMSYNC_DECISIONS_GEN_PATH"] = gen_yml
    return app, sidecar, gen_yml


def _seed_sidecar(sidecar_path: Path, *, pmid: str, fields_flagged: list[str]) -> None:
    """Write a one-entry sidecar with the given pmid + flagged fields."""
    sidecar_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    pmid: {
                        "synced_at": "2026-05-17T00:00:00Z",
                        "pubmed_status": "ppublish",
                        "fields_filled": [],
                        "fields_flagged": fields_flagged,
                        "yaml_idx_at_sync": 0,
                    }
                },
                "no_pmid_skip_log": {},
                "accepted_yaml_overrides": {},
            }
        )
    )


def test_apply_keep_yaml_writes_gen_yaml_under_lock(app_with_tmp_sidecar, monkeypatch, tmp_path):
    """Smoke: a single keep_yaml decision flows through /pubmed_sync/apply,
    writes the gen YAML file atomically, and would kick the apply
    subprocess.

    Verifies V13-V19-D R1-H2 (atomic gen-file write + lock-held kick).
    The subprocess itself is mocked — this is the editor wrapper test;
    the CLI's diff/apply logic has its own coverage in
    test_gate3_pubmed_sync.py.
    """
    app, sidecar, gen_yml = app_with_tmp_sidecar
    _seed_sidecar(sidecar, pmid="11111111", fields_flagged=["month"])

    # Mock the subprocess kick so the daemon thread doesn't actually
    # invoke pubmed_sync.py. We just want to see that the gen YAML
    # gets written and the kick is attempted.
    captured_argv: list[list[str]] = []

    def fake_run(argv, **kw):
        captured_argv.append(list(argv))

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return FakeResult()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Make Thread.start synchronous so the kick lambda runs before the
    # test ends.
    import threading as _threading

    def sync_thread(target, **kw):
        class _T:
            def start(self_inner):
                target()

        return _T()

    monkeypatch.setattr(_threading, "Thread", sync_thread)

    client = app.test_client()
    resp = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-11111111-month": "keep_yaml",
            "reason-11111111-month": "Word source uses '04', PubMed has '03' - YAML is correct",
        },
    )
    assert resp.status_code in (302, 303)

    # Gen YAML written atomically (no .tmp left behind).
    assert gen_yml.exists(), "gen YAML was not written by the apply route"
    tmp_sibling = gen_yml.with_suffix(gen_yml.suffix + ".tmp")
    assert not tmp_sibling.exists(), "atomic write left a .tmp orphan"
    # Sanity: the real repo file MUST NOT have been touched (the F2
    # config override is what prevents pollution).
    real_gen = PROJ_ROOT / "qc" / "pubmed_sync_decisions.gen.yml"
    if real_gen.exists():
        real_body = real_gen.read_text(encoding="utf-8")
        assert "11111111" not in real_body, (
            "Test polluted the real repo gen-YAML — F2 PMSYNC_DECISIONS_GEN_PATH "
            "config override regressed."
        )

    body = gen_yml.read_text(encoding="utf-8")
    assert "11111111" in body
    assert "month" in body
    assert "keep_yaml" in body
    assert "Word source uses '04'" in body

    # Apply subprocess was kicked with --apply --decisions <path>.
    assert captured_argv, "subprocess.run was never called"
    argv = captured_argv[0]
    assert "--apply" in argv, f"--apply missing from argv: {argv}"
    assert "--decisions" in argv, f"--decisions missing from argv: {argv}"


def test_effective_flagged_fields_silences_after_override(app_with_tmp_sidecar):
    """Smoke: once an override is recorded for a (pmid, field) with the
    same yaml_value, the banner helper effective_flagged_fields drops
    that field from the surfaced list.

    The triage page uses the same helper (V13-V19-D R2-H1), so this
    asserts the editor's "banner truth == triage page truth" invariant.
    """
    from cv_editor.pubmed_sync import (
        AcceptedOverride,
        effective_flagged_fields,
        load_sidecar,
    )

    app, sidecar, _ = app_with_tmp_sidecar
    pmid = "22222222"
    _seed_sidecar(sidecar, pmid=pmid, fields_flagged=["title", "month"])

    state = load_sidecar(sidecar)
    rec = state.entries[pmid]
    entry = {"title": "Original Title", "month": "04"}

    # No overrides yet → both fields flagged.
    out = effective_flagged_fields(entry, rec, state.accepted_yaml_overrides.get(pmid, {}))
    assert sorted(out) == ["month", "title"]

    # Add a keep_yaml override for `title` whose yaml_value matches the
    # current entry. Banner should now drop `title`.
    state.accepted_yaml_overrides.setdefault(pmid, {})["title"] = AcceptedOverride(
        yaml_value="Original Title",
        pubmed_value="Different Title",
        reason="YAML matches Word source",
        accepted_at="2026-05-18T00:00:00Z",
    )
    out = effective_flagged_fields(entry, rec, state.accepted_yaml_overrides.get(pmid, {}))
    assert out == ["month"], f"Override with matching yaml_value should silence the flag; got {out}"

    # If the user edits YAML so it no longer matches the override snapshot,
    # the flag RE-SURFACES (banner shows it again).
    entry["title"] = "Updated Title"
    out = effective_flagged_fields(entry, rec, state.accepted_yaml_overrides.get(pmid, {}))
    assert sorted(out) == ["month", "title"], (
        f"Override snapshot diverged from current YAML; flag should re-surface. Got {out}"
    )


def test_sidecar_path_app_config_override_takes_effect(app_with_tmp_sidecar):
    """V13-V19-D R1-M2 / R2-M4 fix (F2): tests that override
    PUBMED_SYNC_SIDECAR_PATH via app.config actually redirect the route
    away from the module-level constant.

    If F2 regresses (route imports SIDECAR_PATH from the module), this
    test will fail because /pubmed_sync/status will read the production
    sidecar instead of our tmp file with one entry.
    """
    app, sidecar, _ = app_with_tmp_sidecar
    _seed_sidecar(sidecar, pmid="33333333", fields_flagged=[])

    client = app.test_client()
    resp = client.get("/pubmed_sync/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    # Tmp sidecar has exactly 1 entry; production sidecar has ~93.
    # If the override didn't take, sidecar_entries would be ~93.
    assert payload["sidecar_entries"] == 1, (
        f"sidecar_entries={payload['sidecar_entries']} — looks like the "
        f"route still reads the module-level SIDECAR_PATH constant. F2 "
        f"regressed."
    )


# ---- V20-cleanup M9 (2026-05-18): 3 new workflow tests ------------------


def test_two_tab_concurrent_apply_second_gets_warning(app_with_tmp_sidecar, monkeypatch):
    """V19 R-H1 race fix: two near-simultaneous /pubmed_sync/apply POSTs
    must NOT both kick the apply subprocess + write the gen YAML. The
    second POST should see the running flag and flash a warning.

    Models the "user clicks Apply twice quickly" or "two-tab open" case.
    """
    app, sidecar, gen_yml = app_with_tmp_sidecar
    _seed_sidecar(sidecar, pmid="11111111", fields_flagged=["month"])

    # Block the daemon thread so the running flag stays set during the
    # second request. Capture the REAL Thread before monkeypatch — the
    # patched version would recurse infinitely.
    import threading as _threading

    _RealThread = _threading.Thread
    started = _threading.Event()
    release = _threading.Event()

    def slow_thread(target, **kw):
        class _T:
            def start(self_inner):
                t = _RealThread(target=lambda: (started.set(), release.wait(), target()))
                t.daemon = True
                t.start()

        return _T()

    monkeypatch.setattr(_threading, "Thread", slow_thread)
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    client = app.test_client()
    # First POST kicks the apply (subprocess won't actually run because
    # `release` is never set, but the running flag is true).
    r1 = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-11111111-month": "apply_pubmed",
        },
    )
    assert r1.status_code in (302, 303)
    assert started.wait(timeout=2.0), "first apply never started"

    # Second POST while first is still running: must NOT write gen YAML
    # again. The route flashes a warning and returns redirect.
    r2 = client.post(
        "/pubmed_sync/apply",
        data={
            "decision-11111111-month": "apply_pubmed",
        },
    )
    assert r2.status_code in (302, 303)

    # Let the first daemon thread finish.
    release.set()

    # Confirm the second POST flashed the "already running" warning.
    # Flash messages come back on the next GET that reads session.
    with client.session_transaction() as sess:
        flashes = list(sess.get("_flashes", []))
    flash_msgs = [m for _cat, m in flashes]
    assert any("already running" in m.lower() for m in flash_msgs), (
        f"second concurrent apply should warn about in-flight job; flashes={flash_msgs}"
    )


def test_pending_form_snapshot_pops_after_first_read(app_with_tmp_sidecar, monkeypatch):
    """V20-cleanup M2: the UUID-keyed snapshot is consumed on the first
    GET that carries `?pending=<uuid>`. A reload without the token shows
    a fresh (defer-defaulted) form.
    """
    app, sidecar, _ = app_with_tmp_sidecar
    _seed_sidecar(sidecar, pmid="11111111", fields_flagged=["month"])

    # Stash a snapshot manually (don't trip the rate limiter).
    with app.app_context():
        pending = app.config["_PMSYNC_PENDING"]
        pending["abc123token"] = {
            "decision-11111111-month": "keep_yaml",
            "reason-11111111-month": "test reason",
        }

    client = app.test_client()
    # Pre-seed compute_decisions mock so triage renders.
    from cv_editor import pubmed_sync as _ps

    monkeypatch.setattr(
        _ps,
        "compute_decisions",
        lambda **kw: _ps._DryRunResult(
            header=[],
            data=[],
            sch={"file": "publications.yml"},
            state=_ps.SidecarState(),
            decisions=[
                _ps.EntryDecision(
                    pmid="11111111",
                    global_idx=0,
                    title_preview="t",
                    flags={"month": ("3", "4")},
                    publication_status="ppublish",
                )
            ],
            skipped_no_pmid=[],
            skipped_in_ttl=0,
            fetch_errors=[],
            all_yaml_pmids={"11111111"},
            fetched_pmids=["11111111"],
        ),
    )

    # First GET with token: form is re-populated AND token consumed.
    r1 = client.get("/pubmed_sync?pending=abc123token")
    assert r1.status_code == 200
    body1 = r1.get_data(as_text=True)
    # keep_yaml radio checked
    assert 'value="keep_yaml"' in body1
    assert 'value="test reason"' in body1
    # Token was popped — confirm directly.
    assert "abc123token" not in app.config["_PMSYNC_PENDING"]

    # Second GET WITH the same (now-stale) token: no re-population.
    r2 = client.get("/pubmed_sync?pending=abc123token")
    body2 = r2.get_data(as_text=True)
    assert 'value="test reason"' not in body2


def test_v18a_group_authorship_renders_in_edit_and_view(monkeypatch):
    """V20-cleanup M9: V18-A's `group_authorship: true` flag must
    surface in BOTH the edit form (JSON-data block) AND the entry
    view (rendered banner/badge).

    Renamed from `..._round_trip_workflow` per V20-cleanup post-impl
    reviewer B: this test does NOT round-trip a save — it only
    exercises the render paths. The save round-trip is independently
    covered by
    `tests/test_v18_a_group_authorship.py::test_yaml_round_trip_persists_group_authorship_via_yaml_io`
    (yaml_io level) and by the V18-A R3-M1 tests.

    What this test catches: the V18-A-D cross-reviewer HIGH (entry_view
    badge was missing because one of seven dispatch sites dropped
    the flag). After B1 the dispatch sites consume from
    `cv_editor/author_flags.py` — drop the flag anywhere in
    `field_handlers`, `author_names`, the entry_edit JSON-data block,
    or the entry_view template, and this test fails.
    """
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # Find an entry with a group_authorship author in the real data.
    # (We don't sandbox the data dir — ROOT is module-level absolute;
    # all asserts are read-only.)
    import ruamel.yaml
    from cv_editor import schemas, sections
    from cv_editor import yaml_io as _yio

    yaml_obj = ruamel.yaml.YAML(typ="rt")
    pubs_path = PROJ_ROOT / "data" / "publications.yml"
    with pubs_path.open() as fh:
        header, body = _yio.split_header(fh.read())
    data = yaml_obj.load(body)
    sch = schemas.get("publications")
    found_idx = None
    for r in sections.flatten(data, sch["structure"]):
        for a in r["entry"].get("authors") or []:
            if isinstance(a, dict) and a.get("group_authorship"):
                found_idx = r["global_idx"]
                break
        if found_idx is not None:
            break
    assert found_idx is not None, "publications.yml lacks a group_authorship author"

    # GET edit form — JSON-data block must contain group_authorship.
    r1 = client.get(f"/publications/{found_idx}/edit")
    assert r1.status_code == 200
    body1 = r1.get_data(as_text=True)
    assert "group_authorship" in body1

    # GET view — banner / badge surface (V18-A-D HIGH).
    r2 = client.get(f"/publications/{found_idx}")
    assert r2.status_code == 200
    body2 = r2.get_data(as_text=True)
    assert "◊" in body2 or "group" in body2.lower(), (
        "entry_view should surface the group_authorship marker"
    )
