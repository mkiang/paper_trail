"""Smoke tests for the paper_trail engine (needs typst on PATH)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("typst") is None, reason="typst not on PATH")


def _variants():
    meta = yaml.safe_load((ROOT / "data" / "meta.yml").read_text())
    return meta.get("build_variants", [])


def _compile(inputs, out):
    argv = ["typst", "compile", "cv.typ", str(out)]
    for k, v in (inputs or {}).items():
        argv += ["--input", f"{k}={str(v).lower() if isinstance(v, bool) else v}"]
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


@pytest.mark.parametrize("variant", _variants(), ids=lambda v: v["filename"])
def test_variant_compiles(variant):
    with tempfile.TemporaryDirectory() as td:
        proc = _compile(variant.get("inputs"), Path(td) / "out.pdf")
        assert proc.returncode == 0, proc.stderr


def test_default_template_compiles():
    with tempfile.TemporaryDirectory() as td:
        proc = _compile({}, Path(td) / "out.pdf")
        assert proc.returncode == 0, proc.stderr


def test_unknown_template_panics():
    with tempfile.TemporaryDirectory() as td:
        proc = _compile({"template": "doesnotexist"}, Path(td) / "out.pdf")
        assert proc.returncode != 0
        assert "Unknown template" in proc.stderr
        assert "modern" in proc.stderr
