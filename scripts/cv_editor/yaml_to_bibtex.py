#!/usr/bin/env python3
"""
Generate publications.bib from data/publications.yml.

Citekey scheme: firstauthor_YYYY_doisuffix
  e.g. public_2020_M20-3100, ech_2025_28584, van_2025_aabb1234

Entry types:
  PRR / OPR -> @article
  OSW       -> @misc

See typst/plans/obj2-bibtex.md for the full field-mapping spec.
"""

import re
import sys
import unicodedata

import yaml

# Make `cv_editor` package importable so we can reuse the canonical name
# helpers (R2-H3 dedup, 2026-05-17). Build.sh and the launcher already
# add this path; this guard keeps the script standalone-runnable.
from cv_editor import paths  # noqa: E402

# Workspace paths from the seam. OUT (publications.bib) stays at the repo/
# workspace root — load-bearing for build.sh, .gitignore, and P6's leak-gate
# denylist (do not move it). A fresh subprocess reads CV_EDITOR_* env here.
ROOT = paths.data_root()
DATA = paths.data_dir() / "publications.yml"
OUT = paths.data_root() / "publications.bib"


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, DATA, OUT
    ROOT = paths.data_root()
    DATA = paths.data_dir() / "publications.yml"
    OUT = paths.data_root() / "publications.bib"


from cv_editor.author_names import (  # noqa: E402
    SUFFIX_TOKENS as _SUFFIX_TOKENS,  # noqa: F401
)
from cv_editor.author_names import (  # noqa: E402
    is_corporate as _ae_is_corporate,
)
from cv_editor.author_names import (  # noqa: E402
    split_person_name as _ae_split_person_name,
)

MONTH_ABBR = {
    1: "jan",
    2: "feb",
    3: "mar",
    4: "apr",
    5: "may",
    6: "jun",
    7: "jul",
    8: "aug",
    9: "sep",
    10: "oct",
    11: "nov",
    12: "dec",
}

# Suborder maps to BibTeX entry type
SUBSECTION_TYPE = {
    "Peer-Reviewed Original Research": "article",
    "Other Peer-reviewed Publications": "article",
    "Other Scholarly Work (Not Peer-reviewed)": "misc",
}


# ----- Helpers -----


def nfkd_ascii(s):
    """Strip diacritics for slugs."""
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def extract_author_name(a):
    if isinstance(a, dict):
        return a.get("name", "")
    return str(a)


# Re-export the canonical helpers from cv_editor.author_names so both
# `./build.sh` (which runs this script) and the editor share one impl.
is_corporate = _ae_is_corporate


def slugify_author(name):
    """First author's surname slug for citekey."""
    if not name:
        return "anon"
    if is_corporate(name):
        # Acronym from uppercase letters: "Example Consortium for Health..." -> "ech"
        letters = re.findall(r"\b[A-Z]", name)
        if letters:
            return "".join(letters).lower()
        # Fallback: first word
        return re.sub(r"[^a-z0-9]", "", nfkd_ascii(name.split()[0]).lower()) or "anon"
    # Person: keep first whitespace-separated token of the surname.
    # "Van Der Berg J" -> "van", "Smith-Jones K" -> "smith-jones".
    tokens = name.split()
    if not tokens:
        return "anon"
    surname_first = tokens[0]  # initials are always at the END
    slug = nfkd_ascii(surname_first).lower()
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    return slug or "anon"


def slugify_doi(doi):
    """Take everything after the first '/'; replace further '/' with '_'; sanitize."""
    if not doi:
        return ""
    s = str(doi).strip()
    parts = s.split("/", 1)
    if len(parts) == 2:
        tail = parts[1]
    else:
        tail = s
    tail = tail.replace("/", "_")
    tail = re.sub(r"[^A-Za-z0-9._\-]", "", tail)
    return tail


def build_citekey(entry):
    authors = entry.get("authors") or []
    first = extract_author_name(authors[0]) if authors else ""
    author_slug = slugify_author(first)
    year = entry.get("year", "")
    if entry.get("doi"):
        suffix = slugify_doi(entry["doi"])
    elif entry.get("pmid"):
        suffix = f"pmid{entry['pmid']}"
    else:
        title = (entry.get("title") or "").lower()
        words = re.findall(r"[a-z0-9]+", nfkd_ascii(title))[:3]
        suffix = "-".join(words) or "untitled"
    return f"{author_slug}_{year}_{suffix}"


SUFFIX_TOKENS = _SUFFIX_TOKENS
split_person_name = _ae_split_person_name


def format_author_for_bibtex(name):
    """Convert 'Surname Initials' -> '{Surname}, Initials' or 'Surname, Initials'."""
    if not name:
        return ""
    if is_corporate(name):
        # Wrap entire corporate author in braces so BibTeX treats it as one entity.
        return "{" + name + "}"
    surname, initials, suffix = split_person_name(name)
    if not surname:
        return name  # fallback
    # Brace compound surnames (more than one whitespace-separated word) so
    # BibTeX doesn't mis-parse "Van Der Berg" as if "Berg" is the surname.
    if " " in surname:
        surname = "{" + surname + "}"
    # Initials stay exactly as in YAML (e.g. "MV", "Y-H", "M-L") so
    # bibliography styles can format them however they prefer without us
    # introducing spurious spaces around hyphens.
    # BibTeX 3-part name form: "Last, Jr, First" when a suffix is present.
    if suffix and initials:
        return f"{surname}, {suffix}, {initials}"
    if initials:
        return f"{surname}, {initials}"
    if suffix:
        return f"{surname}, {suffix}"
    return surname


def authors_field(authors):
    formatted = []
    for a in authors:
        n = extract_author_name(a)
        if n:
            formatted.append(format_author_for_bibtex(n))
    return " and ".join(formatted)


# ----- Text escaping -----

# These need escaping when they're literal (not part of Typst markup we convert)
LATEX_ESCAPE = {
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
}


def escape_text(s):
    r"""
    Convert Typst markup + LaTeX-escape special chars.

    Order matters:
      1. Pre-escape `\$` -> placeholder (preserve already-escaped dollars).
      2. Convert *bold* and _italic_ Typst spans to \textbf{...} / \textit{...}.
      3. Escape remaining bare $ & % # _ { }.
      4. Restore placeholder.

    Em-dashes (---), en-dashes (--), and Unicode pass through unchanged.
    """
    if s is None:
        return ""
    s = str(s)

    # Stash already-escaped dollars
    s = s.replace(r"\$", "\x00DOLLAR\x00")

    # Convert Typst _italic_ -> \textit{...}. Use a non-greedy match between single
    # underscores, where the underscores are NOT preceded/followed by alphanumerics
    # (so plain identifiers like FY_2025 don't get mangled).
    s = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\\textit{\1}", s)
    # Typst *bold* -> \textbf{...} with the same word-boundary guard.
    s = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"\\textbf{\1}", s)

    # Escape bare LaTeX specials. _ is not escaped here because by this point
    # any underscores left are part of identifiers we should preserve as-is for
    # human readability — but BibTeX values are inside braces, where _ is safe.
    s = s.replace("&", r"\&").replace("%", r"\%").replace("#", r"\#")
    # Escape bare { } that aren't ours. Skip for now — none in current data.
    # Bare $ -> escape
    s = s.replace("$", r"\$")

    s = s.replace("\x00DOLLAR\x00", r"\$")
    return s


def escape_title(s):
    """Titles get double-braced for case preservation, after markup conversion."""
    return "{{" + escape_text(s) + "}}"


def format_pages(p):
    if not p:
        return ""
    s = str(p).strip()
    # If a single ASCII hyphen separates two numeric/alnum tokens, expand to '--'.
    s = re.sub(r"(?<=\w)-(?=\w)", "--", s, count=1)
    return s


# ----- Emit a single entry -----


def emit_entry(entry, citekey, entry_type):
    fields = []

    authors = authors_field(entry.get("authors") or [])
    if authors:
        fields.append(("author", "{" + authors + "}"))

    title = entry.get("title")
    if title:
        fields.append(("title", escape_title(title)))

    journal = entry.get("journal")
    if journal:
        # For @misc, journal becomes howpublished
        key = "journal" if entry_type == "article" else "howpublished"
        fields.append((key, "{" + escape_text(journal) + "}"))

    if entry.get("year") is not None:
        fields.append(("year", "{" + str(entry["year"]) + "}"))

    if entry.get("month") is not None:
        m = entry["month"]
        if isinstance(m, int) and 1 <= m <= 12:
            fields.append(("month", MONTH_ABBR[m]))

    if entry.get("volume"):
        fields.append(("volume", "{" + str(entry["volume"]) + "}"))

    if entry.get("issue"):
        fields.append(("number", "{" + str(entry["issue"]) + "}"))

    if entry.get("pages"):
        fields.append(("pages", "{" + format_pages(entry["pages"]) + "}"))

    if entry.get("doi"):
        fields.append(("doi", "{" + str(entry["doi"]) + "}"))

    if entry.get("pmid"):
        fields.append(("pmid", "{" + str(entry["pmid"]) + "}"))

    if entry.get("pmcid"):
        fields.append(("pmcid", "{" + str(entry["pmcid"]) + "}"))

    note_parts = []
    if entry.get("date_qualifier"):
        note_parts.append(str(entry["date_qualifier"]))
    if note_parts:
        fields.append(("note", "{" + escape_text("; ".join(note_parts)) + "}"))

    lines = [f"@{entry_type}{{{citekey},"]
    width = max(len(k) for k, _ in fields) if fields else 0
    for k, v in fields:
        lines.append(f"  {k.ljust(width)} = {v},")
    # Drop trailing comma on the last field for cleanliness
    if len(lines) > 1 and lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines)


# ----- Main -----


def main():
    with open(DATA) as f:
        data = yaml.safe_load(f)

    flat = []
    for section in data:
        sub = section.get("subsection", "")
        entry_type = SUBSECTION_TYPE.get(sub, "misc")
        for entry in section.get("entries", []) or []:
            flat.append((sub, entry_type, entry))

    # Build citekeys; resolve collisions.
    citekeys = []
    seen = {}
    for sub, etype, entry in flat:
        base = build_citekey(entry)
        n = seen.get(base, 0)
        if n == 0:
            ck = base
        else:
            ck = base + chr(ord("a") + n - 1)
        seen[base] = n + 1
        citekeys.append(ck)

    # If a base collided, the FIRST occurrence stays unsuffixed and the rest
    # get a, b, c. That's the BibTeX/biblatex convention. Re-confirm by
    # checking the seen counts.
    chunks = [
        "% Auto-generated from data/publications.yml by scripts/yaml_to_bibtex.py.",
        "% Do not edit by hand — edit the YAML and re-run ./build.sh.",
        f"% Entries: {len(flat)}",
        "",
    ]
    for (sub, etype, entry), ck in zip(flat, citekeys):
        chunks.append(emit_entry(entry, ck, etype))
        chunks.append("")

    OUT.write_text("\n".join(chunks).rstrip() + "\n")
    print(f"[bib] wrote {len(flat)} entries to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
