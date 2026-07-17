"""Author-flag spec (V20, 2026-05-18 — B1 refactor).

V18-A added `group_authorship` as the third per-author boolean
alongside `co_first` / `co_senior`. Three flags with near-identical
shape (glyph + footnote sentence + UI label + serialization rule)
dispatched in 11+ places — `author_names`, `preprint`, `csv_export`,
`bibtex_parse`, `citation_parse`, `enrichment`, `notes_helpers`,
`templates/bespoke/render.typ`, `entry_edit.html`, `entry_view.html`.
The V18-A-D review caught a cross-reviewer-consensus HIGH
(entry_view.html missing the ◊ badge) — exactly the failure mode this
extraction prevents.

This module is the single source of truth for the Python side. The
Typst renderer keeps its own mirror tuple at module scope in
`templates/bespoke/render.typ` (anchored by `// AUTHOR_FLAGS_BEGIN/END` comments);
`tests/test_author_flags.py:test_typst_mirror_lists_keys_in_same_order`
asserts the orderings match.

Design notes:

* `is_lead_eligible` discriminates whether a flag promotes its bearer
  to a lead-author position for OA-decision QC purposes. Today
  `co_first` and `co_senior` are eligible (they auto-promote to
  co_first / co_senior in `notes_helpers.self_author_position`);
  `group_authorship` is NOT (an author marked group_authorship in
  middle position stays 'middle', so `needs_contribution_note`
  returns False). V18-A baked this in via comments; B1 codifies it
  as a property of the flag.
* Falsy flags MUST NOT serialize to YAML — `form_to_yaml_author`
  returns a plain string when all flags are False. This keeps the
  YAML clean. Asymmetric: `author_to_form` accepts both shapes.
* BibTeX export silently drops all flags. `yaml_to_bibtex.py` reads
  only `name`. Don't try to encode flags in BibTeX — bibstyles
  wouldn't know what to do with the glyphs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorFlag:
    """Spec for one per-author boolean."""

    key: str  # YAML / form field name
    label: str  # human-readable UI label
    glyph: str  # rendered superscript marker
    footnote: str  # auto-appended footnote sentence (period-terminated)
    is_lead_eligible: bool  # if True, set→promotes to lead position for OA QC


# Order is LOAD-BEARING — the Typst-side mirror walks this order
# positionally for footnote composition. When adding a 4th flag,
# update templates/bespoke/render.typ's `// AUTHOR_FLAGS_BEGIN`...`// AUTHOR_FLAGS_END`
# block and tests/test_author_flags.py will assert they match.
AUTHOR_FLAGS: tuple[AuthorFlag, ...] = (
    AuthorFlag(
        key="co_first",
        label="co-first",
        glyph="†",
        footnote="First authors contributed equally.",
        is_lead_eligible=True,
    ),
    AuthorFlag(
        key="co_senior",
        label="co-senior",
        glyph="‡",
        footnote="Senior authors contributed equally.",
        is_lead_eligible=True,
    ),
    AuthorFlag(
        key="group_authorship",
        label="group author",
        glyph="◊",
        footnote="Group authorship.",
        is_lead_eligible=False,
    ),
)

ALL_FLAG_KEYS: tuple[str, ...] = tuple(f.key for f in AUTHOR_FLAGS)
LEAD_FLAG_KEYS: tuple[str, ...] = tuple(f.key for f in AUTHOR_FLAGS if f.is_lead_eligible)


def flag_defaults_dict() -> dict[str, bool]:
    """Default per-flag values for a freshly-constructed form-shape author.
    All keys present, all False — matches the pre-extraction literal
    {"co_first": False, "co_senior": False, "group_authorship": False}.
    """
    return {f.key: False for f in AUTHOR_FLAGS}
