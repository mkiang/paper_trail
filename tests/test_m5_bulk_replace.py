"""M5 CP5: the structure-aware global search/replace ENGINE (bulk_replace).

Flask-free unit tests on in-memory ruamel trees. The route-level multi-file
all-or-nothing + manifest behavior is covered separately (CP6/CP7). No writes
to real data.
"""

from __future__ import annotations

import io

from cv_editor import bulk_replace as br
from ruamel.yaml import YAML


def _load(text: str):
    y = YAML()
    y.preserve_quotes = True
    return y.load(text)


def _dump(data) -> str:
    y = YAML()
    y.preserve_quotes = True
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


PUBS = """\
- subsection: Peer-Reviewed Original Research
  entries:
    - title: A Metro study of NIH funding
      authors:
        - Public JQ
      journal: JAMA
      year: 2024
      doi: 10.1001/metro.2024
      pmcid: PMC123
      pages: 100-metro
"""


# ---------- allow-list ----------


def test_replaceable_fields_excludes_structured_text():
    f = br.replaceable_fields("publications")
    assert "title" in f and "journal" in f
    # structured text fields are hard-refused:
    for bad in ("doi", "pmcid", "epub_date", "pages"):
        assert bad not in f
    # non-text types never eligible:
    for bad in ("year", "pmid", "volume", "issue", "authors", "notes", "open_access"):
        assert bad not in f


def test_replaceable_fields_refuses_date_and_project():
    assert "date" not in br.replaceable_fields("research_support")
    assert "project" not in br.replaceable_fields("research_support")
    assert "agency" in br.replaceable_fields("research_support")  # free text allowed


def test_meta_excluded():
    assert br.replaceable_fields("meta") == []
    assert "meta" not in br.searchable_sections()


# ---------- collect ----------


def test_collect_hits_only_allowed_fields():
    data = _load(PUBS)
    hits = br.collect_in_section(data, "publications", "metro", "Metro U", case_sensitive=False)
    fields = {h.field for h in hits}
    assert fields == {"title"}  # NOT doi/pages (refused), NOT journal (no match)
    h = hits[0]
    assert h.count == 1
    assert h.before == "A Metro study of NIH funding"
    assert h.after == "A Metro U study of NIH funding"
    assert h.key == "publications|0|title"


def test_case_sensitive_default():
    data = _load(PUBS)
    # Default is case-sensitive: lowercase needle does NOT match "Metro".
    assert br.collect_in_section(data, "publications", "metro", "X") == []
    # Capitalized needle matches.
    hits = br.collect_in_section(data, "publications", "Metro", "X")
    assert len(hits) == 1 and hits[0].field == "title"


def test_empty_needle_and_noop_replacement():
    data = _load(PUBS)
    assert br.collect_in_section(data, "publications", "", "X") == []
    # replacement == needle -> after == before -> no hit
    assert br.collect_in_section(data, "publications", "Metro", "Metro") == []


def test_count_multiple_occurrences():
    data = _load("- subsection: S\n  entries:\n    - title: aa AA aa\n      journal: J\n")
    hits = br.collect_in_section(data, "publications", "aa", "bb", case_sensitive=True)
    assert len(hits) == 1 and hits[0].count == 2
    assert hits[0].after == "bb AA bb"


# ---------- literal (non-regex) semantics ----------


def test_replace_in_is_literal_not_regex():
    assert br.replace_in("a.b axb", "a.b", "Z", case_sensitive=True) == "Z axb"
    # replacement is inserted verbatim (no backref expansion)
    assert br.replace_in("xx", "x", r"\1", case_sensitive=True) == r"\1\1"
    # case-insensitive literal with regex-special needle
    assert br.replace_in("A.B", "a.b", "Z", case_sensitive=False) == "Z"


# ---------- markup-balance lint ----------


def test_markup_unbalanced_flags_orphaned_delimiter():
    # "*Novel*" -> replace "Novel*" with "New" orphans the leading *
    assert br.markup_unbalanced("*Novel* method", "*New method") is True


def test_markup_unbalanced_flags_introduced_dollar():
    assert br.markup_unbalanced("cost five", "cost $5") is True


def test_markup_balanced_clean_replace_not_flagged():
    assert br.markup_unbalanced("the Metro study", "the Harvard study") is False
    # escaped dollar carried through is fine
    assert br.markup_unbalanced(r"\$5 grant", r"\$10 grant") is False


def test_markup_unbalanced_flags_orphaned_dollar_by_removal():
    # balanced $...$ -> removing ONE $ leaves an orphan (opens math mode). The
    # count-increase-only heuristic missed this; now any $-count change flags.
    assert br.markup_unbalanced("a $x$ b", "a $x b") is True


def test_markup_dollar_count_preserved_not_flagged():
    assert br.markup_unbalanced("a $x$ b", "a $y$ b") is False


def test_collect_sets_markup_unbalanced_flag():
    data = _load("- subsection: S\n  entries:\n    - title: '*Novel* idea'\n      journal: J\n")
    hits = br.collect_in_section(data, "publications", "Novel*", "New", case_sensitive=True)
    assert len(hits) == 1 and hits[0].markup_unbalanced is True


# ---------- apply ----------


def test_apply_only_selected_keys():
    data = _load(
        "- subsection: S\n  entries:\n"
        "    - title: Metro A\n      journal: Metro J\n"
        "    - title: Metro B\n      journal: Other\n"
    )
    # Select only entry 0's title.
    n = br.apply_in_section(
        data, "publications", {(0, "title")}, "Metro", "Harvard", case_sensitive=True
    )
    assert n == 1
    assert data[0]["entries"][0]["title"] == "Harvard A"
    assert data[0]["entries"][0]["journal"] == "Metro J"  # not selected -> unchanged
    assert data[0]["entries"][1]["title"] == "Metro B"  # not selected -> unchanged


def test_apply_preserves_comments_and_order():
    data = _load(
        "- subsection: S\n  entries:\n"
        "    - title: Old Title  # keep me\n      journal: JAMA\n      year: 2024\n"
    )
    br.apply_in_section(data, "publications", {(0, "title")}, "Old", "New", case_sensitive=True)
    out = _dump(data)
    assert "New Title" in out
    assert "# keep me" in out  # comment survived ruamel round-trip
    assert out.index("title") < out.index("journal") < out.index("year")  # order kept


def test_apply_refuses_structured_field_even_if_selected():
    data = _load(PUBS)
    # Try to force a replace into doi (refused) -> apply ignores it.
    n = br.apply_in_section(data, "publications", {(0, "doi")}, "metro", "X", case_sensitive=False)
    assert n == 0
    assert data[0]["entries"][0]["doi"] == "10.1001/metro.2024"
