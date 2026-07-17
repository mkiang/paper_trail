#!/usr/bin/env python3
"""Discover publications from an ORCID iD (M5 5b, CP5a) — READ-ONLY dry-run.

    python scripts/orcid_import.py 0000-0002-1825-0097
    python scripts/orcid_import.py 0000-0002-1825-0097 --dry-run
    python scripts/orcid_import.py 0000-0002-1825-0097 --data-dir /path/to/data

Fetches the ORCID public `/works` summary, extracts DOI/PMID refs, and prints a
new / already-in-CV / no-usable-id partition against `data/publications.yml`. It
NEVER writes and NEVER enriches — discovery only. Actually importing a discovered
DOI/PMID is the editor's job (the existing DOI/PMID import flow re-enriches from
PubMed/Crossref and stages for review). v1 is dry-run only; `--dry-run` is accepted
as an explicit no-op so the contract is legible.

Outbound: one GET to pub.orcid.org via the SSRF-safe seam, UA `cv-editor/1.0`, no
PII, no auth (gotcha #14).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cv_editor import orcid_client, paths, schemas, sections, yaml_io  # noqa: E402


def _ref_line(r: "orcid_client.WorkRef") -> str:
    tag = f"doi:{r.doi}" if r.doi else (f"pmid:{r.pmid}" if r.pmid else "(no id)")
    title = r.title or "(untitled)"
    return f"  - {tag:<28} {title}"


def _load_existing(data_dir: Path) -> list:
    _, data = yaml_io.load(data_dir / "publications.yml")
    # Read the structure from the schema (same as the import-tab route) so the
    # publications structure has a single source of truth, not a duplicated literal.
    structure = schemas.get("publications")["structure"]
    return [rec["entry"] for rec in sections.flatten(data, structure)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover publications from an ORCID iD (read-only).")
    ap.add_argument("orcid_id", help="ORCID iD, e.g. 0000-0002-1825-0097")
    ap.add_argument(
        "--dry-run", action="store_true", help="no-op in v1 (the CLI is always read-only)"
    )
    ap.add_argument("--data-dir", default=None, help="data dir (default: ./data)")
    args = ap.parse_args(argv)

    if not orcid_client.is_valid_orcid_id(args.orcid_id):
        print(
            f"Not a well-formed ORCID iD: {args.orcid_id!r} "
            "(expected 0000-0000-0000-0000, last char may be X).",
            file=sys.stderr,
        )
        return 2

    works = orcid_client.fetch_works(args.orcid_id)
    if works is None:
        print(
            f"Could not fetch works for {args.orcid_id} — unknown iD, no public "
            "works, or a network error. Check the iD and try again.",
            file=sys.stderr,
        )
        return 1

    refs = orcid_client.extract_external_ids(works)
    data_dir = Path(args.data_dir) if args.data_dir else paths.data_dir()
    part = orcid_client.partition_against_cv(refs, _load_existing(data_dir))

    print(f"ORCID import (dry-run) for {args.orcid_id} — {len(refs)} works discovered.\n")
    print(f"NEW ({len(part.new)}):")
    for r in part.new:
        print(_ref_line(r))
    print(f"\nALREADY IN CV ({len(part.in_cv)}):")
    for r in part.in_cv:
        print(_ref_line(r))
    print(f"\nNO USABLE ID ({len(part.no_id)}) — add manually:")
    for r in part.no_id:
        print(_ref_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
