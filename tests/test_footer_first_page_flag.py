"""footer_first_page: a per-variant `--input` that prints the dated footer on page 1.

`meta.yml`'s `footer.show_on_first_page` is global, so one variant (a CV a
committee wants dated on its first page) could not differ from the rest. The
input overrides it for that variant only; absent, the global setting applies.

Four seams carry it and each is pinned here: the editor's input list
(`BOOLEAN_INPUTS`, so the Style form can set and persist it), the Style form's
checkbox, the variant chip, and the `modern` template's `setup()` (the bespoke
template reads the same input; it is private and tested in the consumer).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from cv_editor import build_variants
from cv_editor.app import create_app
from pdf_text_extract import extract_words

ROOT = Path(__file__).resolve().parent.parent

_HAS_TYPST = shutil.which("typst") is not None
typst_required = pytest.mark.skipif(not _HAS_TYPST, reason="typst not on PATH")


def test_footer_first_page_is_a_default_false_boolean_input():
    assert "footer_first_page" in build_variants.BOOLEAN_INPUTS
    assert "footer_first_page" not in build_variants.DEFAULT_TRUE_INPUTS


def test_form_round_trip_persists_true_and_elides_false():
    form = build_variants.default_form()
    form["filename"] = "x"
    assert form["footer_first_page"] is False
    assert "footer_first_page" not in build_variants.form_to_variant(form)["inputs"]

    form["footer_first_page"] = True
    variant = build_variants.form_to_variant(form)
    assert variant["inputs"]["footer_first_page"] is True
    assert build_variants.variant_to_form(variant)["footer_first_page"] is True


def test_explicit_false_survives_a_style_save():
    """The checkbox can only say true, but `false` is a legitimate hand-written
    value (force the footer off for one variant). A save must not erase it."""
    from ruamel.yaml.comments import CommentedMap

    existing = CommentedMap(
        {"filename": "x", "inputs": CommentedMap({"audience": "full", "footer_first_page": False})}
    )
    form = build_variants.variant_to_form(existing)
    form["filename"] = "x"
    saved = build_variants.form_to_variant(form, existing=existing)
    assert saved["inputs"]["footer_first_page"] is False

    form["footer_first_page"] = True
    assert (
        build_variants.form_to_variant(form, existing=existing)["inputs"]["footer_first_page"]
        is True
    )


def test_explicit_false_gets_an_off_chip():
    chips = build_variants.variant_chips({"inputs": {"footer_first_page": False}})
    assert {"label": "footer_first_page: off", "kind": "off"} in chips


def test_variant_chip_names_the_flag():
    chips = build_variants.variant_chips({"inputs": {"footer_first_page": True}})
    assert {"label": "footer first page", "kind": "on"} in chips


def test_style_edit_page_renders_the_checkbox():
    app = create_app()
    app.config["TESTING"] = True
    body = app.test_client().get("/style/0/edit").data.decode("utf-8")
    assert 'name="footer_first_page"' in body
    assert "overrides that setting for this variant only" in body


def _page0_word_count(inputs: dict, *, meta_on_first_page: bool) -> int:
    """Compile the modern template from a throwaway copy of the tree whose
    meta.yml sets footer.show_on_first_page, so BOTH override directions are
    exercised against a known global setting."""
    with tempfile.TemporaryDirectory() as td:
        tree = Path(td) / "tree"
        tree.mkdir()
        for name in ("cv.typ", "data", "src", "templates"):
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(src, tree / name)
            else:
                shutil.copy(src, tree / name)
        meta = tree / "data" / "meta.yml"
        text = meta.read_text()
        assert "show_on_first_page:" in text
        lines = [
            (
                f"  show_on_first_page: {'true' if meta_on_first_page else 'false'}"
                if ln.strip().startswith("show_on_first_page:")
                else ln
            )
            for ln in text.splitlines()
        ]
        meta.write_text("\n".join(lines) + "\n")
        out = Path(td) / "o.pdf"
        argv = [
            "typst",
            "compile",
            "--root",
            str(tree),
            "--ignore-system-fonts",
            "--input",
            "template=modern",
        ]
        for k, v in inputs.items():
            argv += ["--input", f"{k}={v}"]
        argv += [str(tree / "cv.typ"), str(out)]
        proc = subprocess.run(argv, cwd=tree, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return len(extract_words(out)[0].words)


@typst_required
def test_modern_template_honours_the_override_in_both_directions():
    """With meta OFF, true must add the footer to page 1; with meta ON, false
    must remove it; absent must follow meta either way. Word counts, not text,
    so the assertions cannot be satisfied by page furniture."""
    off_absent = _page0_word_count({}, meta_on_first_page=False)
    off_true = _page0_word_count({"footer_first_page": "true"}, meta_on_first_page=False)
    on_absent = _page0_word_count({}, meta_on_first_page=True)
    on_false = _page0_word_count({"footer_first_page": "false"}, meta_on_first_page=True)
    assert off_true > off_absent, "footer_first_page=true added nothing to page 1"
    assert on_false < on_absent, "footer_first_page=false did not remove the footer"
    assert on_absent == off_true, "absent did not defer to a meta that turns the footer on"
    assert on_false == off_absent, "forced-off page differs from a meta-off page"
