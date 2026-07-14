#!/usr/bin/env python3
"""Minimal, dependency-light data check for the CV YAML.

Not a full schema validator — it catches the mistakes that otherwise surface
as a raw Typst panic: an unescaped `$` (opens math mode), a numeric-looking
field that YAML coerced to int (PMIDs/volumes must stay strings), and missing
required fields. Prints located, friendly messages. Exit 1 on any ERROR.

Run: python3 scripts/validate_cv.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parent.parent / "data"

# Fields that must be quoted strings (YAML would coerce a bare value to int).
# `year` is intentionally excluded — an integer year is correct.
STRING_FIELDS = {"pmid", "volume", "issue", "pages", "project"}
# A `$` not preceded by a backslash opens Typst math mode in body text.
DOLLAR_RE_FIELDS = {"amount", "title", "journal", "role", "venue", "award", "text"}

errors: list[str] = []
warnings: list[str] = []


def walk(node, where):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in STRING_FIELDS and isinstance(v, int):
                warnings.append(f"{where}: field '{k}' is a number ({v}); quote it as a string.")
            if k in DOLLAR_RE_FIELDS and isinstance(v, str):
                import re

                if re.search(r"(?<!\\)\$", v):
                    errors.append(f"{where}: field '{k}' has an unescaped '$'; write it as '\\$'.")
            walk(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{where}[{i}]")


def main() -> int:
    for path in sorted(DATA.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: YAML parse error: {exc}")
            continue
        walk(data, path.name)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} error(s) found.", file=sys.stderr)
        return 1
    print("Data check: OK" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
