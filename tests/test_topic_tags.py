"""Pin the publications `tags` field and the empty-vocabulary guard (1.2.5).

Two things are under test and they fail in opposite directions.

`tags` is a website-export field, inert in the Typst renderer, so — exactly like
`web`/`slides`/`paper_pdf` in test_web_export_fields.py — nothing else would
notice if a refactor dropped it. Hence the presence pins.

The empty-`choices` guard is the more important half. `choices` for this field
type is DATA-DRIVEN, so it can legitimately come back empty (a malformed
`meta.yml`, an unreadable data dir), and before the guard that state made the
next ordinary save DELETE the field: zero checkboxes rendered, the JS wrote `[]`
at mount, `validate_entry` skipped the empty value, and the apply handler popped
the key — with `js_mounted=1`, so the gotcha #72 sentinel never fired, and with
every test green. These tests plant that state deliberately.
"""

from cv_editor import build_variants as bv
from cv_editor import schemas
from cv_editor.field_handlers import FIELD_HANDLERS, JSON_FIELD_TYPES
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _fields(key):
    return {f["name"]: f for f in schemas.SCHEMAS[key]["fields"]}


# ---- the field itself -------------------------------------------------------


def test_publications_has_a_tags_field():
    f = _fields("publications")
    assert f["tags"]["type"] == "audiences_set"
    assert f["tags"]["choices"] is schemas.TAGS, (
        "the field must hold the SAME list object the widen helper mutates in "
        "place, or the form renders a frozen snapshot of the vocabulary"
    )


def test_tags_is_registered_unconditionally():
    """Presence must NOT depend on whether a vocabulary resolved.

    A data-dependent field list cannot be pinned by a test (the engine's own
    corpus differs from any host's), and — worse — the two paths that rebuild an
    entry from `sch["fields"]` alone (a publications subsection move, a preprint
    promotion) would silently drop an unregistered key from an already-tagged
    entry.
    """
    original = list(schemas.TAGS)
    try:
        schemas.TAGS[:] = []
        assert "tags" in _fields("publications")
    finally:
        schemas.TAGS[:] = original


def test_tags_reuses_audiences_set_so_json_field_types_is_unchanged():
    assert "tags" not in JSON_FIELD_TYPES  # it is a field NAME, not a type
    assert "audiences_set" in JSON_FIELD_TYPES


def test_only_audiences_and_hide_from_are_semantic_audience_fields():
    """A differently-named field of the same type must stay inert for
    visibility. Everything semantic about audiences keys on the NAME."""
    assert bv.TAG_FIELD == "tags"
    assert bv.TAG_FIELD not in ("audiences", "hide-from")


# ---- the empty-vocabulary guard --------------------------------------------


def _apply(field, form_value, existing):
    entry = CommentedMap(existing)
    FIELD_HANDLERS[field["type"]].apply({field["name"]: form_value}, field, entry)
    return entry


def test_empty_choices_leaves_an_existing_value_alone():
    field = {"name": "tags", "type": "audiences_set", "choices": []}
    entry = _apply(field, [], {"title": "x", "tags": ["Health inequities"]})
    assert entry["tags"] == ["Health inequities"], (
        "with no vocabulary configured, 'nothing checked' is not a choice the "
        "user could have made and must not clear the field"
    )


def test_empty_choices_does_not_invent_a_value():
    field = {"name": "tags", "type": "audiences_set", "choices": []}
    entry = _apply(field, [], {"title": "x"})
    assert "tags" not in entry


def test_a_populated_vocabulary_still_clears_on_uncheck_all():
    """The guard must not break the legitimate case: with a real vocabulary,
    unchecking every box DOES clear the field."""
    field = {"name": "tags", "type": "audiences_set", "choices": ["Health inequities"]}
    entry = _apply(field, [], {"title": "x", "tags": ["Health inequities"]})
    assert "tags" not in entry


def test_audiences_can_never_reach_the_guard():
    """INERTNESS PROOF for the shared handler: the audiences vocabulary is a
    non-empty base set unioned with the corpus, so `audiences`/`hide-from` keep
    their clear-on-uncheck behaviour unchanged."""
    assert bv.BASE_AUDIENCES
    assert set(bv.BASE_AUDIENCES).issubset(set(bv.audience_choices({}, None)))
    field = _fields("appointments")["audiences"]
    assert field["choices"], "audiences choices must never be empty"
    entry = _apply(field, [], {"role": "x", "audiences": ["academic"]})
    assert "audiences" not in entry


def test_values_are_written_flow_style():
    """The applier and the form must agree on representation, or one reflows
    what the other wrote."""
    field = {"name": "tags", "type": "audiences_set", "choices": ["A", "B"]}
    entry = _apply(field, ["A", "B"], {})
    assert isinstance(entry["tags"], CommentedSeq)
    assert entry["tags"].fa.flow_style() is True


# ---- tag_choices ------------------------------------------------------------


def test_tag_choices_is_declared_order_then_sorted_extras():
    meta = {"tags": ["Zed", "Alpha"]}

    def load(_key):
        return [{"entries": [{"tags": ["Beta", "Alpha"]}, {"tags": ["Aardvark"]}]}]

    assert bv.tag_choices(meta, load) == ("Zed", "Alpha", "Aardvark", "Beta")


def test_tag_choices_unions_the_corpus_so_a_stored_tag_is_always_offered():
    """The self-healing half: a tag the data carries but meta forgot must still
    be offered, because a value the form cannot offer is one the save deletes."""

    def load(_key):
        return [{"entries": [{"tags": ["Only in data"]}]}]

    assert "Only in data" in bv.tag_choices({"tags": []}, load)


def test_tag_choices_skips_a_non_list_tags_value():
    """`tags: substance use` iterates CHARACTER BY CHARACTER. Widening from it
    would add every letter to the vocabulary, and once in the union each letter
    validates, is offered as a checkbox, and exports as a real tag id."""

    def load(_key):
        return [{"entries": [{"tags": "substance use"}]}]

    assert bv.tag_choices({"tags": ["Real"]}, load) == ("Real",)


def test_tag_choices_is_empty_with_no_meta_and_no_data():
    assert bv.tag_choices({}, None) == ()
    assert bv.tag_choices(None, None) == ()


def test_tag_choices_survives_an_unreadable_section():
    def load(_key):
        raise OSError("no such file")

    assert bv.tag_choices({"tags": ["Real"]}, load) == ("Real",)


def test_the_engine_corpus_declares_a_vocabulary():
    """The engine's own `data/meta.yml` must declare tags, or the upstream suite
    exercises the field with an empty vocabulary and the release ships with no
    coverage of the populated path."""
    assert schemas.TAGS, "data/meta.yml should declare a `tags:` block"


def test_the_guard_does_NOT_cover_a_fresh_rebuild_and_here_is_the_proof():
    """CHARACTERIZATION TEST for a KNOWN GAP — it pins what currently happens, not
    what should.

    Every other test here builds `CommentedMap(existing)`, i.e. an entry that
    already carries the value. That is the in-place edit path. A publications
    SUBSECTION MOVE takes a different branch — `sections_routes.py` calls
    `_form_to_entry(form_data, sch)` with NO `existing=`, so the entry starts EMPTY.
    There is then no prior value for the empty-`choices` guard to preserve, and the
    posted `tags_json` is `[]` because the form rendered zero checkboxes. The moved
    entry comes out with no `tags` key, and `validate_entry` skips empty optional
    values, so nothing downstream can tell it from "never tagged".

    Trigger needs BOTH an empty vocabulary and a subsection move, so it is narrow.
    The fix belongs at the call site (pass `existing=`), which also closes that
    branch's long-standing unmanaged-key hole; it is an engine behaviour change and
    is tracked as such.

    WHEN THAT FIX LANDS, THIS TEST SHOULD FAIL. Invert it then — do not delete it.
    """
    field = {"name": "tags", "type": "audiences_set", "choices": []}
    fresh = CommentedMap()  # the shape the rebuild path actually uses
    FIELD_HANDLERS["audiences_set"].apply({"tags": []}, field, fresh)
    assert "tags" not in fresh, (
        "if this now HOLDS a value, the fresh-rebuild gap has been closed — "
        "invert this assertion and update the comment in _apply_audiences_set"
    )

    # Contrast: the in-place path, which the guard DOES cover.
    in_place = CommentedMap({"tags": ["Health inequities"]})
    FIELD_HANDLERS["audiences_set"].apply({"tags": []}, field, in_place)
    assert in_place["tags"] == ["Health inequities"]
