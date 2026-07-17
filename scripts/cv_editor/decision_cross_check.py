"""
V23-B Phase 1.5 (2026-05-26): cross-system decision silencing.

Two pure read-time predicates that allow a `keep_yaml` decision in
either the QC triage system (V23-B) or the PubMed-sync system (V19)
to silence the matching flag in BOTH surfaces. Plan:
`typst/plans/v23b-phase1.5-cross-system-silencing.md`.

Architecture is bilateral at N=2. If a third decision system ever
lands (e.g. Crossref-vs-YAML triage), refactor to a single
`silenced_by_any_other(finding, system, registry)` predicate driven
by a `CROSS_SYSTEM_REGISTRY` list. Premature at N=2.

Read-time only — no write-time mirror. Each sidecar remains its own
source of truth; this module only exposes predicates the existing
filter loops invoke. Apply-path auto-clear (when one system's
`apply` changes YAML) is wired separately in app.py.

5-field overlap is documented in `CROSS_FIELDS`. Outside-set fields
(`month`, `day` in System B; `pages` in System A) are silenced only
by their own system.
"""

from __future__ import annotations

from typing import Optional

from cv_editor.author_names import joined_author_names_normalized, norm_author_name

# 5-field overlap between System A (QC sweep `diff_entry`) and
# System B (PubMed sync `FLAG_FIELDS`). Both can flag mismatches on
# these; only these fields cross-silence.
CROSS_FIELDS = frozenset({"authors", "title", "journal", "doi", "year"})


def _normalize_for_compare(field: str, raw: Optional[str]) -> Optional[str]:
    """Re-canonicalize a stored snapshot for cross-system comparison.

    System A stores authors yaml_value as `"; "`-joined NORMALIZED
    names; System B stores raw names. The two snapshots will NOT
    compare equal for any entry with diacritics or `"Surname, Initials"`
    form unless we re-normalize. For non-authors fields, the stored
    snapshot is already a plain string (str(yv) at decision time);
    just pass through.
    """
    if raw is None:
        return None
    if field == "authors":
        # Re-tokenize a "; "-joined name string and normalize each
        # piece via the canonical author normalizer.
        parts = [p.strip() for p in str(raw).split(";")]
        parts = [norm_author_name(p) for p in parts if p]
        return "; ".join(parts)
    return str(raw)


def _live_authors_value(entry: dict) -> str:
    """Live YAML authors value, normalized for cross-system compare.
    Mirrors qc_publications.diff_entry's yaml_value format."""
    return joined_author_names_normalized(entry.get("authors") or [])


def live_value_for_compare(entry: dict, field: str) -> Optional[str]:
    """Compute the canonical form of an entry's CURRENT YAML value
    for `field`, suitable for cross-system comparison. Authors get
    the normalized join; other fields get `str(v)` or None."""
    if entry is None:
        return None
    if field == "authors":
        return _live_authors_value(entry)
    v = entry.get(field)
    if v is None:
        return None
    return str(v)


# ----- Index builders (called once per request) -----


def build_pmsync_overrides_index(pmsync_state) -> dict:
    """`{(pmid, field): AcceptedOverride}` for fast cross-check lookup.

    pmsync_state is `pubmed_sync.SidecarState`-shaped: a `.accepted_yaml_overrides`
    dict-of-dicts. Tolerates None / missing attribute (returns empty)."""
    out: dict = {}
    overrides = getattr(pmsync_state, "accepted_yaml_overrides", None) or {}
    for pmid, fields in overrides.items():
        if not isinstance(fields, dict):
            continue
        for field, ov in fields.items():
            if field not in CROSS_FIELDS:
                continue
            out[(str(pmid), field)] = ov
    return out


def build_qc_decisions_index(qc_sidecar, qc_decisions) -> dict:
    """`{(pmid, field): (finding_id, Decision)}` for fast cross-check
    lookup. Only includes MISMATCH/VARIANT keep_yaml decisions on
    CROSS_FIELDS where the finding carries a pmid. ID_ENRICHMENT is
    deliberately excluded (different semantics — "you don't have a
    value" vs "your value disagrees"). Tombstoned decisions are
    excluded automatically (Decisions.get returns None)."""
    out: dict = {}
    if qc_sidecar is None or qc_decisions is None:
        return out
    findings = (qc_sidecar or {}).get("findings", {}) or {}
    # Only MISMATCH + VARIANT cross-silence to System B.
    for section_key in ("mismatches", "variants"):
        for f in findings.get(section_key, []) or []:
            fid = f.get("id")
            if not fid:
                continue
            field = f.get("field")
            if field not in CROSS_FIELDS:
                continue
            pmid = f.get("pmid")
            if not pmid:
                continue
            dec = qc_decisions.get(fid)
            if dec is None:
                continue  # no decision (or tombstoned)
            if dec.decision != "keep_yaml":
                continue  # only keep_yaml cross-silences
            out[(str(pmid), field)] = (fid, dec)
    return out


# ----- Predicates -----


def silenced_by_pubmed_sync(
    finding: dict,
    pmsync_overrides_index: dict,
    entry: Optional[dict],
) -> Optional[dict]:
    """Return a badge-record dict if a PubMed-sync override silences
    this QC finding; otherwise None.

    Match: finding has a pmid, field is in CROSS_FIELDS, finding type
    is MISMATCH/VARIANT, the PubMed-sync override's yaml_value matches
    the entry's CURRENT YAML value (both canonicalized). If the live
    YAML has drifted from the override's snapshot, return None — the
    OTHER system's own re-surface logic will fire.
    """
    if not finding:
        return None
    ftype = finding.get("type") or finding.get("_finding_type")
    if ftype not in ("MISMATCH", "VARIANT"):
        return None
    field = finding.get("field")
    if field not in CROSS_FIELDS:
        return None
    pmid = finding.get("pmid")
    if not pmid:
        return None
    override = pmsync_overrides_index.get((str(pmid), field))
    if override is None:
        return None
    # Compare normalized stored snapshot to normalized current YAML.
    stored = _normalize_for_compare(field, getattr(override, "yaml_value", None))
    live = _normalize_for_compare(field, live_value_for_compare(entry, field)) if entry else None
    if stored is None or live is None or stored != live:
        return None
    return {
        "system": "pubmed_sync",
        "reason": getattr(override, "reason", "") or "",
        "decided_at": getattr(override, "accepted_at", "") or "",
        "source_value": getattr(override, "pubmed_value", "") or "",
        "finding_type": ftype,
    }


def silenced_by_qc(
    pmid: str,
    field: str,
    entry: Optional[dict],
    qc_decisions_index: dict,
) -> Optional[dict]:
    """Return a badge-record dict if a QC keep_yaml decision silences
    a PubMed-sync flag on (pmid, field); otherwise None.

    Symmetric to silenced_by_pubmed_sync. Match: field is in CROSS_FIELDS,
    QC decision exists (not tombstoned) and is keep_yaml on a
    MISMATCH/VARIANT, decision's yaml_value_at_decision matches entry's
    CURRENT YAML (both canonicalized)."""
    if not pmid or field not in CROSS_FIELDS:
        return None
    entry_in_index = qc_decisions_index.get((str(pmid), field))
    if entry_in_index is None:
        return None
    fid, dec = entry_in_index
    # Already gated by build_qc_decisions_index, but be defensive.
    if dec.decision != "keep_yaml":
        return None
    if dec.finding_type not in ("MISMATCH", "VARIANT"):
        return None
    stored = _normalize_for_compare(field, dec.yaml_value_at_decision)
    live = _normalize_for_compare(field, live_value_for_compare(entry, field)) if entry else None
    if stored is None or live is None or stored != live:
        return None
    return {
        "system": "qc_triage",
        "reason": dec.reason or "",
        "decided_at": dec.decided_at or "",
        "source_value": dec.canonical_value_at_decision or "",
        "finding_type": dec.finding_type,
        "finding_id": fid,
    }
