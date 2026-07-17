"""CP4 Group 1 — env propagation, H6 fail-loud guard, H1 doctor legs.

Regression guards for the paper_trail-inversion pre-P7 hardening pass:
  * paths.is_inside_install_tree — prefix containment, NOT a "site-packages"
    substring (the installed _LEGACY_ROOT is <venv>/lib/pythonX.Y, ABOVE
    site-packages).
  * create_app refuses to boot when the WRITE workspace resolves inside the
    install tree (would write YAML into site-packages) — but boots normally
    for a repo / tmp workspace, and (route-smoke) for an external env root.
  * doctor's version-handshake + env legs skip-not-error pre-install so
    `make doctor` stays green in the private dev repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import doctor
import pytest
from cv_editor import paths
from cv_editor.app import create_app


def _paper_trail_installed() -> bool:
    from importlib import metadata

    try:
        metadata.distribution("paper-trail")
        return True
    except Exception:
        return False


# --- paths.is_inside_install_tree -------------------------------------------


def test_is_inside_install_tree_true_for_prefix_lib():
    # The exact H6 hazard shape: <prefix>/lib/pythonX.Y (above site-packages).
    p = Path(sys.prefix) / "lib" / "python3.13" / "data"
    assert paths.is_inside_install_tree(p) is True


def test_is_inside_install_tree_false_for_repo_root():
    # A normal checkout is never under the interpreter prefix.
    assert paths.is_inside_install_tree(paths.project_root()) is False


def test_is_inside_install_tree_false_for_tmp(tmp_path):
    assert paths.is_inside_install_tree(tmp_path) is False


# --- H6 create_app guard ----------------------------------------------------


def test_create_app_refuses_workspace_inside_install_tree(monkeypatch):
    fake = Path(sys.prefix) / "lib" / "python3.13" / "cv-workspace"
    monkeypatch.setattr(paths, "data_root", lambda: fake)
    with pytest.raises(RuntimeError, match="install tree"):
        create_app()


def test_create_app_boots_with_normal_workspace():
    # data_root() resolves to the repo / write-isolated tmp — never the prefix.
    app = create_app()
    assert app is not None


# --- doctor version helpers -------------------------------------------------


def test_norm_ver_strips_leading_v():
    assert doctor._norm_ver("v1.0.0") == "1.0.0"
    assert doctor._norm_ver("1.0.0") == "1.0.0"
    assert doctor._norm_ver(None) is None


def test_typst_toml_version_reads_package_table(tmp_path):
    toml = tmp_path / "typst.toml"
    toml.write_text('[package]\nname = "paper-trail"\nversion = "1.2.3"\n')
    assert doctor._typst_toml_version(toml) == "1.2.3"


def test_typst_toml_version_missing_file_is_none(tmp_path):
    assert doctor._typst_toml_version(tmp_path / "nope.toml") is None


@pytest.mark.skipif(
    _paper_trail_installed(), reason="paper-trail is installed (exported/public tree)"
)
def test_installed_paper_trail_version_none_when_absent():
    # Private dev repo (pre-P7): paper-trail is NOT a pip dependency.
    assert doctor._installed_paper_trail_version() is None


@pytest.mark.skipif(
    not _paper_trail_installed(), reason="paper-trail not installed (private dev repo)"
)
def test_installed_paper_trail_version_present_when_installed():
    # Exported public tree / post-P7: paper-trail IS the installed package.
    v = doctor._installed_paper_trail_version()
    assert isinstance(v, str) and v


def test_pyproject_pin_absent_pre_inversion():
    # The private pyproject does not (yet) pin paper-trail.
    assert doctor._pyproject_paper_trail_pin(paths.project_root() / "pyproject.toml") is None


# --- doctor legs: green now, hard-fail on real breakers ---------------------


def test_check_versions_green_in_repo():
    # Exactly one @local version dir, typst.toml matches it, paper-trail not
    # installed -> advisory pass.
    assert doctor._check_versions() is True


def test_check_env_returns_bool():
    assert isinstance(doctor._check_env(), bool)


def test_check_env_flags_env_var_pointing_at_missing_dir(monkeypatch):
    monkeypatch.setenv(paths.ENV_DATA_ROOT, "/no/such/workspace/xyz")
    assert doctor._check_env() is False


def test_check_env_flags_workspace_inside_install_tree(monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: Path(sys.prefix) / "lib" / "x")
    assert doctor._check_env() is False
