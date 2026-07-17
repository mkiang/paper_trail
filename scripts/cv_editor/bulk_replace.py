"""Structure-aware global search/replace across CV section data (M5 5c-ii).

v1 scope (USER-CONFIRMED 2026-05-30): LITERAL substring match/replace in
free-text fields (`text` / `textarea`) ONLY. Structural / ID / date / numeric
fields are HARD-REFUSED — a replace inside a PMID/DOI/year/volume breaks the ID,
the BibTeX citekey, or the quoted-numeric YAML invariant. `meta` is excluded
wholesale (self_bold auto-bolds CV-wide; single_record isn't in get_container).
The PUBLICATIONS author list (`author_list` type) + `grant_amount` are DEFERRED to
v2 (author_rename already covers cross-entry publication-author renames; grant_amount
carries the `\\$` escape). NOTE: the presentations `authors` field is plain `text`
(a comma-joined string), so it IS in scope — a replace touching a self_bold name
there will change how the name renders. The preview makes that visible.

Built FRESH — NOT a fork of `author_rename` (which is single-file + hardcoded to
one structure + has an author-marker-preservation contract a string replace would
break). This walks EVERY section via `schemas.get(key)["structure"]` +
`sections.flatten`, and is field-TYPE driven (NOT the lossy `/search`
`_walk_scalars`, which would silently rewrite IDs/URLs). Flask-free + unit-tested.

The route does the ask -> preview -> apply staging, carries per-file `mtime_ns`,
runs an all-or-nothing preflight, and writes each touched file via
`yaml_io.write_with_backup` (the SACRED atomic pipeline). This module only
finds + transforms an in-memory ruamel tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cv_editor import schemas
from cv_editor.sections import flatten

# Only these field types are replace-eligible in v1.
REPLACEABLE_FIELD_TYPES = frozenset({"text", "textarea"})

# Text/textarea fields that are nonetheless STRUCTURED — IDs, dates, page ranges,
# epub dates, grant numbers. A literal replace inside one breaks the ID, the date
# parse, the BibTeX citekey (doi suffix), or the quoted-numeric invariant. These
# are HARD-REFUSED even though their schema type is text. (doi + pmcid ALSO carry a
# `regex`, so the regex-refusal below catches them too — belt and suspenders.)
_REFUSE_TEXT_FIELDS = frozenset({"doi", "pmcid", "epub_date", "pages", "date", "project"})

# Fields (by priority) used to label an entry in the preview.
_LABEL_FIELDS = ("title", "award", "course", "degree", "role", "name", "agency", "venue")


def searchable_sections() -> list[str]:
    """Every section EXCEPT meta (excluded wholesale — see module docstring)."""
    return [k for k in schemas.all_sections() if k != "meta"]


def replaceable_fields(section_key: str) -> list[str]:
    """The v1 replace allow-list for a section: text/textarea fields that are NOT
    structured (no `regex` constraint, not in the structured-field denylist)."""
    if section_key == "meta":
        return []
    out = []
    for f in schemas.get(section_key).get("fields", []):
        if f["type"] not in REPLACEABLE_FIELD_TYPES:
            continue
        if f["name"] in _REFUSE_TEXT_FIELDS:
            continue
        if f.get("regex"):  # validated/structured text (doi, pmcid)
            continue
        out.append(f["name"])
    return out


@dataclass(frozen=True)
class Hit:
    """One replace candidate: a single (entry, field) whose value contains the
    needle >=1 time. `before`/`after` are the FULL field value (all occurrences
    replaced); `count` is the occurrence count; `markup_unbalanced` flags a
    replacement that would orphan a `*`/`_` delimiter or introduce a bare `$`."""

    section: str
    global_idx: int
    field: str
    entry_label: str
    before: str
    after: str
    count: int
    markup_unbalanced: bool

    @property
    def key(self) -> str:
        """Stable id for a preview checkbox / apply selection."""
        return f"{self.section}|{self.global_idx}|{self.field}"


def _entry_label(entry, global_idx: int) -> str:
    if isinstance(entry, dict):
        for f in _LABEL_FIELDS:
            v = entry.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip()[:80]
    return f"entry #{global_idx + 1}"


def _count(hay: str, needle: str, case_sensitive: bool) -> int:
    if not needle:
        return 0
    if case_sensitive:
        return hay.count(needle)
    return hay.lower().count(needle.lower())


def replace_in(hay: str, needle: str, repl: str, case_sensitive: bool) -> str:
    """Literal (non-regex) replace of every `needle` with `repl`. Case-insensitive
    mode matches without regard to case but inserts `repl` verbatim; `repl` is NOT
    interpreted as a regex template (no backref expansion)."""
    if not needle:
        return hay
    if case_sensitive:
        return hay.replace(needle, repl)
    return re.sub(re.escape(needle), lambda _m: repl, hay, flags=re.IGNORECASE)


def _unescaped_count(s: str, ch: str) -> int:
    """Count occurrences of `ch` not immediately preceded by a backslash."""
    return len(re.findall(r"(?<!\\)" + re.escape(ch), s))


def markup_unbalanced(before: str, after: str) -> bool:
    """True if the replacement likely breaks Typst markup: it flips the odd/even
    parity of unescaped `*` or `_` (orphaning a delimiter), or CHANGES the count of
    unescaped `$` at all (adding one opens math mode; removing one from a balanced
    pair orphans the other — both break the build). Heuristic, no full Typst parser;
    a false positive only defaults a hit to unchecked, so erring conservative is safe."""
    for ch in ("*", "_"):
        if _unescaped_count(before, ch) % 2 != _unescaped_count(after, ch) % 2:
            return True
    if _unescaped_count(after, "$") != _unescaped_count(before, "$"):
        return True
    return False


def collect_in_section(
    data, section_key: str, needle: str, replacement: str, case_sensitive: bool = True
) -> list[Hit]:
    """All Hits for one section's in-memory tree. Empty for meta / empty needle."""
    if section_key == "meta" or not needle:
        return []
    sch = schemas.get(section_key)
    rfields = replaceable_fields(section_key)
    hits: list[Hit] = []
    for rec in flatten(data, sch["structure"]):
        entry = rec["entry"]
        if not isinstance(entry, dict):
            continue
        label = _entry_label(entry, rec["global_idx"])
        for fname in rfields:
            v = entry.get(fname)
            if not isinstance(v, str):
                continue
            cnt = _count(v, needle, case_sensitive)
            if cnt == 0:
                continue
            after = replace_in(v, needle, replacement, case_sensitive)
            if after == v:
                continue  # replacement == needle, or case-folded no-op
            hits.append(
                Hit(
                    section_key,
                    rec["global_idx"],
                    fname,
                    label,
                    v,
                    after,
                    cnt,
                    markup_unbalanced(v, after),
                )
            )
    return hits


def apply_in_section(
    data,
    section_key: str,
    selected_keys: set[tuple[int, str]],
    needle: str,
    replacement: str,
    case_sensitive: bool = True,
) -> int:
    """Replace in the SELECTED (global_idx, field) cells of one section's tree.
    Mutates in place (CommentedMap keeps comments + key order). Returns the count
    of fields actually changed. Re-derives from the passed tree — callers MUST
    pass freshly-loaded data guarded by an mtime check (no trusting stale offsets)."""
    if section_key == "meta" or not needle or not selected_keys:
        return 0
    sch = schemas.get(section_key)
    rfields = set(replaceable_fields(section_key))
    n = 0
    for rec in flatten(data, sch["structure"]):
        entry = rec["entry"]
        if not isinstance(entry, dict):
            continue
        for fname in rfields:
            if (rec["global_idx"], fname) not in selected_keys:
                continue
            v = entry.get(fname)
            if not isinstance(v, str):
                continue
            new = replace_in(v, needle, replacement, case_sensitive)
            if new != v:
                entry[fname] = new
                n += 1
    return n
