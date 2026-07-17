"""Tests for the V20 cv_editor/author_flags.py extraction.

Pre-refactor invariants:
- self_author_position() intentionally IGNORES `group_authorship` flag when
  classifying position. A middle-positioned author with
  group_authorship: true resolves to 'middle' (not 'co_first'). See
  V18-A-D commentary.
- Falsy flags do NOT serialize to YAML (form_to_yaml_author returns a
  plain string when all flags are False).
- Truthy flags serialize as dict-form authors.

This file is the regression net for B1: if the extraction
accidentally promotes group authors to lead status via `is_lead_eligible`,
test_group_authorship_middle_author_not_lead must fail loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _engine_guards import bespoke_required
from cv_editor import notes_helpers
from cv_editor.author_flags import ALL_FLAG_KEYS, AUTHOR_FLAGS
from cv_editor.author_names import form_to_yaml_author

# ---- Spec integrity --------------------------------------------------


def test_three_flags_registered():
    assert len(AUTHOR_FLAGS) == 3
    keys = {f.key for f in AUTHOR_FLAGS}
    assert keys == {"co_first", "co_senior", "group_authorship"}


def test_flag_keys_order_stable():
    """Order is load-bearing — Typst mirror reads tuple positionally."""
    assert ALL_FLAG_KEYS == ("co_first", "co_senior", "group_authorship")


def test_group_authorship_is_not_lead_eligible():
    """The is_lead_eligible bit is what stops self_author_position from
    promoting group authors. THIS is the field the B1 refactor adds —
    if it's True, V18-A's OA-banner-suppression breaks."""
    by_key = {f.key: f for f in AUTHOR_FLAGS}
    assert by_key["co_first"].is_lead_eligible is True
    assert by_key["co_senior"].is_lead_eligible is True
    assert by_key["group_authorship"].is_lead_eligible is False


def test_each_flag_has_glyph_and_footnote():
    glyphs = {f.glyph for f in AUTHOR_FLAGS}
    assert glyphs == {"†", "‡", "◊"}
    for f in AUTHOR_FLAGS:
        assert f.footnote.endswith("."), f"footnote should end with period: {f.key}"


# ---- self_author_position invariants (V18-A baked in) ----------------------


def test_self_author_position_first_author():
    authors = ["Public JQ", "Smith J", "Jones K"]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "first"


def test_self_author_position_last_author():
    authors = ["Smith J", "Jones K", "Public JQ"]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "last"


def test_self_author_position_middle_author():
    authors = ["Smith J", "Public JQ", "Jones K"]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "middle"


def test_self_author_position_co_first():
    authors = [
        "Smith J",
        {"name": "Public JQ", "co_first": True},
        "Jones K",
    ]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "co_first"


def test_self_author_position_co_senior():
    authors = [
        "Smith J",
        {"name": "Public JQ", "co_senior": True},
        "Jones K",
    ]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "co_senior"


def test_self_author_position_group_authorship_middle_author_stays_middle():
    """B1 invariant: a middle-positioned group_authorship author does
    NOT get promoted to a lead position.

    If this test fails after B1's extraction, the AuthorFlag
    is_lead_eligible discriminator has been wired up wrong — group
    authors are being treated as leads.
    """
    authors = [
        "Smith J",
        {"name": "Public JQ", "group_authorship": True},
        "Jones K",
    ]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "middle"


def test_self_author_position_absent():
    authors = ["Smith J", "Jones K"]
    assert notes_helpers.self_author_position(authors, "Public JQ") == "absent"


# ---- form_to_yaml_author shape ---------------------------------------


def test_falsy_flags_serialize_as_string():
    """All-False → bare string. The asymmetric on-write rule keeps the
    YAML clean."""
    form = {
        "name": "Smith J",
        "co_first": False,
        "co_senior": False,
        "group_authorship": False,
    }
    result = form_to_yaml_author(form)
    assert result == "Smith J"


def test_truthy_flag_serializes_as_dict():
    form = {
        "name": "Public JQ",
        "co_first": True,
        "co_senior": False,
        "group_authorship": False,
    }
    result = form_to_yaml_author(form)
    assert isinstance(result, dict)
    assert result["name"] == "Public JQ"
    assert result.get("co_first") is True
    assert "co_senior" not in result  # falsy NOT serialized
    assert "group_authorship" not in result


def test_group_authorship_serializes_independently():
    form = {
        "name": "Example Consortium",
        "co_first": False,
        "co_senior": False,
        "group_authorship": True,
    }
    result = form_to_yaml_author(form)
    assert isinstance(result, dict)
    assert result.get("group_authorship") is True


# ---- Typst mirror ordering ------------------------------------------


@bespoke_required
def test_typst_mirror_lists_keys_in_same_order():
    """The renderer keeps its own tuple at templates/bespoke/render.typ module scope;
    keys must appear in the SAME order as Python's ALL_FLAG_KEYS.
    Order is load-bearing: the Typst code may index by position when
    composing footnotes.
    """
    typst_file = Path(__file__).resolve().parents[1] / "templates" / "bespoke" / "render.typ"
    text = typst_file.read_text()
    # Anchor markers added in the same B1 commit
    start = text.index("// AUTHOR_FLAGS_BEGIN")
    end = text.index("// AUTHOR_FLAGS_END", start)
    block = text[start:end]
    # Each key must appear in order; later keys appear after earlier ones.
    last_pos = -1
    for key in ALL_FLAG_KEYS:
        pos = block.find(f'"{key}"')
        assert pos > last_pos, (
            f"Typst mirror missing or out-of-order: {key} (pos={pos}, "
            f"last_pos={last_pos}). Block:\n{block}"
        )
        last_pos = pos


@bespoke_required
def test_typst_footnote_text_matches_python_spec():
    """Each AuthorFlag.footnote must appear verbatim in the renderer's
    AUTHOR_FOOTNOTES block. V20 post-impl review caught a drift between
    `author_flags.py` ('Authors contributed equally.') and
    `render.typ` ('First authors contributed equally.') — exactly the
    failure mode the spec extraction was supposed to prevent. This
    regression guard runs every test cycle.
    """
    typst_file = Path(__file__).resolve().parents[1] / "templates" / "bespoke" / "render.typ"
    text = typst_file.read_text()
    start = text.index("// AUTHOR_FOOTNOTES_BEGIN")
    end = text.index("// AUTHOR_FOOTNOTES_END", start)
    block = text[start:end]
    for f in AUTHOR_FLAGS:
        assert f.footnote in block, (
            f"AuthorFlag({f.key!r}).footnote = {f.footnote!r} not in "
            f"render.typ's AUTHOR_FOOTNOTES block. Edit one to match "
            f"the other. Block:\n{block}"
        )
