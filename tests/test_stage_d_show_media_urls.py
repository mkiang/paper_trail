"""Stage D / I6 (2026-05-25): show_media_urls flag.

Adds a render-time flag that controls whether media outlet names render
as hyperlinks (default true; set false to render plain text). Default
true preserves byte-identical output for all existing variants.

The flag is the second default-true flag (first was show_dollars) so it
follows the same carve-out pattern in build_variants.py: NOT in
BOOLEAN_INPUTS (which is "default false, set true to enable"); persist
only when explicitly false; chip only when explicitly false.

The templates/bespoke/emit.typ mirror (emit-media-outlets) was updated in lockstep
with templates/bespoke/render.typ:format-media-outlets. The byte-diff drift guard in
tests/test_flatten.py exercises BOTH branches via the
everything_plain_media variant (show_media_urls=false).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE, bespoke_required, flags_typ_path
from cv_editor import build_variants
from cv_editor.app import create_app
from ruamel.yaml.comments import CommentedMap

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---- default_form / variant_to_form / form_to_variant round-trips ----


def test_default_form_includes_show_media_urls_true():
    """A brand-new variant defaults the box checked (renderer default
    is true; checkbox state must match)."""
    form = build_variants.default_form()
    assert form["show_media_urls"] is True


def test_variant_to_form_treats_missing_key_as_true():
    """A variant whose meta.yml doesn't carry show_media_urls (the
    common case: default true is elided) renders the box checked."""
    variant = CommentedMap()
    variant["inputs"] = CommentedMap()
    out = build_variants.variant_to_form(variant)
    assert out["show_media_urls"] is True


def test_variant_to_form_treats_explicit_false_as_unchecked():
    variant = CommentedMap()
    inputs = CommentedMap()
    inputs["show_media_urls"] = False
    variant["inputs"] = inputs
    out = build_variants.variant_to_form(variant)
    assert out["show_media_urls"] is False


def test_form_to_variant_elides_default_true():
    """Checked box (the common case) writes NO key to meta.yml — we
    don't want every variant cluttered with show_media_urls: true."""
    form = build_variants.default_form()
    form["filename"] = "test"
    variant = build_variants.form_to_variant(form)
    assert "show_media_urls" not in (variant.get("inputs") or {})


def test_form_to_variant_persists_explicit_false():
    """Unchecked box writes show_media_urls: false to meta.yml."""
    form = build_variants.default_form()
    form["filename"] = "test"
    form["show_media_urls"] = False
    variant = build_variants.form_to_variant(form)
    assert variant["inputs"]["show_media_urls"] is False


def test_form_to_variant_preserves_existing_show_media_urls_false_on_round_trip():
    """A user opens an existing variant with show_media_urls=false,
    re-renders the form (box unchecked), saves without changes —
    the false value survives."""
    existing = CommentedMap()
    existing_inputs = CommentedMap()
    existing_inputs["show_media_urls"] = False
    existing["inputs"] = existing_inputs
    existing["filename"] = "test"
    form = build_variants.variant_to_form(existing)
    form["filename"] = "test"
    variant = build_variants.form_to_variant(form, existing=existing)
    assert variant["inputs"]["show_media_urls"] is False


# ---- variant_chips ----


def test_variant_chips_silent_when_show_media_urls_default():
    """Default-true is silent — no chip on variants that don't
    explicitly turn it off (matches show_dollars chip behavior)."""
    variant = CommentedMap()
    variant["filename"] = "test"
    variant["inputs"] = CommentedMap()
    chips = build_variants.variant_chips(variant)
    labels = [c["label"] for c in chips]
    assert not any("show_media_urls" in label for label in labels)


def test_variant_chips_emits_off_chip_when_explicit_false():
    variant = CommentedMap()
    variant["filename"] = "test"
    inputs = CommentedMap()
    inputs["show_media_urls"] = False
    variant["inputs"] = inputs
    chips = build_variants.variant_chips(variant)
    matching = [c for c in chips if "show_media_urls" in c["label"]]
    assert len(matching) == 1
    assert matching[0]["kind"] == "off"
    assert matching[0]["label"] == "show_media_urls: off"


# ---- Style editor route smoke ----


def test_style_edit_renders_show_media_urls_checkbox(client):
    """GET /style/<idx>/edit for ANY variant must render the new
    checkbox. With default-true, the box is checked unless the variant
    explicitly carries show_media_urls: false in meta.yml."""
    # Use the first build_variant (idx=0); the route is /style/<int:idx>/edit.
    resp = client.get("/style/0/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'name="show_media_urls"' in body
    # The descriptive prose mentions the show_media dependency so the
    # user understands when toggling does nothing.
    assert "Has no effect when" in body
    # Default-true → checked attribute present (no variant in meta.yml
    # currently flips it false).
    assert 'name="show_media_urls" checked' in body


# ---- _KNOWN_INPUT_KEYS ----


def test_known_input_keys_includes_show_media_urls():
    """If a user added a custom flag to meta.yml AND the form has
    show_media_urls support, _KNOWN_INPUT_KEYS must list the latter so
    form_to_variant doesn't strip it on round-trip."""
    assert "show_media_urls" in build_variants._KNOWN_INPUT_KEYS


# ---- Renderer integration: the flag is recognized by lib/flags.typ ----


def test_show_media_urls_default_true_in_flags_typ():
    """Smoke test that lib/flags.typ declares the flag with the
    expected default. Catches a typo'd flag name or default value."""
    flags = flags_typ_path(ROOT).read_text()
    assert 'show_media_urls' in flags
    assert '"show_media_urls"' in flags
    assert 'default: "true"' in flags
    # The Typst variable name uses kebab-case per the file's convention.
    assert "show-media-urls" in flags


@bespoke_required
def test_show_media_urls_imported_in_render_and_emit():
    """Both templates/bespoke/render.typ and templates/bespoke/emit.typ must import the flag, or
    the gated branches will fail at compile time. Pinned because the
    emit mirror is load-bearing per scripts/CLAUDE.md gotcha #41."""
    render = (ROOT / "templates" / "bespoke" / "render.typ").read_text()
    emit = (ROOT / "templates" / "bespoke" / "emit.typ").read_text()
    assert "show-media-urls" in render
    assert "show-media-urls" in emit


# ---- POST /style/save round-trip: the regression guard the post-impl
# review caught was missing (the bug it caught: style_save read every
# OTHER flag from request.form but silently ignored show_media_urls). ----


def test_style_save_persists_show_media_urls_false(client, tmp_path, monkeypatch):
    """POST /style/save with show_media_urls unchecked must write
    `show_media_urls: false` to the saved variant's inputs. Without
    this test, the original Stage D implementation silently dropped
    the user's toggle (the form-build block in style_save read
    show_dollars + BOOLEAN_INPUTS but not show_media_urls)."""
    # Copy meta.yml to a tmp location so we don't mutate the real one.
    import shutil

    src_meta = ROOT / "data" / "meta.yml"
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    tmp_meta = tmp_data / "meta.yml"
    shutil.copy(src_meta, tmp_meta)

    # Re-create app pointed at the tmp workspace via the P1 seam (data_dir
    # is the WORKSPACE root — parent of data/; the reset fixture in
    # conftest restores the default root after the test).
    from cv_editor.app import create_app

    app = create_app(data_dir=tmp_path)
    app.config["TESTING"] = True
    c = app.test_client()

    # Any variant works for this round-trip; use the first (data-agnostic).
    import yaml as _yaml
    from cv_editor import yaml_io

    initial_text = tmp_meta.read_text()
    parsed = _yaml.safe_load(initial_text)
    variants = parsed.get("build_variants") or []
    cv_idx = 0
    cv_variant = variants[cv_idx]
    cv_filename = cv_variant.get("filename") or ""
    cv_audience = (cv_variant.get("inputs") or {}).get("audience", "")
    mtime_ns = yaml_io.mtime_ns(tmp_meta)

    # POST every field as a properly-rendered form WITHOUT the
    # show_media_urls checkbox (simulating the user un-checking it).
    # Critical: pass mode=edit + idx + mtime_ns so the route accepts.
    resp = c.post(
        "/style/save",
        data={
            "mode": "edit",
            "idx": str(cv_idx),
            "mtime_ns": str(mtime_ns),
            "filename": cv_filename,
            "audience": cv_audience,  # keep the variant's own audience
            # show_dollars: omit = unchecked → expect false in YAML
            # show_media_urls: omit = unchecked → expect false in YAML
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), (
        f"unexpected status {resp.status_code}; body: {resp.get_data(as_text=True)[:500]}"
    )

    # Re-parse the saved YAML and assert show_media_urls=false survives.
    saved_text = tmp_meta.read_text()
    saved = _yaml.safe_load(saved_text)
    cv_after = saved["build_variants"][cv_idx]
    inputs_after = cv_after.get("inputs") or {}
    assert inputs_after.get("show_media_urls") is False, (
        "show_media_urls toggle was silently dropped — the style_save "
        "route's form-build block must include the field. "
        f"saved inputs: {inputs_after}"
    )


# ---- Render-time effect: PDF for show_media_urls=false has fewer
# link annotations than the default-true variant. End-to-end smoke
# that the flag's visible effect is real (the byte-identical test in
# test_flatten only verifies render-vs-emit parity, not that links
# are actually missing). ----


def test_pdf_has_fewer_link_annotations_when_show_media_urls_false(tmp_path):
    """Build everything.pdf with show_media_urls=true (default) and
    show_media_urls=false; assert the second has strictly FEWER link
    annotations. This is the only test that exercises the user-visible
    effect of the flag end-to-end."""
    import shutil
    import subprocess

    if not (shutil.which("typst") and HAS_BESPOKE):
        pytest.skip("typst not on PATH")
    try:
        import fitz  # PyMuPDF
    except ImportError:
        pytest.skip("PyMuPDF (fitz) not installed")

    cv_typ = ROOT / "cv.typ"
    common_inputs = [
        "--input",
        "audience=industry",
        "--input",
        "show_highlighted=true",
        "--input",
        "show_dollars=false",
        "--input",
        "show_oa=true",
        "--input",
        "show_citations=true",
        "--input",
        "show_notes=true",
        "--input",
        "show_media=true",
    ]

    def _compile_and_count_links(out: Path, extra_inputs: list[str]) -> int:
        argv = [
            "typst",
            "compile",
            "--root",
            str(ROOT),
            "--font-path",
            "fonts",
            "--ignore-system-fonts",
            *common_inputs,
            *extra_inputs,
            str(cv_typ),
            str(out),
        ]
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 0, f"typst compile failed:\n{proc.stderr}"
        doc = fitz.open(out)
        count = sum(len(page.get_links()) for page in doc)
        doc.close()
        return count

    linked = _compile_and_count_links(tmp_path / "linked.pdf", [])
    plain = _compile_and_count_links(
        tmp_path / "plain.pdf",
        ["--input", "show_media_urls=false"],
    )
    assert plain < linked, (
        f"Expected fewer PDF link annotations with show_media_urls=false; "
        f"got linked={linked}, plain={plain}. The flag's visible effect "
        "is missing — check format-media-outlets in templates/bespoke/render.typ."
    )
