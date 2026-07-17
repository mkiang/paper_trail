"""
Structure-aware navigation for the CV editor.

Each section's YAML follows one of four shapes:

    flat_list                — top-level list of entry dicts.
                               (research_support, mentees, honors)
    list_of_subsections      — top-level list of {subsection, entries}.
                               (publications, presentations, service)
    clusters                 — top-level list of {institution, city?, entries}.
                               (teaching, education)
    subsections_of_clusters  — top-level list of {subsection, clusters: [...]}.
                               (appointments)

`flatten()` yields one record per leaf entry with the same shape regardless
of structure, so list views, search, and CRUD can stay structure-agnostic.

Each record is:

    {
        "global_idx": int,       # 0-based index across the whole section
        "loc": tuple[int, ...],  # locator into the data tree (see get_container)
        "entry": CommentedMap,   # the leaf entry itself
        "ctx": {                 # human-readable context fields
            "subsection": str,   # for list_of_subsections / subsections_of_clusters
            "institution": str,  # for clusters / subsections_of_clusters
            "city": str,         # for clusters / subsections_of_clusters
        },
    }

`get_container(data, structure, loc)` returns `(seq, idx)` so callers can
mutate by `seq.pop(idx)` / `seq.insert(j, entry)` regardless of structure.

`insert_entry(data, structure, target, entry)` puts a new entry at the
top of the target subsection / cluster, creating a new cluster on the fly
for cluster-based sections when the institution doesn't already exist.

`move_entry(data, structure, loc, target)` is pop-then-insert.

Subsection / cluster ordering is preserved as-is in YAML — the renderer
auto-sorts entries inside each container, so insert-at-top is the right
default for "newest first" semantics.
"""

from __future__ import annotations

from ruamel.yaml.comments import CommentedMap, CommentedSeq

STRUCTURES = {
    "flat_list",
    "list_of_subsections",
    "clusters",
    "subsections_of_clusters",
    "single_record",
}


def flatten(data, structure: str):
    """Yield {global_idx, loc, entry, ctx} per leaf entry."""
    if structure == "single_record":
        if data is not None:
            yield {"global_idx": 0, "loc": (), "entry": data, "ctx": {}}
        return

    if data is None:
        return

    if structure == "flat_list":
        for i, e in enumerate(data):
            yield {"global_idx": i, "loc": (i,), "entry": e, "ctx": {}}
        return

    if structure == "list_of_subsections":
        idx = 0
        for s_i, sec in enumerate(data):
            sub = str(sec.get("subsection", ""))
            for e_i, e in enumerate(sec.get("entries") or []):
                yield {
                    "global_idx": idx,
                    "loc": (s_i, e_i),
                    "entry": e,
                    "ctx": {"subsection": sub},
                }
                idx += 1
        return

    if structure == "clusters":
        idx = 0
        for c_i, cl in enumerate(data):
            inst = str(cl.get("institution", ""))
            city = str(cl.get("city", ""))
            for e_i, e in enumerate(cl.get("entries") or []):
                yield {
                    "global_idx": idx,
                    "loc": (c_i, e_i),
                    "entry": e,
                    "ctx": {"institution": inst, "city": city},
                }
                idx += 1
        return

    if structure == "subsections_of_clusters":
        idx = 0
        for s_i, sec in enumerate(data):
            sub = str(sec.get("subsection", ""))
            for c_i, cl in enumerate(sec.get("clusters") or []):
                inst = str(cl.get("institution", ""))
                city = str(cl.get("city", ""))
                for e_i, e in enumerate(cl.get("entries") or []):
                    yield {
                        "global_idx": idx,
                        "loc": (s_i, c_i, e_i),
                        "entry": e,
                        "ctx": {"subsection": sub, "institution": inst, "city": city},
                    }
                    idx += 1
        return

    raise ValueError(f"unknown structure: {structure}")


def locate(data, structure: str, global_idx: int):
    """Return the flat record matching global_idx, or None."""
    for r in flatten(data, structure):
        if r["global_idx"] == global_idx:
            return r
    return None


def get_container(data, structure: str, loc: tuple):
    """Return (entries_seq, entry_index) for the locator."""
    if structure == "flat_list":
        return data, loc[0]
    if structure == "list_of_subsections":
        return data[loc[0]]["entries"], loc[1]
    if structure == "clusters":
        return data[loc[0]]["entries"], loc[1]
    if structure == "subsections_of_clusters":
        return data[loc[0]]["clusters"][loc[1]]["entries"], loc[2]
    raise ValueError(f"unknown structure: {structure}")


def find_subsection_idx(data, name: str) -> int:
    for i, sec in enumerate(data or []):
        if str(sec.get("subsection")) == name:
            return i
    return -1


def find_cluster_idx_in_clusters(data, institution: str) -> int:
    for i, cl in enumerate(data or []):
        if str(cl.get("institution")) == institution:
            return i
    return -1


def find_cluster_idx_in_subsection(sec, institution: str) -> int:
    for i, cl in enumerate(sec.get("clusters") or []):
        if str(cl.get("institution")) == institution:
            return i
    return -1


def insert_entry(data, structure: str, target: dict, entry) -> tuple:
    """
    Insert entry at the top of the target group. Returns the locator of the
    inserted entry.

    `target` keys depend on structure:
      flat_list                 -> {} (target ignored)
      list_of_subsections       -> {subsection: ...}
      clusters                  -> {institution: ..., city?: ...}
      subsections_of_clusters   -> {subsection: ..., institution: ..., city?: ...}

    For cluster-based structures, missing clusters are created on the fly.
    For list_of_subsections / subsections_of_clusters, a missing subsection
    group is also created on the fly (appended), so filing the first entry
    into a schema-defined-but-empty subsection works. The CALLER is responsible
    for restricting `subsection` to the schema's `subsections` list (entry_save
    does this) — schema is the source of truth, not the current data.
    """
    if structure == "flat_list":
        data.insert(0, entry)
        return (0,)

    if structure == "list_of_subsections":
        sub = target.get("subsection")
        s_i = find_subsection_idx(data, sub)
        if s_i < 0:
            data.append(_new_subsection_group(sub, with_clusters=False))
            s_i = len(data) - 1
        data[s_i]["entries"].insert(0, entry)
        return (s_i, 0)

    if structure == "clusters":
        inst = target.get("institution") or ""
        c_i = find_cluster_idx_in_clusters(data, inst)
        if c_i < 0:
            new_cl = _new_cluster(inst, target.get("city"))
            new_cl["entries"].insert(0, entry)
            data.append(new_cl)
            return (len(data) - 1, 0)
        data[c_i]["entries"].insert(0, entry)
        return (c_i, 0)

    if structure == "subsections_of_clusters":
        sub = target.get("subsection")
        s_i = find_subsection_idx(data, sub)
        if s_i < 0:
            data.append(_new_subsection_group(sub, with_clusters=True))
            s_i = len(data) - 1
        inst = target.get("institution") or ""
        c_i = find_cluster_idx_in_subsection(data[s_i], inst)
        if c_i < 0:
            new_cl = _new_cluster(inst, target.get("city"))
            new_cl["entries"].insert(0, entry)
            data[s_i].setdefault("clusters", CommentedSeq()).append(new_cl)
            return (s_i, len(data[s_i]["clusters"]) - 1, 0)
        data[s_i]["clusters"][c_i]["entries"].insert(0, entry)
        return (s_i, c_i, 0)

    raise ValueError(f"unknown structure: {structure}")


def _new_cluster(institution: str, city: str | None):
    cm = CommentedMap()
    cm["institution"] = institution
    if city:
        cm["city"] = city
    cm["entries"] = CommentedSeq()
    return cm


def _new_subsection_group(subsection: str, *, with_clusters: bool):
    """A fresh subsection group for `subsection`. `list_of_subsections` groups
    hold their items under `entries`; `subsections_of_clusters` groups hold
    `clusters` (each cluster has its own `entries`). Appended by insert_entry
    when the first entry is filed into a not-yet-present (but schema-valid)
    subsection; the user can reorder it afterward (subsection order is
    author-controlled in the data)."""
    grp = CommentedMap()
    grp["subsection"] = subsection
    grp["clusters" if with_clusters else "entries"] = CommentedSeq()
    return grp


def delete_entry(data, structure: str, loc: tuple) -> None:
    seq, idx = get_container(data, structure, loc)
    seq.pop(idx)


def move_entry(data, structure: str, loc: tuple, target: dict) -> tuple:
    """Pop the entry at loc and insert it at the top of target. Returns new loc."""
    seq, idx = get_container(data, structure, loc)
    entry = seq.pop(idx)
    return insert_entry(data, structure, target, entry)


def update_in_place(data, structure: str, loc: tuple, new_entry) -> None:
    """Replace the entry at loc."""
    seq, idx = get_container(data, structure, loc)
    seq[idx] = new_entry


# ---- target choices for "manual add" forms ----


def list_targets(data, structure: str) -> list[dict]:
    """Return distinct target groups available for inserting a new entry.

    Each result is a dict suitable for passing to insert_entry as `target`.
    The UI uses this to populate a dropdown. For `flat_list`, returns [{}]
    (no choice).
    """
    if structure == "flat_list" or structure == "single_record":
        return [{}]

    if structure == "list_of_subsections":
        return [{"subsection": str(s.get("subsection", ""))} for s in (data or [])]

    if structure == "clusters":
        return [
            {"institution": str(c.get("institution", "")), "city": str(c.get("city", ""))}
            for c in (data or [])
        ]

    if structure == "subsections_of_clusters":
        out = []
        for sec in data or []:
            sub = str(sec.get("subsection", ""))
            for cl in sec.get("clusters") or []:
                out.append(
                    {
                        "subsection": sub,
                        "institution": str(cl.get("institution", "")),
                        "city": str(cl.get("city", "")),
                    }
                )
        return out

    return []
