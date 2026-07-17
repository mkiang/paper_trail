"""
External-API enrichment for the CV editor (V1b).

The low-level fetch + parse helpers (PubMed efetch, Crossref, NCBI ID
Converter) live in :mod:`cv_editor.pubmed_client`. This module keeps the
editor-specific orchestration:

  - find_pmid_by_doi: PubMed esearch by DOI; returns (pmid, alternates).
  - enrich_via_pmid / enrich_via_doi: parallel-fire APIs with per-call
    timeout + total budget; returns {source: {ok, data|error}}.
  - to_form_entry / merge_canonical_into_form: shape the canonical
    metadata to the editor's form schema (with author-disagreement
    triage for the paste-citation flow).

HTTP cache is shared with QC at typst/.cache/qc/ (preserved here so the
"Find missing IDs" button in the editor benefits from prior QC runs).
"""

from __future__ import annotations

import concurrent.futures as _futures
import time
import unicodedata

from cv_editor import paths, pubmed_client
from cv_editor.author_flags import flag_defaults_dict as _flag_defaults
from cv_editor.pubmed_client import (
    _month_to_int,  # noqa: F401 — re-exported for bibtex_parse._month_to_int
)

ROOT = paths.data_root()  # workspace
CACHE_DIR = paths.cache_dir() / "qc"

UA = "cv-editor/1.0"
DEFAULT_TIMEOUT_S = 10
DEFAULT_TOTAL_BUDGET_S = 15
POLITE_SLEEP_S = 0.1  # editor is interactive; less polite than QC's 0.4

# Default kwargs threaded through every pubmed_client call so the editor
# keeps its own cache dir + UA + politeness profile.
_ED_KW = dict(cache_dir=CACHE_DIR, ua=UA, polite_sleep=POLITE_SLEEP_S)


@paths.on_configure
def _refresh_paths() -> None:
    # _ED_KW captures CACHE_DIR by value, so rebuild it when the root moves.
    global ROOT, CACHE_DIR, _ED_KW
    ROOT = paths.data_root()
    CACHE_DIR = paths.cache_dir() / "qc"
    _ED_KW = dict(cache_dir=CACHE_DIR, ua=UA, polite_sleep=POLITE_SLEEP_S)


# ----- Normalization (subset of qc_publications helpers) -----


def _nfkd(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


# ----- Low-level fetchers (delegate to pubmed_client) -----


def fetch_pubmed_batch(
    pmids: list[str], timeout: float = DEFAULT_TIMEOUT_S, use_cache: bool = True
) -> dict:
    """Return dict {pmid: parsed_record}. Batches up to 200 PMIDs per call."""
    return pubmed_client.fetch_pubmed_batch(
        pmids,
        use_cache=use_cache,
        timeout=timeout,
        **_ED_KW,
    )


def find_pmid_by_doi(
    doi: str, timeout: float = DEFAULT_TIMEOUT_S, use_cache: bool = True
) -> tuple[str | None, list[str]]:
    return pubmed_client.find_pmid_by_doi(
        doi,
        use_cache=use_cache,
        timeout=timeout,
        **_ED_KW,
    )


def fetch_crossref(
    doi: str, timeout: float = DEFAULT_TIMEOUT_S, use_cache: bool = True
) -> dict | None:
    return pubmed_client.fetch_crossref(
        doi,
        use_cache=use_cache,
        timeout=timeout,
        **_ED_KW,
    )


def convert_ids(
    seed: str, timeout: float = DEFAULT_TIMEOUT_S, use_cache: bool = True
) -> dict | None:
    return pubmed_client.convert_ids(
        seed,
        use_cache=use_cache,
        timeout=timeout,
        **_ED_KW,
    )


# ----- Orchestration -----


def _safe(fn, *args, **kw):
    try:
        return {"ok": True, "data": fn(*args, **kw)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def enrich_via_pmid(pmid: str, total_budget_s: float = DEFAULT_TOTAL_BUDGET_S) -> dict:
    """Single-PMID enrichment: fetch PubMed + ID Converter (for PMCID/DOI cross-fill)."""
    out = {"pubmed": None, "idconverter": None}
    deadline = time.time() + total_budget_s
    with _futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = {
            "pubmed": pool.submit(_safe, fetch_pubmed_batch, [pmid]),
            "idconverter": pool.submit(_safe, convert_ids, pmid),
        }
        for name, fut in futs.items():
            try:
                rem = max(0.1, deadline - time.time())
                out[name] = fut.result(timeout=rem)
            except _futures.TimeoutError:
                out[name] = {"ok": False, "error": "timed out"}
    return out


def enrich_via_doi(doi: str, total_budget_s: float = DEFAULT_TOTAL_BUDGET_S) -> dict:
    """DOI enrichment: parallel-fire Crossref + (PubMed search-by-DOI -> efetch) + ID Converter."""
    out = {"crossref": None, "pubmed": None, "idconverter": None}
    deadline = time.time() + total_budget_s

    def pubmed_chain(d):
        pmid, _alts = find_pmid_by_doi(d)
        if not pmid:
            return None
        recs = fetch_pubmed_batch([pmid])
        return recs.get(pmid)

    with _futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            "crossref": pool.submit(_safe, fetch_crossref, doi),
            "pubmed": pool.submit(_safe, pubmed_chain, doi),
            "idconverter": pool.submit(_safe, convert_ids, doi),
        }
        for name, fut in futs.items():
            try:
                rem = max(0.1, deadline - time.time())
                out[name] = fut.result(timeout=rem)
            except _futures.TimeoutError:
                out[name] = {"ok": False, "error": "timed out"}
    return out


# ----- Form-shape projection -----


def to_form_entry(canonical: dict, source: str = "pubmed") -> dict:
    """Convert a canonical PubMed/Crossref record into the editor's form-entry shape."""
    if not canonical:
        return {}
    out = {
        "title": (canonical.get("title") or "").strip().rstrip("."),
        "journal": canonical.get("journal_full") or canonical.get("journal_iso") or "",
        "year": canonical.get("year") or None,
        "month": canonical.get("month"),
        "day": canonical.get("day"),
        "volume": str(canonical.get("volume") or "") or None,
        "issue": str(canonical.get("issue") or "") or None,
        "pages": canonical.get("pages") or None,
        "doi": canonical.get("doi") or None,
        "pmcid": canonical.get("pmcid") or None,
        "authors": [{"name": n, **_flag_defaults()} for n in canonical.get("authors", []) or []],
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def merge_canonical_into_form(
    parsed: dict, enrichment_result: dict, non_author_fields: list[str] | None = None
) -> tuple[dict, dict]:
    """Merge canonical metadata into a parsed-from-paste form entry.

    Per the locked plan: accept canonical for title/journal/year/vol/issue/
    pages/IDs by default, but defer authors to manual review (return both
    parsed and canonical so the form can show side-by-side).

    Returns (merged_form, disagreements_report) where the report is
    {field: {parsed: ..., canonical: ..., source: ...}} for any field where
    parsed != canonical AND canonical is non-empty.
    """
    if non_author_fields is None:
        non_author_fields = [
            "title",
            "journal",
            "year",
            "month",
            "day",
            "volume",
            "issue",
            "pages",
            "doi",
            "pmcid",
        ]
    merged = dict(parsed or {})
    disagree = {}

    canonical = None
    source = None
    pubmed_data = (enrichment_result.get("pubmed") or {}).get("data")
    crossref_data = (enrichment_result.get("crossref") or {}).get("data")
    if pubmed_data:
        canonical = to_form_entry(pubmed_data, "pubmed")
        source = "pubmed"
    elif crossref_data:
        canonical = to_form_entry(crossref_data, "crossref")
        source = "crossref"

    if canonical:
        for field in non_author_fields:
            cv = canonical.get(field)
            pv = merged.get(field)
            if cv in (None, "", []):
                continue
            if pv != cv and pv not in (None, "", []):
                disagree[field] = {"parsed": pv, "canonical": cv, "source": source}
            merged[field] = cv  # accept canonical
        # Authors:
        #   - if parsed has none, just take canonical (DOI/PMID-lookup case).
        #   - if parsed has some and they differ, stage as a disagreement so
        #     the user reviews (citation-paste case where the user typed
        #     authors that may reflect their preferred form).
        if canonical.get("authors"):
            parsed_authors = merged.get("authors") or []
            canonical_authors = canonical["authors"]
            if not parsed_authors:
                merged["authors"] = canonical_authors
            elif [a.get("name") for a in parsed_authors] != [
                a.get("name") for a in canonical_authors
            ]:
                disagree["authors"] = {
                    "parsed": parsed_authors,
                    "canonical": canonical_authors,
                    "source": source,
                }

    # ID enrichment from ID Converter (purely additive).
    conv_data = (enrichment_result.get("idconverter") or {}).get("data") or {}
    for k in ("pmid", "pmcid", "doi"):
        if not merged.get(k) and conv_data.get(k):
            merged[k] = conv_data[k]

    return merged, disagree
