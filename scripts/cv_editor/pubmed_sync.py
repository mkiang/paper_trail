#!/usr/bin/env python3
"""
Gate 3 — PubMed enrichment + sync tracking.

Enriches data/publications.yml against PubMed E-utilities safely:
  - Auto-fills missing fields (pmcid, volume, issue, pages, month, day,
    epub_date). NEVER overwrites a value that already exists in YAML.
  - Backfills a newly-assigned PMID onto a DOI-only, non-preprint entry:
    the dry-run esearches `<doi>[doi]` (live, cache-bypassed, TTL-throttled
    ~14d), verifies via an exact DOI round-trip, and records the resolution
    in the sidecar; --apply then auto-fills pmid + pmcid + ... See gotcha #81.
  - Flags disagreements on authors / title / journal / doi for user
    triage. Gate 2 reverse pass proved YAML is often more accurate than
    PubMed, so we don't auto-write here.
  - Persists per-PMID sync state in data/publications_pubmed_sync.json
    so periodic re-runs only re-fetch stale entries (90d for ppublish,
    14d for epub-ahead-of-print, 30d for received/revised).

Two-phase contract:
  --dry-run (default): warm cache, write qc/pubmed_sync_report.md, and
                       update the sidecar (fields_flagged refresh +
                       doi_resolve_state). Never writes publications.yml.
  --apply:             same fetch (cache-warm), write auto-fills via
                       yaml_io.write_with_backup, update sidecar. Reads
                       (never esearches) DOI resolutions the dry-run found.

Polite-fetch: UA 'cv-pubmed-sync/1.0' (no PII), per-host throttle
0.34s for eutils.ncbi.nlm.nih.gov.

Usage:
    python3 scripts/pubmed_sync.py --dry-run
    python3 scripts/pubmed_sync.py --apply
    python3 scripts/pubmed_sync.py --force
    python3 scripts/pubmed_sync.py --only-epub
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cv_editor import paths, preprint, pubmed_client, schemas, sections, yaml_io  # noqa: E402
from cv_editor.author_names import norm_author_name  # noqa: E402
from cv_editor.host_throttle import HostThrottle as _SharedHostThrottle  # noqa: E402

# Workspace paths from the seam. Real, monkeypatch-able globals (tests set
# PUBS_PATH / SIDECAR_PATH) refreshed by the hook; a fresh subprocess reads
# the CV_EDITOR_* env at import via these accessors.
PUBS_PATH = paths.data_dir() / "publications.yml"
SIDECAR_PATH = paths.data_dir() / "publications_pubmed_sync.json"
CACHE_DIR = paths.cache_dir() / "pubmed"
REPORT_PATH = paths.qc_dir() / "pubmed_sync_report.md"


@paths.on_configure
def _refresh_paths() -> None:
    global PUBS_PATH, SIDECAR_PATH, CACHE_DIR, REPORT_PATH
    PUBS_PATH = paths.data_dir() / "publications.yml"
    SIDECAR_PATH = paths.data_dir() / "publications_pubmed_sync.json"
    CACHE_DIR = paths.cache_dir() / "pubmed"
    REPORT_PATH = paths.qc_dir() / "pubmed_sync_report.md"


UA = "cv-pubmed-sync/1.0"
NCBI_HOST_GAP_S = 0.34
SIDECAR_VERSION = 1

# What we'll auto-fill from PubMed when YAML is missing a value. Order
# is preservation-friendly (matches typical YAML field order).
AUTO_FILL_FIELDS: tuple[str, ...] = (
    "pmcid",
    "volume",
    "issue",
    "pages",
    "month",
    "day",
)

# What we'll flag for user triage when YAML has a value that DIFFERS
# from PubMed. Never auto-overwritten. Includes year/month/day because
# an epub-ahead-of-print → published transition changes the date; the
# renderer auto-sorts by (-year, -month, -day) at render time, so once
# the user accepts a date change via the editor the entry reorders in
# the next PDF build. Without flagging these here, the silent epub
# transition would never surface.
FLAG_FIELDS: tuple[str, ...] = (
    "authors",
    "title",
    "journal",
    "doi",
    "year",
    "month",
    "day",
)

# TTL by publication_status (days). "Stale" = synced longer ago than TTL.
TTL_DAYS_DEFAULT: dict[str, int] = {
    "ppublish": 90,
    "epublish": 14,
    "aheadofprint": 14,
    "received": 30,
    "revised": 30,
    "": 30,  # unknown status → semi-active default
}

# DOI→PMID backfill (2026-07-11). A published paper often carries a DOI
# for weeks before it acquires a PubMed PMID. When a DOI-only, non-preprint
# entry is due (not resolved within DOI_RESOLVE_TTL_DAYS), the dry-run pass
# esearches `<doi>[doi]` (cache-bypassed) to discover the PMID, then
# auto-fills pmid + pmcid + volume/... on apply — same treatment as any
# other auto-fill. See gotcha #81.
DOI_RESOLVE_TTL_DAYS = 14
# Secondary / fallback title-overlap gate on a DOI→PMID match. The PRIMARY
# guard is an exact DOI round-trip (the fetched record's own DOI must equal
# ours); this fallback only applies when the record carries no DOI. 0.5
# reuses the precedent in preprint.build_promotion_diff.
TITLE_OVERLAP_MIN = 0.5


# ----- Sidecar I/O -----


@dataclass
class EntryRecord:
    synced_at: str
    pubmed_status: str
    fields_filled: list[str] = field(default_factory=list)
    fields_flagged: list[str] = field(default_factory=list)
    yaml_idx_at_sync: int | None = None

    def to_json(self) -> dict:
        return {
            "synced_at": self.synced_at,
            "pubmed_status": self.pubmed_status,
            "fields_filled": sorted(self.fields_filled),
            "fields_flagged": sorted(self.fields_flagged),
            "yaml_idx_at_sync": self.yaml_idx_at_sync,
        }


@dataclass
class AcceptedOverride:
    yaml_value: str
    pubmed_value: str
    reason: str
    accepted_at: str

    def to_json(self) -> dict:
        return {
            "yaml_value": self.yaml_value,
            "pubmed_value": self.pubmed_value,
            "reason": self.reason,
            "accepted_at": self.accepted_at,
        }


@dataclass
class SidecarState:
    entries: dict[str, EntryRecord] = field(default_factory=dict)
    no_pmid_skip_log: dict[str, str] = field(default_factory=dict)
    # accepted_yaml_overrides[pmid][field] = AcceptedOverride
    # If the YAML value later changes from yaml_value, the override no
    # longer applies (the next sync re-surfaces the flag for triage).
    accepted_yaml_overrides: dict[str, dict[str, AcceptedOverride]] = field(default_factory=dict)
    # DOI→PMID backfill state (2026-07-11). Keyed by lowercased DOI:
    #   {"last_attempt": iso, "status": "resolved"|"no_record"|"needs_review",
    #    "pmid": str|None, "candidate_pmid": str|None, "overlap": float|None}
    # Purely additive — old sidecars (no key) load with {} and are NOT a
    # version mismatch, so bumping SIDECAR_VERSION (which would wipe the
    # accepted_yaml_overrides) is unnecessary. See gotcha #81.
    doi_resolve_state: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "version": SIDECAR_VERSION,
            "entries": {k: v.to_json() for k, v in self.entries.items()},
            "no_pmid_skip_log": dict(self.no_pmid_skip_log),
            "accepted_yaml_overrides": {
                pmid: {f: ov.to_json() for f, ov in fields.items()}
                for pmid, fields in self.accepted_yaml_overrides.items()
            },
            "doi_resolve_state": {k: dict(v) for k, v in self.doi_resolve_state.items()},
        }


def load_sidecar(path: Path) -> SidecarState:
    """Load the sidecar; resilient to missing or corrupt files.

    Missing file → empty state (silent; first run).
    Corrupt JSON → empty state + WARNING to stderr (silent recovery would
    mask a real bug, e.g., a crashed `save_sidecar` from a prior run).
    """
    # V20 (2026-05-18): delegate version-check + JSON-shape to
    # cv_editor.versioned_json. The helper warns with the `[sync]`
    # prefix; OSError on a real read failure propagates to the
    # surrounding main()-level try/except in the CLI / Flask app.
    from cv_editor.versioned_json import load_versioned

    raw = load_versioned(
        path,
        SIDECAR_VERSION,
        component_name="sync",
    )
    if raw is None:
        return SidecarState()
    entries = {}
    for pmid, rec in (raw.get("entries") or {}).items():
        entries[str(pmid)] = EntryRecord(
            synced_at=rec.get("synced_at", ""),
            pubmed_status=rec.get("pubmed_status", ""),
            fields_filled=list(rec.get("fields_filled") or []),
            fields_flagged=list(rec.get("fields_flagged") or []),
            yaml_idx_at_sync=rec.get("yaml_idx_at_sync"),
        )
    no_pmid_skip = {str(k): v for k, v in (raw.get("no_pmid_skip_log") or {}).items()}
    overrides: dict[str, dict[str, AcceptedOverride]] = {}
    for pmid, fields in (raw.get("accepted_yaml_overrides") or {}).items():
        per_pmid: dict[str, AcceptedOverride] = {}
        for fname, ov in (fields or {}).items():
            per_pmid[fname] = AcceptedOverride(
                yaml_value=str(ov.get("yaml_value", "")),
                pubmed_value=str(ov.get("pubmed_value", "")),
                reason=str(ov.get("reason", "")),
                accepted_at=str(ov.get("accepted_at", "")),
            )
        if per_pmid:
            overrides[str(pmid)] = per_pmid
    doi_resolve_state = {
        str(k): dict(v)
        for k, v in (raw.get("doi_resolve_state") or {}).items()
        if isinstance(v, dict)
    }
    return SidecarState(
        entries=entries,
        no_pmid_skip_log=no_pmid_skip,
        accepted_yaml_overrides=overrides,
        doi_resolve_state=doi_resolve_state,
    )


def save_sidecar(path: Path, state: SidecarState) -> None:
    """Atomic write via cv_editor.atomic_json (V14 extraction, 2026-05-17)."""
    from cv_editor.atomic_json import atomic_write_json

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state.to_json())


# ----- TTL -----


def parse_synced_at(s: str) -> datetime | None:
    """Tolerant ISO-8601 parser for sidecar synced_at strings."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _resolve_due(last_iso: str | None, now: datetime, ttl_days: int) -> bool:
    """Whether a DOI-only entry is due for a fresh esearch resolution.

    None / unparseable last-attempt → due (first check or corrupt state).
    Otherwise due once the last attempt is `ttl_days` or more old.
    """
    last = parse_synced_at(last_iso or "")
    if last is None:
        return True
    return (now - last).days >= ttl_days


def needs_refresh(
    record: EntryRecord | None,
    *,
    now: datetime,
    only_epub: bool = False,
    force: bool = False,
    ttl_overrides: dict[str, int] | None = None,
) -> bool:
    """Decide whether to re-fetch this PMID's PubMed record.

    Note: `only_epub` is checked BEFORE `force` so that
    `--only-epub --force` (and the editor's force-dryrun path) still
    narrows the set to epub/ahead-of-print entries. Inverting this order
    silently neuters --only-epub when combined with the dry-run sidecar
    refresh introduced 2026-05-17 — see V13-V19-D R1-H1.
    """
    ttl_days = dict(TTL_DAYS_DEFAULT)
    if ttl_overrides:
        ttl_days.update(ttl_overrides)
    if only_epub:
        if record is None:
            return False
        status = (record.pubmed_status or "").lower()
        if status not in ("epublish", "aheadofprint"):
            return False
    if force:
        return True
    if record is None:
        return False if only_epub else True
    status = (record.pubmed_status or "").lower()
    synced = parse_synced_at(record.synced_at)
    if synced is None:
        return True
    ttl = ttl_days.get(status, ttl_days[""])
    age = (now - synced).days
    return age >= ttl


# ----- Per-host throttle -----
# Migrated to cv_editor.host_throttle.HostThrottle (V14 extraction,
# 2026-05-17). Local class removed; existing call sites import the
# shared class with default gap = NCBI_HOST_GAP_S.


def HostThrottle(gap: float = NCBI_HOST_GAP_S):
    """Back-compat constructor: returns a shared HostThrottle preconfigured
    with `gap` as the default. Existing call sites pass a positional float."""
    return _SharedHostThrottle(default_gap=gap)


# ----- Author / field normalization -----


def _nfkd(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def _norm_text(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _norm_title(s: object) -> str:
    """Title comparison: NFKD-fold, lowercase, collapse whitespace, strip
    trailing punctuation (PubMed often drops the trailing period)."""
    t = _nfkd(str(s or "")).lower()
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"[\.,;!?]+$", "", t).strip()


def _norm_journal(s: object) -> str:
    return _nfkd(str(s or "")).lower().strip().rstrip(".")


def _yaml_author_names(authors: list) -> list[str]:
    """Flatten YAML's author list (strings or {name, ...} dicts) to names."""
    out = []
    for a in authors or []:
        if isinstance(a, dict):
            n = a.get("name") or ""
        else:
            n = str(a)
        if n:
            out.append(n)
    return out


def _authors_differ(yaml_authors: list, pubmed_authors: list[str]) -> bool:
    """Compare YAML and PubMed author lists after normalization."""
    y = [norm_author_name(n).lower() for n in _yaml_author_names(yaml_authors)]
    p = [norm_author_name(n).lower() for n in pubmed_authors or []]
    return y != p


# ----- Diff -----


@dataclass
class EntryDecision:
    pmid: str
    global_idx: int
    title_preview: str
    fills: dict[str, object] = field(default_factory=dict)
    flags: dict[str, tuple[object, object]] = field(default_factory=dict)
    # Raw values per field. flags[] holds display-friendly strings (e.g.
    # joined "; "-separated author names); raw_yaml / raw_pubmed hold
    # the original typed values (list-of-authors stays a list) so the
    # apply step can write them back to YAML preserving shape.
    raw_yaml: dict[str, object] = field(default_factory=dict)
    raw_pubmed: dict[str, object] = field(default_factory=dict)
    # Fields where a flag would have surfaced but was suppressed by an
    # existing accepted_yaml_overrides record (YAML snapshot still matches).
    silenced: dict[str, AcceptedOverride] = field(default_factory=dict)
    # Fields where an override existed but the YAML value diverged from
    # the snapshot — re-flagged for re-triage.
    resurfaced: dict[str, AcceptedOverride] = field(default_factory=dict)
    # V23-B Phase 1.5 (2026-05-26): fields silenced by a matching QC
    # keep_yaml decision on the same (pmid, field). Carries badge
    # metadata dict for the UI; not an AcceptedOverride.
    cross_silenced: dict[str, dict] = field(default_factory=dict)
    publication_status: str = ""
    error: str = ""
    # True when this decision's `pmid` fill came from a DOI→PMID
    # resolution (the entry had a doi but no pmid). Labels the report;
    # not a UI trigger (the pmid fill flows through apply_fills like any
    # other auto-fill). See gotcha #81.
    resolved_from_doi: bool = False


def diff_one(yaml_entry: dict, pm_rec: dict, *, pmid: str, global_idx: int) -> EntryDecision:
    """Compute fills + flags for one entry against its PubMed record."""
    title_preview = (yaml_entry.get("title") or "")[:60]
    dec = EntryDecision(
        pmid=pmid,
        global_idx=global_idx,
        title_preview=title_preview,
        publication_status=pm_rec.get("publication_status", ""),
    )

    # ----- Auto-fill: only fill if YAML field is missing/empty -----
    for field_name in AUTO_FILL_FIELDS:
        existing = yaml_entry.get(field_name)
        if existing not in (None, "", []):
            continue
        if field_name == "pmcid":
            v = pm_rec.get("pmcid") or ""
        elif field_name in ("month", "day"):
            v = pm_rec.get(field_name)
        else:
            v = pm_rec.get(field_name) or ""
        if v in (None, "", 0):
            continue
        dec.fills[field_name] = v

    # ----- Flag: any disagreement on these (never overwrite) -----
    yaml_title = _norm_title(yaml_entry.get("title"))
    pm_title = _norm_title(pm_rec.get("title"))
    if yaml_title and pm_title and yaml_title != pm_title:
        dec.flags["title"] = (yaml_entry.get("title"), pm_rec.get("title"))
        dec.raw_yaml["title"] = yaml_entry.get("title")
        dec.raw_pubmed["title"] = pm_rec.get("title")

    yaml_journal = _norm_journal(yaml_entry.get("journal"))
    pm_journal_full = _norm_journal(pm_rec.get("journal_full"))
    pm_journal_iso = _norm_journal(pm_rec.get("journal_iso"))
    if yaml_journal and yaml_journal not in (pm_journal_full, pm_journal_iso):
        if pm_journal_full or pm_journal_iso:
            pm_j = pm_rec.get("journal_full") or pm_rec.get("journal_iso")
            dec.flags["journal"] = (yaml_entry.get("journal"), pm_j)
            dec.raw_yaml["journal"] = yaml_entry.get("journal")
            dec.raw_pubmed["journal"] = pm_j

    yaml_doi = str(yaml_entry.get("doi") or "").lower().strip()
    pm_doi = str(pm_rec.get("doi") or "").lower().strip()
    if yaml_doi and pm_doi and yaml_doi != pm_doi:
        dec.flags["doi"] = (yaml_entry.get("doi"), pm_rec.get("doi"))
        dec.raw_yaml["doi"] = yaml_entry.get("doi")
        dec.raw_pubmed["doi"] = pm_rec.get("doi")

    if yaml_entry.get("authors") and pm_rec.get("authors"):
        if _authors_differ(yaml_entry["authors"], pm_rec["authors"]):
            dec.flags["authors"] = (
                "; ".join(_yaml_author_names(yaml_entry["authors"])),
                "; ".join(pm_rec["authors"]),
            )
            # Raw values are the actual lists (preserving co_first /
            # co_senior dicts on the YAML side) so apply_pubmed can
            # merge them while preserving markers.
            dec.raw_yaml["authors"] = list(yaml_entry["authors"])
            dec.raw_pubmed["authors"] = list(pm_rec["authors"])

    # Year / month / day: flag any disagreement so epub→published
    # transitions surface for user review. The renderer auto-sorts by
    # (-year, -month, -day) so once the user accepts an updated date
    # via the editor, the entry reorders on the next ./build.sh.
    for date_field in ("year", "month", "day"):
        yaml_v = yaml_entry.get(date_field)
        pm_v = pm_rec.get(date_field)
        if yaml_v in (None, "", 0) or pm_v in (None, "", 0):
            continue
        if str(yaml_v).strip() != str(pm_v).strip():
            dec.flags[date_field] = (yaml_v, pm_v)
            dec.raw_yaml[date_field] = yaml_v
            dec.raw_pubmed[date_field] = pm_v

    return dec


def apply_overrides_to_decision(
    dec: EntryDecision,
    overrides_for_pmid: dict[str, AcceptedOverride] | None,
    *,
    entry: dict | None = None,
    qc_decisions_index: dict | None = None,
) -> None:
    """Suppress flags that match an accepted override; re-surface those
    whose YAML value has changed since acceptance.

    Mutates ``dec`` in place. Safe to call when ``overrides_for_pmid`` is
    None or empty (no-op).

    V23-B Phase 1.5 (2026-05-26): after PubMed's own filter, if
    ``qc_decisions_index`` is passed (a `{(pmid, field): (fid, Decision)}`
    map from `decision_cross_check.build_qc_decisions_index`), apply
    cross-system silencing. Fields silenced by a QC `keep_yaml` decision
    on the same (pmid, field) move from ``dec.flags`` to
    ``dec.cross_silenced`` carrying badge metadata. ``entry`` is required
    for the cross-check (to compute current YAML in the canonical form).
    """
    if overrides_for_pmid:
        for field_name in list(dec.flags.keys()):
            override = overrides_for_pmid.get(field_name)
            if override is None:
                continue
            current_yaml = str(dec.flags[field_name][0])
            if current_yaml == override.yaml_value:
                dec.silenced[field_name] = override
                del dec.flags[field_name]
            else:
                dec.resurfaced[field_name] = override
    # V23-B Phase 1.5: cross-system silencing from QC keep_yaml decisions.
    if qc_decisions_index and entry is not None:
        from cv_editor.decision_cross_check import silenced_by_qc

        for field_name in list(dec.flags.keys()):
            try:
                badge = silenced_by_qc(
                    str(dec.pmid),
                    field_name,
                    entry,
                    qc_decisions_index,
                )
            except Exception:
                badge = None  # never raise into the filter loop
            if badge is None:
                continue
            dec.cross_silenced[field_name] = badge
            del dec.flags[field_name]


def effective_flagged_fields(
    entry: dict,
    sidecar_rec: EntryRecord | None,
    overrides_for_pmid: dict[str, AcceptedOverride] | None,
    *,
    pmid: str | None = None,
    qc_decisions_index: dict | None = None,
) -> list[str]:
    """Return the list of fields in `sidecar_rec.fields_flagged` that
    are still effectively flagged given the current YAML state.

    A field is "effectively flagged" when:
      * It's listed in `sidecar_rec.fields_flagged`, AND
      * Either no accepted override exists for it, OR the override's
        snapshot `yaml_value` no longer matches the current YAML value
        (i.e. the field has re-surfaced — same predicate as
        `apply_overrides_to_decision`), AND
      * No matching QC `keep_yaml` decision cross-silences it
        (V23-B Phase 1.5, 2026-05-26).

    Used by the entry_view banner so it stays in lockstep with the
    triage page (V13-V19-D R2-H1 invariant).

    Comparison logic mirrors `diff_one`'s normalization for the relevant
    field types so banner truth matches dry-run truth.
    """
    if not sidecar_rec or not sidecar_rec.fields_flagged:
        return []
    pmid = pmid or ""
    out: list[str] = []
    for field_name in sidecar_rec.fields_flagged:
        # Compute the current YAML value in the same shape diff_one
        # would have seen at apply-time.
        if field_name == "authors":
            current_yaml = "; ".join(_yaml_author_names(entry.get("authors") or []))
        else:
            current_yaml = str(entry.get(field_name) or "")
        # PubMed sync's own override silencing.
        if overrides_for_pmid:
            override = overrides_for_pmid.get(field_name)
            if override is not None:
                if current_yaml == override.yaml_value:
                    continue  # silenced — drop from banner
                # Re-surfaced — fall through to cross-check, then add.
        # V23-B Phase 1.5: cross-system silencing.
        if qc_decisions_index and pmid:
            try:
                from cv_editor.decision_cross_check import silenced_by_qc

                badge = silenced_by_qc(
                    str(pmid),
                    field_name,
                    entry,
                    qc_decisions_index,
                )
            except Exception:
                badge = None
            if badge is not None:
                continue  # cross-silenced — drop from banner
        out.append(field_name)
    return out


def cross_silenced_flagged_fields(
    entry: dict,
    sidecar_rec: EntryRecord | None,
    overrides_for_pmid: dict[str, AcceptedOverride] | None,
    qc_decisions_index: dict | None,
    *,
    pmid: str | None = None,
) -> list[tuple[str, dict]]:
    """Companion to `effective_flagged_fields`: return [(field, badge)]
    for fields silenced by the cross-check. Used by the entry_view
    banner sub-line and the PubMed sync triage page's "Silenced by QC"
    section. Phase 1.5 (2026-05-26).
    """
    if not sidecar_rec or not sidecar_rec.fields_flagged or not qc_decisions_index:
        return []
    if not pmid:
        return []
    from cv_editor.decision_cross_check import silenced_by_qc

    out: list[tuple[str, dict]] = []
    for field_name in sidecar_rec.fields_flagged:
        # Don't shadow PubMed sync's own silencing — PubMed sync wins
        # at the same field (returns dropped from banner before cross-
        # check fires).
        if overrides_for_pmid and field_name in overrides_for_pmid:
            # If PubMed's snapshot matches, PubMed silenced it; skip.
            if field_name == "authors":
                current_yaml = "; ".join(_yaml_author_names(entry.get("authors") or []))
            else:
                current_yaml = str(entry.get(field_name) or "")
            if current_yaml == overrides_for_pmid[field_name].yaml_value:
                continue
        try:
            badge = silenced_by_qc(
                str(pmid),
                field_name,
                entry,
                qc_decisions_index,
            )
        except Exception:
            badge = None
        if badge is not None:
            out.append((field_name, badge))
    return out


# ----- Decisions file (qc/pubmed_sync_decisions.yml) -----

DECISION_KEEP_YAML = "keep_yaml"
DECISION_APPLY_PUBMED = "apply_pubmed"
_VALID_DECISIONS = (DECISION_KEEP_YAML, DECISION_APPLY_PUBMED)


@dataclass
class Decision:
    pmid: str
    field: str
    decision: str  # one of _VALID_DECISIONS
    reason: str


class DecisionsFileError(ValueError):
    """Raised when the decisions file fails validation."""


def load_decisions(path: Path) -> list[Decision]:
    """Parse and validate a decisions YAML file.

    Schema:
        decisions:
          - pmid: '123'
            field: authors
            decision: keep_yaml   # or apply_pubmed
            reason: 'why'

    Raises DecisionsFileError on any structural problem so the operator
    learns of issues before any YAML is written.
    """
    import yaml as pyyaml  # local — pyyaml already in deps

    if not path.exists():
        raise DecisionsFileError(f"decisions file does not exist: {path}")
    raw = pyyaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict) or "decisions" not in raw:
        raise DecisionsFileError(
            f"decisions file must be a mapping with a 'decisions:' key; got {type(raw).__name__}"
        )
    out: list[Decision] = []
    for i, item in enumerate(raw["decisions"] or []):
        if not isinstance(item, dict):
            raise DecisionsFileError(f"decisions[{i}] must be a mapping")
        pmid = str(item.get("pmid") or "").strip()
        fname = str(item.get("field") or "").strip()
        dec_val = str(item.get("decision") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not pmid:
            raise DecisionsFileError(f"decisions[{i}] missing 'pmid'")
        if not fname:
            raise DecisionsFileError(f"decisions[{i}] (pmid {pmid}) missing 'field'")
        if not dec_val:
            # Allow blank decisions in a template (skip silently).
            continue
        if dec_val not in _VALID_DECISIONS:
            raise DecisionsFileError(
                f"decisions[{i}] (pmid {pmid}, field {fname}) has invalid "
                f"decision={dec_val!r}; must be one of {_VALID_DECISIONS}"
            )
        if dec_val == DECISION_KEEP_YAML and not reason:
            raise DecisionsFileError(
                f"decisions[{i}] (pmid {pmid}, field {fname}) is keep_yaml "
                f"but missing a 'reason' (required to record why YAML wins)"
            )
        out.append(Decision(pmid=pmid, field=fname, decision=dec_val, reason=reason))
    return out


def _author_list_preview(value: str, max_authors: int = 4) -> str:
    """Show first ``max_authors`` semicolon-separated names then ``[+N more]``.

    Beats raw char-truncation for author lists because PubMed/YAML name
    boundaries are semantic — a 120-char cut can land mid-name and make
    visual comparison error-prone (U-M1).
    """
    if not value:
        return ""
    parts = [p.strip() for p in value.split(";") if p.strip()]
    if len(parts) <= max_authors:
        return "; ".join(parts)
    rest = len(parts) - max_authors
    return "; ".join(parts[:max_authors]) + f"; [+{rest} more]"


def write_decisions_template(
    path: Path,
    decisions: list[EntryDecision],
) -> None:
    """Write a YAML template listing every flagged item for user fill-in.

    Generated after every dry-run; the user copies it to
    ``pubmed_sync_decisions.yml`` and fills in each ``decision:`` field.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Gate 3 decisions — copy this file to `pubmed_sync_decisions.yml`")
    lines.append("# then fill in `decision:` for each item and re-run:")
    lines.append(
        "#   python -m cv_editor.pubmed_sync --apply --decisions qc/pubmed_sync_decisions.yml"
    )
    lines.append("#")
    lines.append("# decision: one of")
    lines.append(f"#   {DECISION_KEEP_YAML}     — record an override; suppress this flag forever")
    lines.append("#                  (until the YAML value changes; then it re-surfaces)")
    lines.append(f"#   {DECISION_APPLY_PUBMED} — write PubMed value to YAML on --apply")
    lines.append("#")
    lines.append("# reason: required for keep_yaml, optional for apply_pubmed")
    lines.append("# Leave `decision:` blank to defer the item to a later session.")
    lines.append("#")
    lines.append("# Example (showing both decision types):")
    lines.append("#   - pmid: '12345678'")
    lines.append("#     field: journal")
    lines.append(f"#     decision: {DECISION_KEEP_YAML}")
    lines.append("#     reason: 'YAML uses preferred Title Case form'")
    lines.append("#   - pmid: '87654321'")
    lines.append("#     field: month")
    lines.append(f"#     decision: {DECISION_APPLY_PUBMED}")
    lines.append("#     reason: 'epub→print transition'")
    lines.append("")
    lines.append("decisions:")
    for d in decisions:
        if not d.flags:
            continue
        for fname, (yv, pv) in d.flags.items():
            if fname == "authors":
                yv_show = _author_list_preview(str(yv or ""))
                pv_show = _author_list_preview(str(pv or ""))
            else:
                yv_show = str(yv or "").replace("\n", " ")[:120]
                pv_show = str(pv or "").replace("\n", " ")[:120]
            lines.append(f"  # idx {d.global_idx} — {d.title_preview}")
            lines.append(f"  #   YAML:   {yv_show}")
            lines.append(f"  #   PubMed: {pv_show}")
            lines.append(f"  - pmid: '{d.pmid}'")
            lines.append(f"    field: {fname}")
            lines.append("    decision:        # keep_yaml | apply_pubmed")
            lines.append("    reason:")
            lines.append("")
    path.write_text("\n".join(lines))


# ----- Apply: merge fills into the data tree -----


def apply_fills(data, sch, decisions: list[EntryDecision]) -> int:
    """Merge each decision's fills into the in-memory ruamel data tree.
    Returns the count of fields written across all entries."""
    n = 0
    for dec in decisions:
        if not dec.fills:
            continue
        rec = sections.locate(data, sch["structure"], dec.global_idx)
        entry = rec["entry"]
        for k, v in dec.fills.items():
            # Preserve YAML quoting: pmcid/volume/issue/pages stay
            # strings; month/day stay ints (schema enforces these).
            if k in ("month", "day"):
                entry[k] = int(v)
            else:
                entry[k] = str(v)
            n += 1
    return n


def _merge_pubmed_authors_preserving_markers(
    yaml_authors: list,
    pubmed_authors: list[str],
) -> list:
    """Return a new author list with PubMed name forms, but preserve
    co_first / co_senior markers that were on matching YAML entries.

    Match is by NFKD-folded + lowercased name (the same comparator
    ``_authors_differ`` uses). If PubMed has an author whose normalized
    name matches a YAML dict-shaped author with markers, the markers
    move to the new entry. Authors without a YAML match become plain
    strings; authors with a marker-bearing YAML match become CommentedMap
    so ruamel preserves the dict shape on write.
    """
    from ruamel.yaml.comments import CommentedMap

    yaml_markers: dict[str, dict] = {}
    for a in yaml_authors or []:
        if isinstance(a, dict):
            name = a.get("name", "")
            key = norm_author_name(name).lower()
            markers = {k: v for k, v in a.items() if k != "name"}
            if key and markers:
                yaml_markers[key] = markers
    out: list = []
    for pm_name in pubmed_authors or []:
        key = norm_author_name(pm_name).lower()
        markers = yaml_markers.get(key)
        if markers:
            cm = CommentedMap()
            cm["name"] = pm_name
            for k, v in markers.items():
                cm[k] = v
            out.append(cm)
        else:
            out.append(pm_name)
    return out


def apply_pubmed_decisions(
    data,
    sch,
    decisions: list[EntryDecision],
    apply_pubmed_decs: list[Decision],
    state: SidecarState | None = None,
) -> int:
    """Write PubMed values for fields the user marked ``apply_pubmed``.

    Looks up each decision's (pmid, field) in the per-entry decision list
    to fetch the typed PubMed value from ``dec.raw_pubmed`` (preserves
    list shape for authors). Skips silently if the flag has already been
    silenced or doesn't appear in this run. Returns the count of fields
    written.
    """
    by_pmid = {d.pmid: d for d in decisions}
    n = 0
    for d in apply_pubmed_decs:
        dec = by_pmid.get(d.pmid)
        if dec is None or d.field not in dec.flags:
            # Either the entry wasn't fetched this run, or it was
            # already silenced. Either way, nothing to write.
            continue
        pv = dec.raw_pubmed.get(d.field)
        if pv is None:
            # Belt-and-suspenders: fall back to the display string. This
            # path shouldn't trigger for runs that go through diff_one;
            # it exists for unit tests that build EntryDecision directly.
            pv = dec.flags[d.field][1]
        rec = sections.locate(data, sch["structure"], dec.global_idx)
        entry = rec["entry"]
        if d.field == "authors":
            yaml_authors = dec.raw_yaml.get(d.field) or entry.get("authors") or []
            entry["authors"] = _merge_pubmed_authors_preserving_markers(
                yaml_authors,
                pv if isinstance(pv, list) else [],
            )
        elif d.field in ("month", "day", "year"):
            # Defensive int-coercion: pubmed_client already returns int
            # or None for these, but unit-tests + future callers may
            # hand-build EntryDecision with strings. Skip + warn rather
            # than crash mid-mutation (which would partially-write the
            # data tree before yaml_io.write_with_backup runs).
            try:
                entry[d.field] = int(pv) if pv else None
            except (TypeError, ValueError):
                print(
                    f"[sync] WARNING: skipping apply_pubmed for PMID {d.pmid} "
                    f"field '{d.field}': PubMed value {pv!r} is not int-coercible",
                    file=sys.stderr,
                )
                continue
        else:
            entry[d.field] = str(pv) if pv is not None else None
        # V13-V19-D R2-H5 (2026-05-17): drop any stale keep_yaml override
        # for this (pmid, field). The user has now chosen to accept
        # PubMed's value; the prior "keep YAML" snapshot is moot.
        # Leaving it stranded means a future PubMed update on the same
        # field surfaces as "resurfaced" against a yaml_value the user
        # no longer cares about.
        if state is not None:
            overrides = state.accepted_yaml_overrides.get(d.pmid)
            if overrides:
                overrides.pop(d.field, None)
                if not overrides:
                    state.accepted_yaml_overrides.pop(d.pmid, None)
        n += 1
    return n


def record_keep_yaml_overrides(
    state: SidecarState,
    decisions: list[EntryDecision],
    keep_yaml_decs: list[Decision],
    *,
    iso_now: str,
    on_skip=None,
) -> int:
    """Record `keep_yaml` decisions into ``state.accepted_yaml_overrides``.

    Snapshots the current YAML value so a future YAML edit re-surfaces
    the flag. Returns the count of overrides recorded.

    `on_skip` (optional callable) is called with (decision, reason_str)
    for each decision that isn't recorded — typically because the
    referenced (pmid, field) didn't flag this run (already silenced,
    typo in the decisions file, or YAML edited between dry-run and
    apply). Callers can route to stderr.
    """
    by_pmid = {d.pmid: d for d in decisions}
    n = 0
    for d in keep_yaml_decs:
        dec = by_pmid.get(d.pmid)
        if dec is None:
            if on_skip:
                on_skip(d, "PMID not fetched this run (in-TTL or unknown)")
            continue
        if d.field not in dec.flags:
            if on_skip:
                on_skip(d, "field not flagged this run (already silenced or typo)")
            continue
        yv, pv = dec.flags[d.field]
        state.accepted_yaml_overrides.setdefault(d.pmid, {})[d.field] = AcceptedOverride(
            yaml_value=str(yv),
            pubmed_value=str(pv),
            reason=d.reason,
            accepted_at=iso_now,
        )
        n += 1
    return n


# ----- Report -----


def write_report(
    path: Path,
    *,
    decisions: list[EntryDecision],
    skipped_no_pmid: list[tuple[int, str, str]],
    skipped_in_ttl: int,
    fetch_errors: list[tuple[str, str]],
    state_before: SidecarState,
    args: argparse.Namespace,
    all_yaml_pmids: set[str] | None = None,
    resolved: list[tuple[int, str, str, str, float]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolved or []
    fillable = [d for d in decisions if d.fills]
    flaggable = [d for d in decisions if d.flags]
    clean = [d for d in decisions if not d.fills and not d.flags]

    lines: list[str] = []
    lines.append("# PubMed sync report (Gate 3)")
    lines.append("")
    lines.append(
        f"_Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')} — "
        f"mode: {'APPLY' if args.apply else 'DRY-RUN'}_"
    )
    lines.append("")
    silenced_count = sum(len(d.silenced) for d in decisions)
    resurfaced_count = sum(len(d.resurfaced) for d in decisions)

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- {len(decisions)} entries fetched / compared")
    lines.append(f"- {len(fillable)} entries with at least one would-fill field")
    lines.append(f"- {sum(len(d.fills) for d in fillable)} total fields that could be filled")
    lines.append(f"- {len(flaggable)} entries with at least one flagged conflict")
    if flaggable:
        lines.append(
            f"- **Decisions needed: {len(flaggable)} entries** — fill in "
            f"`qc/pubmed_sync_decisions.yml` (template at "
            f"`qc/pubmed_sync_decisions.template.yml`) before --apply."
        )
    if silenced_count:
        lines.append(
            f"- {silenced_count} flags silenced by accepted overrides (YAML snapshot still matches)"
        )
    if resurfaced_count:
        lines.append(
            f"- {resurfaced_count} flags re-surfaced (YAML changed since "
            f"acceptance; needs re-triage)"
        )
    lines.append(f"- {len(clean)} entries unchanged (YAML matches PubMed within tolerance)")
    lines.append(f"- {skipped_in_ttl} entries skipped (sync within TTL)")
    lines.append(f"- {len(skipped_no_pmid)} entries skipped (no PMID; logged in sidecar)")
    if fetch_errors:
        lines.append(f"- {len(fetch_errors)} fetch errors (PubMed returned no record)")
    lines.append("")

    # Would-fill
    lines.append("## Would-fill (auto-fillable; YAML field is missing)")
    lines.append("")
    if fillable:
        lines.append("| # | Title | Field | PubMed value |")
        lines.append("|---|---|---|---|")
        for d in fillable:
            for fld, val in d.fills.items():
                title = d.title_preview.replace("|", "\\|")
                v = str(val).replace("|", "\\|")
                lines.append(f"| {d.global_idx} | {title} | `{fld}` | `{v}` |")
    else:
        lines.append("_None — all auto-fillable fields are already present in YAML._")
    lines.append("")

    # Resolved from DOI (2026-07-11): DOI-only entries whose PMID was
    # discovered via esearch. The `pmid` fill also shows in Would-fill
    # above; this section adds the DOI→PMID provenance + title overlap for
    # auditing an auto-written identity key. Entries that matched a PMID
    # but failed a safety guard appear in "Skipped (no PMID)" below with a
    # "needs manual check" reason.
    lines.append("## Resolved from DOI (new PMID discovered)")
    lines.append("")
    if resolved:
        lines.append("| # | Title | DOI | → PMID | Title overlap |")
        lines.append("|---|---|---|---|---|")
        for gidx, title, doi, pmid, overlap in resolved:
            t = str(title).replace("|", "\\|")[:60]
            d = str(doi).replace("|", "\\|")
            lines.append(f"| {gidx} | {t} | `{d}` | `{pmid}` | {overlap:.2f} |")
    else:
        lines.append("_None — no DOI-only entry resolved to a new PMID this run._")
    lines.append("")

    # Would-flag
    lines.append("## Would-flag (disagreements; user triage required)")
    lines.append("")
    if flaggable:
        for d in flaggable:
            lines.append(f"### idx {d.global_idx} (PMID {d.pmid}) — {d.title_preview}")
            lines.append("")
            lines.append("| Field | YAML value | PubMed value |")
            lines.append("|---|---|---|")
            for fld, (yv, pv) in d.flags.items():
                y = str(yv or "").replace("|", "\\|").replace("\n", " ")
                p = str(pv or "").replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {fld} | {y[:160]} | {p[:160]} |")
            lines.append("")
    else:
        lines.append("_None — every entry's PubMed record agrees with YAML._")
    lines.append("")

    # Re-surfaced (override existed but YAML diverged from snapshot)
    if resurfaced_count:
        lines.append("## Re-surfaced after YAML change")
        lines.append("")
        lines.append(
            "These entries had an accepted override, but the YAML value "
            "has changed since the override was recorded. The override no "
            "longer applies; the conflict is shown again for re-triage."
        )
        lines.append("")
        for d in decisions:
            if not d.resurfaced:
                continue
            lines.append(f"### idx {d.global_idx} (PMID {d.pmid}) — {d.title_preview}")
            lines.append("")
            for fname, ov in d.resurfaced.items():
                lines.append(
                    f"- `{fname}` — accepted {ov.accepted_at} as "
                    f"`{ov.yaml_value[:80]}`; reason: {ov.reason}"
                )
            lines.append("")

    # Silenced (override matched current YAML; flag suppressed this run)
    if silenced_count:
        lines.append("## Silenced by accepted overrides")
        lines.append("")
        lines.append(
            f"_{silenced_count} flagged conflicts were suppressed because "
            f"the user previously accepted the YAML form. Snapshot still "
            f"matches → no action needed._"
        )
        lines.append("")

    # No PMID
    lines.append("## Skipped (no PMID)")
    lines.append("")
    if skipped_no_pmid:
        lines.append("| # | Title | Reason |")
        lines.append("|---|---|---|")
        for idx, title, reason in skipped_no_pmid:
            t = title.replace("|", "\\|")[:60]
            r = reason.replace("|", "\\|")
            lines.append(f"| {idx} | {t} | {r} |")
    else:
        lines.append("_None._")
    lines.append("")

    # Fetch errors
    if fetch_errors:
        lines.append("## Fetch errors")
        lines.append("")
        for pmid, msg in fetch_errors:
            lines.append(f"- PMID {pmid}: {msg}")
        lines.append("")

    # Orphan PMIDs (in sidecar but not in current YAML — manual cleanup).
    # If the caller passed `all_yaml_pmids` we use that (authoritative);
    # otherwise we fall back to "PMIDs in this run's decisions", which
    # over-counts orphans when --only-stale skipped fresh entries.
    sidecar_pmids = set(state_before.entries.keys())
    if all_yaml_pmids is None:
        all_yaml_pmids = {d.pmid for d in decisions}
    # A DOI-resolved-but-not-yet-applied PMID gets a sidecar `entries`
    # record on the resolving dry-run, yet isn't in YAML until --apply.
    # Exclude those so they don't read as false orphans.
    resolved_pmids = {v["pmid"] for v in state_before.doi_resolve_state.values() if v.get("pmid")}
    orphan = sidecar_pmids - all_yaml_pmids - resolved_pmids
    if orphan:
        lines.append("## Sidecar orphans (PMID in sidecar, not in current YAML)")
        lines.append("")
        for pmid in sorted(orphan):
            rec = state_before.entries[pmid]
            lines.append(
                f"- PMID `{pmid}` (last synced {rec.synced_at}; status `{rec.pubmed_status}`)"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("**Next steps:**")
    lines.append("")
    if args.apply:
        lines.append(
            "- Auto-fills have been written to `data/publications.yml` via "
            "`yaml_io.write_with_backup` (atomic-tmp + filelock + parse-verify + backup)."
        )
        lines.append("- Sidecar updated at `data/publications_pubmed_sync.json`.")
        lines.append(
            "- Re-run `--dry-run` later to catch newly flagged conflicts as PubMed records evolve."
        )
    else:
        lines.append("This was a DRY RUN. To apply:")
        lines.append("")
        lines.append(
            "1. Copy `qc/pubmed_sync_decisions.template.yml` to `qc/pubmed_sync_decisions.yml`."
        )
        lines.append("2. Walk the **Would-flag** section above. For each flag, fill in:")
        lines.append(
            "   - `decision: keep_yaml` (and a `reason:`) if YAML is correct — silences this flag forever."
        )
        lines.append(
            "   - `decision: apply_pubmed` if PubMed is correct — writes its value to YAML."
        )
        lines.append("   - Leave `decision:` blank to defer to a later session (skipped silently).")
        lines.append(
            "3. Re-run: `.venv/bin/python -m cv_editor.pubmed_sync --apply --decisions qc/pubmed_sync_decisions.yml`"
        )
        lines.append("")
        lines.append(
            "Per Gate 2 evidence, YAML usually wins on author / title / journal disagreements."
        )
    path.write_text("\n".join(lines) + "\n")


# ----- Main -----


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "PubMed enrichment + sync tracking (Gate 3). "
            "Two-phase workflow: --dry-run (default) → walk the report → fill "
            "qc/pubmed_sync_decisions.yml → re-run with --apply --decisions."
        ),
        epilog=("Exit codes: 0 success; 2 PubMed fetch failed; 3 decisions file validation error."),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help=(
            "Fetch + report only; no writes to data files. THIS IS THE DEFAULT. "
            "Use this first, walk qc/pubmed_sync_report.md, then re-run --apply."
        ),
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply auto-fills to publications.yml + record decisions in "
            "the sidecar. Use after walking the dry-run report."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore TTL; refresh every PMID. Overrides --only-stale and --only-epub. "
            "On a dry-run also re-resolves every DOI-only entry immediately "
            "(ignores the DOI-resolve TTL)."
        ),
    )
    p.add_argument(
        "--only-stale",
        action="store_true",
        help="(default) Refresh only entries past TTL.",
    )
    p.add_argument(
        "--only-epub",
        action="store_true",
        help=(
            "Only refresh entries currently marked epub/ahead-of-print "
            "(faster cadence). Composes with --force to refresh ALL epub "
            "entries regardless of TTL."
        ),
    )
    p.add_argument(
        "--ttl-days",
        type=int,
        default=None,
        help=(
            "Override TTL (days) for ppublish entries only. Epub TTL stays "
            "at 14d; use --force --only-epub for expedited epub checks."
        ),
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass .cache/pubmed/ on this run.",
    )
    p.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help=(
            "Path to a decisions YAML file (typically "
            "qc/pubmed_sync_decisions.yml). Only honored with --apply."
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr progress lines (errors and warnings still print).",
    )
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


@dataclass
class _DryRunResult:
    """In-process dry-run output. Used by `main()` for the CLI path AND
    by the V19 editor surface for the triage page. Avoids re-implementing
    the diff loop in two places."""

    header: list
    data: object
    sch: dict
    state: SidecarState
    decisions: list[EntryDecision]
    skipped_no_pmid: list[tuple[int, str, str]]
    skipped_in_ttl: int
    fetch_errors: list[tuple[str, str]]
    all_yaml_pmids: set[str]
    fetched_pmids: list[str]
    # DOI→PMID backfill (2026-07-11). `resolved` rows feed write_report's
    # provenance section: (global_idx, title, doi, pmid, overlap).
    # `resolution_changed` tells main() to persist the sidecar even when
    # nothing else changed. Both DEFAULTED so the ~6 hand-constructed
    # _DryRunResult(...) call sites in tests keep working.
    resolved: list[tuple[int, str, str, str, float]] = field(default_factory=list)
    resolution_changed: bool = False


def compute_decisions(
    *,
    only_epub: bool = False,
    force: bool = False,
    resolve_dois: bool = False,
    force_resolve: bool = False,
    ttl_days: int | None = None,
    no_cache: bool = False,
    log=None,
    now: datetime | None = None,
    pubs_path: Path | None = None,
    sidecar_path: Path | None = None,
    cache_dir: Path | None = None,
) -> _DryRunResult:
    """Run a single dry-run pass and return the structured result.

    Pure read with respect to YAML and the sidecar — the caller decides
    whether to write the report, the decisions template, or update the
    sidecar. Side-effect: warms the PubMed `.cache/pubmed/` directory
    (unless `no_cache=True`).

    Defaults match the CLI's --dry-run flags. The editor (V19) supplies
    explicit kwargs.

    `resolve_dois` (default False — safe) gates the DOI→PMID backfill:
    when True, DOI-only non-preprint entries that are due (TTL, or `force`)
    get a LIVE, cache-bypassed esearch to discover a newly-assigned PMID.
    Only the background CLI dry-run passes `resolve_dois=True`; the editor
    request-thread caller leaves it False so no uncached network call ever
    happens on the hot path. The "already resolved in the sidecar" branch
    runs regardless of `resolve_dois`, so `--apply` (and the editor's
    read path) still surface + write what a prior dry-run resolved.

    `force_resolve` (the user's explicit `--force`, NOT the dry-run's
    implicit `force=True`) ignores DOI_RESOLVE_TTL_DAYS and re-esearches
    every due DOI-only entry. It is DISTINCT from `force` on purpose:
    main() inflates `force` to True on every dry-run (to re-diff known
    PMIDs from warm cache), so keying DOI re-resolution on `force` would
    silently defeat the TTL. See gotcha #81.
    """
    log = log or (lambda *a, **kw: None)
    now = now or datetime.now(timezone.utc)
    pubs_path = pubs_path or PUBS_PATH
    sidecar_path = sidecar_path or SIDECAR_PATH
    cache_dir = cache_dir or CACHE_DIR

    header, data = yaml_io.load(pubs_path)
    sch = schemas.SCHEMAS["publications"]
    flat = list(sections.flatten(data, sch["structure"]))
    log(f"[sync] loaded {len(flat)} publications")

    state = load_sidecar(sidecar_path)
    log(
        f"[sync] sidecar: {len(state.entries)} tracked PMIDs, "
        f"{len(state.no_pmid_skip_log)} no-PMID skips"
    )

    ttl_overrides = {"ppublish": ttl_days} if ttl_days else None
    to_fetch: list[tuple[str, int, dict]] = []
    # DOI-only entries: due for a fresh esearch (to_resolve) vs. already
    # resolved by a prior dry-run (resolved_known — (pmid, doi_key, gidx, entry)).
    to_resolve: list[tuple[str, int, dict]] = []
    resolved_known: list[tuple[str, str, int, dict]] = []
    skipped_no_pmid: list[tuple[int, str, str]] = []
    skipped_in_ttl = 0
    all_yaml_pmids: set[str] = set()

    for row in flat:
        entry = row["entry"]
        gidx = row["global_idx"]
        pmid = str(entry.get("pmid") or "").strip()
        if pmid:
            all_yaml_pmids.add(pmid)
        if not pmid:
            doi = str(entry.get("doi") or "").strip()
            if not doi or preprint.is_preprint(entry):
                # Genuine no-DOI, or a preprint (the promote flow owns those —
                # never stamp a preprint-pilot PMID onto a preprint entry).
                reason = state.no_pmid_skip_log.get(str(gidx)) or _infer_no_pmid_reason(entry)
                skipped_no_pmid.append((gidx, entry.get("title") or "", reason))
                state.no_pmid_skip_log[str(gidx)] = reason
                continue
            doi_key = doi.lower()
            st = state.doi_resolve_state.get(doi_key)
            if st and st.get("status") == "resolved" and st.get("pmid"):
                resolved_known.append((str(st["pmid"]), doi_key, gidx, entry))
            elif resolve_dois and (
                force_resolve
                or _resolve_due(st.get("last_attempt") if st else None, now, DOI_RESOLVE_TTL_DAYS)
            ):
                to_resolve.append((doi_key, gidx, entry))
            else:
                skipped_no_pmid.append(
                    (gidx, entry.get("title") or "", "DOI-only, re-check pending (in TTL)")
                )
            continue
        record = state.entries.get(pmid)
        if needs_refresh(
            record,
            now=now,
            only_epub=only_epub,
            force=force,
            ttl_overrides=ttl_overrides,
        ):
            to_fetch.append((pmid, gidx, entry))
        else:
            skipped_in_ttl += 1

    log(
        f"[sync] to fetch: {len(to_fetch)}  resolved(known): {len(resolved_known)}  "
        f"to resolve: {len(to_resolve)}  in-TTL: {skipped_in_ttl}  no-PMID: {len(skipped_no_pmid)}"
    )

    decisions: list[EntryDecision] = []
    fetch_errors: list[tuple[str, str]] = []
    fetched_pmids: list[str] = []
    resolved_report: list[tuple[int, str, str, str, float]] = []
    resolution_changed = False
    throttle = HostThrottle(NCBI_HOST_GAP_S)

    # ----- Resolution phase: esearch DOI-only entries that are due -----
    # Background dry-run only (resolve_dois=True). Live + cache-bypassed so
    # a prior "not indexed yet" is never frozen. Builds the fill decision
    # inline from the record already fetched for the DOI round-trip guard
    # (no double fetch).
    if to_resolve:
        log(f"[sync] DOI resolve: {len(to_resolve)} DOI-only entries due for esearch")
        for doi_key, gidx, entry in to_resolve:
            title = entry.get("title") or ""
            throttle.wait("eutils.ncbi.nlm.nih.gov")
            try:
                pmid_found, alternates = pubmed_client.find_pmid_by_doi(
                    doi_key,
                    use_cache=False,
                    cache_dir=cache_dir,
                    ua=UA,
                    polite_sleep=NCBI_HOST_GAP_S,
                    timeout=30,
                    raise_on_error=True,
                )
            except Exception as e:
                # Transient failure — DON'T record an attempt (that would
                # freeze re-checking for the full TTL). Retry next run.
                log(f"[sync]   esearch failed for {doi_key}: {type(e).__name__}; retry next run")
                continue
            now_iso = now.isoformat()
            resolution_changed = True
            if not pmid_found:
                state.doi_resolve_state[doi_key] = {
                    "last_attempt": now_iso,
                    "status": "no_record",
                    "pmid": None,
                    "candidate_pmid": None,
                    "overlap": None,
                }
                skipped_no_pmid.append((gidx, title, "DOI-only, no PubMed record yet"))
                continue
            # Guard 1: DOI→PMID must be 1:1 (alternates = errata/retraction
            # ambiguity). Guard 2: never write a PMID another entry uses.
            if alternates or pmid_found in all_yaml_pmids:
                state.doi_resolve_state[doi_key] = {
                    "last_attempt": now_iso,
                    "status": "needs_review",
                    "pmid": None,
                    "candidate_pmid": pmid_found,
                    "overlap": None,
                }
                skipped_no_pmid.append(
                    (
                        gidx,
                        title,
                        f"DOI matched PMID {pmid_found} but ambiguous/collision; needs manual check",
                    )
                )
                continue
            throttle.wait("eutils.ncbi.nlm.nih.gov")
            rec = pubmed_client.fetch_pubmed_batch(
                [pmid_found],
                use_cache=not no_cache,
                cache_dir=cache_dir,
                ua=UA,
                polite_sleep=NCBI_HOST_GAP_S,
                timeout=30,
            ).get(pmid_found)
            # Guard 3 (PRIMARY): the record's OWN doi must round-trip to ours.
            # Title overlap is only a fallback for records that carry no doi.
            rec_doi = str((rec or {}).get("doi") or "").lower().strip()
            overlap = preprint.title_overlap(title, (rec or {}).get("title") or "")
            if rec is None or not (rec_doi == doi_key or overlap >= TITLE_OVERLAP_MIN):
                state.doi_resolve_state[doi_key] = {
                    "last_attempt": now_iso,
                    "status": "needs_review",
                    "pmid": None,
                    "candidate_pmid": pmid_found,
                    "overlap": round(overlap, 3),
                }
                skipped_no_pmid.append(
                    (
                        gidx,
                        title,
                        f"DOI matched PMID {pmid_found} but could not verify "
                        f"(overlap {overlap:.2f}); needs manual check",
                    )
                )
                continue
            # Verified — record + build the fill decision inline.
            state.doi_resolve_state[doi_key] = {
                "last_attempt": now_iso,
                "status": "resolved",
                "pmid": pmid_found,
                "candidate_pmid": None,
                "overlap": round(overlap, 3),
            }
            dec = diff_one(entry, rec, pmid=pmid_found, global_idx=gidx)
            dec.fills = {"pmid": pmid_found, **dec.fills}
            dec.resolved_from_doi = True
            apply_overrides_to_decision(dec, state.accepted_yaml_overrides.get(pmid_found))
            decisions.append(dec)
            fetched_pmids.append(pmid_found)
            resolved_report.append((gidx, title, doi_key, pmid_found, round(overlap, 3)))

    # ----- Fetch phase: known PMIDs (A) + already-resolved DOI-only (C) -----
    # `doi_key` is None for a normal PMID entry; set for a DOI-resolved one
    # (verified on a prior run) so we inject the pmid fill here.
    batch: list[tuple[str, int, dict, str | None]] = [(p, g, e, None) for (p, g, e) in to_fetch]
    batch += [(p, g, e, dk) for (p, dk, g, e) in resolved_known]
    if batch:
        throttle.wait("eutils.ncbi.nlm.nih.gov")
        pmids = [p for p, _, _, _ in batch]
        log(f"[sync] PubMed efetch: {len(pmids)} PMIDs (batched up to 200/request)")
        pm_recs = pubmed_client.fetch_pubmed_batch(
            pmids,
            use_cache=not no_cache,
            cache_dir=cache_dir,
            ua=UA,
            polite_sleep=NCBI_HOST_GAP_S,
            timeout=30,
        )
        fetched_pmids.extend(pmids)
        for pmid, gidx, entry, doi_key in batch:
            pm_rec = pm_recs.get(pmid)
            if pm_rec is None:
                fetch_errors.append((pmid, "no record returned"))
                continue
            dec = diff_one(entry, pm_rec, pmid=pmid, global_idx=gidx)
            if doi_key is not None:
                dec.fills = {"pmid": pmid, **dec.fills}
                dec.resolved_from_doi = True
                resolved_report.append(
                    (
                        gidx,
                        entry.get("title") or "",
                        doi_key,
                        pmid,
                        float((state.doi_resolve_state.get(doi_key) or {}).get("overlap") or 0.0),
                    )
                )
            apply_overrides_to_decision(
                dec,
                state.accepted_yaml_overrides.get(pmid),
            )
            decisions.append(dec)

    return _DryRunResult(
        header=header,
        data=data,
        sch=sch,
        state=state,
        decisions=decisions,
        skipped_no_pmid=skipped_no_pmid,
        skipped_in_ttl=skipped_in_ttl,
        fetch_errors=fetch_errors,
        all_yaml_pmids=all_yaml_pmids,
        fetched_pmids=fetched_pmids,
        resolved=resolved_report,
        resolution_changed=resolution_changed,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)

    log = (
        (lambda *a, **kw: None)
        if args.quiet
        else (lambda *a, **kw: print(*a, **kw, file=sys.stderr))
    )

    def _relpath(p: Path) -> str:
        """Best-effort relative path; fall back to absolute for tmp-dir tests."""
        try:
            return str(p.relative_to(paths.data_root()))
        except ValueError:
            return str(p)

    # Live-test fix (2026-05-17): dry-run always force=True so
    # `fields_flagged` gets refreshed for EVERY sidecar entry, not just
    # those past TTL. A user who edits YAML to fix a flagged field
    # without running --apply would otherwise leave stale flags lingering
    # forever (the cached sidecar `fields_flagged` is what the editor's
    # entry_view banner reads). With a warm `.cache/pubmed/` the
    # force-fetch is just local cache hits — same cost as the existing
    # behavior. --apply keeps TTL-aware fetching to limit PubMed API
    # calls during the commit phase.
    effective_force = args.force or not args.apply
    try:
        result = compute_decisions(
            only_epub=args.only_epub,
            force=effective_force,
            # DOI resolution happens on the dry-run pass only; apply reads
            # what the dry-run recorded in the sidecar and writes it, but
            # never fires a live esearch. force_resolve is the user's real
            # --force (ignore the 14-day TTL) — NOT effective_force, which
            # is always True on a dry-run and would defeat the TTL.
            resolve_dois=not args.apply,
            force_resolve=args.force,
            ttl_days=args.ttl_days,
            no_cache=args.no_cache,
            log=log,
            now=now,
        )
    except Exception as e:
        print(f"[sync] FATAL: PubMed fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    header = result.header
    data = result.data
    sch = result.sch
    state = result.state
    decisions = result.decisions
    skipped_no_pmid = result.skipped_no_pmid
    skipped_in_ttl = result.skipped_in_ttl
    fetch_errors = result.fetch_errors
    all_yaml_pmids = result.all_yaml_pmids

    # Write report (both modes)
    write_report(
        REPORT_PATH,
        decisions=decisions,
        skipped_no_pmid=skipped_no_pmid,
        skipped_in_ttl=skipped_in_ttl,
        fetch_errors=fetch_errors,
        state_before=state,
        args=args,
        all_yaml_pmids=all_yaml_pmids,
        resolved=result.resolved,
    )
    log(f"[sync] report: {_relpath(REPORT_PATH)}")

    fillable = sum(1 for d in decisions if d.fills)
    flaggable = sum(1 for d in decisions if d.flags)
    log(
        f"[sync] decisions: {fillable} would-fill, {flaggable} would-flag, "
        f"{len(decisions) - fillable - flaggable} unchanged"
    )

    if not args.apply:
        # Write a decisions template next to the report so the user has
        # a one-line-per-flag form to fill in.
        template_path = REPORT_PATH.with_name("pubmed_sync_decisions.template.yml")
        write_decisions_template(template_path, decisions)
        log(f"[sync] decisions template: {_relpath(template_path)}")

        # Live-test fix (2026-05-17): refresh `fields_flagged` on the
        # sidecar even in dry-run mode. It's observation, not action —
        # not writing it stranded users who edited YAML directly to fix
        # a flagged field but never re-ran --apply, leaving the banner
        # on entry_view promising a triage row that no longer exists.
        # `synced_at` stays untouched so TTL semantics survive.
        sidecar_refreshes = 0
        for dec in decisions:
            prev = state.entries.get(dec.pmid)
            prev_synced_at = prev.synced_at if prev else ""
            prev_filled = list(prev.fields_filled) if prev else []
            new_flagged = list(dec.flags.keys())
            new_pubmed_status = dec.publication_status or (prev.pubmed_status if prev else "")
            # V13-V19-D R2-H3 (2026-05-17): also compare pubmed_status so
            # an epub→ppublish transition forces a sidecar refresh. Without
            # this, --only-epub on a transitioned entry kept its old
            # epublish status forever (since the apply path's update never
            # fires for in-TTL entries the user doesn't triage).
            if (
                prev
                and list(prev.fields_flagged or []) == new_flagged
                and prev.pubmed_status == new_pubmed_status
            ):
                continue  # no change; preserve record verbatim
            state.entries[dec.pmid] = EntryRecord(
                synced_at=prev_synced_at,  # preserve TTL — dry-run doesn't extend it
                pubmed_status=new_pubmed_status,
                fields_filled=prev_filled,  # dry-run never auto-fills
                fields_flagged=new_flagged,
                yaml_idx_at_sync=dec.global_idx,
            )
            sidecar_refreshes += 1
        # Persist when fields_flagged changed OR the DOI-resolution phase
        # mutated doi_resolve_state (recorded an attempt / a resolution /
        # a needs-review) — the TTL throttle + auto-fill both depend on
        # that state surviving to the next run.
        if sidecar_refreshes or result.resolution_changed:
            save_sidecar(SIDECAR_PATH, state)
            log(
                f"[sync] sidecar fields_flagged refreshed for "
                f"{sidecar_refreshes} entr{'y' if sidecar_refreshes == 1 else 'ies'}"
                + ("; doi_resolve_state updated" if result.resolution_changed else "")
            )
        log("[sync] DRY RUN — no YAML writes. Re-run with --apply to commit fills.")
        return 0

    # ----- Apply path -----
    # Load + validate decisions file (if provided) before any writes.
    keep_yaml_decs: list[Decision] = []
    apply_pubmed_decs: list[Decision] = []
    if args.decisions is not None:
        try:
            user_decisions = load_decisions(args.decisions)
        except DecisionsFileError as e:
            print(
                f"[sync] FATAL: {e}\n"
                f"[sync]   Fix the file at {args.decisions} and re-run with the "
                f"same --apply --decisions arguments. No YAML or sidecar writes "
                f"happened.",
                file=sys.stderr,
            )
            return 3
        for d in user_decisions:
            if d.decision == DECISION_KEEP_YAML:
                keep_yaml_decs.append(d)
            else:
                apply_pubmed_decs.append(d)
        log(
            f"[sync] decisions: {len(keep_yaml_decs)} keep_yaml, "
            f"{len(apply_pubmed_decs)} apply_pubmed"
        )

    fields_written = apply_fills(data, sch, decisions)
    fields_written += apply_pubmed_decisions(data, sch, decisions, apply_pubmed_decs, state=state)
    if fields_written == 0:
        log(
            "[sync] no new YAML writes needed (auto-fills already present; "
            "any apply_pubmed decisions matched existing values). Refreshing "
            "sidecar with current sync timestamps."
        )
    else:
        log(f"[sync] applying {fields_written} field writes via yaml_io.write_with_backup")
        mtime = yaml_io.mtime_ns(PUBS_PATH) if hasattr(yaml_io, "mtime_ns") else None
        backup = yaml_io.write_with_backup(
            PUBS_PATH,
            header,
            data,
            expected_mtime_ns=mtime,
        )
        log(f"[sync] wrote {_relpath(PUBS_PATH)}; backup at {backup.name}")

    # Update sidecar for every decision (filled or not).
    # Live-test fix (2026-05-17): subtract fields handled by THIS run's
    # decisions when writing fields_flagged. Previously, accepting a
    # keep_yaml in this run would still leave the just-silenced field
    # listed in fields_flagged, because record_keep_yaml_overrides runs
    # AFTER this loop. The entry_view banner then promised a triage row
    # that the next dry-run wouldn't actually surface (because
    # apply_overrides_to_decision would silence it). Same logic for
    # apply_pubmed_decs — those fields now match PubMed, so they're not
    # disagreements anymore.
    handled_per_pmid: dict[str, set[str]] = {}
    for d in keep_yaml_decs:
        handled_per_pmid.setdefault(d.pmid, set()).add(d.field)
    for d in apply_pubmed_decs:
        handled_per_pmid.setdefault(d.pmid, set()).add(d.field)

    iso_now = now.isoformat(timespec="seconds")
    for dec in decisions:
        handled = handled_per_pmid.get(dec.pmid, set())
        fields_flagged_now = [f for f in dec.flags.keys() if f not in handled]
        state.entries[dec.pmid] = EntryRecord(
            synced_at=iso_now,
            pubmed_status=dec.publication_status or "",
            fields_filled=list(dec.fills.keys()),
            fields_flagged=fields_flagged_now,
            yaml_idx_at_sync=dec.global_idx,
        )

    # Record keep_yaml overrides into the sidecar (post-fill so the
    # snapshot reflects the YAML state the user actually approved).
    skipped_decisions: list[tuple[Decision, str]] = []
    n_overrides = record_keep_yaml_overrides(
        state,
        decisions,
        keep_yaml_decs,
        iso_now=iso_now,
        on_skip=lambda d, reason: skipped_decisions.append((d, reason)),
    )
    if n_overrides:
        log(f"[sync] recorded {n_overrides} accepted overrides in sidecar")
    for d, reason in skipped_decisions:
        # Always print these to stderr (not gated on --quiet) so the
        # user catches typos / stale decisions files.
        print(
            f"[sync] WARNING: decision for PMID {d.pmid} field '{d.field}' not recorded: {reason}",
            file=sys.stderr,
        )

    save_sidecar(SIDECAR_PATH, state)
    log(
        f"[sync] sidecar: {_relpath(SIDECAR_PATH)} updated "
        f"({len(state.entries)} entries, "
        f"{sum(len(v) for v in state.accepted_yaml_overrides.values())} overrides)"
    )
    return 0


def _infer_no_pmid_reason(entry: dict) -> str:
    if entry.get("preprint_doi"):
        return "preprint (has preprint_doi, no PMID)"
    if entry.get("doi"):
        return "DOI-only (likely report / monograph)"
    return "no PMID, no DOI"


if __name__ == "__main__":
    sys.exit(main())
