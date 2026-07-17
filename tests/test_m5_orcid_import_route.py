"""M5 5b CP5b: the ORCID import-tab UI (`source=="orcid"` on /publications/import).

Discovery only — the route fetches the ORCID works list, partitions against the
CV, and renders a table whose "new" rows fire the EXISTING single-ID `doi_pmid`
import. It NEVER writes. `orcid_client.fetch_works` is mocked at the module seam
so there is ZERO real network; the partition reads the live data/publications.yml
read-only (the in-CV assertion pulls a real DOI from the corpus so it can't drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import orcid_client, sections, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent
VALID_ID = "0000-0002-1825-0097"


# ---------- ORCID /works fixture shape (mirrors test_m5_orcid_import) ----------


def _eid(t, value, *, rel="self", norm=None):
    d = {"external-id-type": t, "external-id-value": value, "external-id-relationship": rel}
    if norm is not None:
        d["external-id-normalized"] = {"value": norm, "transient": True}
    return d


def _group(eids, *, title="A work", put=100):
    ws = [{"put-code": put, "title": {"title": {"value": title}}, "type": "journal-article"}]
    return {"external-ids": {"external-id": eids}, "work-summary": ws}


def _works(*groups):
    return {"group": list(groups)}


def _first_corpus_doi() -> str:
    """A real DOI from data/publications.yml so the in-CV bucket assertion is
    robust to corpus edits (the corpus always has DOI-bearing entries)."""
    _, data = yaml_io.load(ROOT / "data" / "publications.yml")
    for rec in sections.flatten(data, "list_of_subsections"):
        doi = (rec["entry"].get("doi") or "").strip()
        if doi:
            return doi
    pytest.skip("no DOI-bearing entry in the corpus")


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


# ---------- the import page exposes the tab ----------


def test_import_page_has_orcid_tab(client):
    body = client.get("/publications/import").get_data(as_text=True)
    assert 'data-tab="orcid"' in body
    assert 'name="orcid_id"' in body
    assert 'value="orcid"' in body  # hidden source field


# ---------- discovery: new + no-id buckets (mocked fetch) ----------


def test_orcid_discover_renders_partition(client, monkeypatch):
    works = _works(
        _group([_eid("doi", "10.9999/orcid-route-new-a")], title="Brand new paper A", put=1),
        _group([_eid("doi", "10.9999/orcid-route-new-b")], title="Brand new paper B", put=2),
        _group([_eid("source-work-id", "x")], title="No usable id work", put=3),
    )
    monkeypatch.setattr(orcid_client, "fetch_works", lambda *a, **k: works)
    r = client.post("/publications/import", data={"source": "orcid", "orcid_id": VALID_ID})
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    assert "3 works discovered" in body
    assert "Brand new paper A" in body and "Brand new paper B" in body
    assert "No usable id work" in body
    assert "New <span" in body and "No usable ID <span" in body
    # Each "new" row fires the existing single-ID import.
    assert 'name="source" value="doi_pmid"' in body
    assert 'value="10.9999/orcid-route-new-a"' in body
    # The no-id bucket offers the manual-add affordance.
    assert "Add manually" in body


def test_orcid_discover_marks_existing_in_cv(client, monkeypatch):
    doi = _first_corpus_doi()
    works = _works(_group([_eid("doi", doi)], title="Already have this one", put=9))
    monkeypatch.setattr(orcid_client, "fetch_works", lambda *a, **k: works)
    body = client.post(
        "/publications/import", data={"source": "orcid", "orcid_id": VALID_ID}
    ).get_data(as_text=True)
    assert "Already have this one" in body
    assert "Already in CV <span" in body
    # An in-CV row is informational only — no `doi_pmid` import sub-form for it
    # (the only place the DOI appears as a form `value="…"` would be a new row).
    assert f'value="{doi.lower()}"' not in body


def test_orcid_discover_escapes_hostile_title(client, monkeypatch):
    """A WorkRef.title comes from the ORCID API (attacker-influenceable). It must
    be HTML-escaped — a regression guard against a future `|safe` in the template."""
    works = _works(_group([_eid("doi", "10.9999/xss")], title="<script>alert(1)</script>", put=7))
    monkeypatch.setattr(orcid_client, "fetch_works", lambda *a, **k: works)
    body = client.post(
        "/publications/import", data={"source": "orcid", "orcid_id": VALID_ID}
    ).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body  # NOT rendered as a live tag
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body  # escaped instead


# ---------- failure paths flash + redirect, never render the table ----------


def test_orcid_invalid_id_flashes_and_redirects(client, monkeypatch):
    def must_not_fetch(*a, **k):
        raise AssertionError("fetch_works called on a malformed iD")

    monkeypatch.setattr(orcid_client, "fetch_works", must_not_fetch)
    r = client.post("/publications/import", data={"source": "orcid", "orcid_id": "not-an-orcid"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/publications/import")


def test_orcid_fetch_failure_flashes_and_redirects(client, monkeypatch):
    monkeypatch.setattr(orcid_client, "fetch_works", lambda *a, **k: None)
    r = client.post("/publications/import", data={"source": "orcid", "orcid_id": VALID_ID})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/publications/import")


def test_orcid_route_is_write_free(client, monkeypatch):
    """The discovery POST must never touch the YAML writer."""
    from cv_editor import yaml_io as _yio

    monkeypatch.setattr(
        orcid_client, "fetch_works", lambda *a, **k: _works(_group([_eid("doi", "10.9999/x")]))
    )

    def _no_write(*a, **k):
        raise AssertionError("discovery route wrote YAML")

    monkeypatch.setattr(_yio, "write_with_backup", _no_write)
    r = client.post("/publications/import", data={"source": "orcid", "orcid_id": VALID_ID})
    assert r.status_code == 200
