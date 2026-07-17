#!/usr/bin/env python3
r"""CP4/B5 discrete-pattern leak scanner — the SINGLE implementation shared by
the exporter's pre-ship ``leak_gate()`` and the public ``ci_leak_check.sh`` CI
gate, so the two can't drift (the render.typ/emit.typ failure class, gotcha #41).

It flags identifier-shaped strings — ORCID, DOI, PMCID, PMID (bare digit run),
phone, email, comma-grouped dollar amount — that are NOT in the fictional
allowlist (``scripts/leak_allow.txt``). This is the layer that makes P8's
acceptance test satisfiable: a seeded fake grant #/PMID/ORCID/DOI/phone in the
shipped example corpus fails the gate red.

SCOPE (documented CP4 judgment call; 4-pair review split resolved here):
  * ``data/`` (the shipped example corpus, incl the ``cv_editor/example_data``
    copy): ALL patterns INCLUDING bare PMID digit-runs. This is the CP3
    Category-A leak surface (real IDs leaked into the corpus historically); it
    has a small, stable fictional set, so a tight allowlist + a real-ID seed
    failing red is achievable without noise.
  * shipped code + docs (everything else that ships): the STRUCTURED patterns
    only — NOT bare digit-runs, which are pure noise in code (line numbers,
    timestamps, epochs). Catches an identifier hardcoded OUTSIDE the corpus.
  * ``tests/``: EXCLUDED from this layer. ~65 fictional-by-design fixtures across
    many DOI-prefix families, already CP3-scrubbed of real IDs; a 65-entry
    allowlist would be pure maintenance for ~zero marginal safety. The caller's
    owner-name/institution TOKEN scan still covers ``tests/`` for the real-name
    class, and CP3's human audit + the private digit cross-check cover unseen
    identifiers (SPEC item 6 residual-risk model).

DEFERRED (SPEC items 4/5, with rationale): the WHOLE-TREE unanchored ``\d{5,}``
rule + the CI diff-baseline. A baseline-less whole-tree digit scan is an FP storm
(every year-range/line-number/epoch trips it); the ``data/``-scoped PMID digit-run
above is the pragmatic corpus substitute.

Per-line exemption: a trailing ``# leak-allow`` marker (the sanctioned hatch).
Unlike the exporter's TOKEN scan, the discrete-pattern layer does NOT exempt an
``x not in y`` line — there is no assert-absence idiom for a raw DOI/PMID literal
in a shipped non-test file, so a ``not in`` exemption here would only open a
bypass (``... not in ... 10.x/realleak.2024.99`` would slip through). The
anchored ``not in`` guard stays in the exporter/CI TOKEN scan, where naming a
forbidden token to assert its absence is legitimate (B6).

Pure Python, NO ``cv_editor`` import — safe to ship, and the exporter's
self-guard (must not import the editor) can call it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STRUCTURED = {
    "orcid": re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b"),
    "doi": re.compile(r"\b10\.\d{2,9}/[-._;()/:A-Za-z0-9]+"),
    "pmc": re.compile(r"\bPMC\d{5,}\b"),
    "phone": re.compile(r"(?<!\d)\d{3}-\d{3}-\d{4}(?!\d)"),
    # alpha TLD (>=2) so a pip URL like `...paper_trail@v1.0.0` is NOT matched.
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b"),
    "dollar": re.compile(r"\$\d{1,3}(?:,\d{3})+"),
}
# data/-only: a bare 7+-digit run (PMID-shaped). NOT applied to code (noise).
PMID = re.compile(r"(?<!\d)\d{7,}(?!\d)")

TEXT_SUFFIXES = {
    ".py",
    ".typ",
    ".toml",
    ".yml",
    ".yaml",
    ".md",
    ".sh",
    ".html",
    ".js",
    ".css",
    ".txt",
    ".cfg",
    ".ini",
    ".in",
    ".json",
}
_SKIP_DIRS = {
    ".git",
    ".venv",
    "output",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
# The gate's own machinery lists fictional tokens / patterns as literals — never
# scan them (they'd self-flag). Mirrors the exporter's TOKEN_SCAN_SKIP.
_SELF_FILES = {"scripts/leak_scan.py", "scripts/leak_allow.txt", "scripts/ci_leak_check.sh"}


def load_allow(allow_path: Path) -> tuple[list[str], list[str], list[str]]:
    """Parse ``leak_allow.txt`` into (prefixes, suffixes, exacts), lowercased.

    Line format: ``<kind> <token>`` where kind is ``prefix`` | ``suffix`` |
    ``exact``. ``#`` comments + blank lines ignored. A matched identifier is
    allowed if it startswith any prefix, endswith any suffix, or equals any
    exact (all case-insensitive)."""
    prefixes: list[str] = []
    suffixes: list[str] = []
    exacts: list[str] = []
    if not allow_path.is_file():
        return prefixes, suffixes, exacts
    for line in allow_path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        kind, tok = parts[0].lower(), parts[1].strip().lower()
        if kind == "prefix":
            prefixes.append(tok)
        elif kind == "suffix":
            suffixes.append(tok)
        elif kind == "exact":
            exacts.append(tok)
    return prefixes, suffixes, exacts


def _allowed(value: str, allow: tuple[list[str], list[str], list[str]]) -> bool:
    prefixes, suffixes, exacts = allow
    v = value.lower()
    return (
        any(v.startswith(p) for p in prefixes)
        or any(v.endswith(s) for s in suffixes)
        or v in exacts
    )


def _scope(rel: str) -> str:
    """'skip' (tests + machinery), 'data' (corpus — all patterns + PMID), or
    'code' (everything else — structured patterns only)."""
    parts = rel.split("/")
    if rel in _SELF_FILES:
        return "skip"
    if "tests" in parts:
        return "skip"
    if "data" in parts or "example_data" in parts:
        return "data"
    return "code"


def scan_tree(root: Path, allow_path: Path) -> list[str]:
    allow = load_allow(allow_path)
    findings: list[str] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix not in TEXT_SUFFIXES:
            continue
        rel = f.relative_to(root).as_posix()
        if _SKIP_DIRS & set(rel.split("/")):
            continue
        scope = _scope(rel)
        if scope == "skip":
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "# leak-allow" in line:
                continue
            pats = dict(STRUCTURED)
            if scope == "data":
                pats["pmid"] = PMID
            for kind, pat in pats.items():
                for m in pat.finditer(line):
                    val = m.group(0)
                    if not _allowed(val, allow):
                        findings.append(f"{rel}:{i}: {kind} {val!r} (not in leak_allow.txt)")
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    allow_path = root / "scripts" / "leak_allow.txt"
    findings = scan_tree(root, allow_path)
    if findings:
        print(f"LEAK SCAN: {len(findings)} discrete-pattern finding(s):", file=sys.stderr)
        for m in findings:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print("LEAK SCAN: CLEAN (no unexpected identifier-shaped strings).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
