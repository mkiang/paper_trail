"""V23-B Phase 3: QC sidecar loader + render helpers (2026-05-25).

Minimal Flask-side adapter over `qc/report.json` (emitted by Phase 0).
Phase 3 only needs read-only access; Phase 1 will add the decisions
sidecar + `effective_findings()` predicate that subtracts overrides.

Why this module exists separately from `qc_publications`:
- `qc_publications.py` is a CLI script; importing it in the Flask app
  triggers its module-level path resolution. Better to keep the load
  surface here and let the CLI side stay agnostic of Flask.
- Mirror the V19 pattern where `pubmed_client.py` holds the read
  surface and `app.py` consumes via small wrappers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from cv_editor.versioned_json import load_versioned

SIDECAR_SCHEMA_VERSION = 1


def load_sidecar(path: Path, *, silent: bool = False) -> dict | None:
    """Load the QC sidecar at `path`. Returns the full dict on success
    or None when the sidecar is missing / corrupt / version-mismatched.

    Templates / routes that call this MUST be prepared for None (no
    sweep has run yet OR the sidecar got clobbered). The Phase 3 UI
    surfaces a "Run QC sweep" affordance in that case.
    """
    return load_versioned(
        path,
        expected_version=SIDECAR_SCHEMA_VERSION,
        component_name="qc_sync",
        silent=silent,
    )


def summary_totals(sidecar: dict | None) -> dict:
    """Extract the `summary.totals` block + total_findings.

    Returns {"total_findings": 0, "totals": {}} for a missing sidecar
    so banner / count templates can be written without None checks.
    """
    if not sidecar:
        return {"total_findings": 0, "totals": {}}
    summary = sidecar.get("summary") or {}
    return {
        "total_findings": int(summary.get("total_findings") or 0),
        "totals": dict(summary.get("totals") or {}),
    }


# Render order on /qc/triage: most actionable first, edit-manually-only
# last (Phase 3 surface), variant clusters and missing_ids at the end
# (Phases 2 + 4 deferred; placeholder rows).
RENDER_ORDER = (
    "mismatches",  # Phase 1 (placeholder this phase)
    "variants",  # Phase 1 (placeholder)
    "id_enrichments",  # Phase 1 (placeholder)
    "pmid_mismatches",  # Phase 3 — edit-manually
    "self_absent",  # Phase 3 — edit-manually
    "author_name_variants",  # Phase 2 (deferred)
    "journal_name_variants",  # Phase 2 (deferred)
    "missing_ids",  # Phase 4 (deferred)
)


# Per-type display metadata for the triage template. The "phase" field
# tells the template whether to render the row with active triage UI
# (Phase 3 today: pmid_mismatches + self_absent) or as a placeholder
# stub awaiting later phases.
FINDING_TYPE_DISPLAY = {
    "mismatches": {
        "title": "External mismatches",
        "blurb": "YAML disagrees with PubMed/Crossref. Phase 1 will add per-row triage.",
        "phase": 1,
    },
    "variants": {
        "title": "External variants",
        "blurb": "Benign formatting diffs (e.g., trailing-period in pages). Phase 1 will add per-row triage.",
        "phase": 1,
    },
    "id_enrichments": {
        "title": "ID enrichment suggestions",
        "blurb": "PubMed/Crossref offers an ID the YAML is missing. Phase 1 will add per-row triage.",
        "phase": 1,
    },
    "pmid_mismatches": {
        "title": "PMIDs that PubMed could not resolve",
        "blurb": "Cited PMID returns no PubMed record. Apply unavailable; edit manually.",
        "phase": 3,  # active in Phase 3
    },
    "self_absent": {
        "title": "Entries where the self-author is not in the author list",
        "blurb": "Author list does not include the self-author in any form. Verify manually.",
        "phase": 3,  # active in Phase 3
    },
    "author_name_variants": {
        "title": "Author-name variants",
        "blurb": "Same author spelled multiple ways across entries. Phase 2 (deferred) will add cross-entry rename.",
        "phase": 2,
    },
    "journal_name_variants": {
        "title": "Journal-name variants",
        "blurb": "Same journal spelled multiple ways across entries. Phase 2 (deferred) will add cross-entry rename.",
        "phase": 2,
    },
    "missing_ids": {
        "title": "Entries missing both PMID and DOI",
        "blurb": "No seed ID available. Phase 4 (deferred) will add title-based PubMed search.",
        "phase": 4,
    },
}


def iter_finding_sections(sidecar: dict | None) -> Iterable[dict]:
    """Yield one dict per finding type in RENDER_ORDER. Each dict carries
    the key, display metadata, and the list of findings (possibly empty).

    Templates iterate over this so the placeholder structure for deferred
    phases stays consistent with what Phase 1/2/4 will populate.
    """
    findings = (sidecar or {}).get("findings") or {}
    for key in RENDER_ORDER:
        display = FINDING_TYPE_DISPLAY[key]
        rows = list(findings.get(key) or [])
        yield {
            "key": key,
            "title": display["title"],
            "blurb": display["blurb"],
            "phase": display["phase"],
            "rows": rows,
            "count": len(rows),
            "is_active_in_phase_3": display["phase"] == 3,
        }


def entry_edit_anchor(finding: dict, *, field: str | None = None) -> str:
    """Build the URL fragment for jumping to entry_edit from a finding.

    For Phase 3 finding types (pmid_mismatches, self_absent):
      - pmid_mismatches: focus the `pmid` field.
      - self_absent: focus the `authors` field.

    For Phase 1 finding types (MISMATCH/VARIANT/ID_ENRICHMENT), the
    finding carries its `field` directly.

    Returns the fragment WITHOUT the leading '#'; the template prepends it.
    Caller composes the full URL: `/publications/<subsection>/<idx>/edit#<fragment>`.
    """
    if field:
        return f"field-{field}"
    ftype = finding.get("type")
    if ftype == "PMID_MISMATCH":
        return "field-pmid"
    if ftype == "SELF_ABSENT":
        return "field-authors"
    # Phase 1: MISMATCH / VARIANT / ID_ENRICHMENT carry the field directly.
    if ftype in ("MISMATCH", "VARIANT"):
        f = finding.get("field")
        if f:
            return f"field-{f}"
    if ftype == "ID_ENRICHMENT":
        f = finding.get("suggested_field")
        if f:
            return f"field-{f}"
    return ""


# ----- V23-B Phase 1: effective findings (per-type re-surface predicates) -----
#
# The triage page and banners must agree on what's "pending" after
# decisions filter the raw sidecar. V13-V19-D R2-H1 (the "banner truth
# = triage truth" invariant) — index banner + entry_view banner + the
# triage page itself ALL use this single helper.


def _entity_yaml_lookup(current_yaml_by_global_idx: dict, finding: dict) -> dict | None:
    """Resolve a finding to its current YAML entry dict.

    The triage page passes `current_yaml_by_global_idx` (dict keyed by
    global_idx) so we can compare yaml_value_at_decision against the
    live YAML value. Returns None if the entry was deleted between
    sweep and decision.
    """
    gidx = finding.get("global_idx")
    if gidx is None:
        return None
    return current_yaml_by_global_idx.get(gidx)


def _live_yaml_value(entry: dict | None, field: str) -> str | None:
    """Read the live YAML value for the finding's field. Returns None
    if the entry is gone OR the field is unset.

    V23-B Phase 1.5 (2026-05-26): author normalizer moved to
    cv_editor.author_names.joined_author_names_normalized so QC + cross-
    check share one canonicalizer (no more brittle CLI-script import)."""
    if entry is None:
        return None
    if field == "authors":
        from cv_editor.author_names import joined_author_names_normalized

        return joined_author_names_normalized(entry.get("authors") or [])
    v = entry.get(field)
    if v is None:
        return None
    return str(v)


def effective_findings(
    sidecar: dict | None,
    decisions,  # qc_decisions.Decisions
    *,
    current_yaml_by_global_idx: dict | None = None,
    pubmed_sync_state=None,
) -> dict:
    """Return {finding_type: [effective_rows]} after filtering by
    decisions sidecar + re-surface predicates.

    Used by the triage page (rendering), index banner (counting), and
    entry_view banner (per-entry counting). Single source of truth so
    all three agree (V13-V19-D R2-H1 invariant).

    `current_yaml_by_global_idx`: optional dict {global_idx -> entry dict}
    so we can compare current YAML against `yaml_value_at_decision`. If
    None, predicates fall back to the sidecar's yaml_value (no live
    re-read).

    `pubmed_sync_state`: optional `pubmed_sync.SidecarState` for the
    V23-B Phase 1.5 cross-system silencing (2026-05-26). When passed,
    findings whose (pmid, field) matches a PubMed-sync `keep_yaml`
    override get moved into a `cross_silenced` bucket instead of the
    active list. The bucket carries per-row badge metadata for the
    UI. When None, cross-check is a no-op (cross_silenced is empty).
    """
    from cv_editor.decision_cross_check import (
        build_pmsync_overrides_index,
        silenced_by_pubmed_sync,
    )
    from cv_editor.qc_decisions import (
        is_silenced_id_enrichment,
        is_silenced_mismatch,
        is_silenced_self_absent,
        is_silenced_variant,
    )

    by_idx = current_yaml_by_global_idx or {}
    findings = (sidecar or {}).get("findings") or {}
    # Build the cross-check index ONCE per call (O(1) per finding then).
    pmsync_idx = build_pmsync_overrides_index(pubmed_sync_state)
    out = {}
    cross_silenced: dict = {"mismatches": [], "variants": [], "id_enrichments": []}

    def _check_cross(f, entry):
        """Return badge metadata if PubMed-sync silences this finding,
        else None. Defensive — never raises into the filter loop."""
        if not pmsync_idx:
            return None
        try:
            return silenced_by_pubmed_sync(f, pmsync_idx, entry)
        except Exception:
            return None

    # MISMATCH
    mm = []
    for f in findings.get("mismatches") or []:
        dec = decisions.get(f.get("id", ""))
        entry = _entity_yaml_lookup(by_idx, f)
        live = _live_yaml_value(entry, f.get("field", ""))
        if is_silenced_mismatch(f, dec, current_yaml_value=live):
            continue
        badge = _check_cross(f, entry)
        if badge is not None:
            cross_silenced["mismatches"].append({**f, "_cross_silenced_by": badge})
            continue
        mm.append(f)
    out["mismatches"] = mm
    # VARIANT
    va = []
    for f in findings.get("variants") or []:
        dec = decisions.get(f.get("id", ""))
        entry = _entity_yaml_lookup(by_idx, f)
        live = _live_yaml_value(entry, f.get("field", ""))
        if is_silenced_variant(f, dec, current_yaml_value=live):
            continue
        badge = _check_cross(f, entry)
        if badge is not None:
            cross_silenced["variants"].append({**f, "_cross_silenced_by": badge})
            continue
        va.append(f)
    out["variants"] = va
    # ID_ENRICHMENT — cross-check excludes ID_ENRICHMENT by design.
    ie = []
    for f in findings.get("id_enrichments") or []:
        dec = decisions.get(f.get("id", ""))
        if not is_silenced_id_enrichment(f, dec):
            ie.append(f)
    out["id_enrichments"] = ie
    # pmid_mismatches: jump-to-edit only (no decision). Pass through.
    out["pmid_mismatches"] = list(findings.get("pmid_mismatches") or [])
    # self_absent (2026-06-08): acknowledgeable. A non-defer decision
    # suppresses the row from the active list (and from banner counts,
    # since they sum `effective`). See is_silenced_self_absent.
    sa = []
    for f in findings.get("self_absent") or []:
        dec = decisions.get(f.get("id", ""))
        if is_silenced_self_absent(f, dec):
            continue
        sa.append(f)
    out["self_absent"] = sa
    # Phase 2/4 types (author_name_variants, journal_name_variants,
    # missing_ids) — Phase 1 doesn't touch them; pass through.
    out["author_name_variants"] = list(findings.get("author_name_variants") or [])
    out["journal_name_variants"] = list(findings.get("journal_name_variants") or [])
    out["missing_ids"] = list(findings.get("missing_ids") or [])
    # V23-B Phase 1.5 (2026-05-26): cross-silenced bucket. Kept as a
    # separate key so existing callers iterating finding-type rows
    # don't accidentally count silenced items as active.
    out["cross_silenced"] = cross_silenced
    return out


def effective_total(effective: dict) -> int:
    """Sum across all effective finding-type lists. EXCLUDES the
    cross_silenced bucket (Phase 1.5, 2026-05-26) — cross-silenced
    findings are not active and must not be counted in the banner."""
    return sum(
        len(v) for k, v in effective.items() if k != "cross_silenced" and isinstance(v, list)
    )


def cross_silenced_total(effective: dict) -> int:
    """Sum of cross-silenced rows across all sections. Used by the
    banner sub-line and the triage page collapsible count."""
    cs = effective.get("cross_silenced") or {}
    return sum(len(v) for v in cs.values() if isinstance(v, list))


def effective_for_entry(
    sidecar: dict | None,
    decisions,
    *,
    global_idx: int,
    current_yaml_by_global_idx: dict | None = None,
    pubmed_sync_state=None,
) -> list:
    """Flat list of all effective findings that target a specific
    entry (by global_idx). Used by the entry_view banner. EXCLUDES
    cross-silenced findings (Phase 1.5, 2026-05-26)."""
    eff = effective_findings(
        sidecar,
        decisions,
        current_yaml_by_global_idx=current_yaml_by_global_idx,
        pubmed_sync_state=pubmed_sync_state,
    )
    out = []
    for key, rows in eff.items():
        if key == "cross_silenced":
            continue  # dict, not list — handled separately
        for f in rows:
            if f.get("global_idx") == global_idx:
                out.append(f)
    return out


def cross_silenced_for_entry(effective: dict, global_idx: int) -> list:
    """Flat list of cross-silenced findings targeting a specific
    entry. Used by the entry_view banner sub-line."""
    cs = effective.get("cross_silenced") or {}
    out = []
    for rows in cs.values():
        if not isinstance(rows, list):
            continue
        for f in rows:
            if f.get("global_idx") == global_idx:
                out.append(f)
    return out


# ----- Tombstone resurface detection (UX M4) -----


def resurfaced_decisions(
    sidecar: dict | None,
    decisions,  # qc_decisions.Decisions
) -> list:
    """Return tombstoned decisions whose (entity, field) matches a
    finding in the CURRENT sidecar. The triage page surfaces these in
    a banner-info: "1 prior decision resurfaced because the canonical
    value changed."

    Today: matches by `(pmid OR doi, field)` extracted from the
    tombstoned finding_id snapshot's `decision.yaml_value_at_decision`
    + sidecar findings. Cheap heuristic — exact match on entity_id +
    field for MISMATCH/VARIANT tombstones.
    """
    if not sidecar:
        return []
    findings = sidecar.get("findings") or {}
    # Build a set of (entity_id, field) for currently-surfaced findings.
    current = set()
    for ftype in ("mismatches", "variants"):
        for f in findings.get(ftype) or []:
            ent = f.get("pmid") or f.get("doi") or ""
            field_name = f.get("field") or ""
            if ent and field_name:
                current.add((ent, field_name))
    # Walk tombstones; surface those whose snapshot matches a current
    # finding but lives under a DIFFERENT finding_id (the prefix in
    # the tombstone key wouldn't match the current).
    out = []
    current_ids = set()
    for ftype, rows in findings.items():
        for f in rows or []:
            fid = f.get("id")
            if fid:
                current_ids.add(fid)
    for tomb_id, tomb in decisions.tombstones.items():
        if tomb_id in current_ids:
            continue  # Tombstone matches a current ID (live decision); not "resurfaced"
        # Tombstone's snapshot may not have pmid/doi extractable from
        # the ID prefix alone. Best effort: match on the snapshot's
        # finding_type + decided context. For now, surface ALL active
        # tombstones whose finding_type might still be relevant.
        # This is intentionally conservative; refine in post-impl review.
        dec_snap = tomb.decision or {}
        if dec_snap.get("finding_type") in ("MISMATCH", "VARIANT", "ID_ENRICHMENT"):
            out.append(
                {
                    "tomb_id": tomb_id,
                    "pruned_at": tomb.pruned_at,
                    "decision_snapshot": dec_snap,
                }
            )
    return out
