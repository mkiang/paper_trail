"""M5-5d CP1: the data/example/ fictional sample corpus.

Read-only tests — nothing here writes. The example corpus is the seed for
blank-CV headers (scaffold derives them via split_header) and the target of
reset-to-example, so these tests pin:
  * strict-clean check_data (0 errors AND 0 warnings) — the deferred 5c CP4
    assertion; active-grant end dates are 2099 so this can't rot;
  * header fidelity to the real data/*.yml headers (single-source contract:
    the example headers ARE the real headers modulo the genericization
    substitutions — a hand-edit to either side fails loudly here);
  * every renderer-required meta key (render-header/setup have NO defaults);
  * editor compatibility (subsection names are schema members, gotcha #77);
  * variant-filename disjointness so example builds can't clobber real PDFs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml as pyyaml
from _engine_guards import HAS_BESPOKE, bespoke_required, real_corpus_required
from cv_editor import data_check, example_build, paths, schemas
from cv_editor.yaml_io import _validate_publications_data, split_header

ROOT = Path(__file__).resolve().parent.parent
# The example corpus: the engine-root data/example privately, the bundled
# cv_editor/example_data in an installed/public tree (paths.example_dir handles
# both — P6b inc-4e, so these tests run in the restructured public tree too).
EXAMPLE = paths.example_dir()
REAL = ROOT / "data"

SECTION_FILES = [
    "publications",
    "presentations",
    "research_support",
    "service",
    "teaching",
    "mentees",
    "honors",
    "education",
    "appointments",
    "meta",
]

# NOTE: the example<->real header single-source drift guard (and its
# HEADER_SUBSTITUTIONS map, which hardcodes the owner's real grant/project/
# amount) lives in the PRIVATE-ONLY tests/test_m5_headers_private.py (P6b §8) —
# it is @real_corpus_required and excluded from the public export.

# Keys templates/bespoke/render.typ:render-header + templates/bespoke/lib/styles.typ:setup
# consume with NO fallback — a meta.yml missing any of these fails every typst compile.
RENDERER_REQUIRED_META_KEYS = [
    "name",
    "position",
    "department",
    "institution",
    "address",
    "contacts",
    "footer",
    "sections",
    "build_variants",
]


def _load(path: Path):
    return pyyaml.safe_load(path.read_text(encoding="utf-8"))


# ---------- CP4-from-5c: the corpus is strict-clean ----------


def test_example_corpus_check_data_strict_clean():
    issues = data_check.check_data(EXAMPLE)
    assert issues == [], [(i.severity, i.section, i.field, i.message) for i in issues]


# ---------- files + headers ----------


def test_all_example_files_exist_with_nonempty_headers():
    for name in SECTION_FILES:
        p = EXAMPLE / f"{name}.yml"
        assert p.exists(), f"missing data/example/{name}.yml"
        header, body = split_header(p.read_text(encoding="utf-8"))
        assert header.strip(), f"{name}.yml header is empty"
        assert body.strip(), f"{name}.yml body is empty"


def test_example_headers_contain_no_personal_name():
    for name in SECTION_FILES:
        text = (EXAMPLE / f"{name}.yml").read_text(encoding="utf-8")
        assert "Kiang" not in text, f"personal name leaked into example {name}.yml"
        assert "mkiang" not in text.lower()


def test_example_data_has_no_institution_prose():
    """Public leak gate (P6b §10): the shipped example corpus must not carry the
    owner's real affiliations in data or schema-doc prose. The example
    institution is fictional ("Example University …"); this denylist is the real
    biography, so it can't false-positive on the sample.
    """
    forbidden = ["Stanford", "Harvard", "San Diego State", "New York University", "NYU"]
    for name in SECTION_FILES:
        text = (EXAMPLE / f"{name}.yml").read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in text, f"real affiliation {tok!r} leaked into example {name}.yml"


# ---------- publications shape ----------


def test_example_publications_pass_authors_shape_guard():
    data = _load(EXAMPLE / "publications.yml")
    _validate_publications_data(data)  # raises CorruptedShapeError on failure


def test_example_publications_exercise_author_flags_and_notes():
    """The corpus must keep exercising the renderer features it demos."""
    text = (EXAMPLE / "publications.yml").read_text(encoding="utf-8")
    for feature in (
        "co_first: true",
        "co_senior: true",
        "group_authorship: true",
        "type: media",
        "type: contributions",
        "type: commentary",
        "open_access:",
        "hide-from:",
        "highlighted: true",
    ):
        assert feature in text, f"example publications lost coverage of {feature}"


def test_example_ids_are_fictional_shapes():
    """Fictional IDs must stay OUTSIDE the real assigned ranges so a
    rendered link can't resolve to someone's actual article: PMIDs real
    ceiling ~40M, PMC ~11M as of 2026 — we pin >= 90M for both."""
    data = _load(EXAMPLE / "publications.yml")
    for sub in data:
        for e in sub["entries"]:
            doi = e.get("doi")
            if doi:
                assert doi.startswith("10.9999/"), doi
            if e.get("pmid"):
                assert int(e["pmid"]) >= 90_000_000, e["pmid"]
            if e.get("pmcid"):
                assert int(e["pmcid"][3:]) >= 90_000_000, e["pmcid"]
            assert e.get("doi") or e.get("pmid"), (
                f"entry {e['title']!r} needs doi or pmid (0-warning gate)"
            )


# ---------- meta ----------


def test_example_meta_carries_every_renderer_required_key():
    meta = _load(EXAMPLE / "meta.yml")
    for key in RENDERER_REQUIRED_META_KEYS:
        assert key in meta, f"example meta.yml missing renderer-required {key}"
    assert meta["footer"].get("template")
    assert meta["footer"].get("date_format")
    assert isinstance(meta["address"], list) and meta["address"]
    assert isinstance(meta["contacts"], list) and meta["contacts"]
    assert meta["self_bold"] == "Public JQ"
    assert set(meta["sections"]) == {
        "education",
        "appointments",
        "publications",
        "presentations",
        "research_support",
        "service",
        "teaching",
        "honors",
        "mentees",
    }


@real_corpus_required
def test_example_variant_filenames_disjoint_from_real():
    ex_names = {v["filename"] for v in _load(EXAMPLE / "meta.yml")["build_variants"]}
    real_names = {v["filename"] for v in _load(REAL / "meta.yml")["build_variants"]}
    assert ex_names, "example meta needs at least one build variant"
    assert not (ex_names & real_names), (
        f"example variants {ex_names & real_names} would clobber real PDFs"
    )


# ---------- editor compatibility (gotcha #77: schema-driven subsections) ----------


def test_example_subsections_are_schema_members():
    for section in ("publications", "presentations", "service", "appointments"):
        sch = schemas.get(section)
        allowed = set(sch["subsections"])
        data = _load(EXAMPLE / f"{section}.yml")
        for group in data:
            assert group["subsection"] in allowed, (
                f"example {section}.yml subsection {group['subsection']!r} "
                "not in schema — the editor would reject saves post-reset"
            )


# ---------- CP2: staged build proof ----------


@bespoke_required
def test_stage_tree_contains_expected_files(tmp_path):
    stage = example_build.stage_tree(EXAMPLE, tmp_path / "stage")
    for rel in (
        "cv.typ",
        "templates/bespoke/render.typ",
        "templates/bespoke/lib/styles.typ",
        "templates/bespoke/lib/typography.typ",
        "templates/bespoke/content/publications.typ",
        "data/meta.yml",
        "data/publications.yml",
        "data/citation_counts.json",
    ):
        assert (stage / rel).exists(), f"staged tree missing {rel}"
    # fonts are NOT staged — compile passes an absolute --font-path instead
    assert not (stage / "fonts").exists()


def test_stage_tree_requires_citation_snapshot(tmp_path):
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "meta.yml").write_text("name: X\n")
    with pytest.raises(FileNotFoundError, match="citation_counts.json"):
        example_build.stage_tree(src, tmp_path / "stage")


@pytest.mark.skipif(
    shutil.which("typst") is None or not HAS_BESPOKE,
    reason="needs typst + bespoke template + fonts/",
)
def test_example_corpus_builds(tmp_path):
    results = example_build.build_corpus(EXAMPLE, tmp_path / "out", stage_dir=tmp_path / "stage")
    assert len(results) == 2, "example meta should define 2 variants"
    for res in results:
        assert res.returncode == 0, res.stderr
        assert res.pdf_path.exists()
        assert res.pdf_path.stat().st_size > 10_000, f"{res.pdf_path} suspiciously small"


# ---------- citation snapshot ----------


def test_example_citation_snapshot_shape_and_doi_join():
    snap = json.loads((EXAMPLE / "citation_counts.json").read_text(encoding="utf-8"))
    assert snap["version"] == 1
    assert isinstance(snap["counts"], dict) and snap["counts"]
    pubs = _load(EXAMPLE / "publications.yml")
    example_dois = {e["doi"].lower() for sub in pubs for e in sub["entries"] if e.get("doi")}
    for doi, rec in snap["counts"].items():
        assert doi == doi.lower(), f"snapshot DOI {doi} not canonical lowercase"
        assert doi in example_dois, f"snapshot DOI {doi} not in example corpus"
        assert isinstance(rec["count"], int)
