"""Tests for scripts/build_flattened.py (2026-07-11).

Build PDF + flattened .typ per variant into output/flattened_typs/. Most tests
are pure (compile_variant / flatten_source monkeypatched at the module-attribute
seam, no typst, no lock). One typst-gated end-to-end runs the real engine into a
tmp dir. A route smoke test covers POST /freeze/flatten/stream.

See scripts/CLAUDE.md gotcha #82.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import build_variant
import pytest
from _engine_guards import HAS_BESPOKE, freeze_required
from build_variant import CompileResult, compile_variant
from cv_editor import build_flattened, build_runner, freezer
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent
_HAS_TYPST = shutil.which("typst") is not None and HAS_BESPOKE  # P5: + bespoke/fonts
typst_required = pytest.mark.skipif(not _HAS_TYPST, reason="typst not on PATH")


def _fake_compile(returncode: int = 0):
    """A stand-in for build_variant.compile_variant that never spawns typst."""

    def fake(*, variant_inputs=None, out_pdf=None, **kw):
        return CompileResult(
            pdf_path=Path(out_pdf) if out_pdf else Path("x.pdf"),
            argv=[],
            returncode=returncode,
            stderr="boom" if returncode else "",
            seconds=0.0,
        )

    return fake


def _fake_flatten(inputs, name):
    return f"// FLAT {name}\n#let meta = (:)\n"


# ---- select_variants ------------------------------------------------------


def test_select_variants_all_returns_every_row():
    meta = {"build_variants": [{"filename": "a"}, {"filename": "b"}, {"filename": "c"}]}
    rows = build_flattened.select_variants(meta, all_variants=True)
    assert [r["filename"] for r in rows] == ["a", "b", "c"]


def test_select_variants_primary_returns_matching_row(monkeypatch):
    meta = {"build_variants": [{"filename": "a"}, {"filename": "fullcv", "inputs": {"x": 1}}]}
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "fullcv")
    rows = build_flattened.select_variants(meta, all_variants=False)
    assert len(rows) == 1
    assert rows[0]["filename"] == "fullcv"
    assert rows[0]["inputs"] == {"x": 1}


def test_select_variants_primary_no_matching_row_synthesizes(monkeypatch):
    # Unreadable/blank meta: default_variant_name falls back to "fullcv" with no
    # matching row → a synthetic no-inputs row (bare compile), not a crash.
    meta = {"build_variants": [{"filename": "a"}]}
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "fullcv")
    rows = build_flattened.select_variants(meta, all_variants=False)
    assert rows == [{"filename": "fullcv"}]


# ---- resolve_template -----------------------------------------------------


def test_resolve_template_defaults_to_disk_default():
    # With no --input/meta template, the default is disk-derived: bespoke when
    # the private template ships, else modern (capabilities.default_template_name).
    expected = "bespoke" if HAS_BESPOKE else "modern"
    assert build_flattened.resolve_template({"filename": "a"}, {}) == expected


def test_resolve_template_from_variant_inputs():
    v = {"filename": "a", "inputs": {"template": "modern"}}
    assert build_flattened.resolve_template(v, {}) == "modern"


def test_resolve_template_from_meta_top_level():
    assert build_flattened.resolve_template({"filename": "a"}, {"template": "modern"}) == "modern"


# ---- build_one ------------------------------------------------------------


def test_build_one_bespoke_writes_typ(monkeypatch, tmp_path):
    monkeypatch.setattr(build_variant, "compile_variant", _fake_compile(0))
    monkeypatch.setattr(freezer, "flatten_source", _fake_flatten)
    # Explicit bespoke so the bespoke flatten path is exercised in ANY tree
    # (the disk default is modern where the bespoke template isn't shipped).
    r = build_flattened.build_one(
        {"filename": "fullcv", "inputs": {"template": "bespoke"}}, {}, tmp_path
    )
    assert r["pdf_ok"] is True
    assert r["flat_written"] is True
    assert r["skipped"] is False
    written = (tmp_path / "flattened_typs" / "fullcv.typ").read_text()
    assert "#let meta" in written


def test_build_one_nonbespoke_skips_flatten_not_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(build_variant, "compile_variant", _fake_compile(0))

    def _boom(inputs, name):  # flatten must NOT be called for a non-bespoke variant
        raise AssertionError("flatten_source should not run for a non-bespoke variant")

    monkeypatch.setattr(freezer, "flatten_source", _boom)
    v = {"filename": "modernvar", "inputs": {"template": "modern"}}
    r = build_flattened.build_one(v, {}, tmp_path)
    assert r["pdf_ok"] is True
    assert r["skipped"] is True
    assert r["flat_written"] is False
    assert r["flat_failed"] is False
    assert not (tmp_path / "flattened_typs" / "modernvar.typ").exists()


def test_build_one_pdf_failure_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(build_variant, "compile_variant", _fake_compile(1))
    monkeypatch.setattr(freezer, "flatten_source", _fake_flatten)
    r = build_flattened.build_one({"filename": "fullcv"}, {}, tmp_path)
    assert r["pdf_ok"] is False


# ---- main() exit-code contract (PDF fail => non-zero; skip alone => zero) ---


def _patch_main_deps(monkeypatch, meta, *, compile_rc=0):
    """No lock spawn, no meta read, no typst — just exercise main()'s loop +
    aggregation + exit code."""
    monkeypatch.setattr(
        build_flattened.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(build_flattened.yaml_io, "load", lambda p: ("", meta))
    monkeypatch.setattr(build_variant, "compile_variant", _fake_compile(compile_rc))
    monkeypatch.setattr(freezer, "flatten_source", _fake_flatten)


def test_main_pdf_failure_returns_nonzero(monkeypatch, tmp_path):
    meta = {"build_variants": [{"filename": "fullcv"}]}
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "fullcv")
    _patch_main_deps(monkeypatch, meta, compile_rc=1)
    assert build_flattened.main(["--output-dir", str(tmp_path)]) == 1


def test_main_nonbespoke_skip_alone_returns_zero(monkeypatch, tmp_path):
    # A non-bespoke variant whose PDF compiles fine: the flatten SKIP must NOT
    # flip the exit code.
    meta = {"build_variants": [{"filename": "modernvar", "inputs": {"template": "modern"}}]}
    _patch_main_deps(monkeypatch, meta, compile_rc=0)
    assert build_flattened.main(["--all", "--output-dir", str(tmp_path)]) == 0


def test_main_bespoke_success_returns_zero(monkeypatch, tmp_path):
    # Explicit bespoke so the flatten-writes-typ path runs regardless of the
    # disk default template (modern in a bespoke-absent public tree).
    meta = {"build_variants": [{"filename": "fullcv", "inputs": {"template": "bespoke"}}]}
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "fullcv")
    _patch_main_deps(monkeypatch, meta, compile_rc=0)
    assert build_flattened.main(["--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "flattened_typs" / "fullcv.typ").exists()
    assert (tmp_path / "flattened_typs" / "README.md").exists()


def test_main_aborts_when_lock_held(monkeypatch, tmp_path):
    meta = {"build_variants": [{"filename": "fullcv"}]}
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "fullcv")
    _patch_main_deps(monkeypatch, meta, compile_rc=0)
    # Lock probe reports the lock is held (non-zero) → abort before any build.
    monkeypatch.setattr(
        build_flattened.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1)
    )
    called = {"n": 0}

    def _should_not_compile(**kw):
        called["n"] += 1
        return _fake_compile(0)(**kw)

    monkeypatch.setattr(build_variant, "compile_variant", _should_not_compile)
    assert build_flattened.main(["--output-dir", str(tmp_path)]) == 1
    assert called["n"] == 0


# ---- build_variant.compile_variant empty-vs-omitted regression (gotcha #82) --


def test_compile_variant_empty_inputs_emits_no_input_flags(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(build_variant.subprocess, "run", fake_run)
    compile_variant(variant_inputs={}, out_pdf=tmp_path / "x.pdf")
    assert "--input" not in captured["argv"], (
        "an EMPTY inputs dict must emit zero --input flags (not EVERYTHING_INPUTS)"
    )


def test_compile_variant_none_inputs_uses_everything(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(build_variant.subprocess, "run", fake_run)
    compile_variant(variant_inputs=None, out_pdf=tmp_path / "x.pdf")
    assert "--input" in captured["argv"]
    assert "audience=industry" in captured["argv"]  # from EVERYTHING_INPUTS


# ---- route smoke: POST /freeze/flatten/stream -----------------------------


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@freeze_required
def test_freeze_flatten_stream_is_event_stream(client, monkeypatch):
    def fake_stream(argv, cmd_str=None):
        yield ("done", {"ok": True, "returncode": 0, "duration_s": 0.0, "cmd": "x"})

    monkeypatch.setattr(build_runner, "stream_subprocess", fake_stream)
    resp = client.post("/freeze/flatten/stream", data={"mode": "primary"})
    assert resp.mimetype == "text/event-stream"
    resp.get_data()


@freeze_required
def test_freeze_flatten_stream_mode_all_appends_flag(client, monkeypatch):
    captured = {}

    def fake_stream(argv, cmd_str=None):
        captured["argv"] = list(argv)
        yield ("done", {"ok": True, "returncode": 0, "duration_s": 0.0, "cmd": "x"})

    monkeypatch.setattr(build_runner, "stream_subprocess", fake_stream)

    client.post("/freeze/flatten/stream", data={"mode": "all"}).get_data()
    assert "--all" in captured["argv"]
    assert captured["argv"][-1].endswith("build_flattened.py") or "--all" in captured["argv"]

    client.post("/freeze/flatten/stream", data={"mode": "primary"}).get_data()
    assert "--all" not in captured["argv"]


# ---- typst-gated end-to-end ----------------------------------------------


@typst_required
def test_main_primary_end_to_end_writes_pdf_and_typ(tmp_path):
    name = build_runner.default_variant_name()
    rc = build_flattened.main(["--output-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / f"{name}.pdf").exists()
    typ = tmp_path / "flattened_typs" / f"{name}.typ"
    assert typ.exists()
    src = typ.read_text()
    assert src.strip()
    assert "#let meta" in src  # baked meta dict => a real flattened file
    assert "#import" not in src  # self-contained (imports stripped)
