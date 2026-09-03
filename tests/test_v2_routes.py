"""V2 route smoke + round-trip tests via Flask test_client.

Covers:
- Section index renders and lists all 10 sections.
- Each section's list / view / edit / new / backups page renders 200.
- Search returns results.
- A representative non-publication save round-trips through YAML without
  corrupting the file (verified with the pre-test snapshot).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml
from _engine_guards import form_body_from_get
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
    """A no-change save of the Meta form must leave meta.yml BYTE-IDENTICAL.

    THIS TEST USED TO ENCODE THE BUG IT WAS NAMED FOR. It built its body with
    `form[fname] = str(v)` over a field list that included `address` (a YAML
    list) and `footer` (a mapping) -- performing the very stringification the
    save path was being blamed for -- and then asserted only that
    `build_variants:` and a leading `#` survived. It was green whether or not
    the save flattened both fields, and it self-restored from a snapshot, so
    the corruption canary never fired either. See gotcha #96.

    Two properties fix that, and both matter:

    1. The body is derived from the GET, the way a browser builds it, so a
       regression in the TEMPLATE is visible. A hand-built body cannot see one:
       the repr lands in the widget's value, not in the JSON block.
    2. The assertion is a SUCCESSFUL save AND byte-identity, in that order.
       Byte-identity alone is satisfied by a REFUSED write, which would let a
       guard regression pass as "nothing changed".
    """
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        body = client.get("/meta/edit").get_data(as_text=True)
        form = form_body_from_get(body)
        form["mtime_ns"] = str(yaml_io.mtime_ns(meta))
        form["mode"] = "edit"

        resp = client.post("/meta/save", data=form, follow_redirects=False)
        # Successful save FIRST -- see the docstring.
        assert resp.status_code in (302, 303), (
            f"expected a successful save, got {resp.status_code}. A refusal would "
            f"satisfy the byte-identity assertion below for the wrong reason."
        )
        assert meta.read_bytes() == snapshot, (
            "a no-change Meta save rewrote meta.yml. This is the gotcha #96 "
            "regression: check whether a non-scalar field is being rendered "
            "into a scalar widget, or a *_json field is posting empty."
        )
    finally:
        meta.write_bytes(snapshot)


def test_meta_save_preserves_the_non_scalar_shapes(client):
    """The shapes themselves, asserted by type rather than by bytes.

    Byte-identity above is the strong assertion, but it fails as one big blob.
    This one names WHICH invariant broke, and pins the two observables from the
    original incident: `address` stayed a list, `footer` stayed a mapping, and
    `show_on_first_page` is still an UNQUOTED YAML bool -- the capital-`F`
    Python `False` in a quoted string was the visible symptom on disk.
    """
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        body = client.get("/meta/edit").get_data(as_text=True)
        form = form_body_from_get(body)
        form["mtime_ns"] = str(yaml_io.mtime_ns(meta))
        form["mode"] = "edit"
        assert client.post("/meta/save", data=form).status_code in (302, 303)

        _, after = yaml_io.load(meta)
        assert isinstance(after["address"], list), (
            f"address is {type(after['address']).__name__}; `..meta.address` is a "
            f"Typst spread, so a string here is a hard no-PDF build failure"
        )
        assert isinstance(after["footer"], dict)
        assert isinstance(after["footer"]["show_on_first_page"], bool)

        text = meta.read_text()
        assert "show_on_first_page: false" in text, "the bool was persisted quoted"
        for signature in ("ordereddict(", "CommentedSeq(", "CommentedMap("):
            assert signature not in text, f"a Python repr reached the file: {signature}"
    finally:
        meta.write_bytes(snapshot)


def test_meta_save_refuses_to_empty_a_renderer_required_field(client):
    """Emptying `address` from the form is refused, and nothing is written.

    `renderer_required` keeps the KEY (an absent one fails the Typst spread),
    which turned the old deleter into a silent EMPTIER -- key present, content
    gone, build green, header lines missing. What the file may legitimately
    contain (`address: []`, which a blank scaffold writes) is not what a form
    may submit.
    """
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        body = client.get("/meta/edit").get_data(as_text=True)
        form = form_body_from_get(body)
        form["mtime_ns"] = str(yaml_io.mtime_ns(meta))
        form["mode"] = "edit"
        form["address_json"] = "[]"

        resp = client.post("/meta/save", data=form, follow_redirects=False)
        assert resp.status_code == 400
        assert meta.read_bytes() == snapshot, "a refused save still wrote"
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


# ---- /meta/footer (gotcha #96) -------------------------------------------


def _footer_post(client, meta, **overrides):
    form = {"mtime_ns": str(yaml_io.mtime_ns(meta))}
    form.update(overrides)
    return client.post("/meta/footer/save", data=form, follow_redirects=False)


def test_meta_footer_save_writes_a_mapping_with_a_real_bool(client):
    """The footer round-trips as a MAPPING, and the bool stays a bool.

    Both halves are no-PDF failures when wrong, and neither is hypothetical:
    the original incident persisted the whole mapping as a string, and the
    obvious "quote everything" fix would persist `show_on_first_page` as a
    string. `pick_style` returns non-str values untouched, so a wrapper there
    survives normalization -- and both templates use the value in a boolean
    context (`page-num > 1 or show-on-first`), which is a hard Typst type
    error, not a cosmetic one.
    """
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        resp = _footer_post(
            client,
            meta,
            template="Test Person (Curriculum Vitae --- {date})",
            date_format="[month repr:long] [year]",
            show_on_first_page="on",
        )
        assert resp.status_code in (302, 303)

        _, after = yaml_io.load(meta)
        footer = after["footer"]
        assert isinstance(footer, dict), f"footer persisted as {type(footer).__name__}"
        assert footer["template"].startswith("Test Person")
        assert isinstance(footer["show_on_first_page"], bool), (
            f"show_on_first_page persisted as {type(footer['show_on_first_page']).__name__}; "
            f"the templates use it in a boolean context"
        )
        assert footer["show_on_first_page"] is True

        text = meta.read_text()
        assert "show_on_first_page: true" in text, "the bool was persisted quoted"
        assert "show_on_first_page: 'true'" not in text
        assert 'show_on_first_page: "true"' not in text
    finally:
        meta.write_bytes(snapshot)


def test_meta_footer_save_keeps_both_hard_accessed_keys_on_a_blank_submit(client):
    """A blank `date_format` falls back to disk; the key never disappears.

    `bespoke/lib/styles.typ:62` and `:64` read `.date_format` and `.template`
    with no `.at()` default, so a mapping missing either one produces no PDF.
    """
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        _, before = yaml_io.load(meta)
        was = dict(before["footer"])

        resp = _footer_post(client, meta, template="", date_format="", show_on_first_page="")
        assert resp.status_code in (302, 303)

        _, after = yaml_io.load(meta)
        assert set(after["footer"]) >= {"template", "date_format"}
        assert after["footer"]["template"] == was["template"]
        assert after["footer"]["date_format"] == was["date_format"]
    finally:
        meta.write_bytes(snapshot)


def test_meta_footer_save_rejects_a_non_typst_date_format_and_writes_nothing(client):
    meta = paths.data_dir() / "meta.yml"
    snapshot = meta.read_bytes()
    try:
        resp = _footer_post(
            client, meta, template="X ({date})", date_format="Month Year", show_on_first_page=""
        )
        assert resp.status_code == 400
        assert meta.read_bytes() == snapshot, "a rejected footer save still wrote"
    finally:
        meta.write_bytes(snapshot)


def test_meta_footer_is_reachable_and_linked_from_meta(client):
    """The route is UNGATED, and /meta links to it.

    `footer` is no longer in `META["fields"]`, so `meta_view.html`'s field loop
    cannot render it -- without an explicit row and link it vanishes from the
    editor with no replacement, reachable only by typing the URL.
    """
    assert client.get("/meta/footer").status_code == 200
    body = client.get("/meta").get_data(as_text=True)
    assert "/meta/footer" in body, "/meta does not link to the footer editor"
