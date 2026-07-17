"""QC title-punctuation detector (2026-06-08).

Flags publication titles ending in a terminal period. The renderer strips
one trailing period so none of these render `..`; this check is a data-
cleanliness advisory in qc/report.md only (not a triage finding type).
"""

from cv_editor import qc_publications


def test_flags_terminal_period():
    assert (
        qc_publications.title_ends_in_period("Invited commentary: motivating better methods.")
        is True
    )


def test_passes_clean_title():
    assert (
        qc_publications.title_ends_in_period("Motivating better methods for measuring drug misuse")
        is False
    )


def test_question_and_exclamation_not_flagged():
    # `?` / `!` are valid title punctuation — only `.` is flagged.
    assert qc_publications.title_ends_in_period("Does X cause Y?") is False
    assert qc_publications.title_ends_in_period("Stop the epidemic!") is False


def test_trailing_whitespace_still_flagged():
    assert qc_publications.title_ends_in_period("Foo bar.   ") is True


def test_abbreviation_endings_flagged_but_render_fine():
    # Expected false positives — the renderer's strip-one-add-one handles
    # them ("...U.S." -> "...U.S. "). Documented in the report blurb.
    assert qc_publications.title_ends_in_period("Opioid deaths in the U.S.") is True


def test_empty_and_none():
    assert qc_publications.title_ends_in_period("") is False
    assert qc_publications.title_ends_in_period(None) is False
