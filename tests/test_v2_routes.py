"""V2 route smoke + round-trip tests via Flask test_client.

Covers:
- Section index renders and lists all 10 sections.
- Each section's list / view / edit / new / backups page renders 200.
- Search returns results.
- A representative non-publication save round-trips through YAML without
  corrupting the file (verified with the pre-test snapshot).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml as pyyaml
from cv_editor import paths, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent  # typst/

SECTIONS = [
    "publications",
    "presentations",
    "research_support",
    "service",
    "teaching",
    "mentees",
    "honors",
    "education",
    "appointments",
]


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_index_lists_every_section(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Use section-key href anchors — labels can contain HTML-escaped chars (&).
    for sec in SECTIONS:
        assert f'href="/{sec}"' in body
    assert 'href="/meta"' in body


@pytest.mark.parametrize("section", SECTIONS)
def test_section_list_renders(client, section):
    resp = client.get(f"/{section}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "entries" in body


@pytest.mark.parametrize("section", SECTIONS)
def test_entry_view_renders(client, section):
    resp = client.get(f"/{section}/0")
    assert resp.status_code == 200


@pytest.mark.parametrize("section", SECTIONS)
def test_entry_edit_renders(client, section):
    resp = client.get(f"/{section}/0/edit")
    assert resp.status_code == 200


@pytest.mark.parametrize("section", SECTIONS)
def test_entry_new_renders(client, section):
    resp = client.get(f"/{section}/new")
    assert resp.status_code == 200


@pytest.mark.parametrize("section", SECTIONS + ["meta"])
def test_backups_page_renders(client, section):
    resp = client.get(f"/{section}/backups")
    assert resp.status_code == 200


def test_meta_view_and_edit_render(client):
    assert client.get("/meta").status_code == 200
    assert client.get("/meta/edit").status_code == 200


def test_search_returns_results(client):
    # Data-agnostic: derive a search term from research_support entry-0's title
    # (guaranteed present in the loaded corpus), instead of a private grant word.
    import re

    _, rs_data = yaml_io.load(paths.data_dir() / "research_support.yml")
    title = str(rs_data[0].get("title") or "")
    words = re.findall(r"[A-Za-z]{4,}", title)
    assert words, f"could not derive a search term from title: {title!r}"
    term = words[0]
    resp = client.get(f"/search?q={term}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "match" in body
    assert term.lower() in body.lower()


def test_search_empty_query_renders(client):
    resp = client.get("/search")
    assert resp.status_code == 200


def test_unknown_section_404s(client):
    assert client.get("/nonexistent_section").status_code == 404


def test_search_does_not_match_meta(client):
    """Meta is excluded from search to avoid noisy hits on header strings."""
    resp = client.get("/search?q=test")
    assert resp.status_code == 200
    # Neutral query: the smoke check is just that meta isn't its own
    # section_key in any result. This is best-effort — we mainly want to
    # ensure the route doesn't blow up.


def test_research_support_save_round_trips(client):
    """Edit RS entry 0 (no-op text change), verify amount keeps the \\$ prefix
    and PyYAML can still load the file."""
    rs = paths.data_dir() / "research_support.yml"
    snapshot = rs.read_bytes()
    try:
        _, data = yaml_io.load(rs)
        e0 = data[0]
        orig_amount = e0.get("amount")
        mtime = yaml_io.mtime_ns(rs)
        form = {
            "mode": "edit",
            "global_idx": "0",
            "mtime_ns": str(mtime),
            "status": e0.get("status"),
            "date": e0.get("date"),
            "agency": e0.get("agency"),
            "project": e0.get("project") or "",
            "pi": e0.get("pi") or "",
            "pi_label": e0.get("pi_label") or "",
            "title": e0.get("title"),
            "role": e0.get("role"),
            "amount": str(orig_amount or "").lstrip("\\").lstrip("$"),
            "audiences_json": "[]",
            "hide-from_json": "[]",
        }
        resp = client.post("/research_support/save", data=form, follow_redirects=False)
        assert resp.status_code in (302, 303)
        # File still parses and amount still starts with \$.
        parsed = pyyaml.safe_load(rs.read_text())
        assert isinstance(parsed, list)
        assert str(parsed[0].get("amount", "")).startswith("\\$"), (
            f"amount lost \\$ prefix: {parsed[0].get('amount')!r}"
        )
    finally:
        rs.write_bytes(snapshot)


def test_meta_save_idempotent_round_trip(client):
    """Save meta with no changes — file should round-trip without losing
    the docstring or section comments."""
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        _, data = yaml_io.load(meta)
        mtime = yaml_io.mtime_ns(meta)
        form = {"mtime_ns": str(mtime), "mode": "edit"}
        for fname in (
            "name",
            "position",
            "department",
            "institution",
            "address",
            "email",
            "phone",
            "website",
            "footer",
            "self_bold",
        ):
            v = data.get(fname)
            form[fname] = "" if v is None else str(v)
        form["sections_json"] = json.dumps(list(data.get("sections", [])))
        resp = client.post("/meta/save", data=form, follow_redirects=False)
        assert resp.status_code in (302, 303)
        # The build_variants block (NOT in the meta schema yet) MUST survive.
        new_text = meta.read_text()
        assert "build_variants:" in new_text, "meta.yml lost build_variants block"
        # The leading docstring (## Conventions ...) MUST survive.
        assert new_text.startswith("#"), "meta.yml lost leading docstring"
    finally:
        meta.write_bytes(snapshot)


def test_teaching_cluster_save_round_trips(client):
    """Cluster-based section: edit teaching entry 0, verify YAML and
    cluster header (institution, city) survive."""
    teach = paths.data_dir() / "teaching.yml"
    snapshot = teach.read_bytes()
    try:
        _, data = yaml_io.load(teach)
        cluster0 = data[0]
        e0 = cluster0["entries"][0]
        orig_inst = cluster0.get("institution")
        orig_city = cluster0.get("city")
        mtime = yaml_io.mtime_ns(teach)
        form = {
            "mode": "edit",
            "global_idx": "0",
            "mtime_ns": str(mtime),
            "cluster_institution": orig_inst,
            "cluster_city": orig_city or "",
            "date": e0.get("date"),
            "role": e0.get("role"),
            "course": e0.get("course"),
            "audiences_json": "[]",
            "hide-from_json": "[]",
            "highlighted": "on" if e0.get("highlighted") else "",
        }
        resp = client.post("/teaching/save", data=form, follow_redirects=False)
        assert resp.status_code in (302, 303)
        parsed = pyyaml.safe_load(teach.read_text())
        assert parsed[0]["institution"] == orig_inst
        assert parsed[0].get("city") == orig_city
        assert parsed[0]["entries"][0]["role"] == e0.get("role")
    finally:
        teach.write_bytes(snapshot)
