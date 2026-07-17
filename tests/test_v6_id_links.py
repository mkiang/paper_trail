"""V6: clickable DOI / PMID / PMCID in entry view + section list.

Unit tests cover the pure id_url filter; smoke tests confirm the
template wires the anchor through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import schemas, sections, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def _id_url(app, value, kind):
    """Invoke the registered id_url Jinja filter directly."""
    return app.jinja_env.filters["id_url"](value, kind)


# ---- pure filter ----


def test_id_url_doi(app):
    assert _id_url(app, "10.9999/nae.2024.2858", "doi") == "https://doi.org/10.9999/nae.2024.2858"


def test_id_url_preprint_doi(app):
    """preprint_doi shares the doi.org pattern — doi.org resolves
    `10.48550/arXiv.NNNN.NNNNN` natively, no special-casing."""
    assert (
        _id_url(app, "10.48550/arXiv.2401.12345", "preprint_doi")
        == "https://doi.org/10.48550/arXiv.2401.12345"
    )


def test_id_url_pmid(app):
    assert _id_url(app, "90002858", "pmid") == "https://pubmed.ncbi.nlm.nih.gov/90002858/"


def test_id_url_pmcid(app):
    assert (
        _id_url(app, "PMC9002858", "pmcid")
        == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9002858/"
    )


def test_id_url_unknown_kind_returns_empty(app):
    assert _id_url(app, "anything", "project") == ""
    assert _id_url(app, "anything", "") == ""


def test_id_url_empty_value_returns_empty(app):
    assert _id_url(app, "", "doi") == ""
    assert _id_url(app, None, "doi") == ""
    assert _id_url(app, "   ", "doi") == ""


def test_id_url_strips_whitespace(app):
    """Defensive: stray whitespace from a hand-edited YAML shouldn't
    break the link."""
    assert _id_url(app, "  10.1/foo  ", "doi") == "https://doi.org/10.1/foo"


# ---- smoke: template wires the anchor through ----


def _find_publication_with_field(field: str) -> int:
    """Return the global_idx of the first publication whose `field` is set."""
    sch = schemas.get("publications")
    _, data = yaml_io.load(ROOT / sch["file"])
    for rec in sections.flatten(data, sch["structure"]):
        v = rec["entry"].get(field)
        if v not in (None, "", []):
            return rec["global_idx"]
    raise AssertionError(f"no publication with {field} found")


def test_entry_view_renders_doi_link(client):
    idx = _find_publication_with_field("doi")
    resp = client.get(f"/publications/{idx}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'href="https://doi.org/' in body
    assert 'class="id-link"' in body
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_entry_view_renders_pmid_link(client):
    idx = _find_publication_with_field("pmid")
    resp = client.get(f"/publications/{idx}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/' in body


def test_entry_view_project_field_not_linked(client):
    """Sanity: `project` (research_support) still renders as plain
    <code>, not as a link, because it has no canonical URL pattern."""
    sch = schemas.get("research_support")
    _, data = yaml_io.load(ROOT / sch["file"])
    for rec in sections.flatten(data, sch["structure"]):
        if rec["entry"].get("project"):
            idx = rec["global_idx"]
            break
    else:
        pytest.skip("no research_support entry with a project number")
    resp = client.get(f"/research_support/{idx}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    project_val = None
    for rec in sections.flatten(data, sch["structure"]):
        if rec["global_idx"] == idx:
            project_val = str(rec["entry"]["project"])
            break
    assert project_val is not None
    assert f'<code>{project_val}</code>' in body
    assert f'href="https://doi.org/{project_val}"' not in body
