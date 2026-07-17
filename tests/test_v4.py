"""V4 tests: build_variants helpers + /style routes + meta.yml round-trip
for the build_variants block."""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import build_variants as bv
from cv_editor import paths, yaml_io
from cv_editor.app import create_app
from ruamel.yaml.comments import CommentedMap

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- build_variants.form_to_variant / variant_to_form round-trip ----


def test_form_to_variant_minimal():
    v = bv.form_to_variant({"filename": "cv"})
    assert v["filename"] == "cv"
    assert dict(v["inputs"]) == {}


def test_form_to_variant_with_audience():
    v = bv.form_to_variant({"filename": "academic", "audience": "academic"})
    assert v["inputs"]["audience"] == "academic"


def test_form_to_variant_with_boolean_flags():
    v = bv.form_to_variant(
        {
            "filename": "everything",
            "audience": "full",
            "show_pending": True,
            "show_oa": True,
            "show_contributions": True,
            "show_notes": True,
            "show_media": True,
            "show_highlighted": True,
        }
    )
    assert v["inputs"]["show_pending"] is True
    assert v["inputs"]["show_oa"] is True
    assert v["inputs"]["show_contributions"] is True
    assert v["inputs"]["show_notes"] is True
    assert v["inputs"]["show_media"] is True
    assert v["inputs"]["show_highlighted"] is True


def test_form_to_variant_unchecked_show_dollars_writes_show_dollars_false():
    v = bv.form_to_variant({"filename": "metro", "audience": "full", "show_dollars": False})
    assert v["inputs"]["show_dollars"] is False


def test_form_to_variant_checked_show_dollars_omits_key():
    v = bv.form_to_variant({"filename": "cv", "audience": "full", "show_dollars": True})
    assert "show_dollars" not in v["inputs"]


def test_form_to_variant_drops_invalid_audience():
    v = bv.form_to_variant({"filename": "x", "audience": "BOGUS"})
    assert "audience" not in v["inputs"]


def test_variant_to_form_round_trip_existing_variants():
    """Every variant currently in meta.yml round-trips form ↔ YAML
    without losing any input keys."""
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    for v in meta.get("build_variants") or []:
        form = bv.variant_to_form(v)
        rebuilt = bv.form_to_variant(form)
        # The style form does not model the per-variant `template` input
        # (template is selected via meta top-level / the registry default, not
        # the flag form), so a variant carrying `template:` in its inputs
        # legitimately drops it on round-trip. Compare modulo that key. The
        # private corpus's variants don't set `template`, so this is a no-op
        # there; the Jane Q Public sample's example variants DO set it.
        expected = {k: val for k, val in (v.get("inputs") or {}).items() if k != "template"}
        assert dict(rebuilt["inputs"]) == expected, (
            f"round-trip mismatch for variant {v.get('filename')}"
        )


# ---- validate_form ----


def test_validate_form_empty_filename():
    e = bv.validate_form({"filename": ""})
    assert e.get("filename") == "required"


def test_validate_form_bad_filename_chars():
    e = bv.validate_form({"filename": "My CV.pdf"})
    assert "filename" in e


def test_validate_form_duplicate_filename():
    e = bv.validate_form({"filename": "cv"}, existing_filenames=["cv", "academic"])
    assert "filename" in e


def test_validate_form_ok():
    # P3: audiences are data-driven; with no `audiences=` passed the vocabulary
    # is the generic base set, so use a base audience for the happy path.
    e = bv.validate_form(
        {"filename": "for-academic-app", "audience": "academic"}, existing_filenames=["cv"]
    )
    assert e == {}


def test_validate_form_accepts_data_present_audience_when_passed():
    # A data-widened audience (e.g. one only present in entry data) validates
    # when the caller passes the widened set — the permissive P3 contract.
    e = bv.validate_form(
        {"filename": "for-x-app", "audience": "metro"},
        existing_filenames=["cv"],
        audiences=("full", "academic", "industry", "public-health", "metro"),
    )
    assert e == {}


# ---- variant_chips ----


def test_variant_chips_default_variant():
    chips = bv.variant_chips({"filename": "cv", "inputs": {}})
    assert any(c["kind"] == "default" for c in chips)


def test_variant_chips_audience_and_flags():
    chips = bv.variant_chips(
        {
            "filename": "x",
            "inputs": {"audience": "academic", "show_oa": True, "show_dollars": False},
        }
    )
    labels = [c["label"] for c in chips]
    assert any("audience: academic" in label for label in labels)
    assert any("show oa" in label for label in labels)
    assert any("show_dollars: off" in label for label in labels)


# ---- variant_typst_argv ----


def test_variant_typst_argv_includes_input_flags():
    v = {"filename": "metro", "inputs": {"audience": "full", "show_dollars": False}}
    argv = bv.variant_typst_argv(v)
    assert argv[0] == "typst" and argv[1] == "compile"
    assert "output/metro.pdf" in argv
    assert "--input" in argv
    assert "audience=full" in argv
    assert "show_dollars=false" in argv  # bool to lowercase string


def test_variant_typst_argv_with_empty_inputs():
    argv = bv.variant_typst_argv({"filename": "cv", "inputs": {}})
    assert "output/cv.pdf" in argv
    assert "--input" not in argv  # no flags appended


# ---- impact_preview ----


def test_impact_preview_uses_audience_filter():
    """For each existing audience choice, preview should produce a
    visible-count <= total."""

    def loader(key):
        return yaml_io.load(paths.data_dir() / f"{key}.yml")[1]

    for aud in ("full", "academic", "industry", "public-health", "metro"):
        p = bv.impact_preview(loader, audience=aud, show_highlighted=False)
        for key, row in p["per_section"].items():
            assert row["visible"] <= row["total"]
        assert p["total_visible"] <= p["total_total"]


def test_impact_preview_show_highlighted_includes_more():
    def loader(key):
        return yaml_io.load(paths.data_dir() / f"{key}.yml")[1]

    off = bv.impact_preview(loader, audience="full", show_highlighted=False)
    on = bv.impact_preview(loader, audience="full", show_highlighted=True)
    # Every section's visible count with show_highlighted=on is >= off.
    for key in off["per_section"]:
        assert on["per_section"][key]["visible"] >= off["per_section"][key]["visible"]
    assert on["total_visible"] >= off["total_visible"]


def test_is_visible_hide_from_wins():
    e = {"audiences": ["academic", "metro"], "hide-from": ["metro"]}
    assert bv.is_visible(e, "metro") is False  # hide-from wins
    assert bv.is_visible(e, "academic") is True


def test_is_visible_empty_audiences_universal():
    e = {}
    assert bv.is_visible(e, "academic") is True
    assert bv.is_visible(e, "full") is True


# ---- /style routes ----


def test_style_list_renders(client):
    body = client.get("/style").get_data(as_text=True)
    assert "build variant" in body
    # Every ACTUAL shipped variant should be listed (data-agnostic).
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    expected_names = [v["filename"] for v in meta["build_variants"]]
    assert expected_names, "corpus meta.yml defines no build_variants"
    for fname in expected_names:
        assert f"{fname}.pdf" in body


def test_style_edit_form_pre_filled_when_show_dollars_false(client, monkeypatch):
    """A variant that explicitly sets show_dollars=false renders the form with
    the checkbox unchecked.

    Uses a synthetic test-only variant injected via monkeypatch so the test
    isn't coupled to whatever flag values happen to live in meta.yml today.
    The same shape of bit-rot already burned test_freeze_create_via_route_with_variant
    (refactored to assert on the stable `audience` value); avoid recreating it
    here by never reading the live `metro`/`cv`/`everything` flag values.
    The synthetic variant never touches disk and is not visible to the UI."""
    import re

    real_load = yaml_io.load
    test_variant = CommentedMap()
    test_variant["filename"] = "_test_show_dollars_false"
    test_variant["inputs"] = CommentedMap([("audience", "full"), ("show_dollars", False)])

    def fake_load(path):
        header, data = real_load(path)
        if path.name == "meta.yml" and isinstance(data, dict):
            variants = list(data.get("build_variants") or [])
            variants.append(test_variant)
            data["build_variants"] = variants
        return header, data

    monkeypatch.setattr(yaml_io, "load", fake_load)

    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    idx = next(
        i
        for i, v in enumerate(meta["build_variants"])
        if v.get("filename") == "_test_show_dollars_false"
    )
    body = client.get(f"/style/{idx}/edit").get_data(as_text=True)

    assert 'name="show_dollars"' in body
    pat = re.compile(r'name="show_dollars"[^>]*checked', re.S)
    assert not pat.search(body), (
        "show_dollars should be unchecked when variant sets show_dollars=false"
    )


def test_synthetic_variant_does_not_leak_to_subsequent_tests(client):
    """Belt-and-suspenders: confirm the monkeypatch from the prior test
    is fully unwound so subsequent tests see the real meta.yml. If this
    fails, the prior test's `monkeypatch.setattr(yaml_io, "load", ...)`
    isn't being restored — likely because some caller captured the
    pre-patch callable into a closure or app.config blob."""
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    filenames = [v.get("filename") for v in (meta.get("build_variants") or [])]
    assert "_test_show_dollars_false" not in filenames, (
        "Synthetic test variant leaked from "
        "test_style_edit_form_pre_filled_when_show_dollars_false. "
        "Check whether yaml_io.load was rebound via something other than "
        "monkeypatch.setattr (e.g., a module-level capture or app.config seam)."
    )


def test_style_new_form_renders(client):
    body = client.get("/style/new").get_data(as_text=True)
    assert "New build variant" in body
    # All boolean flag inputs present.
    for f in (
        "review",
        "show_pending",
        "show_oa",
        "show_contributions",
        "show_notes",
        "show_media",
        "show_highlighted",
        "show_dollars",
    ):
        assert f'name="{f}"' in body


def test_style_save_create_round_trips(client):
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            "/style/save",
            data={
                "mode": "new",
                "mtime_ns": str(mtime),
                "filename": "test-variant-v4",
                "audience": "academic",
                "review": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        _, meta = yaml_io.load(meta_path)
        v = [
            vv
            for vv in (meta.get("build_variants") or [])
            if vv.get("filename") == "test-variant-v4"
        ]
        assert len(v) == 1
        assert v[0]["inputs"]["audience"] == "academic"
        assert v[0]["inputs"]["review"] is True
    finally:
        meta_path.write_bytes(snapshot)


def test_style_save_duplicate_filename_409s(client):
    """Saving a new variant with a filename that's already used should
    re-render the form with an error."""
    meta_path = paths.data_dir() / "meta.yml"
    mtime = yaml_io.mtime_ns(meta_path)
    _, meta = yaml_io.load(meta_path)
    existing = bv.default_variant_name(meta)  # first shipped variant, already in use
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": str(mtime),
            "filename": existing,  # already in use
            "audience": "",
        },
    )
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "Could not save" in body
    assert "already uses" in body or "filename" in body


def test_style_save_invalid_filename_400s(client):
    meta_path = paths.data_dir() / "meta.yml"
    mtime = yaml_io.mtime_ns(meta_path)
    resp = client.post(
        "/style/save",
        data={
            "mode": "new",
            "mtime_ns": str(mtime),
            "filename": "Has Spaces",
            "audience": "",
        },
    )
    assert resp.status_code == 400


def test_style_save_preserves_meta_docstring(client):
    """A round-trip through /style/save must NOT clobber the YAML
    conventions docstring at the top of meta.yml or the build_variants
    section comment."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            "/style/save",
            data={
                "mode": "new",
                "mtime_ns": str(mtime),
                "filename": "test-doc-survive",
                "audience": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        new_text = meta_path.read_text()
        assert new_text.startswith("#"), "leading docstring lost"
        assert "build_variants:" in new_text
    finally:
        meta_path.write_bytes(snapshot)


def test_style_delete_round_trips(client):
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        # Create a deletable variant first.
        mtime = yaml_io.mtime_ns(meta_path)
        client.post(
            "/style/save",
            data={
                "mode": "new",
                "mtime_ns": str(mtime),
                "filename": "v4-deleteme",
                "audience": "",
            },
        )
        # Find its idx.
        _, meta = yaml_io.load(meta_path)
        idx = next(
            i
            for i, v in enumerate(meta.get("build_variants") or [])
            if v.get("filename") == "v4-deleteme"
        )
        # Delete it.
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            f"/style/{idx}/delete", data={"mtime_ns": str(mtime)}, follow_redirects=False
        )
        assert resp.status_code in (302, 303)
        _, meta = yaml_io.load(meta_path)
        assert not any(v.get("filename") == "v4-deleteme" for v in meta.get("build_variants") or [])
    finally:
        meta_path.write_bytes(snapshot)


def test_style_duplicate_appends_copy_suffix(client):
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        # Duplicate the first variant (idx 0); the duplicate gets a -copy suffix.
        _, meta0 = yaml_io.load(meta_path)
        first = (meta0.get("build_variants") or [])[0].get("filename")
        mtime = yaml_io.mtime_ns(meta_path)
        resp = client.post(
            "/style/0/duplicate", data={"mtime_ns": str(mtime)}, follow_redirects=False
        )
        assert resp.status_code in (302, 303)
        _, meta = yaml_io.load(meta_path)
        filenames = [v.get("filename") for v in meta.get("build_variants") or []]
        assert f"{first}-copy" in filenames
    finally:
        meta_path.write_bytes(snapshot)


def test_style_build_stream_returns_event_stream(client):
    resp = client.post("/style/0/build/stream", buffered=False)
    assert resp.mimetype == "text/event-stream"
    try:
        resp.close()
    except Exception:
        pass


def test_style_oob_idx_404s(client):
    assert client.get("/style/9999/edit").status_code == 404
    resp = client.post("/style/9999/delete", data={"mtime_ns": "0"})
    assert resp.status_code == 404


# ---- Tier B / B5 (2026-05-27): style_save 409 via _make_pending_store ----


def test_style_save_409_stale_mtime_redirects_with_pending_token(client):
    """Stale mtime on /style/save mode=edit redirects to style_edit?pending=<uuid>
    NOT a 4xx — browsers don't auto-follow 4xx Location headers, which used to
    leave the user on a "Redirecting" stub with their style edits lost. Mirrors
    the entry_save 409 fix (Stage B / I8, gotcha #46)."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        _, meta = yaml_io.load(meta_path)
        # Pick the default (first) variant — always present, idx by meta.yml order.
        default = bv.default_variant_name(meta)
        idx = next(i for i, v in enumerate(meta["build_variants"]) if v.get("filename") == default)
        # Deliberately stale mtime_ns so write_with_backup raises StaleFileError.
        resp = client.post(
            "/style/save",
            data={
                "mode": "edit",
                "idx": str(idx),
                "mtime_ns": "1",  # stale
                "filename": "cv-renamed",
                "audience": "academic",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert f"/style/{idx}/edit" in resp.headers["Location"]
        assert "pending=" in resp.headers["Location"]
        assert "pending_cause=StaleFileError" in resp.headers["Location"]
        # No write actually happened.
        assert meta_path.read_bytes() == snapshot
    finally:
        meta_path.write_bytes(snapshot)


def test_style_save_409_new_mode_redirects_to_style_new(client):
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        resp = client.post(
            "/style/save",
            data={
                "mode": "new",
                "mtime_ns": "1",
                "filename": "fresh-variant",
                "audience": "industry",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/style/new" in resp.headers["Location"]
        assert "pending=" in resp.headers["Location"]
    finally:
        meta_path.write_bytes(snapshot)


def test_style_edit_re_renders_form_from_pending_snapshot(client):
    """Stale mtime → 302 → GET style_edit?pending=<uuid> shows the user's
    unsaved form values, not the on-disk variant."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        _, meta = yaml_io.load(meta_path)
        default = bv.default_variant_name(meta)
        idx = next(i for i, v in enumerate(meta["build_variants"]) if v.get("filename") == default)
        resp = client.post(
            "/style/save",
            data={
                "mode": "edit",
                "idx": str(idx),
                "mtime_ns": "1",  # stale
                "filename": "cv-user-was-renaming-this",
                "audience": "full",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # Follow the redirect to the recovery render.
        recovery = client.get(resp.headers["Location"])
        body = recovery.get_data(as_text=True)
        assert recovery.status_code == 200
        # The user's unsaved typed filename must appear pre-filled.
        assert 'value="cv-user-was-renaming-this"' in body
        # And the recovery banner-warn must explain the cause.
        assert "another save" in body.lower() or "different tab" in body.lower()
    finally:
        meta_path.write_bytes(snapshot)


def test_style_save_409_round_trips_all_ui_rendered_flags(client):
    """Parity guard: every flag that the style_edit form renders as a
    checkbox survives the 409 → re-render path. Without this, adding a
    new default-true flag (the Stage D / I6 near-miss) could silently
    drop the value on the recovery path.

    Note: bv.BOOLEAN_INPUTS includes `show_citations`, which is a real
    renderer flag set via meta.yml on the `everything` variant but
    deliberately not exposed in the UI (no editor checkbox). We
    discover the UI-rendered subset by checking the recovery body for
    `name="<key>"` presence before asserting on `checked`.
    """
    import re

    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        _, meta = yaml_io.load(meta_path)
        default = bv.default_variant_name(meta)
        idx = next(i for i, v in enumerate(meta["build_variants"]) if v.get("filename") == default)
        post_data = {
            "mode": "edit",
            "idx": str(idx),
            "mtime_ns": "1",  # stale
            "filename": "cv-parity-test",
            "audience": "full",
        }
        # Flip every BOOLEAN_INPUTS key on; leave DEFAULT_TRUE_INPUTS off.
        for key in bv.BOOLEAN_INPUTS:
            post_data[key] = "on"
        resp = client.post("/style/save", data=post_data, follow_redirects=False)
        assert resp.status_code == 302
        recovery = client.get(resp.headers["Location"])
        body = recovery.get_data(as_text=True)

        # For every BOOLEAN_INPUTS key that IS rendered as a checkbox,
        # assert it's re-checked. For keys not in the UI (show_citations),
        # the snapshot still carries the value (verified separately by
        # the on-disk form_to_variant tests); just skip the UI check.
        ui_rendered_booleans = [k for k in bv.BOOLEAN_INPUTS if f'name="{k}"' in body]
        assert ui_rendered_booleans, "expected at least one BOOLEAN_INPUTS key in UI"
        for key in ui_rendered_booleans:
            assert re.search(rf'name="{re.escape(key)}"[^>]*checked', body, re.S), (
                f"UI-rendered BOOLEAN_INPUTS key '{key}' lost its 'on' state across 409 round-trip"
            )

        # Every UI-rendered DEFAULT_TRUE_INPUTS key should be UNCHECKED.
        ui_rendered_default_true = [k for k in bv.DEFAULT_TRUE_INPUTS if f'name="{k}"' in body]
        assert ui_rendered_default_true, "expected at least one DEFAULT_TRUE_INPUTS key in UI"
        for key in ui_rendered_default_true:
            assert not re.search(rf'name="{re.escape(key)}"[^>]*checked', body, re.S), (
                f"UI-rendered DEFAULT_TRUE_INPUTS key '{key}' lost its 'off' state across 409 round-trip"
            )
    finally:
        meta_path.write_bytes(snapshot)


def test_style_pending_pop_is_idempotent(client):
    """Same pattern as _PMSYNC_PENDING / _ENTRY_PENDING: tokens pop on
    first read; a stale token returns an empty dict."""
    meta_path = paths.data_dir() / "meta.yml"
    snapshot = meta_path.read_bytes()
    try:
        _, meta = yaml_io.load(meta_path)
        default = bv.default_variant_name(meta)
        idx = next(i for i, v in enumerate(meta["build_variants"]) if v.get("filename") == default)
        # Trigger a stash via 409.
        resp = client.post(
            "/style/save",
            data={
                "mode": "edit",
                "idx": str(idx),
                "mtime_ns": "1",
                "filename": "cv-idempotent",
                "audience": "full",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        # First pop: form values present.
        body1 = client.get(loc).get_data(as_text=True)
        assert 'value="cv-idempotent"' in body1
        # Second pop with the SAME token: snapshot already consumed; the
        # template falls back to the on-disk variant_to_form values.
        body2 = client.get(loc).get_data(as_text=True)
        assert 'value="cv-idempotent"' not in body2
    finally:
        meta_path.write_bytes(snapshot)
