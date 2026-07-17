"""M5-5d CP6: build_runner.default_variant_name() + index gauge plumbing.

Behavior-identical for the real corpus (fullcv is meta.yml's first
variant); the point is that the quick Rebuild + staleness gauge keep
working after a reset-to-blank/example, whose metas define no fullcv.
"""

from __future__ import annotations

import pytest
from cv_editor import build_runner, build_variants, paths, yaml_io


def test_real_corpus_resolves_to_first_configured_variant():
    # Data-agnostic: the resolved default must equal the first build variant
    # configured in the live corpus (fullcv in the private tree, example-cv
    # in the Jane Q Public sample) — derived, never hardcoded.
    _, meta = yaml_io.load(paths.data_dir() / "meta.yml")
    expected = build_variants.default_variant_name(meta)
    assert build_runner.default_variant_name() == expected


def test_meta_without_fullcv_resolves_to_first_variant(monkeypatch):
    monkeypatch.setattr(
        yaml_io,
        "load",
        lambda p: ("", {"build_variants": [{"filename": "cv"}, {"filename": "other"}]}),
    )
    assert build_runner.default_variant_name() == "cv"


def test_meta_first_variant_wins_regardless_of_name(monkeypatch):
    # P3: the default is the FIRST build variant, with no special-casing of any
    # particular name (was "prefer fullcv even if it isn't first").
    monkeypatch.setattr(
        yaml_io,
        "load",
        lambda p: ("", {"build_variants": [{"filename": "cv"}, {"filename": "fullcv"}]}),
    )
    assert build_runner.default_variant_name() == "cv"


def test_unreadable_meta_falls_back_to_constant(monkeypatch):
    def boom(p):
        raise OSError("simulated")

    monkeypatch.setattr(yaml_io, "load", boom)
    # P3: the generic fallback is DEFAULT_VARIANT ("cv"), not a personal name.
    assert build_runner.default_variant_name() == "cv"


def test_empty_variants_falls_back_to_constant(monkeypatch):
    monkeypatch.setattr(yaml_io, "load", lambda p: ("", {"build_variants": []}))
    # P3: generic fallback ("cv") when meta defines no build variants.
    assert build_runner.default_variant_name() == "cv"


def test_default_variant_argv_follows_resolved_name(monkeypatch):
    monkeypatch.setattr(
        yaml_io,
        "load",
        lambda p: ("", {"build_variants": [{"filename": "cv", "inputs": {}}]}),
    )
    argv, cmd_str = build_runner._default_variant_argv()
    assert "output/cv.pdf" in cmd_str
    assert "mkiangcv" not in cmd_str


@pytest.fixture
def client():
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_index_renders_default_variant_in_rebuild_copy(client, monkeypatch):
    """GET-only, HERMETIC: force the pdf-missing branch (the variant name
    only appears in the rebuild-needed / pdf-missing branches; on a
    freshly-built tree the 'all up-to-date' branch would render no name)."""
    monkeypatch.setattr(build_runner, "default_variant_name", lambda: "hermetic-variant")
    body = client.get("/").get_data(as_text=True)
    # output/hermetic-variant.pdf never exists -> the not-present branch
    # must name the resolved variant.
    assert "hermetic-variant.pdf" in body
