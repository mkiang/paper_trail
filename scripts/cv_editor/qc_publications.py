#!/usr/bin/env python3
"""
QC report for data/publications.yml.

Cross-checks each entry against PubMed (via PMID) and Crossref (via DOI),
flags internal inconsistencies (author name / journal name spellings),
and surfaces ID enrichment opportunities (missing PMID / DOI / PMCID
that can be looked up from another ID).

Usage:
    python3 scripts/qc_publications.py
    python3 scripts/qc_publications.py --no-cache

Output:
    typst/qc/report.md
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cv_editor import paths, pubmed_client, qc_ids
from cv_editor.atomic_json import atomic_write_json  # noqa: E402
from cv_editor.author_names import (  # noqa: E402
    extract_author_name,
    nfkd,
    norm_author_name,
    self_surname_tokens,
)

# Workspace paths from the seam (data/qc/.cache). Real, monkeypatch-able
# module globals — tests do `monkeypatch.setattr(qc_publications,
# "SIDECAR_PATH", ...)` — refreshed by the hook whenever the root moves. A
# fresh subprocess reads the CV_EDITOR_* env at import via these accessors.
DATA = paths.data_dir() / "publications.yml"
CACHE_DIR = paths.cache_dir() / "qc"
QC_DIR = paths.qc_dir()
REPORT_PATH = QC_DIR / "report.md"
SIDECAR_PATH = QC_DIR / "report.json"  # V23-B Phase 0

SIDECAR_SCHEMA_VERSION = 1  # integer for versioned_json.load_versioned
SIDECAR_QC_SCRIPT_VERSION = "1.0"  # human-readable, bumps when output semantics change

UA = "cv-qc-script/1.0"
POLITE_SLEEP_S = 0.4

# Default kwargs threaded through every pubmed_client call so qc keeps
# its own cache dir + UA + politeness profile.
_QC_KW = dict(cache_dir=CACHE_DIR, ua=UA, polite_sleep=POLITE_SLEEP_S)


@paths.on_configure
def _refresh_paths() -> None:
    # _QC_KW captures CACHE_DIR by value, so rebuild it when the root moves
    # (mirrors enrichment._ED_KW). Placed AFTER _QC_KW/UA so the immediate
    # registration-fire doesn't hit an undefined global.
    global DATA, CACHE_DIR, QC_DIR, REPORT_PATH, SIDECAR_PATH, _QC_KW
    DATA = paths.data_dir() / "publications.yml"
    CACHE_DIR = paths.cache_dir() / "qc"
    QC_DIR = paths.qc_dir()
    REPORT_PATH = QC_DIR / "report.md"
    SIDECAR_PATH = QC_DIR / "report.json"
    _QC_KW = dict(cache_dir=CACHE_DIR, ua=UA, polite_sleep=POLITE_SLEEP_S)


def http_get_cached(url, use_cache=True):
    return pubmed_client.http_get_cached(url, use_cache=use_cache, **_QC_KW)


def fetch_pubmed_batch(pmids, use_cache=True):
    return pubmed_client.fetch_pubmed_batch(pmids, use_cache=use_cache, **_QC_KW)


def fetch_crossref(doi, use_cache=True):
    return pubmed_client.fetch_crossref(doi, use_cache=use_cache, **_QC_KW)


def convert_ids(seed, use_cache=True):
    return pubmed_client.convert_ids(seed, use_cache=use_cache, **_QC_KW)


parse_pubmed_article = pubmed_client.parse_pubmed_article


# ----- Normalization helpers -----
# V23-B Phase 1.5 (2026-05-26): nfkd / norm_author_name / extract_author_name
# moved to cv_editor.author_names so cross-system silencing and the QC
# sweep share ONE canonicalizer. Re-imported here for CLI / test
# backward-compat.


def norm_title(t):
    if not t:
        return ""
    t = nfkd(t).lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[\.,;!?]+$", "", t).strip()
    return t


def title_ends_in_period(title) -> bool:
    """True if a title ends in a terminal period (after stripping trailing
    whitespace). Flags only `.`, not `?`/`!` (which are valid title
    punctuation). Abbreviation endings (U.S., et al.) also match and are
    expected false positives — the renderer strips one trailing period so
    none of these render `..`."""
    return (title or "").rstrip().endswith(".")


def norm_journal(j):
    if not j:
        return ""
    j = nfkd(j).lower().strip().rstrip(".")
    j = re.sub(r"\s+", " ", j)
    return j


def normalize_pages(p):
    """Expand abbreviated ranges so '300-12' compares equal to '300-312'."""
    p = str(p).strip().rstrip(".").lower()
    p = re.sub(r"\s+", "", p)
    m = re.match(r"^(\d+)-(\d+)$", p)
    if m:
        start, end = m.group(1), m.group(2)
        if len(end) < len(start):
            end = start[: len(start) - len(end)] + end
        return f"{start}-{end}"
    return p


# ----- PubMed / Crossref / ID Converter -----
# Low-level fetch + parse lives in cv_editor.pubmed_client and is bound
# at the top of this file (fetch_pubmed_batch, parse_pubmed_article,
# fetch_crossref, convert_ids) — kept as module attributes for callers
# that still ``from qc_publications import fetch_pubmed_batch``.


# ----- Diff -----


def diff_entry(entry, ext):
    issues = []
    if not ext:
        return issues

    if entry.get("title") and ext.get("title"):
        if norm_title(entry["title"]) != norm_title(ext["title"]):
            issues.append(("MISMATCH", "title", entry["title"], ext["title"]))

    if entry.get("journal"):
        nj = norm_journal(entry["journal"])
        candidates = [
            norm_journal(ext.get("journal_full", "")),
            norm_journal(ext.get("journal_iso", "")),
        ]
        candidates = [c for c in candidates if c]
        if candidates and nj not in candidates:
            issues.append(
                (
                    "VARIANT",
                    "journal",
                    entry["journal"],
                    ext.get("journal_full") or ext.get("journal_iso") or "",
                )
            )

    if entry.get("year") and ext.get("year"):
        if str(entry["year"]).strip() != str(ext["year"]).strip():
            issues.append(("MISMATCH", "year", str(entry["year"]), str(ext["year"])))

    if entry.get("volume") and ext.get("volume"):
        if str(entry["volume"]).strip() != str(ext["volume"]).strip():
            issues.append(("MISMATCH", "volume", str(entry["volume"]), str(ext["volume"])))

    if entry.get("issue") and ext.get("issue"):
        if str(entry["issue"]).strip() != str(ext["issue"]).strip():
            issues.append(("MISMATCH", "issue", str(entry["issue"]), str(ext["issue"])))

    if entry.get("pages") and ext.get("pages"):
        if normalize_pages(entry["pages"]) != normalize_pages(ext["pages"]):
            issues.append(("VARIANT", "pages", str(entry["pages"]), str(ext["pages"])))

    if entry.get("doi") and ext.get("doi"):
        if str(entry["doi"]).lower().strip() != str(ext["doi"]).lower().strip():
            issues.append(("MISMATCH", "doi", str(entry["doi"]), str(ext["doi"])))

    if entry.get("authors") and ext.get("authors"):
        cited = [norm_author_name(extract_author_name(a)) for a in entry["authors"]]
        canon = [norm_author_name(a) for a in ext["authors"]]
        cited_surnames = [a.split()[0].lower() if a.split() else "" for a in cited]
        canon_surnames = [a.split()[0].lower() if a.split() else "" for a in canon]
        if cited_surnames != canon_surnames:
            sev = "MISMATCH" if len(cited) != len(canon) else "VARIANT"
            issues.append((sev, "authors", "; ".join(cited), "; ".join(canon)))

    return issues


# ----- Phase 0 helper: mark whether an authors diff is length-changing -----
# Authors mismatches with length_changed=True must NOT be silently
# bulk-applied (would drop co-first/co-senior/group markers). Phase 1's
# apply path uses this flag to require explicit per-field confirmation.
def _authors_length_changed(yaml_value: str, canonical_value: str) -> bool:
    """A "; "-joined authors string from diff_entry. Length comparison
    based on number of authors (split count), not character count."""
    y = (yaml_value or "").split("; ") if yaml_value else []
    c = (canonical_value or "").split("; ") if canonical_value else []
    return len(y) != len(c)


# ----- Main -----


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    use_cache = not args.no_cache

    with open(DATA) as f:
        data = yaml.safe_load(f)

    # Self-name detection (M3.2): derive surname tokens from meta.self_bold
    # rather than a hardcoded name, so the QC self-absent check + report honor
    # whatever name the CV is configured for. self_surname_tokens mirrors
    # templates/bespoke/render.typ's string-or-list parsing and returns an empty
    # set when nothing is derivable (an unset self_bold yields no self-name to
    # check against).
    with open(paths.data_dir() / "meta.yml") as f:
        meta = yaml.safe_load(f) or {}
    self_bold = meta.get("self_bold", "")
    self_surnames = self_surname_tokens(self_bold)
    if isinstance(self_bold, str):
        self_bold_label = self_bold.strip() or "the author"
    elif isinstance(self_bold, (list, tuple)) and self_bold:
        self_bold_label = ", ".join(str(t) for t in self_bold)
    else:
        self_bold_label = "the author"

    flat = []
    for s_idx, section in enumerate(data):
        for e_idx, entry in enumerate(section["entries"]):
            flat.append((s_idx, section["subsection"], e_idx, entry))
    print(f"[qc] loaded {len(flat)} entries across {len(data)} subsections", file=sys.stderr)

    pmids = [str(e["pmid"]) for *_, e in flat if e.get("pmid")]
    print(f"[qc] PubMed efetch: {len(pmids)} PMIDs", file=sys.stderr)
    pm_recs = fetch_pubmed_batch(pmids, use_cache=use_cache) if pmids else {}

    doi_only = [e for *_, e in flat if e.get("doi") and not e.get("pmid")]
    print(f"[qc] Crossref: {len(doi_only)} DOI-only entries", file=sys.stderr)
    cr_recs = {}
    for e in doi_only:
        rec = fetch_crossref(e["doi"], use_cache=use_cache)
        if rec:
            cr_recs[e["doi"]] = rec

    mismatches, variants, pmid_mismatches = [], [], []
    for s_idx, sec, e_idx, entry in flat:
        ext, source = None, None
        if entry.get("pmid"):
            ext = pm_recs.get(str(entry["pmid"]))
            source = "PubMed"
            if ext is None:
                # V23-B Phase 0 split: PMID-not-found rows go to their
                # own list so the apply path can't accidentally overwrite
                # a PMID with "(no record returned)". See correctness
                # reviewer H1.
                pmid_mismatches.append(
                    {
                        "subsection": sec,
                        "entry_index": e_idx + 1,
                        "global_idx": s_idx * 10000 + e_idx,  # stable-within-sweep ordering
                        "title_preview": (entry.get("title") or "")[:60],
                        "pmid": str(entry["pmid"]),
                        "reason": "PubMed returned no record for this PMID",
                        "source": "pubmed",
                    }
                )
                continue
        elif entry.get("doi"):
            ext = cr_recs.get(entry["doi"])
            source = "Crossref"
        if ext is None:
            continue
        for sev, field, cited, canon in diff_entry(entry, ext):
            row = {
                "subsection": sec,
                "entry_index": e_idx + 1,
                "global_idx": s_idx * 10000 + e_idx,
                "pmid": str(entry["pmid"]) if entry.get("pmid") else None,
                "doi": entry.get("doi"),
                "title_preview": (entry.get("title") or "")[:60],
                "source": source,
                "field": field,
                "cited": cited,
                "canonical": canon,
            }
            if field == "authors":
                row["length_changed"] = _authors_length_changed(cited, canon)
            (mismatches if sev == "MISMATCH" else variants).append(row)

    # Internal: author name variants
    author_forms = defaultdict(set)
    for *_, entry in flat:
        for a in entry.get("authors") or []:
            name = extract_author_name(a)
            key = norm_author_name(name).lower()
            if key:
                author_forms[key].add(name)
    author_variants = {k: sorted(v) for k, v in author_forms.items() if len(v) > 1}

    # Internal: journal variants
    journal_forms = defaultdict(set)
    for *_, entry in flat:
        j = entry.get("journal")
        if j:
            journal_forms[norm_journal(j)].add(j)
    journal_variants = {k: sorted(v) for k, v in journal_forms.items() if len(v) > 1}

    # ID enrichment
    enrichments = []
    for s_idx, sec, e_idx, entry in flat:
        have = {k: entry.get(k) for k in ("pmid", "doi", "pmcid") if entry.get(k)}
        if not have:
            continue
        if set(have.keys()) == {"pmid", "doi", "pmcid"}:
            continue

        suggested = {}
        # First, harvest from the PubMed record we already have
        if entry.get("pmid"):
            pmrec = pm_recs.get(str(entry["pmid"])) or {}
            if "doi" not in have and pmrec.get("doi"):
                suggested["doi"] = pmrec["doi"]
            if "pmcid" not in have and pmrec.get("pmcid"):
                suggested["pmcid"] = pmrec["pmcid"]

        # If still missing something, query ID converter
        still_missing = set(("pmid", "doi", "pmcid")) - set(have.keys()) - set(suggested.keys())
        if still_missing:
            seed = entry.get("doi") or entry.get("pmid") or entry.get("pmcid")
            conv = convert_ids(seed, use_cache=use_cache)
            if conv:
                for k in still_missing:
                    if conv.get(k):
                        suggested[k] = conv[k]

        if suggested:
            enrichments.append(
                {
                    "subsection": sec,
                    "entry_index": e_idx + 1,
                    "title_preview": (entry.get("title") or "")[:60],
                    "have": have,
                    "suggested": suggested,
                }
            )

    missing_ids = [
        {
            "subsection": sec,
            "entry_index": e_idx + 1,
            "title_preview": (entry.get("title") or "")[:60],
        }
        for s_idx, sec, e_idx, entry in flat
        if not entry.get("pmid") and not entry.get("doi")
    ]

    self_absent = []
    for s_idx, sec, e_idx, entry in flat:
        names = [extract_author_name(a) for a in (entry.get("authors") or [])]
        if not any(any(sn in nfkd(n).lower() for sn in self_surnames) for n in names):
            self_absent.append(
                {
                    "subsection": sec,
                    "entry_index": e_idx + 1,
                    # Post-impl C-H2 follow-up (2026-05-25): self_absent
                    # rows need global_idx for the triage page's
                    # jump-to-edit URL translation (qc_to_seq[global_idx]).
                    "global_idx": s_idx * 10000 + e_idx,
                    "title_preview": (entry.get("title") or "")[:60],
                }
            )

    # Titles ending in a terminal period. The renderer strips one trailing
    # period so it won't render "..", but a stray period is usually a
    # copy-paste artifact worth cleaning. Abbreviation endings (U.S., et al.,
    # Jr.) are expected false positives and render fine. Markdown-report only;
    # not a triage finding type.
    title_punctuation = [
        {
            "subsection": sec,
            "entry_index": e_idx + 1,
            "title_preview": (entry.get("title") or "")[:60],
        }
        for s_idx, sec, e_idx, entry in flat
        if title_ends_in_period(entry.get("title"))
    ]

    ctx = {
        "total": len(flat),
        "with_pmid": len(pmids),
        "with_doi_only": len(doi_only),
        "mismatches": mismatches,
        "variants": variants,
        "pmid_mismatches": pmid_mismatches,
        "author_variants": author_variants,
        "journal_variants": journal_variants,
        "enrichments": enrichments,
        "missing_ids": missing_ids,
        "self_absent": self_absent,
        "title_punctuation": title_punctuation,
        "self_bold_label": self_bold_label,
    }
    write_report(REPORT_PATH, ctx)
    print(f"[qc] wrote {REPORT_PATH}", file=sys.stderr)

    # V23-B Phase 0: emit JSON sidecar for the triage UI.
    write_sidecar(SIDECAR_PATH, ctx, flat_entries=flat)
    print(f"[qc] wrote {SIDECAR_PATH}", file=sys.stderr)


def section_table(lines, title, rows):
    lines.append(f"## {title}\n")
    if not rows:
        lines.append("_None._\n")
        return
    lines.append("| Subsection | # | Title | Source | Field | Cited | Canonical |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        cited_raw = str(r["cited"]).replace("|", "\\|").replace("\n", " ")
        canon_raw = str(r["canonical"]).replace("|", "\\|").replace("\n", " ")
        cited_disp, canon_disp = focus_on_diff(cited_raw, canon_raw, width=80)
        title_short = r["title_preview"].replace("|", "\\|")
        lines.append(
            f"| {r['subsection']} | {r['entry_index']} | {title_short} | "
            f"{r['source']} | {r['field']} | {cited_disp} | {canon_disp} |"
        )
    lines.append("")


def focus_on_diff(a, b, width=80):
    """For a semicolon-separated list (author list), surface only the
    elements that differ. For other fields, truncate around the first
    differing character. Avoids hiding diffs past the truncation point."""
    if "; " in a and "; " in b:
        a_parts = a.split("; ")
        b_parts = b.split("; ")
        diffs = []
        for i in range(max(len(a_parts), len(b_parts))):
            ap = a_parts[i] if i < len(a_parts) else "(missing)"
            bp = b_parts[i] if i < len(b_parts) else "(missing)"
            if ap.lower() != bp.lower():
                diffs.append((i, ap, bp))
        if diffs:
            a_show = "; ".join(f"[{i}] {ap}" for i, ap, _ in diffs)
            b_show = "; ".join(f"[{i}] {bp}" for i, _, bp in diffs)
            return a_show[: width * 2], b_show[: width * 2]
    if a == b:
        return a[:width], b[:width]
    # Find first differing char; center the truncation window on it
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    start = max(0, i - 20)
    return a[start : start + width], b[start : start + width]


def write_report(path, ctx):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Publications QC Report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- {ctx['total']} entries reviewed")
    lines.append(f"- {ctx['with_pmid']} with PMID (verified against PubMed)")
    lines.append(f"- {ctx['with_doi_only']} with DOI only (verified against Crossref)")
    lines.append(f"- {len(ctx['mismatches'])} external mismatches")
    lines.append(f"- {len(ctx['variants'])} external variants (likely benign formatting)")
    lines.append(f"- {len(ctx.get('pmid_mismatches') or [])} PMIDs that PubMed could not resolve")
    lines.append(f"- {len(ctx['author_variants'])} internal author-name variants")
    lines.append(f"- {len(ctx['journal_variants'])} internal journal-name variants")
    lines.append(f"- {len(ctx['enrichments'])} ID enrichment suggestions")
    lines.append(f"- {len(ctx['missing_ids'])} entries missing both PMID and DOI")
    lines.append(
        f"- {len(ctx['self_absent'])} entries where `{ctx.get('self_bold_label') or 'the author'}` was not detected"
    )
    lines.append(
        f"- {len(ctx.get('title_punctuation') or [])} titles ending in a period (renders fine; check for stray periods)"
    )
    lines.append("")

    section_table(lines, "External mismatches", ctx["mismatches"])
    section_table(lines, "External variants", ctx["variants"])

    lines.append("## PMIDs that PubMed could not resolve")
    lines.append("")
    lines.append(
        "_These entries cite a PMID that returns no PubMed record. Apply unavailable; edit manually._"
    )
    lines.append("")
    pmm = ctx.get("pmid_mismatches") or []
    if pmm:
        for r in pmm:
            lines.append(
                f"- {r['subsection']} #{r['entry_index']} — PMID `{r['pmid']}` — {r['title_preview']}"
            )
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Internal author-name variants")
    lines.append("")
    if ctx["author_variants"]:
        for key, forms in sorted(ctx["author_variants"].items()):
            lines.append(f"- `{key}` → " + ", ".join(f"`{f}`" for f in forms))
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Internal journal-name variants")
    lines.append("")
    if ctx["journal_variants"]:
        for key, forms in sorted(ctx["journal_variants"].items()):
            lines.append(f"- `{key}` → " + ", ".join(f"`{f}`" for f in forms))
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## ID enrichment suggestions")
    lines.append("")
    if ctx["enrichments"]:
        for r in ctx["enrichments"]:
            have = ", ".join(f"{k}=`{v}`" for k, v in r["have"].items())
            sug = ", ".join(f"{k}=`{v}`" for k, v in r["suggested"].items())
            lines.append(f"- **{r['subsection']} #{r['entry_index']}** — {r['title_preview']}")
            lines.append(f"  - have: {have}")
            lines.append(f"  - suggested: {sug}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Entries missing both PMID and DOI")
    lines.append("")
    if ctx["missing_ids"]:
        for r in ctx["missing_ids"]:
            lines.append(f"- {r['subsection']} #{r['entry_index']} — {r['title_preview']}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(
        f"## Entries where `{ctx.get('self_bold_label') or 'the author'}` was not detected"
    )
    lines.append("")
    if ctx["self_absent"]:
        for r in ctx["self_absent"]:
            lines.append(f"- {r['subsection']} #{r['entry_index']} — {r['title_preview']}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Titles ending in a period")
    lines.append("")
    lines.append(
        "_Title ends in a terminal period. The renderer strips one trailing "
        "period so it won't render `..`, but a stray period is usually a "
        "copy-paste artifact worth cleaning. Abbreviation endings (`U.S.`, "
        "`et al.`, `Jr.`) are expected here and render fine._"
    )
    lines.append("")
    if ctx.get("title_punctuation"):
        for r in ctx["title_punctuation"]:
            lines.append(f"- {r['subsection']} #{r['entry_index']} — {r['title_preview']}")
    else:
        lines.append("_None._")
    lines.append("")

    path.write_text("\n".join(lines) + "\n")


# ----- V23-B Phase 0: structured JSON sidecar emit -----


def _entry_key_args(row: dict) -> dict:
    """Build the kwargs for qc_ids._entity_key from a row dict.
    Picks PMID first, then DOI, then title."""
    pmid = row.get("pmid")
    doi = row.get("doi")
    title = row.get("title_preview")
    return {"pmid": pmid, "doi": doi, "title": title}


def _row_to_mismatch_finding(row: dict) -> dict:
    """Convert a markdown-report mismatches row -> sidecar finding dict."""
    source = (row.get("source") or "").lower() or "unknown"
    fid = qc_ids.mismatch_id(
        source=source,
        field=row["field"],
        canonical=str(row.get("canonical") or ""),
        **_entry_key_args(row),
    )
    out = {
        "id": fid,
        "type": "MISMATCH",
        "global_idx": row.get("global_idx"),
        "subsection": row["subsection"],
        "entry_index": row["entry_index"],
        "pmid": row.get("pmid"),
        "doi": row.get("doi"),
        "title_preview": row["title_preview"],
        "field": row["field"],
        "yaml_value": str(row.get("cited") or ""),
        "canonical_value": str(row.get("canonical") or ""),
        "source": source,
    }
    if row["field"] == "authors":
        out["length_changed"] = bool(row.get("length_changed"))
    return out


def _row_to_variant_finding(row: dict) -> dict:
    source = (row.get("source") or "").lower() or "unknown"
    fid = qc_ids.variant_id(
        source=source,
        field=row["field"],
        canonical=str(row.get("canonical") or ""),
        **_entry_key_args(row),
    )
    out = {
        "id": fid,
        "type": "VARIANT",
        "global_idx": row.get("global_idx"),
        "subsection": row["subsection"],
        "entry_index": row["entry_index"],
        "pmid": row.get("pmid"),
        "doi": row.get("doi"),
        "title_preview": row["title_preview"],
        "field": row["field"],
        "yaml_value": str(row.get("cited") or ""),
        "canonical_value": str(row.get("canonical") or ""),
        "source": source,
    }
    if row["field"] == "authors":
        out["length_changed"] = bool(row.get("length_changed"))
    return out


def _row_to_pmid_mismatch_finding(row: dict) -> dict:
    fid = qc_ids.pmid_mismatch_id(pmid=row["pmid"])
    return {
        "id": fid,
        "type": "PMID_MISMATCH",
        "global_idx": row.get("global_idx"),
        "subsection": row["subsection"],
        "entry_index": row["entry_index"],
        "pmid": row["pmid"],
        "title_preview": row["title_preview"],
        "reason": row.get("reason", "PubMed returned no record for this PMID"),
        "source": row.get("source", "pubmed"),
    }


def _row_to_id_enrichment_findings(row: dict) -> list:
    """One enrichment row may suggest multiple fields (doi, pmcid).
    Emit one finding per suggested field."""
    out = []
    have = row.get("have", {})
    for sfield, svalue in (row.get("suggested") or {}).items():
        fid = qc_ids.id_enrichment_id(
            pmid=have.get("pmid"),
            doi=have.get("doi"),
            pmcid=have.get("pmcid"),
            title=row.get("title_preview"),
            suggested_field=sfield,
        )
        out.append(
            {
                "id": fid,
                "type": "ID_ENRICHMENT",
                "subsection": row["subsection"],
                "entry_index": row["entry_index"],
                "title_preview": row["title_preview"],
                "have": dict(have),
                "suggested_field": sfield,
                "suggested_value": svalue,
            }
        )
    return out


def _row_to_self_absent_finding(row: dict, flat_entries: list) -> dict:
    # self_absent rows don't carry PMID/DOI directly; resolve from flat
    # via (subsection, entry_index).
    target = None
    for _, sec, e_idx, entry in flat_entries:
        if sec == row["subsection"] and e_idx + 1 == row["entry_index"]:
            target = entry
            break
    pmid = str(target["pmid"]) if target and target.get("pmid") else None
    doi = target.get("doi") if target else None
    fid = qc_ids.self_absent_id(pmid=pmid, doi=doi, title=row["title_preview"])
    return {
        "id": fid,
        "type": "SELF_ABSENT",
        "subsection": row["subsection"],
        "entry_index": row["entry_index"],
        "pmid": pmid,
        "doi": doi,
        "title_preview": row["title_preview"],
    }


def _row_to_missing_ids_finding(row: dict, flat_entries: list) -> dict:
    target = None
    for _, sec, e_idx, entry in flat_entries:
        if sec == row["subsection"] and e_idx + 1 == row["entry_index"]:
            target = entry
            break
    year = None
    if target and target.get("year"):
        try:
            year = int(str(target["year"]).strip())
        except (TypeError, ValueError):
            year = None
    fid = qc_ids.missing_ids_id(title=row["title_preview"], year=year)
    return {
        "id": fid,
        "type": "MISSING_IDS",
        "subsection": row["subsection"],
        "entry_index": row["entry_index"],
        "title_preview": row["title_preview"],
        "year": year,
        "candidates": [],  # Phase 4 will populate
    }


def _cluster_to_author_variant_finding(normalized_key: str, raw_forms: list) -> dict:
    fid = qc_ids.author_name_variant_id(normalized_key=normalized_key)
    return {
        "id": fid,
        "type": "AUTHOR_NAME_VARIANT",
        "normalized_key": normalized_key,
        "raw_forms": list(raw_forms),
    }


def _cluster_to_journal_variant_finding(normalized_key: str, raw_forms: list) -> dict:
    fid = qc_ids.journal_name_variant_id(normalized_key=normalized_key)
    return {
        "id": fid,
        "type": "JOURNAL_NAME_VARIANT",
        "normalized_key": normalized_key,
        "raw_forms": list(raw_forms),
    }


def write_sidecar(path: Path, ctx: dict, flat_entries: list) -> None:
    """Emit the V23-B Phase 0 structured JSON sidecar.

    Sibling of qc/report.md. Read by the Phase 1 triage UI (when it
    ships) and the Phase 3 jump-to-edit page. Atomic write via
    cv_editor.atomic_json.atomic_write_json so partial writes can't
    poison the triage UI.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mismatches = [_row_to_mismatch_finding(r) for r in ctx["mismatches"]]
    variants = [_row_to_variant_finding(r) for r in ctx["variants"]]
    pmid_mismatches = [_row_to_pmid_mismatch_finding(r) for r in ctx["pmid_mismatches"]]
    id_enrichments = []
    for r in ctx["enrichments"]:
        id_enrichments.extend(_row_to_id_enrichment_findings(r))
    self_absent = [_row_to_self_absent_finding(r, flat_entries) for r in ctx["self_absent"]]
    missing_ids = [_row_to_missing_ids_finding(r, flat_entries) for r in ctx["missing_ids"]]
    author_name_variants = [
        _cluster_to_author_variant_finding(k, v) for k, v in ctx["author_variants"].items()
    ]
    journal_name_variants = [
        _cluster_to_journal_variant_finding(k, v) for k, v in ctx["journal_variants"].items()
    ]

    totals = {
        "mismatches": len(mismatches),
        "variants": len(variants),
        "pmid_mismatches": len(pmid_mismatches),
        "id_enrichments": len(id_enrichments),
        "author_name_variants": len(author_name_variants),
        "journal_name_variants": len(journal_name_variants),
        "self_absent": len(self_absent),
        "missing_ids": len(missing_ids),
    }

    try:
        mtime_ns = os.stat(DATA).st_mtime_ns
    except FileNotFoundError:
        mtime_ns = None

    sidecar = {
        "version": SIDECAR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "qc_script_version": SIDECAR_QC_SCRIPT_VERSION,
        "publications_yml_mtime_ns": mtime_ns,
        "cache_key_version": 1,
        "summary": {
            "totals": totals,
            "total_findings": sum(totals.values()),
        },
        "findings": {
            "mismatches": mismatches,
            "variants": variants,
            "pmid_mismatches": pmid_mismatches,
            "id_enrichments": id_enrichments,
            "author_name_variants": author_name_variants,
            "journal_name_variants": journal_name_variants,
            "self_absent": self_absent,
            "missing_ids": missing_ids,
        },
    }
    atomic_write_json(path, sidecar)


if __name__ == "__main__":
    main()
