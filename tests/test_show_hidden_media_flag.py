"""show_media + show_hidden_media split (2026-05-26).

Pre-change: `show_media=true` rendered the media block, but individual
outlets marked `highlighted: true` only surfaced when the global
`show_highlighted=true` was also set. The user wanted finer control:
two media-specific flags so they can opt into the hidden pile without
turning on the global highlight switch.

New semantics:
- `show_media=true`              -> non-hidden outlets only render
- `show_hidden_media=true`       -> highlighted: true outlets only render
- both                           -> all outlets render
- neither                        -> media block suppressed entirely
- `show_highlighted` no longer affects individual outlets (still gates
  whole `type: media, highlighted: true` notes -- rare in practice).

This test compiles the real CV with each of the 4 flag combos and
asserts that fixture outlets (one known-hidden, one known-non-hidden,
both unique in the data) appear/disappear as expected.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE
from cv_editor import yaml_io
from pdf_text_extract import extract_words

ROOT = Path(__file__).resolve().parent.parent
CV = ROOT / "cv.typ"

# Fixture outlets are DERIVED from data/publications.yml at run time (not
# hardcoded), so no real outlet name ships in source. We pick one outlet name
# that appears exactly once and is highlighted (hidden pile) and one that
# appears exactly once and is NOT highlighted; single-occurrence makes each
# outlet's presence/absence in the rendered PDF an unambiguous signal.


def _derive_media_fixtures():
    """Return (hidden, visible): a unique highlighted outlet name and a unique
    non-highlighted outlet name from the live corpus. Either is None if the
    corpus lacks such an outlet (e.g. the public example corpus)."""
    _, data = yaml_io.load(ROOT / "data" / "publications.yml")
    counts = Counter()
    highlighted = {}
    for sub in data or []:
        for entry in sub.get("entries", []) or []:
            for note in entry.get("notes", []) or []:
                if not (isinstance(note, dict) and note.get("type") == "media"):
                    continue
                for outlet in note.get("outlets", []) or []:
                    if isinstance(outlet, dict):
                        name = outlet.get("name") or outlet.get("text")
                        hl = bool(outlet.get("highlighted"))
                    else:
                        name, hl = str(outlet), False
                    if not name:
                        continue
                    counts[name] += 1
                    highlighted[name] = hl  # for count==1 this is THE value
    hidden = next((n for n, c in counts.items() if c == 1 and highlighted[n]), None)
    visible = next((n for n, c in counts.items() if c == 1 and not highlighted[n]), None)
    return hidden, visible


@pytest.fixture(scope="module")
def media_fixtures():
    hidden, visible = _derive_media_fixtures()
    if not hidden or not visible:
        pytest.skip("corpus lacks a unique highlighted + unique non-highlighted media outlet")
    return hidden, visible


# Common inputs that surface media notes at all (show_notes is the
# master gate over typed-note sub-bullets).
BASE = {"audience": "industry", "show_notes": "true"}

_HAS_TYPST = shutil.which("typst") is not None and HAS_BESPOKE  # P5: + bespoke/fonts
typst_required = pytest.mark.skipif(not _HAS_TYPST, reason="typst not on PATH")


def _compile(inputs: dict, out: Path) -> None:
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
    argv += [str(CV), str(out)]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"typst compile failed:\n{proc.stderr}"


def _text_of(pdf: Path) -> str:
    return " ".join(w.text for page in extract_words(pdf) for w in page.words)


@pytest.fixture(scope="module")
def workdir():
    d = Path(tempfile.mkdtemp(prefix="test_show_hidden_media_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@typst_required
def test_neither_flag_hides_all_media(workdir, media_fixtures):
    """show_media=false + show_hidden_media=false: neither fixture
    outlet appears (media block entirely suppressed)."""
    hidden, visible = media_fixtures
    out = workdir / "neither.pdf"
    _compile(BASE, out)
    text = _text_of(out)
    assert hidden not in text
    assert visible not in text


@typst_required
def test_show_media_only_shows_non_hidden(workdir, media_fixtures):
    """show_media=true alone: non-hidden outlet appears, hidden does not."""
    hidden, visible = media_fixtures
    out = workdir / "media_only.pdf"
    _compile({**BASE, "show_media": "true"}, out)
    text = _text_of(out)
    assert visible in text, (
        f"{visible!r} missing -- show_media=true should surface non-highlighted outlets"
    )
    assert hidden not in text, (
        f"{hidden!r} leaked -- show_media alone should NOT surface "
        f"highlighted: true outlets; that requires show_hidden_media"
    )


@typst_required
def test_show_hidden_media_only_shows_hidden(workdir, media_fixtures):
    """show_hidden_media=true alone: hidden outlet appears, non-hidden does not.
    This is the new behavior; previously there was no way to get this slice."""
    hidden, visible = media_fixtures
    out = workdir / "hidden_only.pdf"
    _compile({**BASE, "show_hidden_media": "true"}, out)
    text = _text_of(out)
    assert hidden in text, (
        f"{hidden!r} missing -- show_hidden_media=true should surface highlighted: true outlets"
    )
    assert visible not in text, (
        f"{visible!r} leaked -- show_hidden_media alone should NOT "
        f"surface non-highlighted outlets; that requires show_media"
    )


@typst_required
def test_both_flags_show_all_media(workdir, media_fixtures):
    """show_media=true + show_hidden_media=true: both fixture outlets appear.
    This is the pre-2026-05-26 `show_media=true show_highlighted=true`
    behavior, now controlled by two media-specific flags."""
    hidden, visible = media_fixtures
    out = workdir / "both.pdf"
    _compile({**BASE, "show_media": "true", "show_hidden_media": "true"}, out)
    text = _text_of(out)
    assert visible in text
    assert hidden in text


@typst_required
def test_show_highlighted_no_longer_surfaces_hidden_outlets(workdir, media_fixtures):
    """Regression guard: `show_highlighted=true` used to surface
    highlighted media outlets even when show_media was on without
    show_hidden_media. As of 2026-05-26, show_highlighted should
    NOT affect individual outlets."""
    hidden, visible = media_fixtures
    out = workdir / "media_plus_global_highlight.pdf"
    _compile({**BASE, "show_media": "true", "show_highlighted": "true"}, out)
    text = _text_of(out)
    assert visible in text
    # The key assertion: even with show_highlighted=true on, the hidden
    # outlet stays hidden because show_hidden_media is off.
    assert hidden not in text, (
        f"{hidden!r} leaked when show_highlighted=true but "
        f"show_hidden_media=false. The 2026-05-26 split decoupled outlet "
        f"visibility from the global show_highlighted flag; outlets are now "
        f"controlled only by show_media + show_hidden_media."
    )


# ---- Style editor: form persistence guards (mirrors the Stage D
# show_media_urls guards in tests/test_stage_d_show_media_urls.py, which
# exist because a new BOOLEAN_INPUTS entry can be silently inert if any
# of the 5 touchpoints isn't updated — see scripts/CLAUDE.md gotcha #50). ----

from cv_editor import build_variants  # noqa: E402
from cv_editor.app import create_app  # noqa: E402


def test_show_hidden_media_is_in_boolean_inputs():
    """The flag must be in BOOLEAN_INPUTS so default_form / variant_to_form
    / form_to_variant / style_save all loop it in. Missing it here is the
    Stage D failure mode that caused show_media_urls to be silently inert."""
    assert "show_hidden_media" in build_variants.BOOLEAN_INPUTS


def test_default_form_includes_show_hidden_media_false():
    """Brand-new variant defaults the checkbox UNchecked (renderer default
    is false, same as show_media)."""
    form = build_variants.default_form()
    assert form["show_hidden_media"] is False


def test_form_to_variant_persists_show_hidden_media_true():
    """Checked box writes show_hidden_media: true to meta.yml."""
    form = build_variants.default_form()
    form["filename"] = "test"
    form["show_hidden_media"] = True
    variant = build_variants.form_to_variant(form)
    assert variant["inputs"]["show_hidden_media"] is True


def test_style_edit_renders_show_hidden_media_checkbox():
    """GET /style/<idx>/edit renders the new checkbox alongside show_media."""
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/style/0/edit")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert 'name="show_hidden_media"' in body
    # Description clarifies the pairing with show_media.
    assert "show_hidden_media" in body
    assert "Pair with" in body  # show_media description references show_hidden_media


def test_everything_variant_has_show_hidden_media_true_in_meta():
    """data/meta.yml's `everything` variant should carry show_hidden_media:
    true so it preserves the pre-split behavior of surfacing all outlets."""
    from pathlib import Path

    from cv_editor import yaml_io

    _, data = yaml_io.load(Path(__file__).resolve().parent.parent / "data" / "meta.yml")
    for v in data.get("build_variants", []):
        if v.get("filename") == "everything":
            inputs = v.get("inputs") or {}
            assert inputs.get("show_hidden_media") is True, (
                "everything variant lost show_hidden_media: true — the 2026-05-26 "
                "split adds it to preserve prior behavior. If this fired, the "
                "everything variant is now missing hidden outlets in renders."
            )
            return
    pytest.skip("no 'everything' variant in shipped meta")
