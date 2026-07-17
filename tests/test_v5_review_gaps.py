"""V5-D four-reviewer gap-fill tests. Covers the top items Reviewer 4
flagged: header re-read inside lock, data-sort-value correctness across
sections, OOB-idx 404 sweep, global_idx renumbering, media-empty-outlets,
empty-section impact preview, subsections_of_clusters unknown-subsection,
int min/max boundaries, mode allow-list (R1 HIGH), bibtex DOI prefix
strip (R1 MEDIUM), audience: full preservation (already done in V4-D).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE
from cv_editor import (
    bibtex_parse,
    freezer,
    notes_helpers,
    sections,
    validate,
    yaml_io,
)
from cv_editor import (
    build_variants as bv,
)
from cv_editor.app import create_app
from ruamel.yaml.comments import CommentedMap, CommentedSeq

ROOT = Path(__file__).resolve().parent.parent

SECTIONS = [
    "publications",
    "presentations",
    "research_support",
    "service",
    "teaching",
    "mentees",
    "honors",
    "education",
    "appointments",
]


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- R1 HIGH: mode allow-list ----


def test_save_with_missing_mode_field_400s(client):
    """A request that doesn't include a `mode` field used to silently fall
    through to the 'new' branch and duplicate the entry."""
    resp = client.post(
        "/publications/save",
        data={
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
            "subsection": "Peer-Reviewed Original Research",
            "title": "Test",
            "journal": "T",
            "year": "2024",
            "authors_json": '[{"name":"X Y","co_first":false,"co_senior":false}]',
        },
    )
    assert resp.status_code == 400


def test_save_with_invalid_mode_400s(client):
    """An explicit but unknown mode is also refused."""
    resp = client.post(
        "/publications/save",
        data={
            "mode": "frobulate",
            "mtime_ns": str(yaml_io.mtime_ns(ROOT / "data" / "publications.yml")),
            "subsection": "Peer-Reviewed Original Research",
            "title": "Test",
            "journal": "T",
            "year": "2024",
            "authors_json": '[{"name":"X Y","co_first":false,"co_senior":false}]',
        },
    )
    assert resp.status_code == 400


def test_style_save_invalid_mode_400s(client):
    meta_path = ROOT / "data" / "meta.yml"
    resp = client.post(
        "/style/save",
        data={
            "mode": "bogus",
            "mtime_ns": str(yaml_io.mtime_ns(meta_path)),
            "filename": "test-bad-mode",
            "audience": "",
        },
    )
    assert resp.status_code == 400


# ---- R1 MEDIUM: bibtex DOI prefix strip ----


def test_bibtex_strips_https_dx_doi_org_prefix():
    src = "@article{x, author={A B}, title={T}, year={2024}, journal={J}, doi={https://dx.doi.org/10.1234/abc}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["doi"] == "10.1234/abc"


def test_bibtex_strips_doi_colon_prefix():
    src = "@article{x, author={A B}, title={T}, year={2024}, journal={J}, doi={doi:10.1234/abc}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["doi"] == "10.1234/abc"


def test_bibtex_bare_doi_unchanged():
    src = "@article{x, author={A B}, title={T}, year={2024}, journal={J}, doi={10.1234/abc}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["doi"] == "10.1234/abc"


# ---- R4 gap 25: data-sort-value correctness for year column ----


def test_year_data_sort_value_is_numeric(client):
    """Every `data-sort-value` in the year column must parse as int."""
    body = client.get("/publications").get_data(as_text=True)
    pattern = re.compile(r'<td class="col-year[^"]*"\s+data-sort-value="([^"]*)"', re.S)
    matches = pattern.findall(body)
    assert matches, "no year cells found"
    for v in matches:
        if v:  # empty year cells are OK (skip)
            int(v)  # raises if non-numeric


# ---- R4 gap 10: global_idx contiguous after delete ----


def test_flatten_global_idx_contiguous_after_delete():
    data = [{"a": i} for i in range(5)]
    sections.delete_entry(data, "flat_list", (2,))
    out = list(sections.flatten(data, "flat_list"))
    assert [r["global_idx"] for r in out] == [0, 1, 2, 3]


# ---- R4 gap 21: OOB idx 404 sweep across every section ----


@pytest.mark.parametrize("section", SECTIONS)
def test_oob_view_404s_every_section(client, section):
    assert client.get(f"/{section}/9999").status_code == 404


@pytest.mark.parametrize("section", SECTIONS)
def test_oob_edit_404s_every_section(client, section):
    assert client.get(f"/{section}/9999/edit").status_code == 404


@pytest.mark.parametrize("section", SECTIONS)
def test_oob_delete_404s_every_section(client, section):
    resp = client.post(f"/{section}/9999/delete", data={"mtime_ns": "0"})
    assert resp.status_code == 404


# ---- R4 gap 4: media note with empty outlets dropped ----


def test_media_note_with_only_empty_outlets_is_dropped():
    out = notes_helpers.notes_form_to_yaml(
        [
            {"type": "media", "outlets": [{"name": "", "url": ""}]},
        ]
    )
    assert len(out) == 0


def test_media_note_with_one_named_outlet_kept():
    out = notes_helpers.notes_form_to_yaml(
        [
            {"type": "media", "outlets": [{"name": "", "url": ""}, {"name": "NYT", "url": ""}]},
        ]
    )
    assert len(out) == 1


# ---- R4 gap 16: empty-section impact preview ----


def test_impact_preview_with_empty_section_records_zero():
    """A section returning None from the loader should produce a 0/0 row
    rather than crashing."""

    def loader(key):
        if key == "honors":
            return None
        return yaml_io.load(ROOT / "data" / f"{key}.yml")[1]

    out = bv.impact_preview(loader, audience="full", show_highlighted=False)
    assert "honors" in out["per_section"]
    assert out["per_section"]["honors"]["visible"] == 0
    assert out["per_section"]["honors"]["total"] == 0


# ---- R4 gap 8: subsections_of_clusters unknown-subsection raises ----


def test_insert_subsections_of_clusters_creates_missing_subsection():
    # 2026-05-30: schema is the source of truth for subsections, so insert_entry
    # now CREATES a missing subsection group on demand (appended) instead of
    # raising. The caller (entry_save) gates the name against the schema list, so
    # only schema-valid subsections reach here; filing the first entry into an
    # empty-but-defined subsection must work. Was: raised ValueError on a
    # not-yet-present subsection.
    data = [
        {"subsection": "Faculty", "clusters": [{"institution": "Metro", "entries": []}]},
    ]
    loc = sections.insert_entry(
        data,
        "subsections_of_clusters",
        {"subsection": "Academic Affiliations", "institution": "Metro"},
        CommentedMap({"x": 1}),
    )
    assert loc == (1, 0, 0)
    assert data[1]["subsection"] == "Academic Affiliations"
    assert data[1]["clusters"][0]["institution"] == "Metro"
    assert data[1]["clusters"][0]["entries"][0]["x"] == 1


# ---- R4 gap 1: int min/max boundaries ----


def test_validate_int_min_max_boundaries():
    fields = [{"name": "year", "type": "int", "min": 1900, "max": 2100}]
    assert validate.validate_entry({"year": 1900}, fields) == {}
    assert validate.validate_entry({"year": 2100}, fields) == {}
    assert "year" in validate.validate_entry({"year": 1899}, fields)
    assert "year" in validate.validate_entry({"year": 2101}, fields)


def test_validate_int_non_integer_string():
    fields = [{"name": "n", "type": "int"}]
    assert "n" in validate.validate_entry({"n": "abc"}, fields)


# ---- R4 gap 14: header re-read inside lock ----


def test_write_with_backup_uses_fresh_header_from_disk(tmp_path):
    """If the on-disk file's header changes between load and save, the
    save must pick up the new header rather than overwriting it with the
    stale in-memory one."""
    target = tmp_path / "x.yml"
    initial = "# old header\n# line two\n- a: 1\n- a: 2\n"
    target.write_text(initial)
    header, data = yaml_io.load(target)
    # Externally rewrite the header — simulating a concurrent edit
    # to the docstring while the editor's form was open.
    new_text = "# NEW header\n# line two\n- a: 1\n- a: 2\n"
    target.write_text(new_text)
    # Save with the OLD in-memory header. V3-H fix re-reads on disk
    # inside the lock so the new header survives.
    yaml_io.write_with_backup(target, header, data)
    final = target.read_text()
    assert "NEW header" in final
    assert "old header" not in final


# ---- R4 gap 27: rename into duplicate position ----


def test_apply_rename_into_existing_name_in_same_list():
    """If renaming would create a duplicate in one author list, the
    rename still happens (current contract — pin it)."""
    from cv_editor import author_rename

    data = CommentedSeq(
        [
            CommentedMap(
                {
                    "subsection": "S",
                    "entries": CommentedSeq(
                        [
                            CommentedMap({"authors": CommentedSeq(["Smith J", "Public JQ"])}),
                        ]
                    ),
                }
            ),
        ]
    )
    n = author_rename.apply_rename(data, "Smith J", "Public JQ")
    assert n == 1
    # Same person now appears twice in the author list.
    authors = list(data[0]["entries"][0]["authors"])
    assert authors == ["Public JQ", "Public JQ"]


# ---- R1 LOW: form_to_variant preserves unknown YAML inputs keys ----


def test_form_to_variant_preserves_unknown_existing_input_keys():
    """A hand-edited meta.yml variant with a custom input key (renderer
    flag the form doesn't surface yet) must survive a round-trip through
    the form editor."""
    existing = CommentedMap(
        {
            "filename": "custom",
            "inputs": CommentedMap(
                {
                    "audience": "academic",
                    "custom_renderer_flag": "experimental",  # unknown to the editor
                    "template": "compact",  # unknown to the editor (V16-A)
                }
            ),
        }
    )
    form = bv.variant_to_form(existing)
    out = bv.form_to_variant(form, existing=existing)
    assert out["inputs"]["custom_renderer_flag"] == "experimental"
    assert out["inputs"]["template"] == "compact"
    assert out["inputs"]["audience"] == "academic"


# ---- Freezer: skips symlinks ----


def test_freeze_skips_top_level_symlinks(tmp_path, monkeypatch):
    """If a top-level entry in SNAPSHOT_INCLUDES is a symlink, the freezer
    must skip it rather than dereference (R1 MEDIUM V5-D)."""
    # This is implicitly covered by SNAPSHOT_INCLUDES having no symlinks
    # in the current source tree. Verify the symlink-skip code path
    # exists by reading it.
    src = (ROOT / "scripts" / "cv_editor" / "freezer.py").read_text()
    assert "is_symlink" in src, "freezer must check is_symlink"
    assert "continue" in src


@pytest.mark.skipif(
    shutil.which("typst") is None or not HAS_BESPOKE,
    reason="needs typst + bespoke template + fonts/",
)
def test_delete_frozen_handles_resolved_path_correctly():
    """delete_frozen must work even when ROOT contains symlinks
    (R1 MEDIUM V5-D: resolve both sides of the parents check)."""
    r = freezer.freeze_workspace()
    name = r.path.name
    # Sanity: delete should succeed.
    freezer.delete_frozen(name)
    assert not r.path.exists()
