"""Shared PubMed / Crossref / NCBI ID Converter client.

Single source of truth for the three external APIs we hit when reading
publication metadata. Extracted from two near-identical copies in
``scripts/qc_publications.py`` and ``scripts/cv_editor/enrichment.py``;
also used by ``scripts/pubmed_sync.py`` (Gate 3).

Callers pass ``ua=``, ``cache_dir=``, ``timeout=``, ``polite_sleep=`` to
control politeness + caching. Defaults are generic per the no-PII rule
(``DEFAULT_UA = 'cv-pubmed-client/1.0'`` — no email, no name, no
affiliation).

The cache is SHA256-keyed on the request URL. Successful responses are
written to ``<cache_dir>/<key>.txt``; failures are NOT cached (failed
responses should retry next run, never become poison-pill hits).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from cv_editor import paths

ROOT = paths.data_root()  # workspace
DEFAULT_CACHE_DIR = paths.cache_dir() / "pubmed"


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, DEFAULT_CACHE_DIR
    ROOT = paths.data_root()
    DEFAULT_CACHE_DIR = paths.cache_dir() / "pubmed"


DEFAULT_UA = "cv-pubmed-client/1.0"
DEFAULT_TIMEOUT_S = 30
DEFAULT_POLITE_SLEEP_S = 0.4


# ----- HTTP cache -----


def http_get_cached(
    url: str,
    *,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ua: str = DEFAULT_UA,
    polite_sleep: float = DEFAULT_POLITE_SLEEP_S,
) -> str:
    cdir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
    cdir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_path = cdir / f"{key}.txt"
    if use_cache and cache_path.exists():
        return cache_path.read_text()
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    cache_path.write_text(text)
    time.sleep(polite_sleep)
    return text


# ----- Helpers -----


def _month_to_int(m: str):
    if not m:
        return None
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    s = m.strip()[:3].lower()
    if s in months:
        return months[s]
    try:
        return int(s)
    except ValueError:
        return None


# ----- PubMed -----


def fetch_pubmed_batch(
    pmids: list[str],
    *,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ua: str = DEFAULT_UA,
    polite_sleep: float = DEFAULT_POLITE_SLEEP_S,
    batch_size: int = 200,
) -> dict[str, dict]:
    """Return ``{pmid: parsed_record}`` for the given PMIDs.

    Batches up to ``batch_size`` PMIDs per request (PubMed efetch limit).
    """
    out: dict[str, dict] = {}
    pmids = [str(p) for p in pmids if p]
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            f"?db=pubmed&id={','.join(batch)}&retmode=xml"
        )
        xml_text = http_get_cached(
            url,
            use_cache=use_cache,
            cache_dir=cache_dir,
            timeout=timeout,
            ua=ua,
            polite_sleep=polite_sleep,
        )
        root = ET.fromstring(xml_text)
        for art in root.findall("PubmedArticle"):
            pmid = art.findtext(".//PMID")
            if pmid:
                out[pmid] = parse_pubmed_article(art)
    return out


def parse_pubmed_article(art) -> dict:
    """Parse a ``<PubmedArticle>`` XML element into a flat dict.

    Surfaces month/day (None if absent), CollectiveName authors (corporate
    /consortium), and publication_status (``ppublish`` / ``aheadofprint``
    / ``epublish`` / etc. — used by Gate 3 TTL routing).
    """
    rec: dict = {}
    rec["title"] = (art.findtext(".//ArticleTitle") or "").strip()
    rec["journal_full"] = (art.findtext(".//Journal/Title") or "").strip()
    rec["journal_iso"] = (art.findtext(".//Journal/ISOAbbreviation") or "").strip()
    rec["volume"] = (art.findtext(".//JournalIssue/Volume") or "").strip()
    rec["issue"] = (art.findtext(".//JournalIssue/Issue") or "").strip()
    rec["pages"] = (art.findtext(".//MedlinePgn") or "").strip()
    raw_year = art.findtext(".//JournalIssue/PubDate/Year") or (
        art.findtext(".//JournalIssue/PubDate/MedlineDate") or ""
    )
    rec["year"] = (raw_year or "").strip()[:4]
    raw_month = art.findtext(".//JournalIssue/PubDate/Month") or ""
    rec["month"] = _month_to_int(raw_month)
    raw_day = art.findtext(".//JournalIssue/PubDate/Day") or ""
    try:
        rec["day"] = int(raw_day) if raw_day else None
    except ValueError:
        rec["day"] = None
    authors = []
    for a in art.findall(".//AuthorList/Author"):
        last = (a.findtext("LastName") or "").strip()
        init = (a.findtext("Initials") or "").strip()
        col = a.findtext("CollectiveName")
        if last:
            authors.append(f"{last} {init}".strip())
        elif col:
            authors.append(col.strip())
    rec["authors"] = authors
    rec["doi"] = ""
    rec["pmcid"] = ""
    # The article's OWN IDs live in PubmedData/ArticleIdList. The deeper
    # ReferenceList carries cited-reference IDs — those must NOT be
    # picked up.
    for ai in art.findall("./PubmedData/ArticleIdList/ArticleId"):
        t = ai.get("IdType")
        if t == "doi" and not rec["doi"]:
            rec["doi"] = (ai.text or "").strip()
        elif t == "pmc" and not rec["pmcid"]:
            rec["pmcid"] = (ai.text or "").strip()
    # Publication status drives Gate 3's TTL logic (aheadofprint TTL is
    # shorter because the record actively transitions to ppublish).
    rec["publication_status"] = (art.findtext("./PubmedData/PublicationStatus") or "").strip()
    return rec


def find_pmid_by_doi(
    doi: str,
    *,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ua: str = DEFAULT_UA,
    polite_sleep: float = DEFAULT_POLITE_SLEEP_S,
    raise_on_error: bool = False,
) -> tuple[str | None, list[str]]:
    """Resolve a DOI to its PubMed PMID via esearch.

    Returns ``(best_pmid, alternates)``. Errata and retractions can
    surface multiple hits; the caller decides how to handle ambiguity.

    By default a network/parse failure is swallowed to ``(None, [])``
    (back-compat for the import flow). Pass ``raise_on_error=True`` to
    re-raise instead, so a caller can distinguish a transient failure
    from a conclusive "no record" (a genuine empty idlist still returns
    ``(None, [])``). pubmed_sync's DOI-resolution loop needs this so a
    network blip does not record a TTL attempt and freeze re-checking.
    """
    if not doi:
        return (None, [])
    term = urllib.parse.quote(f"{doi}[doi]")
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={term}&retmode=json&retmax=10"
    )
    try:
        text = http_get_cached(
            url,
            use_cache=use_cache,
            cache_dir=cache_dir,
            timeout=timeout,
            ua=ua,
            polite_sleep=polite_sleep,
        )
        result = json.loads(text).get("esearchresult", {})
    except Exception:
        if raise_on_error:
            raise
        return (None, [])
    ids = result.get("idlist") or []
    if not ids:
        return (None, [])
    return (ids[0], ids[1:])


# ----- Crossref -----


def fetch_crossref(
    doi: str,
    *,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ua: str = DEFAULT_UA,
    polite_sleep: float = DEFAULT_POLITE_SLEEP_S,
) -> dict | None:
    if not doi:
        return None
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    try:
        text = http_get_cached(
            url,
            use_cache=use_cache,
            cache_dir=cache_dir,
            timeout=timeout,
            ua=ua,
            polite_sleep=polite_sleep,
        )
        msg = json.loads(text).get("message", {})
    except Exception:
        return None
    rec: dict = {}
    rec["title"] = (msg.get("title") or [""])[0]
    rec["journal_full"] = (msg.get("container-title") or [""])[0]
    rec["journal_iso"] = (
        (msg.get("short-container-title") or [""])[0] if msg.get("short-container-title") else ""
    )
    rec["volume"] = msg.get("volume") or ""
    rec["issue"] = msg.get("issue") or ""
    rec["pages"] = msg.get("page") or ""
    parts = (msg.get("issued", {}).get("date-parts") or [[""]])[0]
    rec["year"] = str(parts[0]) if parts and parts[0] else ""
    rec["month"] = parts[1] if len(parts) > 1 else None
    rec["day"] = parts[2] if len(parts) > 2 else None
    authors = []
    for a in msg.get("author", []) or []:
        last = a.get("family") or ""
        given = a.get("given") or ""
        initials = "".join(p[0] for p in re.split(r"[\s\-\.]+", given) if p)
        if last:
            authors.append(f"{last} {initials}".strip())
    rec["authors"] = authors
    rec["doi"] = (msg.get("DOI") or "").lower()
    rec["pmcid"] = ""  # Crossref doesn't reliably carry PMCID
    return rec


# ----- NCBI ID Converter -----


def convert_ids(
    seed: str,
    *,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ua: str = DEFAULT_UA,
    polite_sleep: float = DEFAULT_POLITE_SLEEP_S,
) -> dict | None:
    if not seed:
        return None
    url = (
        "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?ids={urllib.parse.quote(str(seed))}&format=json"
    )
    try:
        text = http_get_cached(
            url,
            use_cache=use_cache,
            cache_dir=cache_dir,
            timeout=timeout,
            ua=ua,
            polite_sleep=polite_sleep,
        )
        j = json.loads(text)
    except Exception:
        return None
    recs = j.get("records") or []
    if not recs:
        return None
    r = recs[0]
    return {
        "pmid": (r.get("pmid") or "").strip() or None,
        "pmcid": (r.get("pmcid") or "").strip() or None,
        "doi": (r.get("doi") or "").strip().lower() or None,
    }
