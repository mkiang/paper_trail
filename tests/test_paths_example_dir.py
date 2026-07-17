"""P6b §4: paths.example_dir() wheel-reachability fallback.

The installed public wheel has no engine-root ``data/example`` (the working
``data/`` IS the fictional corpus), so ``example_dir()`` falls back to the copy
bundled as ``cv_editor`` package data, located via importlib.resources. In the
private repo the engine-root copy exists, so the fallback never fires
(byte-identical behaviour).
"""

from __future__ import annotations

import importlib.resources as _res
import os

import pytest
from cv_editor import paths


def test_example_dir_uses_engine_root_when_present():
    """When the engine root carries a data/example copy (private/source), it
    wins over the bundled fallback. An installed/public tree has no engine-root
    copy (it uses the bundled cv_editor/example_data), so skip there."""
    engine_copy = paths.project_root() / "data" / "example"
    if not engine_copy.is_dir():
        pytest.skip("no engine-root data/example — installed/public tree uses the bundled copy")
    assert paths.example_dir() == engine_copy
    assert paths.example_dir().is_dir()


def test_example_dir_falls_back_to_bundled_when_engine_root_absent(tmp_path, monkeypatch):
    """Installed-wheel case: no engine-root data/example -> bundled package data."""
    proj = tmp_path / "engine"
    proj.mkdir()
    paths.configure(project_root=proj)
    assert not (proj / "data" / "example").exists()

    pkg = tmp_path / "site-packages" / "cv_editor"
    (pkg / "example_data").mkdir(parents=True)
    monkeypatch.setattr(_res, "files", lambda name: pkg)

    assert paths.example_dir() == pkg / "example_data"


def test_example_dir_returns_engine_candidate_when_nothing_found(tmp_path, monkeypatch):
    """Neither engine-root nor a bundle: return the (absent) engine path so the
    failure surfaces loudly downstream, not silently here."""
    proj = tmp_path / "engine"
    proj.mkdir()
    paths.configure(project_root=proj)
    # simulate a package with no example_data bundle
    empty_pkg = tmp_path / "site-packages" / "cv_editor"
    empty_pkg.mkdir(parents=True)
    monkeypatch.setattr(_res, "files", lambda name: empty_pkg)

    assert paths.example_dir() == proj / "data" / "example"


def test_reset_restores_typst_package_path(tmp_path):
    """Regression: configure(project_root=X) points TYPST_PACKAGE_PATH at
    X/packages; reset() MUST restore it to the legacy default, else @local
    resolution is stranded at a dead path for every subsequent test (which
    breaks freeze/`typst query`). This is the pollution that surfaced in P6b
    inc-4a."""
    proj = tmp_path / "engine"
    proj.mkdir()
    paths.configure(project_root=proj)
    assert os.environ["TYPST_PACKAGE_PATH"] == str(proj / "packages")
    paths.reset()
    # The VALUE is restored to the legacy default (this is the regression guard).
    assert os.environ["TYPST_PACKAGE_PATH"] == str(paths.project_root() / "packages")
    # The dir itself exists in the private repo; a restructured/installed tree
    # resolves @local from src/ instead, so only assert presence when it's there.
    pkg = paths.project_root() / "packages"
    if not pkg.is_dir():
        pytest.skip("no packages/ dir — restructured/public tree resolves @local from src/")
    assert pkg.is_dir()
