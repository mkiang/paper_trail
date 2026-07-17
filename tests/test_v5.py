"""V5 tests: sortable column markup + freezer workspace snapshot."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE, freeze_required
from cv_editor import freezer, paths, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent

# Freeze now flattens via `typst query`, so the create/render-path tests need
# the typst binary. The pure helpers (list/delete/prune, path-traversal) don't.
_HAS_TYPST = shutil.which("typst") is not None and HAS_BESPOKE  # P5: + bespoke/fonts
needs_typst = pytest.mark.skipif(not _HAS_TYPST, reason="typst not on PATH")


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- V5-A: sortable column markup ----


def test_sortable_markup_present_on_publications(client):
    body = client.get("/publications").get_data(as_text=True)
    assert "sortable-col" in body
    assert "data-sort-value" in body
    assert "sort-indicator" in body


def test_sortable_columns_have_kind_attr(client):
    """All columns get data-kind=text now that date/year carry a
    pre-normalized sort value in data-sort-value (post-2026-05 fix:
    parseFloat broke cross-year date sort)."""
    body = client.get("/publications").get_data(as_text=True)
    assert 'data-col="year" data-kind="text"' in body


def test_sortable_markup_on_every_section(client):
    """All sections with list_columns should have sortable headers."""
    for s in (
        "publications",
        "presentations",
        "research_support",
        "service",
        "teaching",
        "mentees",
        "honors",
        "education",
        "appointments",
    ):
        body = client.get(f"/{s}").get_data(as_text=True)
        assert "sortable-col" in body, f"sortable markup missing on {s}"


def test_sortable_renumber_not_baked_into_markup(client):
    """The # column has class num and gets renumbered by JS — confirm
    the server emits the raw loop.index (so JS can overwrite cleanly)."""
    body = client.get("/honors").get_data(as_text=True)
    # First row should have <td class="num">1</td> (server-side numbering).
    assert '<td class="num">1</td>' in body


# ---- V5-B: freezer ----


@needs_typst
def test_freeze_creates_self_contained_flattened_file():
    """The frozen dir is a single self-contained cv.typ + fonts + render.sh +
    README — no lib/, content/, or data/, and cv.typ has no #import."""
    r = freezer.freeze_workspace()
    try:
        assert r.path.is_dir()
        for rel in ("cv.typ", "fonts", "README.md", "render.sh"):
            assert (r.path / rel).exists(), f"missing: {rel}"
        for rel in ("lib", "content", "templates", "data", "publications.bib"):
            assert not (r.path / rel).exists(), f"flattened dir must not contain {rel}"
        cv = (r.path / "cv.typ").read_text()
        assert "#import" not in cv, "flattened cv.typ must be self-contained"
        assert "#let ty = (" in cv and "#let meta = (" in cv
        assert "#pub-entry(" in cv, "body should contain literal entry calls"
        mode = (r.path / "render.sh").stat().st_mode
        assert mode & stat.S_IXUSR, "render.sh not executable"
    finally:
        shutil.rmtree(r.path, ignore_errors=True)


@needs_typst
def test_freeze_render_sh_has_typst_compile_command():
    r = freezer.freeze_workspace()
    try:
        sh = (r.path / "render.sh").read_text()
        assert "typst compile" in sh
        assert "--font-path fonts" in sh
        assert "--ignore-system-fonts" in sh
        assert "--input" not in sh, "flattened render.sh needs no --input"
    finally:
        shutil.rmtree(r.path, ignore_errors=True)


@needs_typst
def test_freeze_readme_documents_render():
    r = freezer.freeze_workspace()
    try:
        readme = (r.path / "README.md").read_text()
        assert "render.sh" in readme
        assert "typst compile" in readme
    finally:
        shutil.rmtree(r.path, ignore_errors=True)


@needs_typst
def test_freeze_bakes_chosen_variant():
    """A non-default variant bakes its flags into the literal content."""
    ev = {
        "audience": "industry",
        "show_oa": "true",
        "show_notes": "true",
        "show_media": "true",
        "show_dollars": "false",
    }
    r = freezer.freeze_workspace(variant_inputs=ev, variant_name="everything")
    try:
        cv = (r.path / "cv.typ").read_text()
        assert "show-dollars = false" in cv
        assert "Selected media coverage:" in cv  # show_notes + show_media baked on
        assert "Variant: everything" in cv
    finally:
        shutil.rmtree(r.path, ignore_errors=True)


@needs_typst
def test_freeze_each_call_produces_distinct_directory():
    """Two calls in quick succession must produce different directories
    (ns-granularity timestamp)."""
    r1 = freezer.freeze_workspace()
    r2 = freezer.freeze_workspace()
    try:
        assert r1.path != r2.path
    finally:
        shutil.rmtree(r1.path, ignore_errors=True)
        shutil.rmtree(r2.path, ignore_errors=True)


@needs_typst
def test_list_frozen_returns_newest_first():
    r1 = freezer.freeze_workspace()
    r2 = freezer.freeze_workspace()
    try:
        listed = freezer.list_frozen()
        names = [r.path.name for r in listed]
        idx1 = names.index(r1.path.name)
        idx2 = names.index(r2.path.name)
        assert idx2 < idx1, "newer freeze should appear first"
    finally:
        shutil.rmtree(r1.path, ignore_errors=True)
        shutil.rmtree(r2.path, ignore_errors=True)


def test_delete_frozen_rejects_path_traversal():
    """delete_frozen must reject names that try to escape output/."""
    with pytest.raises(ValueError):
        freezer.delete_frozen("../../etc/passwd")
    with pytest.raises(ValueError):
        freezer.delete_frozen("frozen-../../bad")
    with pytest.raises(ValueError):
        freezer.delete_frozen("not-a-frozen-dir")
    with pytest.raises(ValueError):
        freezer.delete_frozen("frozen-nodigit")


def test_delete_frozen_missing_dir_raises_not_found():
    with pytest.raises(FileNotFoundError):
        freezer.delete_frozen("frozen-9999999999999999999")


@needs_typst
def test_freeze_create_via_route(client):
    """POST /freeze creates a directory + flashes ok."""
    resp = client.post("/freeze", follow_redirects=False)
    assert resp.status_code in (302, 303)
    # Find the newly-created dir and clean it up.
    listed = freezer.list_frozen()
    if listed:
        # Newest is the one we just made; clean it.
        for r in listed:
            # Best-effort: only delete our test-created ones (newest).
            if r.path.exists():
                shutil.rmtree(r.path, ignore_errors=True)
                break


@needs_typst
def test_freeze_create_via_route_with_variant(client):
    """POST /freeze with a non-default variant bakes that variant's flags.
    Checks the `audience` bake -- it's stable across UI edits because every
    variant always has an audience field. (Previous version checked
    show-dollars, but meta.yml flag values shift as the user edits variants
    via the Style UI, so the test bit-rotted.)"""
    # Data-agnostic: use an actual shipped variant (+ its audience) from meta.
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    variant = meta["build_variants"][0]
    vname = variant["filename"]
    vaud = (variant.get("inputs") or {}).get("audience", "full")
    resp = client.post("/freeze", data={"variant": vname}, follow_redirects=False)
    assert resp.status_code in (302, 303)
    listed = freezer.list_frozen()
    assert listed
    newest = listed[0]
    try:
        cv = (newest.path / "cv.typ").read_text()
        assert f"Variant: {vname}" in cv
        # The variant's audience is baked as `#let audience = "<aud>"`. This
        # proves variant inputs thread through `typst query` ->
        # _query_flatten -> _assemble_cv_typ.
        assert f'#let audience = "{vaud}"' in cv
    finally:
        shutil.rmtree(newest.path, ignore_errors=True)


@freeze_required
def test_freeze_create_rejects_unknown_variant(client):
    """An unknown variant name is rejected with 400 (no typst needed)."""
    resp = client.post("/freeze", data={"variant": "no-such-variant-xyz"}, follow_redirects=False)
    assert resp.status_code == 400


@freeze_required
def test_freeze_create_surfaces_query_failure(client, monkeypatch):
    """If `typst query` fails, the route flashes + returns 500, no traceback,
    and leaves no orphan frozen dir."""
    before = {r.path.name for r in freezer.list_frozen()}

    def boom(*a, **k):
        raise RuntimeError("typst query (flatten.typ) failed: synthetic")

    monkeypatch.setattr(freezer, "freeze_workspace", boom)
    # Data-agnostic: a valid (known) shipped variant name; the route validates
    # the name before calling the (monkeypatched) freeze_workspace.
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    vname = meta["build_variants"][0]["filename"]
    resp = client.post("/freeze", data={"variant": vname}, follow_redirects=False)
    assert resp.status_code == 500
    after = {r.path.name for r in freezer.list_frozen()}
    assert after == before, "failed freeze must not leave an orphan directory"


@needs_typst
def test_freeze_delete_via_route(client):
    """POST /freeze/<name>/delete removes a frozen workspace."""
    r = freezer.freeze_workspace()
    try:
        resp = client.post(f"/freeze/{r.path.name}/delete", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert not r.path.exists()
    finally:
        if r.path.exists():
            shutil.rmtree(r.path, ignore_errors=True)


@freeze_required
def test_freeze_delete_invalid_name_400s(client):
    resp = client.post("/freeze/not-a-frozen/delete", follow_redirects=False)
    assert resp.status_code == 400


@freeze_required
def test_freeze_list_route_renders(client):
    body = client.get("/freeze").get_data(as_text=True)
    assert "Freeze" in body
    assert "Freeze &amp; flatten" in body
    assert 'select name="variant"' in body
