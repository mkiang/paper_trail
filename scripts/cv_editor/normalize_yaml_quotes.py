#!/usr/bin/env python3
"""Normalize YAML quoting style across data/*.yml.

Rules:
- Over-quote is fine; decorative quotes are OK
- Prefer DOUBLE QUOTES over single
- EXCEPTION: strings containing `\\` (backslash) MUST use single quotes —
  YAML's double-quoted scalars process escape sequences and would corrupt
  literal `\\$` (used for Typst dollar-sign escapes in grant amounts).
- Integers, booleans, structural elements stay unquoted
- Required-quote cases (titles with `: `, strings that look like numbers
  but are strings, etc.) are handled automatically by ruamel.yaml

Approach: round-trip through ruamel.yaml with a custom representer that
chooses the quote style per scalar value. The structural representation
(indentation, key order, list style) is preserved by ruamel's RoundTrip.

Verification: this script ONLY rewrites the .yml files. The caller is
expected to rebuild the PDFs and compare them byte-for-byte to confirm
no semantic change.
"""

import re
import sys
from pathlib import Path

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import (
        DoubleQuotedScalarString,
        PlainScalarString,
        SingleQuotedScalarString,
    )
except ImportError:
    # M1 (2026-05-29): do NOT auto-install. This script is invoked as a
    # subprocess from inside yaml_io.write_with_backup's file lock; a
    # `pip install --break-system-packages` here would silently mutate the
    # user's Python environment (and fail confusingly on a managed/
    # externally-managed interpreter). Fail loudly with an actionable hint.
    print(
        "ruamel.yaml is required but not installed. Run `pip install -e .` "
        "(or `make install`) in the project root.",
        file=sys.stderr,
    )
    sys.exit(1)


# Patterns that REQUIRE explicit quoting (YAML would otherwise mis-parse,
# or change the semantic type).
_NUMERIC_LOOKING = re.compile(r"^-?\d+(\.\d+)?$")
_BOOL_LOOKING = re.compile(r"^(true|false|yes|no|on|off|null|~)$", re.IGNORECASE)
_DATE_LOOKING = re.compile(r"^\d{4}-\d{2}-\d{2}")


def pick_style(s):
    """Return ruamel scalar wrapper for `s` per the project rules.

    Plain (no quote) is only used for unambiguous strings that contain
    nothing requiring quotes. Everything else gets quoted; backslash
    strings get single quotes (to preserve escapes literally), others
    get double quotes.
    """
    if not isinstance(s, str):
        return s
    # Backslash strings MUST be single-quoted (e.g. '\$75,000')
    if "\\" in s:
        return SingleQuotedScalarString(s)
    # Strings containing double quotes — single-quote them (cleaner than
    # escaping each ").
    if '"' in s:
        return SingleQuotedScalarString(s)
    # Strings that would be misparsed without quotes — double-quote.
    if (
        s == ""
        or s[0] in " \t!&*[]{}|>'\"%@`#,"
        or s[-1] in " \t"
        or _NUMERIC_LOOKING.match(s)
        or _BOOL_LOOKING.match(s)
        or _DATE_LOOKING.match(s)
        or ": " in s
        or s.endswith(":")
        or "  " in s
    ):
        return DoubleQuotedScalarString(s)
    # Optional-quote case: keep plain (no quote). The renderer treats
    # plain and quoted identically, so leave these unwrapped for less
    # visual noise. ruamel will re-quote if needed (e.g. embedded
    # special chars we missed).
    return PlainScalarString(s)


def walk(o):
    """Recursively wrap every string with the appropriate quote style."""
    if isinstance(o, dict):
        for k in list(o.keys()):
            o[k] = walk(o[k])
        return o
    if isinstance(o, list):
        for i, v in enumerate(o):
            o[i] = walk(v)
        return o
    if isinstance(o, str):
        return pick_style(o)
    return o


def normalize_file(path):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # don't auto-wrap lines
    yaml.indent(mapping=2, sequence=2, offset=0)

    with open(path) as f:
        # Preserve the leading comment block (docstring) before any data.
        text = f.read()
    # Split on first non-comment, non-blank line to preserve docstring header.
    lines = text.splitlines(keepends=True)
    header_end = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            header_end = i
            break
    header = "".join(lines[:header_end])

    # Load only the data portion through ruamel.
    data_text = "".join(lines[header_end:])
    data = yaml.load(data_text)
    if data is not None:
        data = walk(data)

    # Dump back, prepend the original header.
    out_path = Path(path)
    with open(out_path, "w") as f:
        f.write(header)
        if data is not None:
            yaml.dump(data, f)
    print(f"normalized: {path}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        normalize_file(p)
