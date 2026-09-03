"""Tests for the V20 FIELD_HANDLERS dispatch table."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cv_editor import field_handlers
from cv_editor.field_handlers import (
    FIELD_HANDLERS,
    JSON_FIELD_TYPES,
    FieldHandler,
    assert_schemas_covered,
    empty_json_value,
)


def test_every_handler_has_apply_and_validate():
    """Sanity: registry shape is uniform."""
    for ftype, handler in FIELD_HANDLERS.items():
        assert isinstance(handler, FieldHandler), ftype
        assert callable(handler.apply), ftype
        assert callable(handler.validate), ftype
        assert handler.name == ftype


def test_every_known_field_type_is_registered():
    """The 13 known field types all dispatch."""
    expected = {
        "int",
        "bool",
        "string",
        "select",
        "string_list",
        "audiences_set",
        "grant_amount",
        "author_list",
        "typed_notes",
        "simple_notes",
        "open_access_dict",
        "text",
        "textarea",
    }
    assert set(FIELD_HANDLERS) >= expected


def test_assert_schemas_covered_passes_on_real_schemas():
    """No typos in the live SCHEMAS — fail-fast guard is happy."""
    from cv_editor import schemas as schemas_module

    assert_schemas_covered(schemas_module.SCHEMAS)


def test_assert_schemas_covered_raises_on_unknown_type():
    bogus = {
        "fake": {
            "file": "fake.yml",
            "fields": [
                {"name": "x", "type": "this_does_not_exist", "label": "X"},
            ],
        },
    }
    with pytest.raises(ImportError, match="Unknown field type"):
        assert_schemas_covered(bogus)


# ---- apply (form, field, entry → mutates entry) ---------------------


def test_apply_int_pops_when_empty():
    entry = CommentedMap()
    entry["age"] = 99
    FIELD_HANDLERS["int"].apply({"age": ""}, {"name": "age", "type": "int"}, entry)
    assert "age" not in entry


def test_apply_int_sets_value():
    entry = CommentedMap()
    FIELD_HANDLERS["int"].apply({"age": "42"}, {"name": "age", "type": "int"}, entry)
    assert entry["age"] == 42


def test_apply_bool_pops_when_falsy():
    entry = CommentedMap()
    entry["flag"] = True
    FIELD_HANDLERS["bool"].apply({"flag": ""}, {"name": "flag", "type": "bool"}, entry)
    assert "flag" not in entry


def test_apply_bool_sets_when_truthy():
    entry = CommentedMap()
    FIELD_HANDLERS["bool"].apply(
        {"flag": "on"},
        {"name": "flag", "type": "bool"},
        entry,
    )
    assert entry["flag"] is True


def test_apply_grant_amount_normalizes_dollar():
    entry = CommentedMap()
    FIELD_HANDLERS["grant_amount"].apply(
        {"amount": "$75,000"},
        {"name": "amount", "type": "grant_amount"},
        entry,
    )
    assert entry["amount"] == "\\$75,000"


# ---- validate (value, field → str | None) ---------------------------


def test_validate_int_rejects_non_int():
    err = FIELD_HANDLERS["int"].validate("abc", {"name": "x", "type": "int"})
    assert err == "must be an integer"


def test_validate_int_range():
    err = FIELD_HANDLERS["int"].validate(
        5,
        {"name": "x", "type": "int", "min": 10, "max": 100},
    )
    assert err == "must be >= 10"


def test_validate_select_must_be_in_choices():
    err = FIELD_HANDLERS["select"].validate(
        "bogus",
        {"name": "x", "type": "select", "choices": ["a", "b"]},
    )
    assert err and "must be one of" in err


def test_validate_author_list_rejects_unnamed():
    err = FIELD_HANDLERS["author_list"].validate(
        [{"name": ""}],
        {"name": "authors", "type": "author_list"},
    )
    assert err and "no name" in err


def test_validate_audiences_set_rejects_unknown():
    err = FIELD_HANDLERS["audiences_set"].validate(
        ["nope"],
        {"name": "audiences", "type": "audiences_set", "choices": ["academic", "industry"]},
    )
    assert err and "unknown audience" in err


def test_validate_open_access_dict_must_be_dict():
    err = FIELD_HANDLERS["open_access_dict"].validate(
        ["not", "a", "dict"],
        {"name": "open_access", "type": "open_access_dict"},
    )
    assert err == "must be a dict"


# ---- V20-cleanup T2: derived JSON_FIELD_TYPES + empty_factory --------


def test_json_field_types_derived_from_registry():
    """JSON_FIELD_TYPES is the single source of truth; no hand-maintained
    set in app.py to drift against."""
    expected = {
        "author_list",
        "typed_notes",
        "open_access_dict",
        "simple_notes",
        "string_list",
        "audiences_set",
    }
    assert JSON_FIELD_TYPES == expected


def test_empty_json_value_dispatches_via_handler():
    assert empty_json_value("open_access_dict") == {}
    assert empty_json_value("author_list") == []
    assert empty_json_value("typed_notes") == []
    assert empty_json_value("simple_notes") == []
    assert empty_json_value("string_list") == []
    assert empty_json_value("audiences_set") == []


def test_empty_json_value_rejects_non_json_type():
    import pytest

    with pytest.raises(ValueError, match="non-JSON type"):
        empty_json_value("text")


def test_validate_int_zero_not_treated_as_empty():
    """V20 post-impl review regression guard (LOW): the optional+empty
    short-circuit must NOT swallow valid int(0). Caught by reviewer A."""
    from cv_editor.validate import validate_entry

    f = {"name": "x", "type": "int", "min": 0, "max": 10}
    errors = validate_entry({"x": 0}, [f])
    assert errors == {}


# ---- _apply_text / _apply_string_list ------------------------------------
#
# Neither had a test before 1.3.0, which is part of why gotcha #96 shipped:
# `_apply_text` is the handler for BOTH `text` and `textarea` and it stores
# `str(v).strip()` unconditionally.


def test_apply_text_stores_the_stripped_scalar():
    entry = CommentedMap()
    field_handlers._apply_text({"name": "  Jane Q Public  "}, {"name": "name"}, entry)
    assert entry["name"] == "Jane Q Public"


def test_apply_text_pops_an_empty_submission():
    entry = CommentedMap({"name": "Jane"})
    field_handlers._apply_text({"name": ""}, {"name": "name"}, entry)
    assert "name" not in entry


def test_apply_text_stringifies_whatever_it_is_given():
    """Characterization, NOT an endorsement.

    This is the store-side half of gotcha #96 and it is behaving correctly: by
    the time a handler runs, a scalar form value really is a `str`. The repr is
    manufactured EARLIER, by the template rendering a non-scalar into a scalar
    widget. The defence therefore lives at render time (a read-only field) and
    at save time (`validate.validate_shapes`), not here -- so this test pins
    what `_apply_text` does rather than pretending it can tell.
    """
    entry = CommentedMap()
    field_handlers._apply_text({"x": ["a", "b"]}, {"name": "x"}, entry)
    assert entry["x"] == "['a', 'b']"


def test_apply_string_list_stores_a_commented_seq_and_strips_blanks():
    entry = CommentedMap()
    field_handlers._apply_string_list(
        {"address": [" line one ", "", "  ", "line two"]}, {"name": "address"}, entry
    )
    assert list(entry["address"]) == ["line one", "line two"]
    assert isinstance(entry["address"], CommentedSeq)


def test_apply_string_list_pops_when_empty_unless_renderer_required():
    entry = CommentedMap({"address": ["a"]})
    field_handlers._apply_string_list({"address": []}, {"name": "address"}, entry)
    assert "address" not in entry

    entry = CommentedMap({"address": ["a"]})
    field_handlers._apply_string_list(
        {"address": []}, {"name": "address", "renderer_required": True}, entry
    )
    assert "address" in entry and list(entry["address"]) == [], (
        "renderer_required must keep the KEY: `..meta.address` is a Typst spread, "
        "so an absent key is a hard no-PDF failure"
    )
