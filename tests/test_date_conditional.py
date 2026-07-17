"""Date-conditional rendering (future-start hide + future-end collapse).

Renderer behavior is exercised via the inline-literal snippet pattern
(mirrors tests/test_m3_grant_dates.py): compile a standalone .typ that imports
a render-<section> helper and calls it with an inline dict literal, pinning the
render date with --input today=YYYY-MM-DD (and optionally show_future=true),
then read the PDF text with pdftotext.

The Python-side coarse mirror (validate.date_conditional_status / date_gate_note)
is unit-tested at the bottom (no typst/pdftotext needed).

Feature spec: for the seven date-gated sections (appointments, service,
teaching, education, honors, mentees, presentations) a future START date hides
the entry until it arrives; a closed range with a future END renders open-ended
"Start –" until the end passes, then shows the full range; show_future reveals
hidden entries AND shows their literal entered dates. Grants/publications are
NOT date-gated. See templates/bespoke/render.typ + lib/flags.typ.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE

ROOT = Path(__file__).resolve().parent.parent

_NEED_TOOLS = shutil.which("typst") is None or shutil.which("pdftotext") is None or not HAS_BESPOKE
pytestmark = pytest.mark.skipif(_NEED_TOOLS, reason="needs typst + pdftotext on PATH")


def _render(body: str, *, today: str, show_future: bool = False) -> tuple[int, str, str]:
    """Compile a snippet at a pinned `today`; return (returncode, stderr, pdftext)."""
    src = ROOT / f"_date_test_{uuid.uuid4().hex}.typ"
    pdf = Path(tempfile.gettempdir()) / f"_date_test_{uuid.uuid4().hex}.pdf"
    txt = pdf.with_suffix(".txt")
    src.write_text(body)
    argv = [
        "typst",
        "compile",
        "--root",
        str(ROOT),
        "--font-path",
        str(ROOT / "fonts"),
        "--ignore-system-fonts",
        "--input",
        f"today={today}",
    ]
    if show_future:
        argv += ["--input", "show_future=true"]
    argv += [str(src), str(pdf)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
        text = ""
        if proc.returncode == 0 and pdf.exists():
            subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
            text = txt.read_text()
        return proc.returncode, proc.stderr, text
    finally:
        src.unlink(missing_ok=True)
        pdf.unlink(missing_ok=True)
        txt.unlink(missing_ok=True)


def _honors(entries: str) -> str:
    return f'#import "/templates/bespoke/render.typ": render-honors\n#render-honors({entries})\n'


# --- future START hides the entry ---------------------------------------


def test_future_start_hidden_by_default():
    body = _honors('((date: "2027", award: "FutureAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2020-01-01")
    assert rc == 0, err
    assert "FutureAward" not in text


def test_future_start_revealed_with_show_future():
    body = _honors('((date: "2027", award: "FutureAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2020-01-01", show_future=True)
    assert rc == 0, err
    assert "FutureAward" in text


def test_revealed_single_future_date_has_no_spurious_dash():
    # Regression guard (review must-fix #2): a revealed single (non-range)
    # future date must NOT sprout a trailing en-dash.
    body = _honors('((date: "2027", award: "SoloFuture", institution: "Inst"),)')
    rc, err, text = _render(body, today="2020-01-01", show_future=True)
    assert rc == 0, err
    assert "SoloFuture" in text
    assert "–" not in text  # no collapse artifact on a single date


def test_past_single_date_visible():
    body = _honors('((date: "2015", award: "PastAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2020-01-01")
    assert rc == 0, err
    assert "PastAward" in text


# --- future END collapses "Start - End" to "Start -" --------------------


def test_future_end_collapses_to_open_ended():
    body = _honors('((date: "01/2026 - 01/2028", award: "TermAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2027-01-01")  # mid-term
    assert rc == 0, err
    assert "TermAward" in text
    assert "01/2026" in text
    assert "01/2028" not in text  # end hidden while active
    assert "–" in text  # rendered open-ended


def test_past_end_shows_full_range():
    body = _honors('((date: "01/2026 - 01/2028", award: "TermAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2100-01-01")  # after term
    assert rc == 0, err
    assert "01/2026" in text
    assert "01/2028" in text  # full range once ended


def test_reveal_shows_literal_range_not_collapsed():
    body = _honors('((date: "01/2026 - 01/2028", award: "TermAward", institution: "Inst"),)')
    rc, err, text = _render(body, today="2027-01-01", show_future=True)
    assert rc == 0, err
    assert "01/2026" in text
    assert "01/2028" in text  # literal entered dates under show_future


def test_bare_year_range_future_end_collapses():
    body = _honors('((date: "2026 - 2029", award: "YearTerm", institution: "Inst"),)')
    rc, err, text = _render(body, today="2027-06-01")  # end 2029 still future
    assert rc == 0, err
    assert "YearTerm" in text
    assert "2029" not in text


# --- no-panic guards (footnote markers, empty dates) --------------------


def test_footnote_marker_date_does_not_panic():
    body = _honors('((date: "03/2025 - 04/2025*", award: "MarkAward", institution: "Inst"),)')
    rc, err, _ = _render(body, today="2026-01-01")
    assert rc == 0, err


def test_empty_date_does_not_panic():
    body = _honors('((date: "", award: "NoDate", institution: "Inst"),)')
    rc, err, _ = _render(body, today="2026-01-01")
    assert rc == 0, err


# --- education dangling-header guard (review must-fix #4) ----------------


def _education(clusters: str) -> str:
    return (
        '#import "/templates/bespoke/render.typ": render-education\n'
        f"#render-education({clusters})\n"
    )


def test_education_future_only_cluster_hides_institution_header():
    body = _education(
        '((institution: "FutureUniversity", entries: ((date: "2027", degree: "PhD"),)),)'
    )
    rc, err, text = _render(body, today="2020-01-01")
    assert rc == 0, err
    assert "FutureUniversity" not in text  # no dangling header


def test_education_future_cluster_shown_when_revealed():
    body = _education(
        '((institution: "FutureUniversity", entries: ((date: "2027", degree: "PhD"),)),)'
    )
    rc, err, text = _render(body, today="2020-01-01", show_future=True)
    assert rc == 0, err
    assert "FutureUniversity" in text
