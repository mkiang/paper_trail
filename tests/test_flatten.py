"""Byte-identical guard for the flatten feature (Part 3, publications-first).

`templates/bespoke/emit.typ` is a PARALLEL re-implementation of the publications render path
(it emits literal Typst source instead of laying out content). It must produce
glyph positions identical to the canonical render path. `tests/flatten_probe.typ`
renders the full document with publications routed through `emit-publications`
and every other section through the real `render-*` path; this test compiles it
and `cv.typ` with the same inputs and asserts the extracted glyph positions match
(the 0.01pt gate used by test_typography_knobs).

Validated on two variants: `cv` (plain) and `everything` (exercises OA
sub-bullets, citation counts, media grouping, highlighted entries, co-author
footnotes). When you change a mirrored render.typ helper, update templates/bespoke/emit.typ
until this test is green again.

Run: PYTHONPATH=scripts .venv/bin/python -m pytest tests/test_flatten.py -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE, bespoke_required, flags_typ_path
from pdf_text_extract import extract_words

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "flatten_probe.typ"
CANON = ROOT / "cv.typ"
EMIT = ROOT / "templates" / "bespoke" / "emit.typ"
RENDER = ROOT / "templates" / "bespoke" / "render.typ"

# Flag sets for the validated variants.
# `cv` here is the bare no-flags render (empty inputs) — a self-contained
# byte-identical-freeze fixture, NOT the old `cv` meta variant (removed
# 2026-06-08); the freezer always names the frozen artifact cv.typ/cv.pdf.
# `everything` mirrors data/meta.yml build_variants. The third
# entry `everything_plain_media` is a Stage D / I6 (2026-05-25) drift
# guard: it flips show_media_urls=false so the byte-diff exercises
# format-media-outlets's no-link branch in BOTH templates/bespoke/render.typ AND
# templates/bespoke/emit.typ. Without this fixture the default (show_media_urls=true)
# is the only branch validated, and the emit.typ mirror could silently
# drift on the false branch.
VARIANTS = {
    "cv": {},
    "everything": {
        "audience": "industry",
        "show_highlighted": "true",
        "show_dollars": "false",
        "show_oa": "true",
        "show_citations": "true",
        "show_notes": "true",
        "show_media": "true",
        # 2026-05-26: show_hidden_media added to exercise the highlighted-
        # outlet branch in both templates/bespoke/render.typ and templates/bespoke/emit.typ. Without
        # this, the byte-diff only covers the non-hidden filter branch.
        "show_hidden_media": "true",
    },
    "everything_plain_media": {
        "audience": "industry",
        "show_highlighted": "true",
        "show_dollars": "false",
        "show_oa": "true",
        "show_citations": "true",
        "show_notes": "true",
        "show_media": "true",
        "show_hidden_media": "true",
        "show_media_urls": "false",
    },
    # 2026-05-26: validate the show_hidden_media=true / show_media=false
    # slice (only highlighted outlets render). Catches drift in the
    # filter predicate's `hl` branch.
    "hidden_media_only": {
        "audience": "industry",
        "show_notes": "true",
        "show_hidden_media": "true",
    },
    # 2026-07-14: date-conditional feature. Pin `today` into the PAST so many
    # real entries become future-start (hidden) or future-end (collapsed),
    # exercising render-today / active-form / the date-gated entry-visible in
    # BOTH render.typ and emit.typ. At the real clock no entry is future-dated,
    # so without this the new emit branches would never be byte-diffed.
    # show_future stays OFF so hide AND collapse both fire.
    "past_today_date_gated": {
        "audience": "full",
        "today": "2016-01-01",
    },
}

_HAS_TYPST = shutil.which("typst") is not None and HAS_BESPOKE  # P5: + bespoke/fonts
typst_required = pytest.mark.skipif(not _HAS_TYPST, reason="typst not on PATH")


def _compile(entry: Path, inputs: dict, out: Path) -> None:
    argv = [
        "typst",
        "compile",
        "--root",
        str(ROOT),
        "--font-path",
        "fonts",
        "--ignore-system-fonts",
    ]
    for k, v in inputs.items():
        argv += ["--input", f"{k}={v}"]
    argv += [str(entry), str(out)]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"typst compile failed for {entry.name}:\n{proc.stderr}"


def _glyph_diffs(pdf_a: Path, pdf_b: Path) -> list[str]:
    a, b = extract_words(pdf_a), extract_words(pdf_b)
    out = []
    if len(a) != len(b):
        return [f"page count {len(a)} != {len(b)}"]
    for pi, (pa, pb) in enumerate(zip(a, b)):
        if len(pa.words) != len(pb.words):
            out.append(f"page {pi}: word count {len(pa.words)} != {len(pb.words)}")
            continue
        for wc, wf in zip(pa.words, pb.words):
            if wc.text != wf.text or abs(wc.x0 - wf.x0) > 0.01 or abs(wc.y0 - wf.y0) > 0.01:
                out.append(
                    f"page {pi}: {wc.text!r}@({wc.x0:.2f},{wc.y0:.2f}) "
                    f"!= {wf.text!r}@({wf.x0:.2f},{wf.y0:.2f})"
                )
    return out


@typst_required
@pytest.mark.parametrize("variant", list(VARIANTS))
def test_emitted_publications_byte_identical(variant):
    inputs = VARIANTS[variant]
    with tempfile.TemporaryDirectory() as td:
        canon = Path(td) / "canon.pdf"
        probe = Path(td) / "probe.pdf"
        _compile(CANON, inputs, canon)
        _compile(PROBE, inputs, probe)
        diffs = _glyph_diffs(canon, probe)
        assert not diffs, (
            f"emit-publications diverges from render-publications on '{variant}':\n"
            + "\n".join(diffs[:20])
        )


@typst_required
@pytest.mark.parametrize("variant", list(VARIANTS))
def test_frozen_file_byte_identical(variant):
    """The actual freeze artifact (a standalone single cv.typ produced by
    freezer.freeze_workspace) renders byte-identical to the canonical build."""
    import shutil as _sh

    from cv_editor import freezer

    inputs = VARIANTS[variant]
    with tempfile.TemporaryDirectory() as td:
        canon = Path(td) / "canon.pdf"
        _compile(CANON, inputs, canon)
        r = freezer.freeze_workspace(variant_inputs=inputs, variant_name=variant)
        try:
            cv_typ = (r.path / "cv.typ").read_text()
            assert "#import" not in cv_typ
            frozen_pdf = r.path / "cv.pdf"
            proc = subprocess.run(
                [
                    "typst",
                    "compile",
                    "--font-path",
                    "fonts",
                    "--ignore-system-fonts",
                    "cv.typ",
                    "cv.pdf",
                ],
                cwd=r.path,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
            diffs = _glyph_diffs(canon, frozen_pdf)
            assert not diffs, f"frozen {variant} cv.typ diverges from canonical:\n" + "\n".join(
                diffs[:20]
            )
        finally:
            _sh.rmtree(r.path, ignore_errors=True)


@bespoke_required
def test_flatten_ty_fields_match_discover_knobs():
    """The ty-dict field list baked into flatten.typ must cover exactly the
    knobs typography_knobs.discover_knobs finds (pure-Python; always runs).
    Field name `body-size` ↔ knob meta_key `ty_body_size`."""
    from cv_editor import typography_knobs

    flat = (ROOT / "flatten.typ").read_text()
    # Pull the field names from the _ty-fields tuple-of-pairs.
    fields = set(re.findall(r'\(\s*"([a-z0-9-]+)"\s*,\s*ty\.', flat))
    assert fields, "no ty fields parsed from flatten.typ"
    # Knob.meta_key is the ty_-stripped, underscored name (e.g. "body_leading");
    # the ty-dict field uses hyphens ("body-leading").
    knob_fields = {k.meta_key.replace("_", "-") for k in typography_knobs.discover_knobs()}
    assert fields == knob_fields, (
        "flatten.typ ty-dict drifted from typography_knobs.discover_knobs:\n"
        f"  only in flatten.typ: {sorted(fields - knob_fields)}\n"
        f"  only in knobs:       {sorted(knob_fields - fields)}"
    )


def test_frozen_visible_def_matches_flags_typ():
    """freezer._VISIBLE_DEF is a hand-copy of lib/flags.typ:visible(). The
    byte-diff test cannot catch a drift here (the emitted body never exercises
    a non-trivial visible() path), so guard it directly with a string compare."""
    from cv_editor import freezer

    flags = flags_typ_path(ROOT).read_text()
    m = re.search(r"^#let visible\(.*?^\}", flags, flags=re.DOTALL | re.MULTILINE)
    assert m, "could not locate visible() in flags.typ"

    def norm(s):
        return "\n".join(line.rstrip() for line in s.strip().splitlines())

    assert norm(m.group(0)) == norm(freezer._VISIBLE_DEF), (
        "freezer._VISIBLE_DEF drifted from lib/flags.typ:visible(); re-sync it."
    )


@bespoke_required
def test_emit_footnote_sentences_match_author_flags_spec():
    """emit.typ hardcodes the co-author footnote glyph+sentence for each flag.
    Pin them to cv_editor.author_flags (the source of truth, already mirrored
    into render.typ) so a wording change can't silently desync the frozen file."""
    from cv_editor import author_flags

    emit_src = EMIT.read_text()
    for f in author_flags.AUTHOR_FLAGS:
        needle = f"#super[{f.glyph}]{f.footnote}"
        assert needle in emit_src, (
            f"emit.typ missing/!= author_flags footnote for {f.key!r}: expected {needle!r}"
        )


@bespoke_required
def test_emit_mirror_anchors_reference_real_render_helpers():
    """Every `// EMIT_MIRROR: render.typ <name>` in emit.typ must name a helper
    that actually exists in render.typ — a cheap, pure-Python drift signal that
    a mirrored helper was renamed/removed without updating emit.typ."""
    emit_src = EMIT.read_text()
    render_src = RENDER.read_text()
    # Only lines that START with the marker are anchors (so prose mentioning
    # the marker in the header comment isn't picked up).
    names = re.findall(r"^// EMIT_MIRROR: render\.typ (\S+)\s*$", emit_src, re.MULTILINE)
    assert names, "no EMIT_MIRROR anchors found in templates/bespoke/emit.typ"
    for name in names:
        assert f"#let {name}(" in render_src or f"#let {name} " in render_src, (
            f"emit.typ mirrors render.typ:{name} but no `#let {name}` exists there"
        )
