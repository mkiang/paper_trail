"""V2 unit tests: sections.py navigation + new field types in validate +
simple_notes round-trip + active-grant date warning."""

from __future__ import annotations

from cv_editor import notes_helpers, schemas, sections, validate
from ruamel.yaml.comments import CommentedMap, CommentedSeq

# ---- sections.flatten / locate ----


def test_flatten_flat_list():
    data = [{"a": 1}, {"a": 2}, {"a": 3}]
    out = list(sections.flatten(data, "flat_list"))
    assert len(out) == 3
    assert [r["global_idx"] for r in out] == [0, 1, 2]
    assert [r["loc"] for r in out] == [(0,), (1,), (2,)]


def test_flatten_list_of_subsections():
    data = [
        {"subsection": "A", "entries": [{"x": 1}, {"x": 2}]},
        {"subsection": "B", "entries": [{"x": 3}]},
    ]
    out = list(sections.flatten(data, "list_of_subsections"))
    assert [r["global_idx"] for r in out] == [0, 1, 2]
    assert out[0]["ctx"]["subsection"] == "A"
    assert out[2]["ctx"]["subsection"] == "B"
    assert out[2]["loc"] == (1, 0)


def test_flatten_clusters():
    data = [
        {"institution": "I1", "city": "C1", "entries": [{"x": 1}]},
        {"institution": "I2", "entries": [{"x": 2}, {"x": 3}]},
    ]
    out = list(sections.flatten(data, "clusters"))
    assert len(out) == 3
    assert out[0]["ctx"]["institution"] == "I1"
    assert out[0]["ctx"]["city"] == "C1"
    assert out[2]["ctx"]["institution"] == "I2"


def test_flatten_subsections_of_clusters():
    data = [
        {
            "subsection": "Faculty",
            "clusters": [
                {"institution": "Metro", "entries": [{"x": 1}, {"x": 2}]},
            ],
        },
        {
            "subsection": "Affiliations",
            "clusters": [
                {"institution": "Berkeley", "entries": [{"x": 3}]},
            ],
        },
    ]
    out = list(sections.flatten(data, "subsections_of_clusters"))
    assert [r["global_idx"] for r in out] == [0, 1, 2]
    assert out[0]["loc"] == (0, 0, 0)
    assert out[2]["loc"] == (1, 0, 0)
    assert out[2]["ctx"]["subsection"] == "Affiliations"
    assert out[2]["ctx"]["institution"] == "Berkeley"


def test_locate_returns_none_for_oob():
    assert sections.locate([{"a": 1}], "flat_list", 99) is None


# ---- sections.insert / delete / move ----


def test_insert_flat_list_prepends():
    data = CommentedSeq([CommentedMap({"x": 1})])
    new = CommentedMap({"x": 2})
    loc = sections.insert_entry(data, "flat_list", {}, new)
    assert loc == (0,)
    assert data[0]["x"] == 2


def test_insert_list_of_subsections_finds_subsection():
    data = CommentedSeq(
        [
            CommentedMap({"subsection": "A", "entries": CommentedSeq()}),
            CommentedMap({"subsection": "B", "entries": CommentedSeq([CommentedMap({"x": 1})])}),
        ]
    )
    new = CommentedMap({"x": 9})
    loc = sections.insert_entry(data, "list_of_subsections", {"subsection": "B"}, new)
    assert loc == (1, 0)
    assert data[1]["entries"][0]["x"] == 9
    # entry 1 in B is the previous one.
    assert data[1]["entries"][1]["x"] == 1


def test_insert_list_of_subsections_creates_missing_group():
    # 2026-05-30: schema is the source of truth, so filing into a schema-valid
    # subsection that has no group in the data yet CREATES the group (appended),
    # rather than raising. (The caller, entry_save, gates on the schema list.)
    data = CommentedSeq([CommentedMap({"subsection": "A", "entries": CommentedSeq()})])
    loc = sections.insert_entry(
        data, "list_of_subsections", {"subsection": "Z"}, CommentedMap({"x": 7})
    )
    assert loc == (1, 0)
    assert data[1]["subsection"] == "Z"
    assert data[1]["entries"][0]["x"] == 7
    assert len(data) == 2


def test_insert_subsections_of_clusters_creates_missing_group():
    # Same on-demand creation for the clustered structure: a new subsection group
    # holds `clusters`, and the cluster (with the entry) is created inside it.
    data = CommentedSeq()
    loc = sections.insert_entry(
        data,
        "subsections_of_clusters",
        {"subsection": "Faculty Appointments", "institution": "Metro", "city": "Metro, CA"},
        CommentedMap({"role": "Prof"}),
    )
    assert loc == (0, 0, 0)
    assert data[0]["subsection"] == "Faculty Appointments"
    assert data[0]["clusters"][0]["institution"] == "Metro"
    assert data[0]["clusters"][0]["entries"][0]["role"] == "Prof"


def test_insert_clusters_creates_missing_cluster():
    data = CommentedSeq()
    new = CommentedMap({"x": 1})
    loc = sections.insert_entry(
        data, "clusters", {"institution": "Metro", "city": "Metro, CA"}, new
    )
    assert loc == (0, 0)
    assert data[0]["institution"] == "Metro"
    assert data[0]["city"] == "Metro, CA"
    assert data[0]["entries"][0]["x"] == 1


def test_delete_then_insert_yields_same_size():
    data = CommentedSeq([CommentedMap({"x": i}) for i in range(3)])
    sections.delete_entry(data, "flat_list", (1,))
    assert len(data) == 2
    sections.insert_entry(data, "flat_list", {}, CommentedMap({"x": 99}))
    assert len(data) == 3


# ---- sections.list_targets ----


def test_list_targets_for_clusters_picks_unique():
    data = [
        {"institution": "S", "city": "Metro"},
        {"institution": "B"},
    ]
    out = sections.list_targets(data, "clusters")
    assert {t["institution"] for t in out} == {"S", "B"}


# ---- validate: new field types ----


def test_validate_select_in_choices():
    fields = [
        {"name": "status", "type": "select", "required": True, "choices": ["active", "previous"]}
    ]
    assert validate.validate_entry({"status": "active"}, fields) == {}
    assert "status" in validate.validate_entry({"status": "bogus"}, fields)


def test_validate_string_list_required():
    fields = [{"name": "extras", "type": "string_list", "required": True}]
    assert validate.validate_entry({"extras": []}, fields) == {"extras": "required"}
    assert validate.validate_entry({"extras": [""]}, fields) == {
        "extras": "at least one entry required"
    }
    assert validate.validate_entry({"extras": ["a"]}, fields) == {}


def test_validate_audiences_set_filters_unknown():
    fields = [{"name": "audiences", "type": "audiences_set", "choices": ["academic", "industry"]}]
    assert validate.validate_entry({"audiences": ["academic"]}, fields) == {}
    err = validate.validate_entry({"audiences": ["bogus"]}, fields)
    assert "audiences" in err


# ---- validate.grant_end_date_warning ----


def test_grant_warning_active_past_end_warns():
    msg = validate.grant_end_date_warning({"status": "active", "date": "01/2020 - 12/2020"})
    assert msg is not None
    assert "past" in msg


def test_grant_warning_active_future_silent():
    msg = validate.grant_end_date_warning({"status": "active", "date": "01/2020 - 12/2099"})
    assert msg is None


def test_grant_warning_open_ended_silent():
    msg = validate.grant_end_date_warning({"status": "active", "date": "01/2020 -"})
    assert msg is None


def test_grant_warning_previous_silent():
    msg = validate.grant_end_date_warning({"status": "previous", "date": "01/2020 - 12/2020"})
    assert msg is None


# ---- simple_notes round-trip ----


def test_simple_notes_form_to_yaml_plain_string():
    out = notes_helpers.simple_notes_form_to_yaml([{"text": "Plain note", "highlighted": False}])
    assert len(out) == 1
    assert out[0] == "Plain note"  # plain string, not dict


def test_simple_notes_form_to_yaml_highlighted_becomes_dict():
    out = notes_helpers.simple_notes_form_to_yaml([{"text": "Hidden one", "highlighted": True}])
    assert len(out) == 1
    assert out[0]["text"] == "Hidden one"
    assert out[0]["highlighted"] is True


def test_simple_notes_form_to_yaml_drops_empty():
    out = notes_helpers.simple_notes_form_to_yaml(
        [{"text": "", "highlighted": False}, {"text": "x", "highlighted": False}]
    )
    assert len(out) == 1
    assert out[0] == "x"


def test_simple_notes_yaml_to_form_round_trips():
    src = ["plain text", {"text": "with highlight", "highlighted": True}]
    form = notes_helpers.simple_notes_yaml_to_form(src)
    assert form == [
        {"text": "plain text", "highlighted": False},
        {"text": "with highlight", "highlighted": True},
    ]
    yaml_again = notes_helpers.simple_notes_form_to_yaml(form)
    assert yaml_again[0] == "plain text"
    assert yaml_again[1]["text"] == "with highlight"
    assert yaml_again[1]["highlighted"] is True


# ---- schemas: every section schema is internally consistent ----


def test_every_section_schema_loads():
    for key in schemas.all_sections():
        sch = schemas.get(key)
        assert "file" in sch
        assert "label" in sch
        assert sch["structure"] in sections.STRUCTURES
        assert isinstance(sch.get("fields"), list)
        for f in sch["fields"]:
            assert "name" in f
            assert "type" in f


def test_schema_subsections_cover_data():
    """Drift guard (2026-05-30): for every subsectioned section, EVERY subsection
    name present in the live data must be a member of the schema's `subsections`
    list. The schema list is the single source of truth — it feeds the edit-form
    dropdown, the bulk-move targets, and save validation. If the data uses a name
    the schema doesn't list, the edit form can't pre-select it (it falls back to
    the first option) and saving it 400s — exactly the presentations bug. Derived
    dynamically from schemas + data, so adding/renaming a subsection in schemas.py
    keeps this honest with no test edit (rename in the schema, then move the
    entries, and this passes again)."""
    from pathlib import Path

    from cv_editor import yaml_io

    root = Path(__file__).resolve().parent.parent
    offenders = {}
    for key in schemas.all_sections():
        sch = schemas.get(key)
        if sch["structure"] not in ("list_of_subsections", "subsections_of_clusters"):
            continue
        allowed = set(sch.get("subsections") or [])
        _, data = yaml_io.load(root / sch["file"])
        # subsection names live at the TOP level of the data for both structures.
        used = {str(grp.get("subsection")) for grp in (data or [])}
        missing = used - allowed
        if missing:
            offenders[key] = sorted(missing)
    assert not offenders, (
        "data uses subsection names not in the schema's `subsections` list "
        f"(dropdown + save will break): {offenders}"
    )
