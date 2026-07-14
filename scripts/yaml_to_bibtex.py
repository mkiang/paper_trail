#!/usr/bin/env python3
"""Generate publications.bib from data/publications.yml.

Citekey scheme: firstauthor_YYYY_doisuffix (e.g. public_2024_jse.2024.0001).
Entry types: peer-reviewed subsections -> @article; other -> @misc.
"""

import re
import sys
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "publications.yml"
OUT = ROOT / "publications.bib"

SUFFIX_TOKENS = {"Jr", "Jr.", "Sr", "Sr.", "II", "III", "IV", "2nd", "3rd"}


def is_corporate(name):
    """Heuristic: corporate if the name has a comma OR more than 4 tokens."""
    return "," in name or len(name.split()) > 4


def split_person_name(name):
    """Return (surname, initials, suffix). Surname may be multi-word."""
    tokens = name.strip().split()
    if not tokens:
        return ("", "", "")
    suffix = ""
    if len(tokens) >= 2 and tokens[-1] in SUFFIX_TOKENS:
        suffix = tokens[-1].rstrip(".")
        tokens = tokens[:-1]
    last = tokens[-1] if tokens else ""
    if re.fullmatch(r"[A-Z]+(-[A-Z]+)*", last):
        return (" ".join(tokens[:-1]), last, suffix)
    return (" ".join(tokens), "", suffix)


MONTH_ABBR = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec",
}

SUBSECTION_TYPE = {
    "Peer-Reviewed Original Research": "article",
    "Other Peer-reviewed Publications": "article",
    "Other Scholarly Work (Not Peer-reviewed)": "misc",
}


def nfkd_ascii(s):
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def extract_author_name(a):
    if isinstance(a, dict):
        return a.get("name", "")
    return str(a)


def slugify_author(name):
    if not name:
        return "anon"
    if is_corporate(name):
        # Acronym from uppercase letters for corporate authors.
        letters = re.findall(r"\b[A-Z]", name)
        if letters:
            return "".join(letters).lower()
        return re.sub(r"[^a-z0-9]", "", nfkd_ascii(name.split()[0]).lower()) or "anon"
    # Person: keep the first whitespace-separated token of the surname.
    tokens = name.split()
    if not tokens:
        return "anon"
    slug = re.sub(r"[^a-z0-9\-]", "", nfkd_ascii(tokens[0]).lower())
    return slug or "anon"


def slugify_doi(doi):
    if not doi:
        return ""
    s = str(doi).strip()
    parts = s.split("/", 1)
    tail = parts[1] if len(parts) == 2 else s
    tail = tail.replace("/", "_")
    return re.sub(r"[^A-Za-z0-9._\-]", "", tail)


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
        words = re.findall(r"[a-z0-9]+", nfkd_ascii((entry.get("title") or "").lower()))[:3]
        suffix = "-".join(words) or "untitled"
    return f"{author_slug}_{year}_{suffix}"


def format_author_for_bibtex(name):
    if not name:
        return ""
    if is_corporate(name):
        return "{" + name + "}"
    surname, initials, suffix = split_person_name(name)
    if not surname:
        return name
    # Brace compound surnames so BibTeX doesn't mis-parse the last word.
    if " " in surname:
        surname = "{" + surname + "}"
    if suffix and initials:
        return f"{surname}, {suffix}, {initials}"
    if initials:
        return f"{surname}, {initials}"
    if suffix:
        return f"{surname}, {suffix}"
    return surname


def authors_field(authors):
    formatted = [format_author_for_bibtex(extract_author_name(a)) for a in authors]
    return " and ".join(x for x in formatted if x)


def escape_text(s):
    if s is None:
        return ""
    s = str(s)
    s = s.replace(r"\$", "\x00DOLLAR\x00")
    s = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\\textit{\1}", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"\\textbf{\1}", s)
    s = s.replace("&", r"\&").replace("%", r"\%").replace("#", r"\#")
    s = s.replace("$", r"\$")
    s = s.replace("\x00DOLLAR\x00", r"\$")
    return s


def escape_title(s):
    return "{{" + escape_text(s) + "}}"


def format_pages(p):
    if not p:
        return ""
    s = str(p).strip()
    return re.sub(r"(?<=\w)-(?=\w)", "--", s, count=1)


def emit_entry(entry, citekey, entry_type):
    fields = []
    authors = authors_field(entry.get("authors") or [])
    if authors:
        fields.append(("author", "{" + authors + "}"))
    if entry.get("title"):
        fields.append(("title", escape_title(entry["title"])))
    if entry.get("journal"):
        key = "journal" if entry_type == "article" else "howpublished"
        fields.append((key, "{" + escape_text(entry["journal"]) + "}"))
    if entry.get("year") is not None:
        fields.append(("year", "{" + str(entry["year"]) + "}"))
    if entry.get("month") is not None and isinstance(entry["month"], int) and 1 <= entry["month"] <= 12:
        fields.append(("month", MONTH_ABBR[entry["month"]]))
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
    if entry.get("date_qualifier"):
        fields.append(("note", "{" + escape_text(str(entry["date_qualifier"])) + "}"))
    lines = [f"@{entry_type}{{{citekey},"]
    width = max((len(k) for k, _ in fields), default=0)
    for k, v in fields:
        lines.append(f"  {k.ljust(width)} = {v},")
    if len(lines) > 1 and lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines)


def main():
    data = yaml.safe_load(DATA.read_text())
    flat = []
    for section in data:
        etype = SUBSECTION_TYPE.get(section.get("subsection", ""), "misc")
        for entry in section.get("entries", []) or []:
            flat.append((etype, entry))
    citekeys = []
    seen = {}
    for etype, entry in flat:
        base = build_citekey(entry)
        n = seen.get(base, 0)
        citekeys.append(base if n == 0 else base + chr(ord("a") + n - 1))
        seen[base] = n + 1
    chunks = [
        "% Auto-generated from data/publications.yml by scripts/yaml_to_bibtex.py.",
        "% Do not edit by hand -- edit the YAML and re-run ./build.sh.",
        f"% Entries: {len(flat)}",
        "",
    ]
    for (etype, entry), ck in zip(flat, citekeys):
        chunks.append(emit_entry(entry, ck, etype))
        chunks.append("")
    OUT.write_text("\n".join(chunks).rstrip() + "\n")
    print(f"[bib] wrote {len(flat)} entries to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
