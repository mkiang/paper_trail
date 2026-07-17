"""M3.6 friendly Typst errors.

Malformed input names ITSELF (the offending value + the expected format / the
valid keys) instead of emitting Typst's bare ``panicked with: "expected
integer, found ..."`` / ``key not found in dictionary``. These guards fire ONLY
on already-broken input, so valid-data output stays byte-identical (verified in
plans/m3-hygiene-strategy.md via the pinned-SOURCE_DATE_EPOCH revert-compare).

typst requires the compiled source to live under ``--root``, so each snippet is
written to a throwaway file in the project root and removed afterwards.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("typst") or not HAS_BESPOKE,
    reason="typst binary not on PATH",
)


def _compile_in_root(body: str) -> subprocess.CompletedProcess:
    src = ROOT / f"_friendly_err_{uuid.uuid4().hex}.typ"
    out = Path(tempfile.gettempdir()) / f"_friendly_err_{uuid.uuid4().hex}.pdf"
    src.write_text(body)
    try:
        return subprocess.run(
            [
                "typst",
                "compile",
                "--root",
                str(ROOT),
                "--font-path",
                str(ROOT / "fonts"),
                "--ignore-system-fonts",
                str(src),
                str(out),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        src.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def test_malformed_date_month_names_itself():
    proc = _compile_in_root(
        '#import "/templates/bespoke/render.typ": parse-month-year\n#parse-month-year("Jan/2026")\n'
    )
    assert proc.returncode != 0
    assert "Could not parse month" in proc.stderr
    assert "MM/YYYY" in proc.stderr


def test_date_sort_key_bad_year_names_itself():
    proc = _compile_in_root(
        '#import "/templates/bespoke/render.typ": date-sort-key\n#date-sort-key("notayear")\n'
    )
    assert proc.returncode != 0
    assert "Could not parse year" in proc.stderr


def test_cv_typ_dispatch_keeps_friendly_guard():
    # The compile test below exercises a hand-copied pattern, not the real
    # dispatch (it reads `sections:` from data/meta.yml, not overridable via
    # --input; and the byte-compare only ever uses VALID data, so it never
    # reaches the panic). This pure-source guard catches a revert of the
    # template's `sections.at(key, default: none)` back to a bare
    # `sections.at(key)`. V16-A moved the section dispatch from cv.typ into
    # templates/bespoke/template.typ:render().
    src = (ROOT / "templates" / "bespoke" / "template.typ").read_text()
    assert "sections.at(key, default: none)" in src
    assert "Unknown section key" in src


def test_unknown_section_key_names_itself():
    # Behavior smoke for the panic pattern (see test_cv_typ_dispatch_keeps_
    # friendly_guard for the source-presence guard on the real cv.typ). cv.typ
    # reads `sections:` from data/meta.yml (not overridable via --input), so
    # this compiles the same `dispatch.at(key, default: none)` + named-panic
    # pattern rather than cv.typ itself.
    proc = _compile_in_root(
        '#let dispatch = (education: 1, publications: 2)\n'
        '#let order = ("education", "publcations")\n'
        '#for key in order {\n'
        '  let f = dispatch.at(key, default: none)\n'
        '  if f == none { panic("Unknown section key \\"" + key + "\\" in '
        'data/meta.yml `sections:`. Valid keys: " + repr(dispatch.keys()) + ".") }\n'
        '}\n'
    )
    assert proc.returncode != 0
    assert "Unknown section key" in proc.stderr
    assert "publcations" in proc.stderr
