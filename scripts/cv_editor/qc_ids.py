"""Stable identifier scheme for QC findings (V23-B Phase 0, 2026-05-25).

QC findings need IDs that are:
  - STABLE across YAML edits that don't touch the finding's content
    (insert/delete/reorder of OTHER entries; idx shifts must not break IDs).
  - DIFFERENT when the finding's canonical value drifts (so a stale
    `keep_yaml` decision doesn't silently silence a re-surfaced finding
    with a different canonical value).

Format: <TYPE-PREFIX>:<source>:<entity>[:<field>][:<hash8>]

The key invariant: an ID encodes (entity-identity, finding-identity,
canonical-content-hash). Two of the three (entity + finding) stay stable
across realistic edits; the third (canonical-content-hash) deliberately
changes when the upstream source changes, so a "keep_yaml" decision
recorded against an old canonical doesn't silently silence a re-surfaced
finding with a new canonical.

Entity-identity preference: PMID > DOI (hashed) > title (hashed). PMID is
the most stable because it never changes once issued; DOI is stable but
longer + has special chars; title is the last-resort fallback (used by
missing_ids findings that have no seed ID at all).

Why NOT use global_idx as part of the discriminator: inserting any
publication shifts every downstream idx. A decision tied to
`global_idx=42` would silently mismatch the next sweep where the same
finding lives at `global_idx=43`. See V23-B plan / correctness reviewer
H2.
"""

from __future__ import annotations

import hashlib
from typing import Optional


def hash8(s: str) -> str:
    """First 8 hex chars of sha256(s). Deterministic, collision-safe at
    this scale (2^32 buckets; we have <100 findings per sweep)."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:8]


def _entity_key(
    *,
    pmid: Optional[str] = None,
    doi: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Stable per-entry identity. PMID preferred (most stable); fall back
    to DOI hash, then to a hash of the (lowercased, stripped) title."""
    if pmid:
        return str(pmid).strip()
    if doi:
        return f"doi:{hash8(str(doi).strip().lower())}"
    if title:
        return f"title:{hash8((title or '').strip().lower())}"
    raise ValueError("entity has no PMID, DOI, or title")


def mismatch_id(
    *,
    source: str,
    field: str,
    canonical: str,
    pmid: Optional[str] = None,
    doi: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """ID for MISMATCH findings.

    Re-surfaces under a NEW id when the canonical value drifts, so a
    stale decision can't silence a re-surfaced finding with a new
    canonical value (correctness reviewer H2).
    """
    entity = _entity_key(pmid=pmid, doi=doi, title=title)
    return f"MM:{source}:{entity}:{field}:{hash8(canonical or '')}"


def variant_id(
    *,
    source: str,
    field: str,
    canonical: str,
    pmid: Optional[str] = None,
    doi: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """ID for VARIANT findings. Distinct prefix from MISMATCH so a
    severity flip (MISMATCH <-> VARIANT) re-surfaces."""
    entity = _entity_key(pmid=pmid, doi=doi, title=title)
    return f"VA:{source}:{entity}:{field}:{hash8(canonical or '')}"


def id_enrichment_id(
    *,
    suggested_field: str,
    pmid: Optional[str] = None,
    doi: Optional[str] = None,
    pmcid: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """ID for ID_ENRICHMENT findings.

    No canonical-hash because the suggested ID is canonical-by-construction
    (PubMed/Crossref/idconv give the value); if it changed, that's a
    different suggested field.
    """
    entity = _entity_key(pmid=pmid, doi=(doi or pmcid), title=title)
    return f"ID:{entity}:{suggested_field}"


def pmid_mismatch_id(*, pmid: str) -> str:
    """ID for PMID_MISMATCH findings (PubMed returned no record for a
    cited PMID). One such finding per failed PMID."""
    return f"PM:pubmed:{str(pmid).strip()}:no_record"


def author_name_variant_id(*, normalized_key: str) -> str:
    """ID for AUTHOR_NAME_VARIANT cluster. The normalized key IS the
    discriminator; raw forms list is the payload."""
    return f"AN:{normalized_key.lower().strip()}"


def journal_name_variant_id(*, normalized_key: str) -> str:
    """ID for JOURNAL_NAME_VARIANT cluster."""
    return f"JN:{normalized_key.lower().strip()}"


def self_absent_id(
    *,
    pmid: Optional[str] = None,
    doi: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """ID for SELF_ABSENT findings (the CV owner not in the author list)."""
    entity = _entity_key(pmid=pmid, doi=doi, title=title)
    return f"SA:{entity}"


def missing_ids_id(*, title: str, year: Optional[int] = None) -> str:
    """ID for MISSING_IDS findings (entry has no PMID, no DOI, no PMCID).

    Deterministic hash of normalized (title + year). Title is the only
    stable handle when no IDs exist; year disambiguates same-titled
    papers across years.
    """
    norm_title = (title or "").strip().lower()
    year_str = str(year) if year else "noyear"
    return f"MI:{hash8(f'{norm_title}|{year_str}')}"
