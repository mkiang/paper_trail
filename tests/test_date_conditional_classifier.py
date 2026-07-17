"""Pure-Python coarse mirror of the date-conditional render feature.

These test validate.date_conditional_status / date_gate_note — the editor-side
classifier that drives the list badge, entry-view banner, and index count. It is
a COARSE mirror of templates/bespoke/render.typ (the renderer is the source of
truth); no typst/pdftotext needed, so the pure-Python classifier tests never
skip. The lone exception is test_date_gated_sections_match_renderer, which READS
templates/bespoke/render.typ and is therefore @bespoke_required (skips cleanly on
a bespoke-absent tree).
"""

from __future__ import annotations

from datetime import date

from _engine_guards import bespoke_required
from cv_editor import validate


def test_status_future_start():
    t = date(2020, 1, 1)
    assert validate.date_conditional_status("2027", today=t) == "future_start"
    assert validate.date_conditional_status("06/2027", today=t) == "future_start"
    assert validate.date_conditional_status("2027 - 2029", today=t) == "future_start"


def test_status_future_end():
    t = date(2027, 1, 1)
    assert validate.date_conditional_status("01/2026 - 01/2028", today=t) == "future_end"
    assert validate.date_conditional_status("2026 - 2029", today=t) == "future_end"


def test_status_none_for_normal():
    t = date(2027, 1, 1)
    assert validate.date_conditional_status("2015", today=t) is None
    assert validate.date_conditional_status("01/2020 - 12/2021", today=t) is None
    assert validate.date_conditional_status("01/2022 -", today=t) is None  # already open
    assert validate.date_conditional_status("", today=t) is None


def test_footnote_marker_end_still_classified():
    t = date(2026, 1, 1)
    # end 04/2027 is future; trailing marker must be stripped
    assert validate.date_conditional_status("03/2025 - 04/2027*", today=t) == "future_end"


def test_gate_note_only_for_date_gated_sections():
    t = date(2020, 1, 1)
    entry = {"date": "2027"}
    assert validate.date_gate_note(entry, "honors", today=t)
    # grants excluded
    assert validate.date_gate_note(entry, "research_support", today=t) is None


def test_gate_note_reports_other_gates():
    t = date(2020, 1, 1)
    entry = {"date": "2027", "highlighted": True, "hide-from": ["public-health"]}
    note = validate.date_gate_note(entry, "honors", today=t)
    assert note
    assert "highlighted" in note
    assert "public-health" in note


@bespoke_required
def test_date_gated_sections_match_renderer():
    # Guard against drift between the Python mirror's section set and the
    # `date-gated: true` call sites in templates/bespoke/render.typ.
    from pathlib import Path

    render = (
        Path(__file__).resolve().parent.parent / "templates" / "bespoke" / "render.typ"
    ).read_text()
    # Every date-gated section's render fn must carry a date-gated: true call.
    # (Cheap structural check: the token appears once per wired section.)
    assert render.count("date-gated: true") >= len(validate.DATE_GATED_SECTIONS)
