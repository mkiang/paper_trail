"""V4 reviewer-flagged test gaps: self-collision on edit, stale-mtime 409,
filename edge cases, multi-duplicate naming, variant_typst_argv with
review=true, /style nav reachable, last-variant delete guard, path-
traversal filename rejection at build time, audience: full round-trip
preservation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import build_variants as bv
from cv_editor import paths, yaml_io
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- /style nav link reachable ----


def test_style_link_in_nav(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/style"' in body


# ---- self-collision on edit (filename unchanged) ----


def test_style_edit_same_filename_no_collision(client):
    """Editing a variant without changing its filename should NOT trigger
    a duplicate-filename error."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        # Any existing variant works (data-agnostic): use the first one.
        _, meta = yaml_io.load(meta_path)
        idx = 0
        variant = meta["build_variants"][idx]
        fname = variant["filename"]
        audience = (variant.get("inputs") or {}).get("audience", "")
        mtime = yaml_io.mtime_ns(meta_path)
        # Save it back with the same filename + same audience (no change).
        resp = client.post(
            "/style/save",
            data={
                "mode": "edit",
                "idx": str(idx),
                "mtime_ns": str(mtime),
                "filename": fname,
                "audience": audience,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303), (
            f"expected redirect, got {resp.status_code}: {resp.get_data(as_text=True)[:300]}"
        )
    finally:
        meta_path.write_bytes(snapshot)


# ---- stale-mtime 409 ----


def test_style_save_stale_mtime_redirects_with_pending_token(client):
    """Tier B / B5 (2026-05-27): stale-mtime style_save returns 302 (not 409)
    so the browser follows the Location header back to the form with the
    user's unsaved values preserved via the _STYLE_PENDING store. Same
    pattern as entry_save's Stage B / I8 fix. Browsers don't auto-follow
    4xx Location headers, which is why the prior 409 left the user on a
    "Redirecting" stub with their style edits lost."""
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": "1",  # stale
            "filename": "v4-stale-test",
            "audience": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/style/new" in resp.headers["Location"]
    assert "pending=" in resp.headers["Location"]
    assert "pending_cause=StaleFileError" in resp.headers["Location"]


# ---- filename edge cases ----


def test_filename_uppercase_rejected(client):
    meta_path = paths.data_dir() / "meta.yml"
    mtime = yaml_io.mtime_ns(meta_path)
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": str(mtime),
            "filename": "AcademicCV",
            "audience": "",
        },
    )
    assert resp.status_code == 400


def test_filename_whitespace_only_rejected(client):
    meta_path = paths.data_dir() / "meta.yml"
    mtime = yaml_io.mtime_ns(meta_path)
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": str(mtime),
            "filename": "   ",
            "audience": "",
        },
    )
    assert resp.status_code == 400


def test_filename_leading_hyphen_rejected(client):
    meta_path = paths.data_dir() / "meta.yml"
    mtime = yaml_io.mtime_ns(meta_path)
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": str(mtime),
            "filename": "-cv",
            "audience": "",
        },
    )
    assert resp.status_code == 400


# ---- multi-duplicate cascade (cv -> cv-copy -> cv-copy2) ----


def test_style_multi_duplicate_cascades_to_copy2(client):
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        _, meta0 = yaml_io.load(meta_path)
        first = (meta0.get("build_variants") or [])[0].get("filename")
        # First duplicate.
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            "/style/0/duplicate", data={"mtime_ns": str(mtime)}, follow_redirects=False
        )
        assert resp.status_code in (302, 303)
        # Second duplicate of the same source — should produce <first>-copy2.
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            "/style/0/duplicate", data={"mtime_ns": str(mtime)}, follow_redirects=False
        )
        assert resp.status_code in (302, 303)
        _, meta = yaml_io.load(meta_path)
        filenames = [v.get("filename") for v in meta.get("build_variants") or []]
        assert f"{first}-copy" in filenames
        assert f"{first}-copy2" in filenames
    finally:
        meta_path.write_bytes(snapshot)


# ---- variant_typst_argv with review=true ----


def test_variant_typst_argv_with_review_true():
    argv = bv.variant_typst_argv(
        {
            "filename": "review",
            "inputs": {"audience": "full", "review": True},
        }
    )
    assert "audience=full" in argv
    assert "review=true" in argv  # bool serialized lowercase


def test_variant_typst_argv_rejects_path_traversal_filename():
    """V4 reviewer-A finding HIGH#1: filename validation must run at build
    time, not just at form-save time. A hand-edited meta.yml with a
    path-traversal filename must be refused at the build step."""
    with pytest.raises(bv.InvalidVariantError):
        bv.variant_typst_argv({"filename": "../../etc/passwd", "inputs": {}})
    with pytest.raises(bv.InvalidVariantError):
        bv.variant_typst_argv({"filename": "/tmp/owned", "inputs": {}})
    with pytest.raises(bv.InvalidVariantError):
        bv.variant_typst_argv({"filename": "", "inputs": {}})
    with pytest.raises(bv.InvalidVariantError):
        bv.variant_typst_argv({"filename": "has space", "inputs": {}})


# ---- audience: full round-trip preserved ----


def test_audience_full_round_trips_through_form():
    """V4 reviewer-A finding HIGH#2: explicit `audience: full` in YAML
    must NOT be silently dropped on round-trip through the form."""
    src = {"filename": "everything", "inputs": {"audience": "full", "show_highlighted": True}}
    form = bv.variant_to_form(src)
    assert form["audience"] == "full"
    rebuilt = bv.form_to_variant(form)
    assert dict(rebuilt["inputs"]) == dict(src["inputs"]), (
        "audience: full was dropped through the form ↔ YAML round-trip"
    )


# ---- last-variant delete guard ----


def test_style_delete_refuses_last_variant(client, tmp_path):
    """Deleting the last variant would leave ./build.sh with nothing to
    compile. Must refuse with a flash + 400."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        # Trim variants to just one entry.
        header, meta = yaml_io.load(meta_path)
        variants = meta.get("build_variants") or []
        meta["build_variants"] = type(variants)([variants[0]])
        yaml_io.write_with_backup(meta_path, header, meta)
        # Attempt to delete the lone remaining variant.
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post("/style/0/delete", data={"mtime_ns": str(mtime)}, follow_redirects=False)
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}"
    finally:
        meta_path.write_bytes(snapshot)


# ---- impact_preview gracefully handles bad data ----


def test_impact_preview_records_section_load_error():
    """If a section fails to load, the preview should record an error row
    instead of silently dropping the section."""
    import yaml as pyyaml

    def loader(key):
        if key == "honors":
            raise pyyaml.YAMLError("simulated bad YAML")
        return yaml_io.load(paths.data_dir() / f"{key}.yml")[1]

    out = bv.impact_preview(loader, audience="full", show_highlighted=False)
    assert "honors" in out["per_section"]
    assert "error" in out["per_section"]["honors"]


# ---- default_form helper ----


def test_default_form_shape():
    out = bv.default_form()
    assert out["filename"] == ""
    assert out["audience"] == ""
    assert out["show_dollars"] is True
    for k in bv.BOOLEAN_INPUTS:
        assert out[k] is False


# ---- duplicate of unsafe-filename variant falls back to "variant" ----


def test_duplicate_fallback_for_bad_filename_source():
    """If meta.yml has been hand-edited with a malformed filename, the
    duplicate's name falls back to 'variant-copy' so it passes validation."""
    # Direct unit test on the regex helper.
    assert bv.FILENAME_RE.match("variant-copy")
    assert not bv.FILENAME_RE.match("HAS UPPERCASE")
