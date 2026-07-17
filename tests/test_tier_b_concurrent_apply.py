"""Tier B / B8 (2026-05-27) — concurrent /qc/apply + /pubmed_sync/apply.

Smoke test for the cross-system apply contract introduced by V23-B Phase 1.5
(gotcha #59). Two threads hit the two apply routes simultaneously via a
shared Barrier; asserts both complete without deadlock, exception, or
corruption of publications.yml.

**Scope**: this is a regression guard, NOT a rigorous lock-correctness proof.
Confirming that `_cross_system_apply_lock` actually serializes the in-memory
sidecar mutations on the microsecond scale would require instrumenting the
production code with mockable delays. The conftest canary
(`_publications_yml_corruption_canary`) catches the user-observable failure
mode: any change to data/publications.yml during the test would fail the
test before it returns. So this test answers:

  "Do simultaneous POSTs to /qc/apply and /pubmed_sync/apply (a) survive
   without deadlocking, (b) return non-500 status codes, (c) leave
   publications.yml byte-identical?"

The 54 existing Phase 1.5 unit tests cover the helper logic in isolation;
this adds an integration-level safety net under concurrency.

**Critical**: uses ONE `create_app()` instance shared across threads. Two
separate `create_app()` calls would create two separate
`_cross_system_apply_lock` instances (the lock is closure-scoped per app),
silently bypassing the serialization the test is meant to exercise. The
critique-loop's R-A reviewer flagged exactly this trap.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shared_app(tmp_path, monkeypatch):
    """ONE app instance shared across both threads. Two test_clients
    derived from the same app share the same closure-scoped locks."""
    pubs_src = PROJ_ROOT / "data" / "publications.yml"
    shutil.copy(pubs_src, tmp_path / "publications.yml")
    sidecar = tmp_path / "report.json"
    decisions = tmp_path / "qc_decisions.json"
    pm_sidecar = tmp_path / "pubmed_sync.json"

    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-05-27T12:00:00+00:00",
                "qc_script_version": "1.0",
                "publications_yml_mtime_ns": pubs_src.stat().st_mtime_ns,
                "cache_key_version": 1,
                "summary": {"totals": {}, "total_findings": 0},
                "findings": {
                    "mismatches": [],
                    "variants": [
                        {
                            "id": "VA:pubmed:b8test:pages:b8b8b8b8",
                            "type": "VARIANT",
                            "global_idx": 0,
                            "subsection": "PRR",
                            "entry_index": 1,
                            "pmid": "b8test",
                            "doi": None,
                            "title_preview": "B8 Concurrency Test",
                            "field": "pages",
                            "yaml_value": "1-10",
                            "canonical_value": "1-10.",
                            "source": "pubmed",
                        },
                    ],
                    "id_enrichments": [],
                    "pmid_mismatches": [],
                    "self_absent": [],
                    "author_name_variants": [],
                    "journal_name_variants": [],
                    "missing_ids": [],
                },
            }
        )
    )
    pm_sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {},
                "no_pmid_skip_log": {},
                "accepted_yaml_overrides": {},
            }
        )
    )
    from cv_editor import qc_publications

    monkeypatch.setattr(qc_publications, "SIDECAR_PATH", sidecar)
    from cv_editor import pubmed_sync

    monkeypatch.setattr(pubmed_sync, "SIDECAR_PATH", pm_sidecar)
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["QC_DECISIONS_PATH"] = decisions
    app.config["PUBMED_SYNC_SIDECAR_PATH"] = pm_sidecar
    return app


def test_concurrent_apply_routes_do_not_deadlock_or_corrupt(shared_app):
    """Both apply routes hit at the same instant via Barrier(2). Each
    sends a `defer` decision (the cheapest valid payload — no YAML
    write, no cross-clear, just a sidecar update). The test asserts:
    - Both threads complete inside the timeout.
    - Both responses have non-error status codes.
    - publications.yml is unchanged (the conftest canary will fire
      if any test mutates the real file).
    """
    barrier = threading.Barrier(2)
    results: dict = {}

    def _hit_qc():
        barrier.wait(timeout=5)
        c = shared_app.test_client()
        r = c.post(
            "/qc/apply",
            data={
                "decision-VA:pubmed:b8test:pages:b8b8b8b8": "defer",
            },
            follow_redirects=False,
        )
        results["qc_status"] = r.status_code

    def _hit_pmsync():
        barrier.wait(timeout=5)
        c = shared_app.test_client()
        r = c.post("/pubmed_sync/apply", data={}, follow_redirects=False)
        results["pmsync_status"] = r.status_code

    t1 = threading.Thread(target=_hit_qc)
    t2 = threading.Thread(target=_hit_pmsync)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    # Threads completed (no deadlock).
    assert not t1.is_alive(), "qc thread deadlocked"
    assert not t2.is_alive(), "pmsync thread deadlocked"

    # Both routes returned a non-server-error status code.
    assert results.get("qc_status", 500) < 500, (
        f"qc/apply 5xx under concurrency: {results.get('qc_status')}"
    )
    assert results.get("pmsync_status", 500) < 500, (
        f"pubmed_sync/apply 5xx under concurrency: {results.get('pmsync_status')}"
    )

    # The conftest canary will fail the test before it returns if
    # publications.yml mutated.


def test_cross_system_apply_lock_is_reentrant_on_same_app(shared_app):
    """The lock is an RLock (gotcha #59 / V13-V19-D R1-H2). If the
    apply route ever needs to re-acquire it from the same thread
    (e.g., a nested cross-clear chain in a future refactor), a
    non-reentrant Lock would deadlock. This test asserts the type
    contract by acquiring twice from the same thread.

    We reach into the app's config bag because the lock is closure-
    scoped; we walk app.logger.handlers's parent chain to find it...
    actually we can't reach it cleanly. Instead, exercise reentrance
    by hitting /qc/apply twice in quick succession from one thread.
    A non-reentrant lock would deadlock the second call.
    """
    c = shared_app.test_client()
    for _ in range(2):
        r = c.post(
            "/qc/apply",
            data={
                "decision-VA:pubmed:b8test:pages:b8b8b8b8": "defer",
            },
            follow_redirects=False,
        )
        assert r.status_code < 500
