"""Unit tests for the contribution-needed warning logic."""

from cv_editor.notes_helpers import needs_contribution_note, self_author_position

SELF = "Public JQ"


def test_first_author():
    e = {"authors": ["Public JQ", "Smith J", "Doe A"]}
    assert self_author_position(e["authors"], SELF) == "first"
    assert needs_contribution_note(e, SELF) is False


def test_last_author():
    e = {"authors": ["Smith J", "Doe A", "Public JQ"]}
    assert self_author_position(e["authors"], SELF) == "last"
    assert needs_contribution_note(e, SELF) is False


def test_co_first_via_dict_form():
    e = {
        "authors": [
            "Smith J",
            {"name": "Public JQ", "co_first": True},
            "Doe A",
            "Other Z",
        ]
    }
    assert self_author_position(e["authors"], SELF) == "co_first"
    assert needs_contribution_note(e, SELF) is False


def test_co_senior_via_dict_form():
    e = {
        "authors": [
            "Smith J",
            "Doe A",
            "Other Z",
            {"name": "Public JQ", "co_senior": True},
        ]
    }
    # Position is technically last, so self_author_position returns 'last'.
    # Either 'last' or 'co_senior' is fine — both skip the warning.
    assert self_author_position(e["authors"], SELF) in {"last", "co_senior"}
    assert needs_contribution_note(e, SELF) is False


def test_middle_no_contributions_warns():
    e = {"authors": ["Smith J", "Public JQ", "Doe A", "Other Z"]}
    assert self_author_position(e["authors"], SELF) == "middle"
    assert needs_contribution_note(e, SELF) is True


def test_middle_with_contributions_no_warn():
    e = {
        "authors": ["Smith J", "Public JQ", "Doe A"],
        "notes": [{"type": "contributions", "text": "Statistical analysis."}],
    }
    assert needs_contribution_note(e, SELF) is False


def test_middle_with_unrelated_notes_still_warns():
    e = {
        "authors": ["Smith J", "Public JQ", "Doe A"],
        "notes": [{"type": "media", "outlets": ["NPR"]}],
    }
    assert needs_contribution_note(e, SELF) is True


def test_corporate_authorship_no_warn():
    e = {"authors": ["Example Consortium for Health"]}
    assert self_author_position(e["authors"], SELF) == "absent"
    assert needs_contribution_note(e, SELF) is False


def test_two_author_paper_public_second_is_last_not_middle():
    # Two-author paper, Public is last → senior. No warning.
    e = {"authors": ["Smith J", "Public JQ"]}
    assert self_author_position(e["authors"], SELF) == "last"
    assert needs_contribution_note(e, SELF) is False
