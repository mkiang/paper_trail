"""DOI→PMID backfill for DOI-only publications (scripts/pubmed_sync.py).

Covers the 2026-07-11 feature: PubMed sync resolves a newly-assigned PMID
for a publication entered with a `doi` but no `pmid`, then auto-fills the
ids on apply. See gotcha #81.

All network is mocked at the module-attribute seam
(`pubmed_client.find_pmid_by_doi` / `fetch_pubmed_batch`); no test hits
the wire. Write tests redirect PUBS_PATH / SIDECAR_PATH / BACKUP_DIR to
tmp so the real corpus + sidecar are never touched (conftest corruption
canary watches both).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cv_editor import pubmed_client  # noqa: E402
from cv_editor import pubmed_sync as ps  # noqa: E402

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


# ---------------- helpers ----------------


def _pm_record(**overrides):
    base = dict(
        title="A clear informative title about mortality.",
        journal_full="Journal of X",
        journal_iso="J X",
        volume="35",
        issue="3",
        pages="100-110",
        year="2025",
        month=3,
        day=15,
        authors=["Public JQ"],
        doi="10.1000/example",
        pmcid="PMC7654321",
        publication_status="ppublish",
    )
    base.update(overrides)
    return base


def _pubs_file(tmp_path, entries):
    """Write a one-subsection publications.yml. `entries` is a list of
    dicts with any of: title, journal, doi, pmid, preprint_doi."""
    lines = [
        "# header",
        "- subsection: 'Peer-Reviewed Original Research'",
        "  entries:",
    ]
    for e in entries:
        lines.append(
            f"    - title: '{e.get('title', 'A clear informative title about mortality.')}'"
        )
        lines.append(f"      journal: '{e.get('journal', 'Journal of X')}'")
        lines.append("      year: 2025")
        lines.append("      authors: ['Public JQ']")
        if e.get("pmid"):
            lines.append(f"      pmid: '{e['pmid']}'")
        if e.get("doi"):
            lines.append(f"      doi: '{e['doi']}'")
        if e.get("preprint_doi"):
            lines.append(f"      preprint_doi: '{e['preprint_doi']}'")
    p = tmp_path / "publications.yml"
    p.write_text("\n".join(lines) + "\n")
    return p


def _run(tmp_path, pubs, sidecar, **kw):
    kw.setdefault("resolve_dois", True)
    kw.setdefault("now", NOW)
    return ps.compute_decisions(
        pubs_path=pubs,
        sidecar_path=sidecar,
        cache_dir=tmp_path / "cache",
        **kw,
    )


# ---------------- happy path ----------------


def test_doi_resolve_happy_path_fills_pmid(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    seen = {}

    def fake_find(doi, **kw):
        seen["doi"] = doi
        seen["kw"] = kw
        return ("40012345", [])

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", fake_find)
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {"40012345": _pm_record(doi="10.1000/example")},
    )

    res = _run(tmp_path, pubs, sc)

    # esearch was live (cache-bypassed) and no-PII.
    assert seen["kw"].get("use_cache") is False
    assert seen["kw"].get("ua") == ps.UA
    assert seen["kw"].get("raise_on_error") is True
    assert seen["doi"] == "10.1000/example"

    assert len(res.decisions) == 1
    dec = res.decisions[0]
    assert dec.resolved_from_doi is True
    assert dec.fills.get("pmid") == "40012345"
    assert dec.fills.get("pmcid") == "PMC7654321"
    # provenance row + sidecar state.
    assert res.resolved and res.resolved[0][3] == "40012345"
    assert res.resolution_changed is True
    assert res.state.doi_resolve_state["10.1000/example"]["status"] == "resolved"
    assert res.state.doi_resolve_state["10.1000/example"]["pmid"] == "40012345"


# ---------------- safety guards ----------------


def test_doi_resolve_ambiguous_alternates_needs_review(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    fetches = []

    monkeypatch.setattr(
        pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", ["40099999"])
    )
    monkeypatch.setattr(
        pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: fetches.append(list(pmids)) or {}
    )

    res = _run(tmp_path, pubs, sc)

    assert res.decisions == []  # never auto-filled
    assert fetches == []  # guard short-circuits before any efetch
    st = res.state.doi_resolve_state["10.1000/example"]
    assert st["status"] == "needs_review"
    assert st["candidate_pmid"] == "40012345"
    assert any("ambiguous/collision" in r[2] for r in res.skipped_no_pmid)


def test_doi_resolve_title_mismatch_needs_review(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example", "title": "Alpha beta gamma delta"}])
    sc = tmp_path / "sidecar.json"

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", []))
    # Record's own DOI differs AND title is unrelated → cannot verify.
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {
            "40012345": _pm_record(doi="10.9999/other", title="Zeta eta theta iota")
        },
    )

    res = _run(tmp_path, pubs, sc)

    assert res.decisions == []
    st = res.state.doi_resolve_state["10.1000/example"]
    assert st["status"] == "needs_review"
    assert st["candidate_pmid"] == "40012345"
    assert any("could not verify" in r[2] for r in res.skipped_no_pmid)


def test_doi_resolve_verifies_on_title_overlap_when_record_lacks_doi(tmp_path, monkeypatch):
    """Fallback guard: record carries no DOI, but the title matches → resolved."""
    pubs = _pubs_file(
        tmp_path,
        [{"doi": "10.1000/example", "title": "A clear informative title about mortality."}],
    )
    sc = tmp_path / "sidecar.json"

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", []))
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {
            "40012345": _pm_record(doi="", title="A clear informative title about mortality.")
        },
    )

    res = _run(tmp_path, pubs, sc)
    assert len(res.decisions) == 1
    assert res.decisions[0].fills.get("pmid") == "40012345"


def test_doi_resolve_collision_with_existing_pmid_needs_review(tmp_path, monkeypatch):
    # Entry A already uses PMID 40012345; entry B (doi-only) resolves to it.
    pubs = _pubs_file(
        tmp_path,
        [
            {"pmid": "40012345", "title": "Existing paper"},
            {"doi": "10.1000/example", "title": "Different paper"},
        ],
    )
    sc = tmp_path / "sidecar.json"

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", []))
    monkeypatch.setattr(
        pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {"40012345": _pm_record()}
    )

    res = _run(tmp_path, pubs, sc)
    st = res.state.doi_resolve_state["10.1000/example"]
    assert st["status"] == "needs_review"
    # No decision carries the collision pmid as a DOI-resolved fill.
    assert not any(d.resolved_from_doi for d in res.decisions)


def test_preprint_doi_entry_never_esearched(tmp_path, monkeypatch):
    # medRxiv DOI prefix → is_preprint True → promote flow owns it.
    pubs = _pubs_file(tmp_path, [{"doi": "10.1101/2025.01.01.99999", "journal": "medRxiv"}])
    sc = tmp_path / "sidecar.json"
    called = {"v": False}

    def boom(*a, **k):
        called["v"] = True
        return (None, [])

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", boom)
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    res = _run(tmp_path, pubs, sc)
    assert called["v"] is False
    assert res.state.doi_resolve_state == {}


# ---------------- TTL throttle ----------------


def test_doi_resolve_ttl_throttles_reattempt(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    n = {"find": 0}

    monkeypatch.setattr(
        pubmed_client,
        "find_pmid_by_doi",
        lambda doi, **kw: (n.__setitem__("find", n["find"] + 1), (None, []))[1],
    )
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    # Run 1: no prior state → due → esearch → records no_record @ NOW.
    r1 = _run(tmp_path, pubs, sc, now=NOW)
    ps.save_sidecar(sc, r1.state)
    assert n["find"] == 1

    # Run 2: within TTL → not due → no esearch.
    _run(tmp_path, pubs, sc, now=NOW + timedelta(days=13))
    assert n["find"] == 1

    # Run 3: past TTL → due again.
    r3 = _run(tmp_path, pubs, sc, now=NOW + timedelta(days=15))
    ps.save_sidecar(sc, r3.state)
    assert n["find"] == 2


def test_force_resolve_ignores_ttl(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    n = {"find": 0}
    monkeypatch.setattr(
        pubmed_client,
        "find_pmid_by_doi",
        lambda doi, **kw: (n.__setitem__("find", n["find"] + 1), (None, []))[1],
    )
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    r1 = _run(tmp_path, pubs, sc, now=NOW)
    ps.save_sidecar(sc, r1.state)
    assert n["find"] == 1
    # 1 day later, well within TTL, but force_resolve overrides.
    _run(tmp_path, pubs, sc, now=NOW + timedelta(days=1), force_resolve=True)
    assert n["find"] == 2


# ---------------- safe default (editor read path) ----------------


def test_resolve_dois_false_never_esearches(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    called = {"v": False}
    monkeypatch.setattr(
        pubmed_client,
        "find_pmid_by_doi",
        lambda *a, **k: (called.__setitem__("v", True), (None, []))[1],
    )
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    res = _run(tmp_path, pubs, sc, resolve_dois=False)
    assert called["v"] is False
    assert any("re-check pending" in r[2] for r in res.skipped_no_pmid)


def test_resolve_dois_false_still_applies_prior_resolution(tmp_path, monkeypatch):
    """The editor read path (and --apply) must surface + fill a DOI that a
    prior background dry-run already resolved, WITHOUT esearching."""
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    sc.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {},
                "no_pmid_skip_log": {},
                "accepted_yaml_overrides": {},
                "doi_resolve_state": {
                    "10.1000/example": {
                        "last_attempt": "2026-07-01T00:00:00+00:00",
                        "status": "resolved",
                        "pmid": "40012345",
                        "candidate_pmid": None,
                        "overlap": 0.9,
                    }
                },
            }
        )
    )
    called = {"v": False}
    monkeypatch.setattr(
        pubmed_client,
        "find_pmid_by_doi",
        lambda *a, **k: (called.__setitem__("v", True), (None, []))[1],
    )
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {"40012345": _pm_record(doi="10.1000/example")},
    )

    res = _run(tmp_path, pubs, sc, resolve_dois=False)
    assert called["v"] is False
    assert len(res.decisions) == 1
    assert res.decisions[0].resolved_from_doi is True
    assert res.decisions[0].fills.get("pmid") == "40012345"


# ---------------- no-record + transient-error non-poison ----------------


def test_no_record_records_attempt(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    seen = {}

    def fake_find(doi, **kw):
        seen.update(kw)
        return (None, [])

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", fake_find)
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    res = _run(tmp_path, pubs, sc)
    assert seen.get("use_cache") is False
    assert seen.get("ua") == ps.UA
    st = res.state.doi_resolve_state["10.1000/example"]
    assert st["status"] == "no_record"
    assert st["last_attempt"] == NOW.isoformat()


def test_transient_network_error_does_not_record_attempt(tmp_path, monkeypatch):
    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"

    def boom(doi, **kw):
        raise RuntimeError("simulated network blip")

    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", boom)
    monkeypatch.setattr(pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})

    res = _run(tmp_path, pubs, sc)
    # No attempt recorded → not frozen for the TTL; retried next run.
    assert res.state.doi_resolve_state == {}
    assert res.resolution_changed is False


# ---------------- sidecar round-trip / back-compat ----------------


def test_sidecar_doi_resolve_state_roundtrip(tmp_path):
    st = ps.SidecarState(
        doi_resolve_state={
            "10.x/y": {
                "last_attempt": "2026-07-01T00:00:00+00:00",
                "status": "resolved",
                "pmid": "1",
                "candidate_pmid": None,
                "overlap": 0.9,
            }
        }
    )
    p = tmp_path / "s.json"
    ps.save_sidecar(p, st)
    loaded = ps.load_sidecar(p)
    assert loaded.doi_resolve_state["10.x/y"]["pmid"] == "1"
    assert loaded.doi_resolve_state["10.x/y"]["status"] == "resolved"


def test_old_sidecar_without_doi_resolve_state_loads_empty(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(
        '{"version": 1, "entries": {}, "no_pmid_skip_log": {}, "accepted_yaml_overrides": {}}'
    )
    loaded = ps.load_sidecar(p)
    assert loaded.doi_resolve_state == {}
    # version is NOT bumped → existing keys still load.
    assert loaded.entries == {}


# ---------------- end-to-end apply writes a quoted pmid ----------------


def test_end_to_end_apply_writes_quoted_pmid(tmp_path, monkeypatch):
    from cv_editor import yaml_io

    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    report = tmp_path / "qc" / "report.md"
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sc)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", []))
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {"40012345": _pm_record(doi="10.1000/example")},
    )

    # Dry-run resolves + records the sidecar; never writes YAML.
    before = pubs.read_text()
    assert ps.main(["--dry-run", "--quiet"]) == 0
    assert pubs.read_text() == before  # YAML untouched by dry-run
    assert ps.load_sidecar(sc).doi_resolve_state["10.1000/example"]["status"] == "resolved"

    # Apply writes the discovered ids.
    assert ps.main(["--apply", "--quiet"]) == 0
    text = pubs.read_text()
    assert "40012345" in text
    assert ("pmid: '40012345'" in text) or ('pmid: "40012345"' in text), text
    assert "PMC7654321" in text


# ---------------- route safety gate ----------------


def test_pubmed_sync_route_never_esearches(monkeypatch):
    """GET /pubmed_sync must not fire a live esearch on the request thread
    (the editor passes resolve_dois=False)."""
    from cv_editor import pubmed_sync

    called = {"v": False}
    monkeypatch.setattr(pubmed_sync.pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})
    monkeypatch.setattr(
        pubmed_sync.pubmed_client,
        "find_pmid_by_doi",
        lambda *a, **k: (called.__setitem__("v", True), (None, []))[1],
    )

    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    resp = app.test_client().get("/pubmed_sync")
    assert resp.status_code == 200
    assert called["v"] is False


# ---------------- "Apply auto-fills" button (commit without a flag decision) ----------------


def test_apply_autofills_route_writes_empty_decisions_and_redirects(tmp_path, monkeypatch):
    """POST /pubmed_sync/apply_autofills writes an EMPTY decisions file
    (so --apply commits only auto-fills) and redirects — without a
    keep/apply flag decision. Kicker thread is stubbed so no subprocess
    fires; gen path redirected to tmp so no real file is touched."""
    import threading as _threading

    from cv_editor import pubmed_sync

    monkeypatch.setattr(pubmed_sync.pubmed_client, "fetch_pubmed_batch", lambda pmids, **kw: {})
    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    gen = tmp_path / "gen.yml"
    app.config["PMSYNC_DECISIONS_GEN_PATH"] = gen

    resp = app.test_client().post("/pubmed_sync/apply_autofills", data={})
    assert resp.status_code in (302, 303)
    assert "/pubmed_sync" in resp.headers["Location"]
    assert gen.exists()
    import yaml as pyyaml

    assert pyyaml.safe_load(gen.read_text()) == {"decisions": []}


def test_apply_with_empty_decisions_file_writes_autofills(tmp_path, monkeypatch):
    """The mechanism behind the button: --apply with an empty decisions
    file still writes every pending auto-fill (incl. a resolved PMID)."""
    import yaml as pyyaml
    from cv_editor import yaml_io

    pubs = _pubs_file(tmp_path, [{"doi": "10.1000/example"}])
    sc = tmp_path / "sidecar.json"
    report = tmp_path / "qc" / "report.md"
    monkeypatch.setattr(ps, "PUBS_PATH", pubs)
    monkeypatch.setattr(ps, "SIDECAR_PATH", sc)
    monkeypatch.setattr(ps, "REPORT_PATH", report)
    monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(pubmed_client, "find_pmid_by_doi", lambda doi, **kw: ("40012345", []))
    monkeypatch.setattr(
        pubmed_client,
        "fetch_pubmed_batch",
        lambda pmids, **kw: {"40012345": _pm_record(doi="10.1000/example")},
    )

    assert ps.main(["--dry-run", "--quiet"]) == 0  # resolves + records sidecar
    decfile = tmp_path / "gen.yml"
    decfile.write_text(pyyaml.safe_dump({"decisions": []}))
    assert ps.main(["--apply", "--quiet", "--decisions", str(decfile)]) == 0
    text = pubs.read_text()
    assert ("pmid: '40012345'" in text) or ('pmid: "40012345"' in text), text


def test_pending_autofills_section_renders():
    """The 'Pending auto-fills' template branch (+ pluralize/selectattr and
    the Apply button) renders without a Jinja error when autofill_rows is
    non-empty. Hermetic: injects the context directly, no data/network."""
    from cv_editor.app import create_app
    from flask import render_template

    app = create_app()
    app.config["TESTING"] = True
    row = {
        "pmid": "40012345",
        "global_idx": 0,
        "title_preview": "A resolved paper",
        "resolved_from_doi": True,
        "fills": [
            {"field": "pmid", "value": "40012345"},
            {"field": "pmcid", "value": "PMC7654321"},
        ],
    }
    status = {
        "running_dryrun": False,
        "running_apply": False,
        "sidecar_entries": 3,
        "accepted_overrides": 0,
        "report_mtime": None,
        "report_url": None,
    }
    with app.test_request_context("/pubmed_sync"):
        html = render_template(
            "pubmed_sync.html",
            status=status,
            triage_rows=[],
            triage_error=None,
            cross_silenced_rows=[],
            autofill_rows=[row],
            pending_form={},
        )
    assert "Pending auto-fills (1)" in html
    assert "Apply auto-fills (1)" in html
    assert "/pubmed_sync/apply_autofills" in html
    # the resolved-from-DOI intro branch rendered (selectattr + pluralize).
    assert "newly-resolved" in html
