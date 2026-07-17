"""M3-tail: active-grant past-end softening (the `strict-dates` flag).

An "active" grant whose end date is in the past is a data-freshness
inconsistency. DEFAULT is now LENIENT: render it in place under Active Support
with its literal dates (no build failure) — which also supports a no-cost
extension whose YAML end date is stale. `--input strict_dates=true` restores
the hard freshness guard (a build-failing panic) for the owner's own QC.

`templates/bespoke/render.typ` and `templates/bespoke/emit.typ` gate the panic IDENTICALLY (mirror by
construction; test_source_guard below asserts both still carry the gate). The
existing tests/test_flatten.py byte-diff already covers grant RENDERING for the
valid (non-past-end) grants in the real data.

Deferred (M5, needs test-data plumbing): a dedicated byte-fixture freezing a
past-end active grant through both paths, to guard the lenient render==emit
mirror for THIS specific case end-to-end. The freeze tool reads data/
relatively, so it needs an isolated data dir to inject the fixture grant.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("typst") is None or not HAS_BESPOKE,
    reason="needs typst + bespoke template + fonts/",
)

_GRANT_SNIPPET = (
    '#import "/templates/bespoke/render.typ": render-research-support\n'
    '#render-research-support(((status: "active", date: "01/2020 - 12/2022", '
    'agency: "NIH", title: "Test Grant", role: "PI", pi: "Smith J"),))\n'
)


def _compile(body: str, strict: bool) -> subprocess.CompletedProcess:
    src = ROOT / f"_grant_test_{uuid.uuid4().hex}.typ"
    out = Path(tempfile.gettempdir()) / f"_grant_test_{uuid.uuid4().hex}.pdf"
    src.write_text(body)
    argv = [
        "typst",
        "compile",
        "--root",
        str(ROOT),
        "--font-path",
        str(ROOT / "fonts"),
        "--ignore-system-fonts",
    ]
    if strict:
        argv += ["--input", "strict_dates=true"]
    argv += [str(src), str(out)]
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    finally:
        src.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def test_past_end_active_grant_lenient_compiles():
    # Default (no strict_dates): a past-end active grant must NOT fail the build.
    proc = _compile(_GRANT_SNIPPET, strict=False)
    assert proc.returncode == 0, proc.stderr


def test_past_end_active_grant_strict_panics():
    proc = _compile(_GRANT_SNIPPET, strict=True)
    assert proc.returncode != 0
    assert "has end date" in proc.stderr
    assert "in the past" in proc.stderr


def test_source_guard_strict_dates_flag_and_mirror():
    # Catches a revert to a hardcoded panic / strict-default, AND a one-sided
    # edit that drops the gate from only render.typ or only emit.typ.
    assert (
        'sys.inputs.at("strict_dates", default: "false")'
        in (ROOT / "lib" / "flags.typ").read_text()
    )
    assert (
        'status == "active" and strict-dates'
        in (ROOT / "templates" / "bespoke" / "render.typ").read_text()
    )
    assert (
        'status == "active" and strict-dates'
        in (ROOT / "templates" / "bespoke" / "emit.typ").read_text()
    )
