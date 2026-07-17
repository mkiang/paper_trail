"""ORCID public-API importer — DISCOVERY ONLY (M5 5b, CP5a).

Pulls a researcher's works from the ORCID public API to DISCOVER DOIs/PMIDs, then
the EXISTING import pipeline (PubMed/Crossref canonical -> enrich -> staging ->
write) ingests them. ORCID metadata NEVER becomes YAML directly; this module only
yields bare {doi, pmid, title} refs. Pure + Flask-free + network only in
`fetch_works` + WRITE-FREE — the dry-run CLI and (later) the import-tab both build
on these functions.

OUTBOUND SAFETY (no-PII rule, gotcha #14): exactly `GET
https://pub.orcid.org/v3.0/{orcid-id}/works` via `url_helpers.safe_urlopen`
(SSRF-guarded; public host passes), headers `User-Agent: cv-editor/1.0` +
`Accept: application/json`. NO `Authorization`, NO OAuth client id/secret, NO
`From:`, NO `?email=`/`?mailto=` — even though ORCID's docs invite an identified
client. The `{orcid-id}` in the path is the lookup SUBJECT (like a DOI in a
Crossref path), not requester metadata.

JSON SHAPE (verified live against pub.orcid.org/v3.0): the response is
`{"group": [{"external-ids": {"external-id": [...]}, "work-summary": [...]}]}`.
Read the GROUP-LEVEL `external-ids` (ORCID's merged/deduped union across the
group's summaries) — NOT `work-summary[0]`, which carries only one summary's ids
and would silently drop a DOI/PMID living on an alternate summary. Per id:
`external-id-type` (lowercase: doi/pmid/pmc/source-work-id/other-id/...),
`external-id-value`, `external-id-normalized.value` (prefer this — strips URL/`doi:`
prefixes), `external-id-relationship` (only `self` is THIS work's id; `part-of` is
the container's, must be excluded).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from cv_editor import url_helpers

UA = "cv-editor/1.0"  # no-PII (gotcha #14); ORCID needs no key for the public API
ORCID_BASE = "https://pub.orcid.org/v3.0"
# 16 digits in four groups; the final char may be an uppercase X checksum digit.
_ORCID_RE = re.compile(r"^(\d{4}-){3}\d{3}[\dX]$")
# Only these external-id types seed the existing DOI/PMID enrich path. Everything
# else ORCID carries (source-work-id, other-id, pmc, eid, isbn, arxiv, ...) is
# ignored for discovery.
_SEED_TYPES = ("doi", "pmid")
_MAX_BYTES = 20_000_000  # ceiling so a pathological response can't balloon memory


def is_valid_orcid_id(orcid_id) -> bool:
    """True if `orcid_id` is a well-formed ORCID iD (format only — a typo that
    stays well-formed just 404s downstream, which the caller handles). The ISO
    7064 mod-11-2 checksum is intentionally NOT validated in v1 (cheap to add
    later; low value for a single-user discovery tool)."""
    return bool(_ORCID_RE.match(str(orcid_id or "")))


@dataclass(frozen=True)
class WorkRef:
    """One discovered work, reduced to the ids the enrich pipeline needs. `title`
    is DISPLAY-ONLY (the dry-run/partition print + the 'add manually' affordance)
    and MUST NEVER be staged into a form — staging always re-enriches from
    PubMed/Crossref (authors-shape guard, gotcha #58)."""

    doi: str | None  # normalized + lowercased (DOIs are case-insensitive)
    pmid: str | None  # bare numeric string
    title: str = ""  # display only
    put_codes: tuple = ()  # ORCID put-codes for the group (provenance)

    @property
    def has_id(self) -> bool:
        return bool(self.doi or self.pmid)


def fetch_works(orcid_id: str, *, urlopen=None, timeout: int = 10) -> dict | None:
    """GET the ORCID public `/works` summary for `orcid_id`. Returns the parsed
    JSON dict, or None on any fetch/parse failure (404 / unknown iD / empty body /
    transport error / non-JSON). Raises ValueError if `orcid_id` is malformed
    (caller should pre-check with `is_valid_orcid_id` for a clean message — this is
    the only thing that catches a typo BEFORE a network call, since ORCID returns
    404 for both malformed and nonexistent iDs).

    `urlopen` is injectable for tests (default: `url_helpers.safe_urlopen`, the
    SSRF-safe seam). The request carries the no-PII UA + Accept:json and NOTHING
    else (no Authorization, no query string)."""
    if not is_valid_orcid_id(orcid_id):
        raise ValueError(f"malformed ORCID iD: {orcid_id!r}")
    opener = urlopen or url_helpers.safe_urlopen
    req = urllib.request.Request(
        f"{ORCID_BASE}/{orcid_id}/works",
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read(_MAX_BYTES)
        return json.loads(body)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # URLError covers HTTPError (404 = unknown/empty iD) + the SSRF block;
        # ValueError covers a non-JSON body. Degrade to None; the caller flashes.
        return None


def _pick_id(eids: list, id_type: str) -> str | None:
    """First `self`-relationship id of `id_type` in a group's external-id list,
    preferring the normalized value. Returns None if absent."""
    for eid in eids:
        if (eid.get("external-id-type") or "").lower() != id_type:
            continue
        if (eid.get("external-id-relationship") or "self").lower() != "self":
            continue  # `part-of`/`version-of` is the container's id, not this work's
        norm = (eid.get("external-id-normalized") or {}).get("value")
        val = (norm or eid.get("external-id-value") or "").strip()
        if val:
            return val
    return None


def _strip_doi_prefix(doi: str) -> str:
    low = doi.lower()
    for pre in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if low.startswith(pre):
            return doi[len(pre) :]
    return doi


def extract_external_ids(works_json: dict) -> list[WorkRef]:
    """One WorkRef per ORCID group, reading the GROUP-LEVEL merged external-ids.
    DOI is preferred over PMID; only `self`-relationship doi/pmid ids count. A group
    whose only ids are non-seed types (source-work-id/other-id/pmc/...) yields a
    WorkRef with no id (-> the 'add manually' bucket at partition time). Duplicate
    DOIs/PMIDs across groups are collapsed to the first occurrence."""
    refs: list[WorkRef] = []
    seen_doi: set[str] = set()
    seen_pmid: set[str] = set()
    for group in works_json.get("group") or []:
        eids = ((group.get("external-ids") or {}).get("external-id")) or []
        raw_doi = _pick_id(eids, "doi")
        pmid = _pick_id(eids, "pmid")
        doi = _strip_doi_prefix(raw_doi).lower() if raw_doi else None
        pmid = str(pmid) if pmid else None
        summaries = group.get("work-summary") or []
        title = ""
        if summaries:
            title = (
                ((summaries[0].get("title") or {}).get("title") or {}).get("value") or ""
            ).strip()
        put_codes = tuple(s.get("put-code") for s in summaries if s.get("put-code") is not None)
        # Cross-group dedup (a DOI/PMID can appear in several groups). A PMID
        # uniquely identifies a paper, so a repeat PMID is the SAME work even if
        # this group also carries a (new-looking) DOI — dedup on EITHER id, not
        # just the DOI, or a bare-PMID group followed by a DOI+PMID group for the
        # same paper double-emits (post-impl review, 2026-05-30). First occurrence
        # wins; a bare-PMID ref re-enriches to the same record + back-fills the DOI.
        if doi and doi in seen_doi:
            continue
        if pmid and pmid in seen_pmid:
            continue
        if doi:
            seen_doi.add(doi)
        if pmid:
            seen_pmid.add(pmid)
        refs.append(WorkRef(doi=doi, pmid=pmid, title=title, put_codes=put_codes))
    return refs


@dataclass
class Partition:
    """Dry-run discovery result. `new` = importable + not already in the CV;
    `in_cv` = the DOI/PMID already exists; `no_id` = ORCID gave no usable DOI/PMID
    (user adds manually). Read-only — no enrichment, no writes."""

    new: list = field(default_factory=list)
    in_cv: list = field(default_factory=list)
    no_id: list = field(default_factory=list)


def _cv_id_index(existing_entries) -> tuple[set, set]:
    """(lowercased DOIs, str PMIDs) present in the CV. gotcha #33: the corpus has
    uppercase DOI suffixes, so dedup is case-insensitive on the DOI; PMIDs are
    sometimes quoted strings + sometimes int-coerced, so compare as str."""
    dois, pmids = set(), set()
    for e in existing_entries or []:
        d = (e.get("doi") or "").strip().lower()
        if d:
            dois.add(d)
        p = str(e.get("pmid") or "").strip()
        if p:
            pmids.add(p)
    return dois, pmids


def partition_against_cv(refs: list[WorkRef], existing_entries) -> Partition:
    """Split discovered refs into new / already-in-CV / no-id against the loaded,
    flattened publication entries. PURE — caller loads + flattens publications.yml
    (Flask-free); this never touches the filesystem or the network."""
    cv_dois, cv_pmids = _cv_id_index(existing_entries)
    part = Partition()
    for r in refs:
        if not r.has_id:
            part.no_id.append(r)
        elif (r.doi and r.doi in cv_dois) or (r.pmid and r.pmid in cv_pmids):
            part.in_cv.append(r)
        else:
            part.new.append(r)
    return part
