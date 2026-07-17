"""
Cross-entry author rename.

Walks `data/publications.yml`, finds every author whose canonical name
matches `old_name` (string-form or dict-form
`{name, co_first?, co_senior?, group_authorship?}`), and replaces the
name with `new_name`. Dict-form flags are preserved.

Returns a list of `{global_idx, subsection, title, before_authors,
after_authors}` describing the affected entries. The caller decides
whether to apply (writes via the standard atomic-tmp path) or just preview.

Used by the editor's `/publications/rename-author` route.
"""

from __future__ import annotations

from ruamel.yaml.comments import CommentedMap

from cv_editor import sections


def _author_name(a) -> str:
    if isinstance(a, dict):
        return str(a.get("name", "")).strip()
    return str(a).strip()


def collect_unique_author_names(data) -> list[str]:
    """Return sorted distinct author names across the section."""
    seen = set()
    for rec in sections.flatten(data, "list_of_subsections"):
        for a in rec["entry"].get("authors") or []:
            n = _author_name(a)
            if n:
                seen.add(n)
    return sorted(seen)


def find_affected(data, old_name: str) -> list[dict]:
    """List entries that would be touched by a rename of `old_name`."""
    out = []
    for rec in sections.flatten(data, "list_of_subsections"):
        authors = rec["entry"].get("authors") or []
        names = [_author_name(a) for a in authors]
        if old_name in names:
            out.append(
                {
                    "global_idx": rec["global_idx"],
                    "subsection": rec["ctx"].get("subsection", ""),
                    "title": str(rec["entry"].get("title", "(no title)"))[:120],
                    "before_authors": list(names),
                    "loc": rec["loc"],
                }
            )
    return out


def apply_rename(data, old_name: str, new_name: str) -> int:
    """Rewrite every matching author name in place. Returns affected count.

    Preserves dict-form flags (co_first / co_senior / group_authorship).
    String-form authors stay string-form. New name is the only mutation.
    """
    n_changed = 0
    for rec in sections.flatten(data, "list_of_subsections"):
        authors = rec["entry"].get("authors") or []
        for i, a in enumerate(authors):
            if _author_name(a) != old_name:
                continue
            if isinstance(a, CommentedMap) or isinstance(a, dict):
                a["name"] = new_name
            else:
                authors[i] = new_name
            n_changed += 1
    return n_changed
