"""M5 5b CP3b: the /export/markdown editor route. Read-only, write-free.

EXPORT_DATA_DIR is pointed at the frozen fixture corpus so these assertions
never depend on (or mutate) the live data/. The route only reads + renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import export_core
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "export"


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    a.config["EXPORT_DATA_DIR"] = str(FIXTURE)
    return a.test_client()


def test_markdown_route_downloads_attachment(client):
    r = client.get("/export/markdown")
    assert r.status_code == 200
    assert r.mimetype == "text/markdown"
    assert 'attachment; filename="cv.md"' in r.headers["Content-Disposition"]
    body = r.get_data(as_text=True)
    assert "# Jane Q Public" in body  # header from the fixture meta
    assert "## Scholarly Publications" in body
    assert "**Public JQ**" in body  # self-bold survived the route


def test_markdown_route_leak_guard(client):
    """The public (fullcv) export must omit every hidden fixture item."""
    body = client.get("/export/markdown").get_data(as_text=True)
    for leaked in (
        "hidden from public",
        "Secret J",
        "Hidden honor",
        "Secret Org",
        "A pending grant",
        "Future Agency",
        "A hidden talk",
        "Secret Venue",
        "Hidden Service",
        "Hidden Appointment",
        "Hidden Mentee",
        "Secret Person",
    ):
        assert leaked not in body, f"LEAK: {leaked!r} reached the public export route"


def test_tools_nav_has_export_link(client):
    body = client.get("/").get_data(as_text=True)
    assert "/export/markdown" in body  # Tools-menu link


def test_markdown_route_variant_param_accepted(client):
    r = client.get("/export/markdown?variant=fullcv")
    assert r.status_code == 200
    assert "# Jane Q Public" in r.get_data(as_text=True)


def test_markdown_route_failure_flashes_and_redirects(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(export_core, "build_model", _boom)
    r = client.get("/export/markdown")
    assert r.status_code == 500
    assert r.headers["Location"].endswith("/")  # redirect to index


# ---------- /export/html ----------


def test_html_route_downloads_attachment(client):
    r = client.get("/export/html")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    assert 'attachment; filename="cv.html"' in r.headers["Content-Disposition"]
    body = r.get_data(as_text=True)
    assert "<!doctype html>" in body
    assert "<h1>Jane Q Public</h1>" in body
    assert "<strong>Public JQ</strong>" in body  # self-bold survived the route
    assert "View variant: fullcv" in body  # audited public view surfaced


def test_html_route_leak_guard(client):
    body = client.get("/export/html").get_data(as_text=True)
    for leaked in (
        "hidden from public",
        "Secret J",
        "Hidden honor",
        "Secret Org",
        "A pending grant",
        "Future Agency",
        "A hidden talk",
        "Secret Venue",
        "Hidden Service",
        "Hidden Appointment",
        "Hidden Mentee",
        "Secret Person",
    ):
        assert leaked not in body, f"LEAK: {leaked!r} reached the public export route"


def test_tools_nav_has_html_export_link(client):
    body = client.get("/").get_data(as_text=True)
    assert "/export/html" in body


def test_html_route_failure_flashes_and_redirects(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(export_core, "build_model", _boom)
    r = client.get("/export/html")
    assert r.status_code == 500
    assert r.headers["Location"].endswith("/")
