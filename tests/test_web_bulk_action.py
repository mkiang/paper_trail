"""A2: bulk `set web: show/hide` on the publications list.

POST /publications/bulk with bulk_action=set_web_show / set_web_hide writes an
explicit `web:` value onto every selected entry. `web` is read only by the
website exporter and is inert in the Typst renderer, so these actions never
touch the CV PDFs. Runs against the conftest-isolated tmp corpus (writes never
reach the real data/).
"""

from __future__ import annotations

import pytest
from cv_editor import paths, schemas, sections, yaml_io
from cv_editor.app import create_app


def _pubs_path():
    return paths.data_dir() / "publications.yml"


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _mtime_ns():
    return yaml_io.mtime_ns(_pubs_path())


def _first_two_global_idxs():
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    recs = list(sections.flatten(data, sch["structure"]))
    return recs[0]["global_idx"], recs[1]["global_idx"]


def _entry_at(idx: int):
    sch = schemas.get("publications")
    _, data = yaml_io.load(_pubs_path())
    rec = sections.locate(data, sch["structure"], idx)
    return rec["entry"]


def test_bulk_set_web_show(client):
    a, b = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_web_show",
            "selected": [str(a), str(b)],
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _entry_at(a).get("web") == "show"
    assert _entry_at(b).get("web") == "show"


def test_bulk_set_web_hide(client):
    a, _ = _first_two_global_idxs()
    resp = client.post(
        "/publications/bulk",
        data={
            "bulk_action": "set_web_hide",
            "selected": [str(a)],
            "mtime_ns": str(_mtime_ns()),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert _entry_at(a).get("web") == "hide"


def test_bulk_web_show_then_hide_overwrites(client):
    a, _ = _first_two_global_idxs()
    client.post(
        "/publications/bulk",
        data={"bulk_action": "set_web_show", "selected": [str(a)], "mtime_ns": str(_mtime_ns())},
    )
    assert _entry_at(a).get("web") == "show"
    client.post(
        "/publications/bulk",
        data={"bulk_action": "set_web_hide", "selected": [str(a)], "mtime_ns": str(_mtime_ns())},
    )
    assert _entry_at(a).get("web") == "hide"


def test_bulk_web_does_not_touch_highlighted(client):
    """web: is orthogonal to the render-gate `highlighted:` — a web action
    must not add/remove highlighted."""
    a, _ = _first_two_global_idxs()
    before = "highlighted" in _entry_at(a)
    client.post(
        "/publications/bulk",
        data={"bulk_action": "set_web_hide", "selected": [str(a)], "mtime_ns": str(_mtime_ns())},
    )
    assert ("highlighted" in _entry_at(a)) == before


def test_section_list_renders_web_bulk_options(client):
    body = client.get("/publications").get_data(as_text=True)
    assert 'value="set_web_show"' in body
    assert 'value="set_web_hide"' in body
