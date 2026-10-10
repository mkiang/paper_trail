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


def test_variant_chip_names_the_flag():
    chips = build_variants.variant_chips({"inputs": {"footer_first_page": True}})
    assert {"label": "footer first page", "kind": "on"} in chips


def test_style_edit_page_renders_the_checkbox():
    app = create_app()
    app.config["TESTING"] = True
    body = app.test_client().get("/style/0/edit").data.decode("utf-8")
    assert 'name="footer_first_page"' in body
    assert "overrides that setting for this variant only" in body


def _page0_word_count(inputs: dict) -> int:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.pdf"
        argv = [
            "typst",
            "compile",
            "--root",
            str(ROOT),
            "--font-path",
            "fonts",
            "--ignore-system-fonts",
            "--input",
            "template=modern",
        ]
        for k, v in inputs.items():
            argv += ["--input", f"{k}={v}"]
        argv += [str(ROOT / "cv.typ"), str(out)]
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return len(extract_words(out)[0].words)


@typst_required
def test_modern_template_honours_the_override_in_both_directions():
    """True adds the footer to page 1; false removes it even when meta turns it
    on; absent leaves the global setting alone. Word counts, not text, so the
    assertion cannot be satisfied by page furniture."""
    absent = _page0_word_count({})
    forced_on = _page0_word_count({"footer_first_page": "true"})
    forced_off = _page0_word_count({"footer_first_page": "false"})
    assert forced_on > forced_off, "footer_first_page=true added nothing to page 1"
    assert forced_off <= absent <= forced_on
    assert absent in (forced_on, forced_off), "absent matched neither override: not 'defer to meta'"
