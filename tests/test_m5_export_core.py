"""M5 5b CP2: export_core model + middle-fidelity formatters + visibility port.

Inline-data unit tests + drift guards. One read-only real-corpus smoke (build_model).
No writes. The MD/HTML emitters + golden-corpus tests come with CP3/CP4.
"""

from __future__ import annotations

from pathlib import Path

from _engine_guards import flags_typ_path
from cv_editor import author_flags
from cv_editor import export_core as ec
from cv_editor.export_core import ExportFlags

ROOT = Path(__file__).resolve().parent.parent


# ---------- visibility port (LITERAL, leak-guard relevant) ----------


def test_visible_truth_table():
    assert ec._visible([], [], "public-health") is True  # empty allowlist = all
    assert ec._visible(["academic"], [], "public-health") is False  # not in nonempty allowlist
    assert ec._visible(["public-health"], [], "public-health") is True
    assert ec._visible(["public-health"], ["public-health"], "public-health") is False  # hide wins
    assert ec._visible(["academic"], [], "full") is True  # full short-circuit


def test_entry_visible_highlighted_gate():
    assert ec._entry_visible({"highlighted": True}, "public-health", False) is False
    assert ec._entry_visible({"highlighted": True}, "public-health", True) is True
    assert ec._entry_visible({}, "public-health", False) is True


def test_flags_typ_visible_still_has_hide_from_first():
    # Drift sentinel: if flags.typ:visible() changes shape, re-check the port.
    src = flags_typ_path(ROOT).read_text()
    assert "#let visible(" in src
    block = src[src.index("#let visible(") :]
    block = block[: block.index("\n}") + 2]
    assert "hide-from" in block and "full" in block  # the 3 clauses still present
    # ORDER-AWARE (Reviewer A-LOW): hide-from must be checked (return false) BEFORE
    # the `full` short-circuit, else `full` would override a hide-from and the leak
    # semantics _entry_visible ports would silently change.
    assert block.index("hide-from.contains") < block.index('"full"'), (
        "flags.typ:visible() reordered — hide-from must win over the `full` "
        "short-circuit; re-verify the export_core._visible port."
    )


# ---------- publication citation (middle fidelity) ----------

PUB = {
    "authors": ["Smith J", "Public JQ", {"name": "Doe A", "co_senior": True}],
    "title": "A study",
    "journal": "JAMA",
    "year": 2024,
    "month": 3,
    "day": 5,
    "volume": "9",
    "issue": "2",
    "pages": "100-12",
    "doi": "10.1/x",
    "pmid": "123",
}


def test_publication_markdown_skeleton():
    b = ec.format_publication(PUB, "md", ["Public JQ"], ExportFlags())
    assert "**Public JQ**" in b  # self-bold
    assert "Doe A‡" in b  # co_senior glyph (bare in MD)
    assert "_JAMA_" in b  # journal italic
    assert "2024 Mar 5;9(2):100-12" in b  # date/vol(iss):pages assembly
    assert "doi: [10.1/x](https://doi.org/10.1/x)" in b
    assert "PubMed PMID: [123](https://pubmed.ncbi.nlm.nih.gov/123/)" in b
    # footnote text REUSED from author_flags (drift guard, not hardcoded)
    co_senior = next(f for f in author_flags.AUTHOR_FLAGS if f.key == "co_senior")
    assert b.endswith("‡" + co_senior.footnote)


def test_publication_html_skeleton():
    b = ec.format_publication(PUB, "html", ["Public JQ"], ExportFlags())
    assert "<strong>Public JQ</strong>" in b
    assert "Doe A<sup>‡</sup>" in b
    assert "<em>JAMA</em>" in b
    assert '<a href="https://doi.org/10.1/x">10.1/x</a>' in b
    assert '<a href="https://pubmed.ncbi.nlm.nih.gov/123/">123</a>' in b


def test_publication_electronic_id_no_volume():
    e = {"authors": ["A"], "title": "T", "journal": "J", "year": 2024, "pages": "kwag069"}
    b = ec.format_publication(e, "md", [], ExportFlags())
    assert "2024; kwag069" in b  # no-volume electronic id uses "; " (with space)


def test_publication_date_qualifier_em_dash():
    e = {
        "authors": ["A"],
        "title": "T",
        "journal": "J",
        "year": 2024,
        "date_qualifier": "Special Issue",
    }
    b = ec.format_publication(e, "md", [], ExportFlags())
    assert "2024 — Special Issue" in b


# ---------- builder-level leak guard + self-bold ----------

PUBS_DATA = [
    {
        "subsection": "S",
        "entries": [
            {"title": "VisibleOne", "authors": ["Public JQ"], "journal": "J", "year": 2024},
            {
                "title": "HiddenAud",
                "authors": ["A"],
                "journal": "J",
                "year": 2024,
                "audiences": ["academic"],
            },
            {
                "title": "HiddenBlock",
                "authors": ["A"],
                "journal": "J",
                "year": 2024,
                "hide-from": ["public-health"],
            },
            {
                "title": "HiddenHL",
                "authors": ["A"],
                "journal": "J",
                "year": 2024,
                "highlighted": True,
            },
        ],
    }
]


def test_publications_builder_excludes_hidden_entries():
    sec = ec.build_publications(PUBS_DATA, "md", "public-health", ["Public JQ"], ExportFlags())
    joined = " ".join(e.body for sub in sec.subsections for e in sub.entries)
    assert "VisibleOne" in joined and "**Public JQ**" in joined
    for leak in ("HiddenAud", "HiddenBlock", "HiddenHL"):
        assert leak not in joined


# ---------- cascade matrix: teaching does NOT cascade, education DOES ----------


def test_teaching_does_not_cascade_cluster_audiences():
    data = [
        {
            "institution": "U",
            "audiences": ["academic"],
            "entries": [{"role": "R", "course": "CourseX", "date": "2024"}],
        }
    ]
    sec = ec.build_teaching(data, "md", "public-health", [], ExportFlags())
    assert sec is not None  # entry (no own audiences) stays visible despite cluster
    assert "CourseX" in sec.subsections[0].clusters[0].entries[0].body


def test_education_cascades_cluster_audiences():
    data = [
        {
            "institution": "U",
            "audiences": ["academic"],
            "entries": [{"degree": "ScD", "date": "2024"}],
        }
    ]
    sec = ec.build_education(data, "md", "public-health", [], ExportFlags())
    assert sec is None  # cluster audiences cascade -> the only entry hidden -> empty


# ---------- grants: literal text, amount markup, pending skipped, dollars on ----------


def test_research_support_grant_rules():
    data = [
        {"status": "pending", "agency": "NIH", "title": "P", "role": "PI", "date": "2025"},
        {
            "status": "active",
            "agency": "NIH",
            "title": "A",
            "role": "PI",
            "date": "01/2020 - 12/2020",
            "amount": "\\$100,000",
            "pi": "Smith",
            "pi_label": "MPI",
            "project": "R01X",
        },
        {"status": "previous", "agency": "NSF", "title": "Prev", "role": "Co-I", "date": "2018"},
    ]
    sec = ec.build_research_support(data, "md", "public-health", [], ExportFlags())
    titles = [s.title for s in sec.subsections]
    assert "Pending Support" not in titles  # show_pending False
    assert titles == ["Active Support", "Previous Support"]
    active = next(s for s in sec.subsections if s.title == "Active Support")
    e = active.entries[0]
    assert e.body.startswith("**NIH**")  # agency strong, literal
    assert "MPI: Smith" in e.body and "R01X" in e.body
    assert any('"A"' in r and "Title:" in r for r in e.sub_rows)  # title literal in quotes
    assert any("100,000" in r for r in e.sub_rows)  # amount shown, \$ stripped


# ---------- dates ----------


def test_format_date_and_month_year():
    assert ec._format_date("2012 - 2016") == "2012 – 2016"
    assert ec._format_date("01/2026 -") == "01/2026 –"
    assert ec._format_date("NIH-funded") == "NIH-funded"  # internal hyphen untouched
    assert ec._format_month_year("05/2026") == "May 2026"
    assert ec._format_month_year("2026") == "2026"


# ---------- flag resolution ----------


def test_resolve_flags_fullcv():
    meta = {"build_variants": [{"filename": "fullcv", "inputs": {"audience": "public-health"}}]}
    f = ec.resolve_flags(meta)
    assert f.audience == "public-health"
    assert f.show_dollars is True and f.show_media_urls is True  # default-ON pair
    assert not any(
        [
            f.show_highlighted,
            f.show_notes,
            f.show_pending,
            f.show_media,
            f.show_oa,
            f.show_citations,
            f.review,
        ]
    )


# ---------- section titles parity ----------


def test_section_titles_match_canonical():
    """Drift guard (gotcha #41 class): export's plain-text titles must equal the M3
    canonical Typst-title map after stripping `#emph[...]` markup to its contents
    (the ONLY transform: honors' emphasised `&` -> literal `&`). Importing the M3
    map (rather than re-hardcoding) means a section rename can't silently leave
    export stale — it fails HERE too, not just in content/ + emit.typ."""
    import re

    from test_m3_section_titles import SECTION_TITLES as CANON

    def _flatten(t):  # "Honors #emph[&] Awards" -> "Honors & Awards"
        return re.sub(r"#emph\[([^\]]*)\]", r"\1", t)

    assert ec.SECTION_TITLES == {k: _flatten(v) for k, v in CANON.items()}, (
        "export_core.SECTION_TITLES drifted from the M3 canonical map "
        "(tests/test_m3_section_titles.py). Update both."
    )
    assert set(ec.SECTION_TITLES) == set(ec._BUILDERS)


# ---------- markup/escape smoke through _mk ----------


def test_mk_escapes_and_self_bolds():
    assert ec._mk("a *b* < c", "html", []) == "a <strong>b</strong> &lt; c"
    assert ec._mk("Public JQ", "html", ["Public JQ"]) == "<strong>Public JQ</strong>"
    assert ec._mk("Public JQ", "md", ["Public JQ"]) == "**Public JQ**"


# ---------- read-only real-corpus smoke ----------


def test_build_model_real_corpus_both_targets():
    for target in ("md", "html"):
        doc = ec.build_model(ROOT / "data", target=target)
        assert doc.target == target
        assert doc.header.name
        assert len(doc.sections) >= 5
        pubs = [s for s in doc.sections if s.key == "publications"]
        assert pubs and pubs[0].subsections and pubs[0].subsections[0].entries
        # leak-guard sanity: no obviously-hidden marker leaks (build_model honored flags)


# ---------- variant / section edge cases ----------


def test_unknown_variant_falls_back_to_public_defaults():
    """A typo'd variant must NOT raise and must NOT silently widen the view — it
    resolves to the public fullcv defaults (the leak-safe direction). Pinned so a
    future change can't make a bad variant name leak audience-restricted content."""
    meta = {"build_variants": [{"filename": "fullcv", "inputs": {"audience": "public-health"}}]}
    flags = ec.resolve_flags(meta, "definitely-not-a-real-variant")
    assert flags.audience == "public-health"
    assert flags.show_highlighted is False and flags.show_pending is False


def test_unknown_section_key_raises(tmp_path):
    (tmp_path / "meta.yml").write_text("name: X\nsections:\n  - not_a_section\n")
    try:
        ec.build_model(tmp_path, target=ec.MD)
        assert False, "expected ValueError on unknown section key"
    except ValueError as e:
        assert "not_a_section" in str(e)


def test_all_hidden_section_is_omitted(tmp_path):
    """A section whose every entry is hidden must drop out entirely (no dangling
    header) — verified end-to-end through the emitter."""
    (tmp_path / "meta.yml").write_text(
        "name: X\nself_bold: X\nsections:\n  - honors\n"
        "build_variants:\n  - filename: fullcv\n    inputs:\n      audience: public-health\n"
    )
    (tmp_path / "honors.yml").write_text(
        "- date: '2024'\n  award: Hidden\n  institution: Org\n  hide-from:\n    - public-health\n"
    )
    from cv_editor import export_emit

    doc = ec.build_model(tmp_path, target=ec.MD)
    assert all(s.key != "honors" for s in doc.sections)
    out = export_emit.render_markdown(doc)
    assert "Honors" not in out and "Hidden" not in out
