"""V1b unit tests: parsers + preprint detection. No network calls."""

from cv_editor import bibtex_parse, citation_parse, preprint

# ---- detect_format ----


def test_detect_bibtex():
    assert citation_parse.detect_format("@article{x, year={2024}}") == "bibtex"


def test_detect_nlm():
    s = "Smith J. Title. JAMA. 2024;100:1. PubMed PMID: 12345678."
    assert citation_parse.detect_format(s) == "nlm"


def test_detect_doi_only():
    assert citation_parse.detect_format("text with 10.9999/nejm.2018.3972 inline") == "doi"


def test_detect_unknown():
    assert citation_parse.detect_format("just plain text") == "unknown"


# ---- detect_id_from_paste ----


def test_detect_id_bare_doi():
    assert citation_parse.detect_id_from_paste("10.9999/nejm.2018.3972") == (
        "10.9999/nejm.2018.3972",
        None,
    )


def test_detect_id_bare_pmid():
    assert citation_parse.detect_id_from_paste("90000055") == (None, "90000055")


def test_detect_id_combined():
    s = "Smith J. Foo. JAMA. 2024. PubMed PMID: 90000055. doi: 10.9999/natmed.2024.3117."
    doi, pmid = citation_parse.detect_id_from_paste(s)
    assert pmid == "90000055"
    assert doi == "10.9999/natmed.2024.3117"


# ---- BibTeX parsing ----


def test_bibtex_simple():
    src = "@article{x, author={Smith, J}, title={Foo}, year={2024}, journal={Test}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["title"] == "Foo"
    assert out["year"] == 2024
    assert out["authors"][0]["name"] == "Smith J"


def test_bibtex_compound_surname():
    src = "@article{x, author={{Van Der Berg}, J}, title={Foo}, year={2024}, journal={J}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["authors"][0]["name"] == "Van Der Berg J"


def test_bibtex_markup_stripped():
    src = "@article{x, author={Doe, A}, title={Test of \\textit{italics} and \\textbf{bold}}, year={2024}, journal={J}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert "\\textit" not in out["title"]
    assert "\\textbf" not in out["title"]
    assert "italics" in out["title"]
    assert "bold" in out["title"]


def test_bibtex_pages_endash_to_hyphen():
    src = "@article{x, author={X, Y}, title={T}, year={2024}, journal={J}, pages={100--110}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert out["pages"] == "100-110"


def test_bibtex_accent_decoded():
    src = "@article{x, author={Marqu\\'{e}s, D}, title={T}, year={2024}, journal={J}}"
    [out] = bibtex_parse.parse_bibtex(src)
    assert "Marqués" in out["authors"][0]["name"]


def test_bibtex_invalid_returns_empty():
    """bibtexparser v2 emits a parsing-failed warning rather than raising;
    the wrapper returns an empty list for unparseable input."""
    out = bibtex_parse.parse_bibtex("@@@ not bibtex @@@ {{{}")
    assert out == []


# ---- NLM parsing ----


def test_nlm_extracts_pmid():
    s = "Smith J, Doe AB. Title of paper. JAMA. 2024 Jan 1;100(1):1-10. doi: 10.1001/jama.2024.1. PubMed PMID: 12345678."
    out = citation_parse.parse_nlm_block(s)
    assert out["pmid"] == "12345678"
    assert out["doi"] == "10.1001/jama.2024.1"


def test_nlm_extracts_pmcid():
    s = "Smith J. T. JAMA. 2024. PubMed PMID: 12345678; PubMed Central PMCID: PMC1234567."
    out = citation_parse.parse_nlm_block(s)
    assert out["pmcid"] == "PMC1234567"


def test_split_blocks_blank_separated():
    text = "First block.\n\nSecond block.\n\nThird block."
    blocks = citation_parse.split_blocks(text)
    assert len(blocks) == 3


def test_split_blocks_bibtex_anchored():
    text = "@article{a, year={2020}}\n@article{b, year={2021}}"
    blocks = citation_parse.split_blocks(text)
    assert len(blocks) == 2
    assert blocks[0].startswith("@article{a")
    assert blocks[1].startswith("@article{b")


# ---- Preprint detection ----


def test_preprint_medrxiv_journal():
    e = {"journal": "Preprint on medRxiv", "doi": "10.1101/2024.01.01.123"}
    assert preprint.is_preprint(e) is True


def test_preprint_nber_word_boundary():
    # "ember" should NOT match — word boundary.
    e1 = {"journal": "Preprint on NBER", "doi": "10.3386/w12345"}
    assert preprint.is_preprint(e1) is True
    e2 = {"journal": "Some random ember journal", "doi": ""}
    assert preprint.is_preprint(e2) is False


def test_preprint_doi_prefix_biorxiv():
    e = {"journal": "", "doi": "10.1101/foo"}
    assert preprint.is_preprint(e) is True


def test_preprint_arxiv_doi_NOT_flagged():
    """Round-3 false-positive guard: 10.48550/arXiv DOIs (mirror DOIs on
    AAAI proceedings etc.) must NOT be classified as preprints."""
    e = {
        "journal": "Proceedings of the AAAI Symposium Series",
        "doi": "10.48550/arXiv.2401.12345",
        "volume": "2",
        "issue": "1",
        "pages": "78-84",
    }
    assert preprint.is_preprint(e) is False


def test_preprint_nbib_real_published_paper():
    e = {"journal": "JAMA Internal Medicine", "doi": "10.9999/jaim.2022.1461"}
    assert preprint.is_preprint(e) is False


# ---- Title overlap ----


def test_title_overlap_identical():
    assert preprint.title_overlap("Foo bar baz", "Foo bar baz") == 1.0


def test_title_overlap_disjoint():
    assert preprint.title_overlap("Foo bar baz", "Quux corge grault") == 0.0


def test_title_overlap_partial():
    o = preprint.title_overlap(
        "Effects of climate change on respiratory mortality",
        "Effects of climate change on respiratory disease",
    )
    assert 0.5 <= o < 1.0


# ---- Promotion preservation matrix ----


def test_promotion_diff_replaces_and_preserves():
    existing = {
        "title": "Preprint version of foo",
        "journal": "Preprint on medRxiv",
        "year": 2023,
        "doi": "10.1101/2023.01.01.123",
        "highlighted": True,
        "notes": [{"type": "contributions", "text": "Did X."}],
    }
    canonical = {
        "title": "Preprint version of foo: clarifying details",
        "journal": "JAMA",
        "year": 2024,
        "month": 5,
        "doi": "10.1001/jama.2024.1",
        "pmid": "12345678",
        "authors": [{"name": "Smith J"}, {"name": "Public JQ"}],
    }
    diff = preprint.build_promotion_diff(existing, canonical)
    assert diff["title_overlap"] > 0.5
    assert "title" in diff["replaces"]
    assert "doi" in diff["replaces"]
    assert "pmid" in diff["replaces"]
    assert "highlighted" in diff["preserves"]
    assert "notes" in diff["preserves"]


def test_promotion_apply_keeps_notes_by_default():
    existing = {"title": "Old", "notes": [{"type": "contributions", "text": "Did X."}]}
    canonical = {"title": "New", "doi": "10.1/x", "authors": [{"name": "Smith J"}]}
    out = preprint.apply_promotion(existing, canonical)
    assert out["notes"] == existing["notes"]
    assert out["title"] == "New"
    assert out["doi"] == "10.1/x"


def test_promotion_apply_drops_notes_when_requested():
    existing = {
        "title": "Old",
        "notes": [{"type": "contributions", "text": "X"}, {"type": "media"}],
    }
    canonical = {"title": "New", "authors": []}
    out = preprint.apply_promotion(existing, canonical, drop_notes=[1])
    assert len(out["notes"]) == 1
    assert out["notes"][0]["type"] == "contributions"
