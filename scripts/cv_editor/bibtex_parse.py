"""
BibTeX parsing for the editor's import flows.

Pipeline:
  raw text  -->  bibtexparser v2 (with LatexDecodingMiddleware)
            -->  strip residual \\textit{} / \\textbf{} / escaped \\& \\% \\_
            -->  per-entry form-shape dict matching the publications schema

bibtexparser v2 is currently a beta release; we pin the version in
requirements (see plans/obj3-editor.md).
"""

from __future__ import annotations

import re

import bibtexparser
from bibtexparser.middlewares import LatexDecodingMiddleware

from cv_editor.author_flags import flag_defaults_dict as _flag_defaults

_MARKUP_RE = [
    (re.compile(r"\\textit\{([^}]+)\}"), r"\1"),
    (re.compile(r"\\emph\{([^}]+)\}"), r"\1"),
    (re.compile(r"\\textbf\{([^}]+)\}"), r"\1"),
    (re.compile(r"\\&"), "&"),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\_"), "_"),
    (re.compile(r"\\\$"), "$"),
    (re.compile(r"\\#"), "#"),
    # BibTeX en/em dashes: latexenc middleware turns `--` into U+2013 and
    # `---` into U+2014 BEFORE this regex runs. Convert both back to ASCII
    # hyphen for YAML storage.
    (re.compile(r"--"), "-"),
    (re.compile("—"), "-"),
    (re.compile("–"), "-"),
]


def _strip_markup(s: str) -> str:
    if not s:
        return ""
    for rx, repl in _MARKUP_RE:
        s = rx.sub(repl, s)
    # Strip any remaining outer braces on a single field value.
    s = re.sub(r"^\{(.+)\}$", r"\1", s.strip())
    return s.strip()


_DOI_URL_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "DOI:",
)


def _strip_doi_prefix(s: str) -> str:
    """Strip any of the common DOI URL prefixes. Reviewer-1 MEDIUM V5-D:
    the prior `.lstrip("https://doi.org/")` strips a character SET, not a
    prefix — it happened to work for canonical DOIs by accident but
    mutilated `https://dx.doi.org/...` etc."""
    for p in _DOI_URL_PREFIXES:
        if s.lower().startswith(p.lower()):
            return s[len(p) :]
    return s


def _author_to_yaml_form(s: str) -> str:
    """Convert one BibTeX author form ('{Van Der Berg}, J' or 'Yang, CJ') into
    the YAML 'Last F' shape ('Van Der Berg J', 'Yang CJ')."""
    s = s.strip()
    # Strip outer braces around a compound surname: "{Van Der Berg}, J" -> "Van Der Berg, J"
    s = re.sub(r"^\{([^}]+)\}", r"\1", s)
    if "," in s:
        last, first = s.split(",", 1)
        last = last.strip()
        # First part may have spaces; collapse to initials by capital letters.
        initials_raw = first.strip().replace(".", "").replace(" ", "")
        return f"{last} {initials_raw}".strip()
    return s


def _split_authors(field_value: str) -> list[str]:
    """BibTeX author lists are separated by ' and '."""
    if not field_value:
        return []
    parts = re.split(r"\s+and\s+", field_value)
    return [_author_to_yaml_form(_strip_markup(p)) for p in parts if p.strip()]


def parse_bibtex(text: str) -> list[dict]:
    """Parse one or more BibTeX entries; return a list of form-shaped dicts.

    Raises ValueError on parse failure (with a hopefully-useful message).
    """
    if not text or not text.strip():
        return []
    try:
        library = bibtexparser.parse_string(text, append_middleware=[LatexDecodingMiddleware()])
    except Exception as e:
        raise ValueError(f"BibTeX parse failed: {e}") from e

    entries = []
    for be in library.entries:
        f = {field.key: field.value for field in be.fields}

        title = _strip_markup(f.get("title", ""))
        journal = _strip_markup(f.get("journal", "") or f.get("booktitle", ""))
        year = (f.get("year") or "").strip()
        try:
            year_int = int(year) if year else None
        except ValueError:
            year_int = None
        month = (f.get("month") or "").strip().lower()
        # Try to int-ify month; otherwise leave None.
        from cv_editor.enrichment import _month_to_int

        month_int = _month_to_int(month) if month else None

        out = {
            "title": title,
            "authors": [
                {"name": a, **_flag_defaults()} for a in _split_authors(f.get("author", ""))
            ],
            "journal": journal,
            "year": year_int,
            "month": month_int,
            "volume": _strip_markup(f.get("volume", "")) or None,
            "issue": _strip_markup(f.get("number", "")) or None,
            "pages": _strip_markup(f.get("pages", "")) or None,
            "doi": _strip_doi_prefix((f.get("doi") or "").strip()) or None,
            "pmid": (f.get("pmid") or "").strip() or None,
            "pmcid": (f.get("pmcid") or "").strip() or None,
            "_bibtex_key": be.key,
            "_bibtex_type": be.entry_type,
        }
        entries.append({k: v for k, v in out.items() if v not in (None, "", [])})
    return entries
