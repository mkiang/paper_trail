"""Walk + substitute helpers for tracker URLs in publications.yml.

T3.1: extracted from three near-identical implementations that lived in
`app.py` (two helpers) and `altmetric_client.py` (one). Single source of
truth for "what counts as a tracker outlet" and "how do we substitute
a URL across the data tree."

All iteration goes through `sections.flatten()` so `pub_global_idx`
matches what the editor's section routes use everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from cv_editor import schemas, sections, url_helpers


@dataclass
class OutletRef:
    """One tracker-bearing outlet, with enough metadata to route + display."""

    pub_global_idx: int  # matches sections.flatten() global_idx
    pub_title: str
    pub_date: str  # short "YYYY · MM" display
    note_idx: int  # index within entry["notes"]
    outlet_idx: int  # index within note["outlets"]
    outlet_name: str
    url: str  # the tracker URL itself

    def as_row(self) -> dict:
        """Dict view for app/template consumers that expect a row mapping."""
        return {
            "pub_idx": self.pub_global_idx,
            "pub_title": self.pub_title,
            "pub_date": self.pub_date,
            "note_idx": self.note_idx,
            "outlet_idx": self.outlet_idx,
            "outlet_name": self.outlet_name,
            "url": self.url,
        }


def _pub_pretty_date(entry) -> str:
    """Short YYYY · MM display. Same logic that lived inline in app.py."""
    y = entry.get("year")
    m = entry.get("month")
    if not y:
        return ""
    if m:
        try:
            return f"{y} · {int(m):02d}"
        except (TypeError, ValueError):
            return f"{y} · {m}"
    return str(y)


def iter_tracker_outlets(data) -> Iterator[OutletRef]:
    """Yield every media-note outlet whose URL is in TRACKER_HOSTS.

    Uses `sections.flatten()` with the publications structure to assign
    pub_global_idx matching the editor's section URL routes.
    """
    pubs_sch = schemas.get("publications")
    for row in sections.flatten(data, pubs_sch["structure"]):
        entry = row["entry"]
        global_idx = row["global_idx"]
        pub_title = entry.get("title") or ""
        pub_date = _pub_pretty_date(entry)
        for note_idx, note in enumerate(entry.get("notes") or []):
            if not isinstance(note, dict) or note.get("type") != "media":
                continue
            for outlet_idx, outlet in enumerate(note.get("outlets") or []):
                if not isinstance(outlet, dict):
                    continue
                u = (outlet.get("url") or "").strip()
                if u and url_helpers.is_tracker_url(u):
                    yield OutletRef(
                        pub_global_idx=global_idx,
                        pub_title=pub_title,
                        pub_date=pub_date,
                        note_idx=note_idx,
                        outlet_idx=outlet_idx,
                        outlet_name=outlet.get("name") or "",
                        url=u,
                    )


def iter_entry_tracker_outlets(entry) -> Iterator[tuple[int, int, str, str]]:
    """Yield (note_idx, outlet_idx, outlet_name, url) for every tracker
    outlet on a single publication entry. Mirrors `iter_tracker_outlets`
    scoped to one entry — keeps the per-entry resolve route from
    open-coding a 4th nested-walk (R2-M5 dedup, 2026-05-17)."""
    for note_idx, note in enumerate(entry.get("notes") or []):
        if not isinstance(note, dict) or note.get("type") != "media":
            continue
        for outlet_idx, outlet in enumerate(note.get("outlets") or []):
            if not isinstance(outlet, dict):
                continue
            u = (outlet.get("url") or "").strip()
            if u and url_helpers.is_tracker_url(u):
                yield (note_idx, outlet_idx, outlet.get("name") or "", u)


def substitute_tracker_urls_on_entry(entry, substitutions) -> int:
    """In-place: walk one publication entry's media-note outlets;
    when an outlet's URL matches a key in `substitutions`, replace
    with the resolved value. Returns count of replacements.

    Input contract: `entry` is a publication dict (has `notes`), NOT
    the `_resolve_idx` wrapper dict (which has `notes` inside `entry`).
    Pass `rec["entry"]`, not `rec`.
    """
    if not substitutions:
        return 0
    n = 0
    for note in entry.get("notes") or []:
        if not isinstance(note, dict) or note.get("type") != "media":
            continue
        for outlet in note.get("outlets") or []:
            if not isinstance(outlet, dict):
                continue
            u = (outlet.get("url") or "").strip()
            if u in substitutions:
                outlet["url"] = substitutions[u]
                n += 1
    return n


def substitute_tracker_urls_in_publications(data, substitutions) -> int:
    """In-place sweep across the entire publications data tree. Returns
    total replacements across all entries. Uses `sections.flatten()`."""
    if not substitutions:
        return 0
    pubs_sch = schemas.get("publications")
    n = 0
    for row in sections.flatten(data, pubs_sch["structure"]):
        n += substitute_tracker_urls_on_entry(row["entry"], substitutions)
    return n


def count_unresolved_trackers(data, cache) -> dict:
    """Return tracker-count summary used by the index banner.

    Args:
        data: publications data tree (from `_load_section("publications")`).
        cache: TrackerCache instance.

    Returns dict with keys total_trackers, pubs_with_trackers, by_status.
    """
    seen_pubs: set[int] = set()
    by_status = {
        "failed_network": 0,
        "failed_rate_limit": 0,
        "failed_no_redirect": 0,
        "unknown": 0,
    }
    total = 0
    for ref in iter_tracker_outlets(data):
        cached = cache.get(ref.url)
        if cached and cached.status == "resolved":
            continue
        total += 1
        seen_pubs.add(ref.pub_global_idx)
        if cached is None:
            by_status["unknown"] += 1
        else:
            key = cached.status if cached.status in by_status else "unknown"
            by_status[key] += 1
    return {
        "total_trackers": total,
        "pubs_with_trackers": len(seen_pubs),
        "by_status": by_status,
    }


def entry_unresolved_tracker_count(entry, cache) -> int:
    """Count unresolved trackers on a single entry. Used by entry_view banner.

    V13-V19-D R1-M6 (2026-05-18): delegate to iter_entry_tracker_outlets
    so the walk-and-filter logic lives in one place. Future changes to
    what counts as a tracker (new domains, new outlet shapes) only need
    to land in iter_entry_tracker_outlets.
    """
    count = 0
    for _ni, _oi, _name, u in iter_entry_tracker_outlets(entry):
        cached = cache.get(u)
        if cached is None or cached.status != "resolved":
            count += 1
    return count
