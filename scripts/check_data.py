#!/usr/bin/env python3
"""CLI shim for whole-corpus CV data validation (M5 5c-i).

    python scripts/check_data.py                 # print located issues
    python scripts/check_data.py --strict        # exit 1 on ANY issue (CI)
    python scripts/check_data.py --data-dir DIR  # validate a different corpus

Exit code: 1 if any ERROR-tier issue (genuine build/save breaker); with
--strict, 1 if ANY issue (warnings included); otherwise 0. WARNINGs alone
never fail the default invocation.

The importable core lives in `cv_editor.data_check.check_data`. `build.sh`
runs this as a friendly preflight with `|| true` (never blocks a build —
typst still enforces real breakers); `make doctor` calls the core directly
(errors fail); the editor surfaces the same Issues via /validate + a banner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `cv_editor` importable when this file is run directly (scripts/ on path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cv_editor import data_check  # noqa: E402


def format_issue(i: "data_check.Issue") -> str:
    loc = f":{i.line}" if i.line else ""
    fld = f" [{i.field}]" if i.field else ""
    return f"  {i.severity.upper():<7} {i.file}{loc}{fld}  {i.entry_label}: {i.message}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate the CV data corpus (offline, read-only).")
    ap.add_argument("--strict", action="store_true", help="exit 1 if ANY issue (warnings included)")
    ap.add_argument(
        "--data-dir", default=None, help="validate a corpus directory other than the default data/"
    )
    args = ap.parse_args(argv)

    issues = data_check.check_data(args.data_dir)
    if not issues:
        print("Data check: all clear (0 errors, 0 warnings).")
        return 0

    # Errors first, then by file/line.
    ordered = sorted(
        issues,
        key=lambda x: (x.severity != data_check.ERROR, x.file, x.line or 0),
    )
    for i in ordered:
        print(format_issue(i))
    counts = data_check.summarize(issues)
    print(
        f"Data check: {counts.get(data_check.ERROR, 0)} error(s), "
        f"{counts.get(data_check.WARNING, 0)} warning(s)."
    )

    if data_check.has_errors(issues):
        return 1
    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
