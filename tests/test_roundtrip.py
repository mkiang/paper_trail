"""
Round-trip integrity tests for the CV editor's YAML pipeline.

These were originally the pre-code spike (scripts/cv_editor/spike_roundtrip.py),
promoted to pytest so they run on every change.

Run from typst/:
    .venv/bin/python -m pytest tests/
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml as pyyaml
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent  # typst/
DATA = ROOT / "data"
PY = sys.executable


# ----- helpers (mirror the production yaml_io.py logic; kept here so
# the tests exercise the same shape independently) -----


def split_header(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    cut = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            cut = i
            break
    return "".join(lines[:cut]), "".join(lines[cut:])


def round_trip_load(text: str):
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=2, offset=0)
    return rt.load(text)


def round_trip_dump(data) -> str:
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=2, offset=0)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_with_normalize(path: Path, header: str, body_data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    new_body = round_trip_dump(body_data) if body_data is not None else ""
    tmp.write_text(header + new_body)
    subprocess.run(
        [PY, "-m", "cv_editor.normalize_yaml_quotes", str(tmp)], check=True, capture_output=True
    )
    pyyaml.safe_load(tmp.read_text())
    os.replace(tmp, path)


def round_trip_yaml_through_normalizer(path: Path) -> str:
    raw = path.read_text()
    header, body_text = split_header(raw)
    data = round_trip_load(body_text) if body_text.strip() else None
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / path.name
        write_with_normalize(tmp, header, data)
        return tmp.read_text()


def round_trip_snippet(body: str) -> str:
    """Run a SYNTHETIC YAML snippet through the same normalizer/ruamel pipeline
    the real files use, and return the resulting text. Prepends a comment header
    (mirrors the docstring headers on the real files) so ``split_header`` has
    something to peel off. No real-corpus read — the edge cases below are
    exercised on fictional data so the test is portable to a public tree."""
    header = "# Synthetic test corpus (fictional data)\n"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "publications.yml"
        src.write_text(header + body)
        return round_trip_yaml_through_normalizer(src)


# ----- tests -----


def test_leading_docstring_header_preserved():
    """publications.yml has a ~90-line docstring; round-trip must preserve it."""
    path = DATA / "publications.yml"
    raw = path.read_text()
    header, _ = split_header(raw)
    assert header.startswith("# Publications")
    assert len(header.splitlines()) >= 80

    new_text = round_trip_yaml_through_normalizer(path)
    new_header, _ = split_header(new_text)
    assert new_header == header


def test_mid_body_comments_preserved():
    """Comments between list items must survive the round-trip."""
    path = DATA / "publications.yml"
    raw = path.read_text()
    header, body_text = split_header(raw)
    data = round_trip_load(body_text)
    data[0]["entries"].yaml_set_comment_before_after_key(
        1, before="THIS-IS-A-MID-BODY-MARKER", indent=2
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "test.yml"
        write_with_normalize(tmp, header, data)
        result = tmp.read_text()
    assert "THIS-IS-A-MID-BODY-MARKER" in result


@pytest.mark.parametrize("yaml_path", sorted(DATA.glob("*.yml")), ids=lambda p: p.name)
def test_byte_identical_through_normalizer(yaml_path: Path):
    """Each YAML round-trips identically vs. just running the normalizer once."""
    with tempfile.TemporaryDirectory() as td:
        ref_copy = Path(td) / yaml_path.name
        ref_copy.write_text(yaml_path.read_text())
        subprocess.run(
            [PY, "-m", "cv_editor.normalize_yaml_quotes", str(ref_copy)],
            check=True,
            capture_output=True,
        )
        normalized_only = ref_copy.read_text()
    pipeline = round_trip_yaml_through_normalizer(yaml_path)
    assert pipeline == normalized_only, (
        f"{yaml_path.name}: pipeline diverges from normalizer-only output"
    )


def test_co_first_dict_round_trip():
    """A dict-form co-first author ({name, co_first: true}) round-trips through
    the normalizer with both the name and the flag intact. Synthetic fictional
    author — exercises the same ruamel dict-in-list edge case with no real
    data read."""
    body = (
        "- subsection: Test\n"
        "  entries:\n"
        "  - title: A synthetic co-first study\n"
        "    authors:\n"
        "    - name: Delacroix M\n"
        "      co_first: true\n"
        "    - Smith AB\n"
        "    year: 2024\n"
    )
    out = round_trip_snippet(body)
    assert "Delacroix M" in out
    assert re.search(r"co_first:\s+true", out), out


def test_bibtexparser_v2_markup_stripped():
    """bibtexparser v2 + latex_encoding decodes accents; second pass strips \\textit/\\textbf."""
    import bibtexparser
    from bibtexparser.middlewares import LatexDecodingMiddleware

    src = r"""@article{test2024,
  author = {Marqu\'{e}s, D and Smith, J},
  title  = {A study of \textit{italicized} terms and \textbf{bold} ones},
  journal = {Journal of Tests},
  year   = {2024}
}"""
    library = bibtexparser.parse_string(src, append_middleware=[LatexDecodingMiddleware()])
    e = library.entries[0]
    title = e.fields_dict["title"].value
    author = e.fields_dict["author"].value
    title = re.sub(r"\\textit\{([^}]+)\}", r"\1", title)
    title = re.sub(r"\\textbf\{([^}]+)\}", r"\1", title)
    assert "italicized" in title and "bold" in title
    assert "\\textit" not in title and "\\textbf" not in title
    assert "Marqués" in author


def test_compound_surname_round_trip():
    """A compound (multi-word) surname stored as a bare scalar survives the
    normalizer round-trip. The real corpus has e.g. "Van Der Berg J"; here a
    fictional equivalent so no real-data read is needed."""
    body = (
        "- subsection: Test\n"
        "  entries:\n"
        "  - title: A synthetic compound-surname study\n"
        "    authors:\n"
        "    - Van Der Berg J\n"
        "    - Smith AB\n"
        "    year: 2024\n"
    )
    out = round_trip_snippet(body)
    assert "Van Der Berg J" in out, out


def test_corporate_author_round_trip():
    """A corporate/consortium author name plus a string carrying ', ' and ': '
    (which force quoting) survive the round-trip. Fictional analog of the
    corporate-author (e.g. Example Consortium for Health) edge case — no real-data read."""
    body = (
        "- subsection: Reports\n"
        "  entries:\n"
        "  - title: A synthetic consensus report\n"
        "    authors:\n"
        "    - name: National Institute of Examples\n"
        "      group_authorship: true\n"
        "    year: 2024\n"
        "    notes:\n"
        "    - type: note\n"
        '      text: "Example City, EX: The Example Press."\n'
    )
    out = round_trip_snippet(body)
    assert "National Institute of Examples" in out, out
    assert "Example City, EX: The Example Press" in out, out


def test_numeric_string_pages_keep_quotes():
    """A numeric-looking `pages:` value stays quoted through the round-trip;
    otherwise YAML would coerce it to an int. Synthetic snippet — the sample
    corpus has no quoted-numeric pages. (A hyphenated range like `123-145`
    isn't numeric-looking, so the normalizer legitimately drops its quotes;
    the load-bearing case is a bare integer such as `1810`.)"""
    body = (
        "- subsection: Test\n"
        "  entries:\n"
        "  - title: A study on a single page\n"
        "    pages: '1810'\n"
        "    year: 2024\n"
    )
    out = round_trip_snippet(body)
    assert re.search(r"pages:\s+[\"']1810[\"']", out), out


def test_grant_amount_backslash_dollar():
    """research_support.yml: '\\$XXX,XXX' must stay single-quoted."""
    path = DATA / "research_support.yml"
    orig = re.findall(r"amount:\s+'\\\$[^']+'", path.read_text())
    assert orig
    new = re.findall(r"amount:\s+'\\\$[^']+'", round_trip_yaml_through_normalizer(path))
    assert len(new) == len(orig)
