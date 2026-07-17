"""
Citation parsers + format detection for the editor's import flows.

Three input formats:
  1. BibTeX (`@article{...}`, etc.) — handled by bibtex_parse.parse_bibtex
  2. NLM Vancouver-style (`Authors. Title. Journal. Year. ... PubMed PMID: N.`)
     — primary path: extract PMID(s) and let enrichment.fetch_pubmed_batch
     return canonical metadata. The citation text itself is just confirmation.
  3. Free-form text containing an inline DOI — extract DOI, fetch via
     enrich_via_doi.

`detect_format` routes a single block to the right parser. The
`parse_citation_block` entry point splits multi-citation pastes (blank-
line separated for NLM; @-anchored for BibTeX) and returns a list of
`(format, parsed_dict_or_id)` tuples for downstream enrichment.
"""

from __future__ import annotations

import re

from cv_editor import bibtex_parse
from cv_editor.author_flags import flag_defaults_dict as _flag_defaults

# --- Detection ---

_PMID_RE = re.compile(r"\bPubMed\s+PMID:\s*(\d+)", re.IGNORECASE)
_PMCID_RE = re.compile(r"\bPubMed\s+Central\s+PMCID:\s*(PMC\d+)", re.IGNORECASE)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", re.IGNORECASE)
_BIBTEX_RE = re.compile(
    r"^\s*@(article|inproceedings|incollection|book|misc|techreport|unpublished|phdthesis|mastersthesis)\s*\{",
    re.IGNORECASE,
)


def detect_format(text: str) -> str:
    """Return one of: 'bibtex' | 'nlm' | 'doi' | 'unknown'."""
    if not text or not text.strip():
        return "unknown"
    if _BIBTEX_RE.match(text):
        return "bibtex"
    if _PMID_RE.search(text):
        return "nlm"
    if _DOI_RE.search(text):
        return "doi"
    return "unknown"


# --- Splitting multi-entry pastes ---


def split_blocks(text: str) -> list[str]:
    """Split a paste containing multiple citations into individual blocks.

    BibTeX: one block per `@type{...}` record.
    NLM/free-form: blocks separated by blank lines OR inferred per-PMID.
    """
    text = (text or "").strip()
    if not text:
        return []

    # If the paste looks like one or more BibTeX entries, split on lines starting with `@type{`.
    if _BIBTEX_RE.match(text):
        # Use a regex split that keeps the @-anchors.
        parts = re.split(r"(?=^\s*@\w+\s*\{)", text, flags=re.MULTILINE)
        return [p.strip() for p in parts if p.strip().startswith("@")]

    # Otherwise: blank-line separation. Falls back to whole-text if no blank lines.
    blocks = re.split(r"\n\s*\n+", text)
    blocks = [b.strip() for b in blocks if b.strip()]
    return blocks if blocks else [text]


# --- NLM Vancouver-format parsing ---


def parse_nlm_block(text: str) -> dict:
    """Parse a single NLM Vancouver-style citation.

    The robust path is: pull the PMID and let PubMed return the canonical
    record. If no PMID (rare — preprints, consortium reports), fall back to a
    regex parser that extracts what it can from the citation text itself.
    """
    pmid_m = _PMID_RE.search(text)
    pmcid_m = _PMCID_RE.search(text)
    doi_m = _DOI_RE.search(text)

    out = {
        "_source": "nlm",
        "_raw": text.strip(),
        "pmid": pmid_m.group(1) if pmid_m else None,
        "pmcid": pmcid_m.group(1) if pmcid_m else None,
        "doi": doi_m.group(1) if doi_m else None,
    }

    # Try to also extract a hint title in case enrichment fails.
    # Strip everything after "PubMed PMID:" first.
    head = text
    if pmid_m:
        head = text[: pmid_m.start()]
    # Vancouver pattern (rough): authors-list dot space title dot space journal dot space year ...
    # Authors stop at the first lone '.': "Smith J, Doe AB, ... Last MV. Title. Journal."
    # Use a forgiving pattern.
    m = re.match(
        r"\s*(?P<authors>[^.]+?\.)\s+"
        r"(?P<title>[^.]+(?:\?|\.))\s+"
        r"(?P<journal>[^.]+?)\.\s*"
        r"(?P<year>\d{4})",
        head,
    )
    if m:
        out["title"] = m.group("title").rstrip(".? ").strip()
        out["journal"] = m.group("journal").strip()
        try:
            out["year"] = int(m.group("year"))
        except ValueError:
            pass
        # Pull authors list and convert each "Last F" -> form dict.
        authors_str = m.group("authors").rstrip(". ")
        author_names = [a.strip() for a in authors_str.split(",") if a.strip()]
        out["authors"] = [{"name": a, **_flag_defaults()} for a in author_names]
    return {k: v for k, v in out.items() if v is not None}


def parse_doi_block(text: str) -> dict:
    """Free-form text with an inline DOI: extract DOI, return a stub form entry."""
    doi_m = _DOI_RE.search(text)
    if not doi_m:
        return {}
    return {"_source": "doi", "_raw": text.strip(), "doi": doi_m.group(1)}


# --- Top-level entry point ---


def parse_citation_block(text: str) -> list[dict]:
    """Parse a paste (one or more citations) into form-shaped dicts.

    Each returned dict has an `_source` ∈ {bibtex, nlm, doi} so the caller
    knows which enrichment pipeline to follow. Empty list if nothing
    parseable was found.
    """
    blocks = split_blocks(text)
    out = []
    for block in blocks:
        fmt = detect_format(block)
        if fmt == "bibtex":
            try:
                for parsed in bibtex_parse.parse_bibtex(block):
                    parsed["_source"] = "bibtex"
                    out.append(parsed)
            except ValueError:
                pass  # malformed BibTeX block — caller surfaces parse errors elsewhere
        elif fmt == "nlm":
            out.append(parse_nlm_block(block))
        elif fmt == "doi":
            d = parse_doi_block(block)
            if d:
                out.append(d)
    return out


def detect_id_from_paste(text: str) -> tuple[str | None, str | None]:
    """For the 'From DOI/PMID' single-input tab: return (doi, pmid).

    Accepts either a bare DOI ('10.NNN/...'), a bare numeric string (PMID),
    or any text containing an inline DOI / `PubMed PMID: N`.
    """
    if not text:
        return (None, None)
    s = text.strip()
    if re.fullmatch(r"\d{4,12}", s):
        return (None, s)
    if re.fullmatch(r"10\.\d{4,9}/\S+", s):
        return (s, None)
    pmid_m = _PMID_RE.search(s)
    doi_m = _DOI_RE.search(s)
    return (doi_m.group(1) if doi_m else None, pmid_m.group(1) if pmid_m else None)
