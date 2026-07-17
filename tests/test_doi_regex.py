"""Python-regex parity checks for the DOI-linkify pattern (RP2, 2026-05-17).

Split out of test_renderer_polish.py (CP3 A4, 2026-07-15): these three tests
are PURE Python — no typst/bespoke build, no real corpus — so they SHIP-RUN in
the public tree, whereas the rest of test_renderer_polish.py builds the real CV
and asserts against real-corpus DOIs (private-only; on the export exclude
manifest). The DOI literals here are FICTIONAL (`10.9999/...`); the test
exercises the regex's structure (trailing-punctuation exclusion, uppercase alpha
suffix, full doi.org URL form), which is value-agnostic.

Not authoritative for the Typst renderer (Typst uses Rust's regex crate); the
authoritative build-time checks live in the (private-only) test_renderer_polish.py.
"""

import re

# The pattern mirrors linkify-dois in templates/bespoke/render.typ.
_DOI_PATTERN = r"(?i)(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/[-._;()/:A-Z0-9]*[-/:A-Z0-9]"


def test_doi_regex_excludes_trailing_period():
    """Python-regex parity for the trailing-`.` case. Catches the obvious
    failure mode but is NOT a substitute for the build-time tests in the
    private-only test_renderer_polish.py."""
    pattern = re.compile(_DOI_PATTERN)
    m = pattern.search("see 10.1234/foo.")
    assert m is not None
    assert m.group() == "10.1234/foo"  # NOT "10.1234/foo."


def test_doi_regex_accepts_uppercase_suffix():
    """A DOI with an uppercase alpha suffix + parens + hyphens must match
    whole (fictional DOI preserving that exact shape)."""
    pattern = re.compile(_DOI_PATTERN)
    m = pattern.search("see 10.9999/S1234-5678(22)00208-0 in the journal")
    assert m is not None
    assert m.group() == "10.9999/S1234-5678(22)00208-0"


def test_doi_regex_accepts_full_doi_org_url():
    pattern = re.compile(_DOI_PATTERN)
    m = pattern.search("see https://doi.org/10.1234/foo for details")
    assert m is not None
    assert m.group() == "https://doi.org/10.1234/foo"
