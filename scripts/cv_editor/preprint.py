"""
Preprint detection and promotion logic (V1b).

`is_preprint(entry)` flags entries published on a preprint server.
Detection is via journal-name substring (with word boundaries) OR DOI
prefix list. The arXiv DOI prefix `10.48550/arXiv` is intentionally
EXCLUDED — see the round-3 critique: it false-positives on AAAI
conference papers that carry an arXiv mirror DOI.

`promote_preprint(existing_entry, canonical_form_entry)` applies the
preservation matrix (locked in plan R3 → R4):
  - notes              KEPT by default; per-note checkbox to drop.
  - subsection         caller decides target (form picks PRR by default).
  - authors            manual review required; not auto-replaced.
  - highlighted        KEPT.
  - pmid / pmcid / doi REPLACED by canonical.
  - date_qualifier     PRESERVED.
"""

from __future__ import annotations

import re

from cv_editor.author_flags import ALL_FLAG_KEYS as _FLAG_KEYS

PREPRINT_JOURNAL_SUBSTRINGS = {"arxiv", "medrxiv", "biorxiv", "ssrn"}
# Plus 'nber' with word boundaries (3-letter substring would be too greedy).
_NBER_RE = re.compile(r"\bnber\b", re.IGNORECASE)
# DOI prefixes for known preprint servers. arXiv 10.48550 deliberately omitted.
PREPRINT_DOI_PREFIXES = ("10.1101/", "10.3386/", "10.2139/ssrn")


def is_preprint(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    journal = (entry.get("journal") or "").lower()
    for s in PREPRINT_JOURNAL_SUBSTRINGS:
        if re.search(rf"\b{s}\b", journal):
            return True
    if _NBER_RE.search(journal):
        return True
    doi = (entry.get("doi") or "").lower()
    return any(doi.startswith(p) for p in PREPRINT_DOI_PREFIXES)


def _norm_author_name(s: str) -> str:
    # Intentionally simpler than author_names.norm_author_name (no NFKD,
    # no comma-replace): _port_flags zips a preprint author list against
    # a canonical author list that came from the same upstream source,
    # so case + whitespace are the only legitimate variation. Don't
    # consolidate without verifying both lists carry the same diacritic
    # form on every paired name.
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _port_flags(preprint_authors: list, canonical_authors: list) -> list:
    """Return canonical author list with dict-form flags ported from
    preprint authors by case-insensitive name match. Canonical entries
    without a match become plain strings (preserves the canonical shape).
    Mirrors `pubmed_sync._merge_pubmed_authors_preserving_markers`.
    """
    flag_map: dict[str, dict] = {}
    for a in preprint_authors or []:
        if isinstance(a, dict):
            flags = {k: True for k in _FLAG_KEYS if a.get(k)}
            if flags:
                flag_map[_norm_author_name(a.get("name", ""))] = flags
    if not flag_map:
        return list(canonical_authors or [])
    out: list = []
    for c in canonical_authors or []:
        name = c.get("name", "") if isinstance(c, dict) else str(c)
        flags = flag_map.get(_norm_author_name(name))
        if flags:
            merged = dict(c) if isinstance(c, dict) else {"name": name}
            merged.update(flags)
            out.append(merged)
        else:
            out.append(c)
    return out


def title_overlap(a: str, b: str) -> float:
    """Token-set overlap fraction. 0..1. Symmetric."""

    def toks(s):
        return set(t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2)

    A, B = toks(a), toks(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def build_promotion_diff(existing: dict, canonical: dict, overlap_threshold: float = 0.5) -> dict:
    """Return a diff report so the UI can show what would change.

    Shape:
      {
        title_overlap: float,
        warn_low_overlap: bool,
        replaces: { field: {old, new} },        # IDs + bibliographic fields
        preserves: [field, ...],                 # untouched (e.g. notes, highlighted, date_qualifier)
        authors_review: {old: [...], new: [...]} # always present; manual merge
      }
    """
    overlap = title_overlap(existing.get("title", ""), canonical.get("title", ""))
    out = {
        "title_overlap": overlap,
        "warn_low_overlap": overlap < overlap_threshold,
        "replaces": {},
        "preserves": [],
        "authors_review": {
            "old": existing.get("authors") or [],
            "new": canonical.get("authors") or [],
        },
    }
    for field in (
        "title",
        "journal",
        "year",
        "month",
        "day",
        "volume",
        "issue",
        "pages",
        "doi",
        "pmid",
        "pmcid",
    ):
        old = existing.get(field)
        new = canonical.get(field)
        if new in (None, "", []):
            continue
        if old != new:
            out["replaces"][field] = {"old": old, "new": new}
    for field in ("notes", "highlighted", "date_qualifier", "open_access", "epub_date"):
        if existing.get(field) not in (None, "", [], {}, False):
            out["preserves"].append(field)
    return out


def apply_promotion(
    existing_form: dict,
    canonical_form: dict,
    chosen_authors: list | None = None,
    drop_notes: list[int] | None = None,
) -> dict:
    """Apply the preservation matrix to produce the merged form entry.

    Args:
      existing_form: the preprint entry, in form-shape
      canonical_form: the canonical published entry (from to_form_entry)
      chosen_authors: caller's per-author resolution (defaults to canonical
                      authors; caller's UI must provide an explicit list).
      drop_notes: indices into existing_form['notes'] to drop. Default: keep all.

    Returns: merged form entry. Caller writes via the usual save path.
    """
    merged = dict(existing_form)

    # 1. Replace bibliographic fields with canonical values where present.
    for field in (
        "title",
        "journal",
        "year",
        "month",
        "day",
        "volume",
        "issue",
        "pages",
        "doi",
        "pmid",
        "pmcid",
    ):
        cv = canonical_form.get(field)
        if cv not in (None, "", []):
            merged[field] = cv

    # 2. Authors: use the caller's resolution; default to canonical authors,
    # but port any dict-form flags (co_first / co_senior / group_authorship)
    # from matching preprint authors to the canonical ones (R1-M4 fix,
    # 2026-05-17). Mirrors `_merge_pubmed_authors_preserving_markers` in
    # `pubmed_sync.py`.
    if chosen_authors is not None:
        merged["authors"] = chosen_authors
    elif canonical_form.get("authors"):
        merged["authors"] = _port_flags(
            existing_form.get("authors") or [],
            canonical_form["authors"],
        )

    # 3. Notes: keep all by default; drop specified indices.
    notes_in = existing_form.get("notes") or []
    if drop_notes:
        merged["notes"] = [n for i, n in enumerate(notes_in) if i not in set(drop_notes)]
    # else: leave notes alone.

    # 4. highlighted, date_qualifier, open_access, epub_date: leave alone.
    return merged
