"""
Author-name parsing/serializing — for both display and round-trip.

YAML stores authors as either:
- plain string: "Public JQ", "Van Der Berg J", "Smith-Jones K", "Roe HG Jr",
                "Example Consortium for Health"
- dict:        {name: "Torres M", co_first: true}
               {name: "Public JQ", co_senior: true}
               {name: "Example Consortium for Health",
                group_authorship: true}

The editor form represents an author as:
    {"name": str, "co_first": bool, "co_senior": bool, "group_authorship": bool}

Lifted from yaml_to_bibtex.py's split_person_name + is_corporate. The
serializer back to YAML uses ruamel CommentedMap (not plain dict) so
the dump round-trips byte-equal to the source.
"""

from __future__ import annotations

import re
import unicodedata

from ruamel.yaml.comments import CommentedMap

from cv_editor.author_flags import AUTHOR_FLAGS, flag_defaults_dict

SUFFIX_TOKENS = {"Jr", "Jr.", "Sr", "Sr.", "II", "III", "IV", "2nd", "3rd"}


# ----- Normalization (V23-B Phase 1.5, 2026-05-26) -----
# Moved from scripts/qc_publications.py so both the QC sweep and the
# new decision_cross_check predicate share ONE canonicalizer for
# authors / titles / journals. Cross-system silencing requires the
# author yaml_value snapshot comparison to be insensitive to diacritics
# and `"Surname, Initials"` form (System B stores raw names; System A
# stores normalized). See gotcha #59.


def nfkd(s):
    if not s:
        return s
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def norm_author_name(name):
    """Canonical form for cross-system comparison. NFKD-fold, drop
    combining marks, replace comma with space, collapse whitespace."""
    if not name:
        return ""
    s = nfkd(name).strip()
    s = s.replace(",", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def extract_author_name(a):
    """YAML author (str or dict) -> the bare name string."""
    if isinstance(a, dict):
        return a.get("name", "")
    return str(a)


def joined_author_names_normalized(authors) -> str:
    """`"; "`-joined normalized author names for an entry. Matches
    qc_publications.diff_entry's authors-mismatch yaml_value format."""
    return "; ".join(norm_author_name(extract_author_name(a)) for a in (authors or []))


def is_corporate(name: str) -> bool:
    """Heuristic: name is corporate if it has a comma OR more than 4 tokens."""
    return "," in name or len(name.split()) > 4


def split_person_name(name: str) -> tuple[str, str, str]:
    """Return (surname, initials, suffix). Surname may be multi-word.

    Examples:
        "Public JQ"            -> ("Public", "JQ", "")
        "Van Der Berg J"       -> ("Van Der Berg", "J", "")
        "Smith-Jones K"   -> ("Smith-Jones", "K", "")
        "Roe HG Jr"          -> ("Roe", "HG", "Jr")
        "Coombs G 3rd"        -> ("Coombs", "G", "3rd")
    """
    tokens = name.strip().split()
    if not tokens:
        return ("", "", "")
    suffix = ""
    if len(tokens) >= 2 and tokens[-1] in SUFFIX_TOKENS:
        suffix = tokens[-1].rstrip(".")
        tokens = tokens[:-1]
    last = tokens[-1] if tokens else ""
    if re.fullmatch(r"[A-Z]+(-[A-Z]+)*", last):
        surname = " ".join(tokens[:-1])
        return (surname, last, suffix)
    return (" ".join(tokens), "", suffix)


def self_surname_tokens(self_bold) -> set[str]:
    """NFKD-folded, lowercased surname tokens for matching the author's own
    name within a publication's author list. Accepts ``meta.self_bold`` as a
    string OR a list (mirrors ``templates/bespoke/render.typ`` ``_self-bold-terms``). The QC
    self-absent check (qc_publications) flags an entry when NO author name
    contains ANY of these tokens as a substring. Returns an empty set when
    nothing is derivable — an unset ``self_bold`` yields no self-name to check
    against (so the check simply doesn't fire).

        "Public JQ"                    -> {"public"}      (typical config)
        "Van Der Berg J"                -> {"van", "der", "berg"}
        "Ng K"                         -> {"ng"}
        ["Public JQ", "J. Q. Public"]  -> {"public"}
        "" / None / []                 -> set()           (no self-name)

    ``strip(".")`` + ``len >= 2`` drops bare initials (so "J. Q. Public"
    contributes only "public") while keeping genuinely short surnames ("Ng").
    """
    if not self_bold:
        terms: list[str] = []
    elif isinstance(self_bold, str):
        terms = [self_bold] if self_bold.strip() else []
    elif isinstance(self_bold, (list, tuple)):
        terms = [str(t) for t in self_bold]
    else:
        # Garbled scalar config (e.g. a YAML typo `self_bold: 2026` or
        # `self_bold: true`): treat as unset so no self-name is derived —
        # never crash the QC sweep with a TypeError.
        terms = []
    tokens: set[str] = set()
    for term in terms:
        surname = split_person_name(norm_author_name(term))[0]
        for tok in surname.split():
            tok = tok.strip().strip(".").lower()
            if len(tok) >= 2:
                tokens.add(tok)
    return tokens or set()


def author_to_form(a) -> dict:
    """YAML author (str or dict) -> form-friendly dict."""
    if isinstance(a, dict):
        out = {"name": a.get("name", "")}
        for f in AUTHOR_FLAGS:
            out[f.key] = bool(a.get(f.key, False))
        return out
    return {"name": str(a), **flag_defaults_dict()}


def normalize_authors_for_render(authors):
    """Defensive normalization for the form / view renderers (task #30,
    2026-05-25). The editor's normal save path (`_apply_author_list`)
    always emits a YAML list, but some publications have ended up with
    `authors:` as a plain string (suspected DOI-import path bug; not
    yet repro'd). Without this guard, `[author_to_form(a) for a in
    (entry.get('authors') or [])]` iterates the string character-by-
    character and renders N one-char author rows — confusing and easy
    to save back into the same broken shape.

    Treat a non-list authors value as a single-author list with that
    value as the name, so the user can edit + correct it in place.
    Returns the list unchanged when already well-shaped."""
    if authors is None:
        return []
    if isinstance(authors, str):
        return [authors]
    if isinstance(authors, list):
        return authors
    # Anything else (int, dict, ...) — wrap as a stringified single author.
    return [str(authors)]


def has_malformed_authors(entry) -> bool:
    """Quick predicate for the index-page banner. True if the entry's
    `authors:` field exists but isn't a non-empty list. See
    `normalize_authors_for_render` for context."""
    if not isinstance(entry, dict):
        return False
    if "authors" not in entry:
        return False
    a = entry.get("authors")
    return not isinstance(a, list) or len(a) == 0


def form_to_yaml_author(form: dict):
    """Form-friendly dict -> YAML author. Idempotent.

    Returns a plain string when no flags are set; otherwise a ruamel
    CommentedMap so the dump matches existing dict-form formatting.
    Falsy flags are NEVER serialized (asymmetric on-write rule keeps
    the YAML clean — author_to_form accepts both shapes).
    """
    name = form.get("name", "").strip()
    if not any(form.get(f.key) for f in AUTHOR_FLAGS):
        return name
    cm = CommentedMap()
    cm["name"] = name
    for f in AUTHOR_FLAGS:
        if form.get(f.key):
            cm[f.key] = True
    return cm


def first_author_display(authors) -> str:
    """For list views: 'Public JQ', 'ECH' (acronym for corporate), '(empty)'."""
    if not authors:
        return "(none)"
    name = authors[0]["name"] if isinstance(authors[0], dict) else str(authors[0])
    if is_corporate(name):
        # Acronym from uppercase letters
        letters = re.findall(r"\b[A-Z]", name)
        return "".join(letters) if letters else name.split()[0]
    return name
