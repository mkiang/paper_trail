"""P5 (paper_trail inversion) — per-template capabilities descriptor + gating.

Covers: the loader (`bespoke`=all True, `modern`=all False, unknown=all False),
the two committed `capabilities.toml` files, and the route/nav gating in
`create_app()` — bespoke registers every freeze/typography/altmetric route and
shows every nav link; a forced-`modern` workspace registers NONE of them and
the nav omits them, while the base publication/style routes stay public.
"""

from __future__ import annotations

import shutil

from _engine_guards import HAS_BESPOKE, bespoke_required
from cv_editor import capabilities, paths
from cv_editor.app import create_app

# Endpoints gated by each capability.
_FREEZE_ENDPOINTS = (
    "freeze_list",
    "freeze_create",
    "freeze_delete",
    "freeze_prune",
    "freeze_flatten_stream",
)
_TYPOGRAPHY_ENDPOINTS = ("style_typography", "style_typography_save")
_ALTMETRIC_ENDPOINTS = (
    "publications_altmetric_resolve",
    "publications_trackers",
    "publications_trackers_verify_resolved",
    "publications_trackers_resolve_all",
    "publication_trackers_resolve_entry",
)
_GATED_ENDPOINTS = _FREEZE_ENDPOINTS + _TYPOGRAPHY_ENDPOINTS + _ALTMETRIC_ENDPOINTS

# Base routes that MUST stay public (share the gated modules) — the FULL
# public surface of both gated route modules.
_PUBLIC_ENDPOINTS = (
    "style_list",
    "style_save",
    "style_new",
    "style_edit",
    "style_delete",
    "style_duplicate",
    "style_build_stream",
    "publication_bulk",
    "publications_fetch_title",
    "publication_rename_author",
    "publication_import",
    "publication_promote",
)


def _endpoints(app):
    return {r.endpoint for r in app.url_map.iter_rules()}


# ---------------------------------------------------------------- loader ----


@bespoke_required
def test_load_bespoke_all_true():
    caps = capabilities.load("bespoke")
    assert caps == capabilities.Capabilities(freeze=True, typography=True, altmetric=True)


def test_load_modern_all_false():
    caps = capabilities.load("modern")
    assert caps == capabilities.Capabilities(freeze=False, typography=False, altmetric=False)


def test_load_nonexistent_all_false():
    caps = capabilities.load("nonexistent-template")
    assert caps == capabilities.Capabilities()
    assert not (caps.freeze or caps.typography or caps.altmetric)


def test_capabilities_default_is_all_false():
    """Fail-safe: an undeclared capability defaults False."""
    assert capabilities.Capabilities() == capabilities.Capabilities(
        freeze=False, typography=False, altmetric=False
    )


# ------------------------------------------------------ committed .toml ----


def _read_caps_toml(name: str) -> dict:
    import tomllib

    path = paths.templates_dir() / name / "capabilities.toml"
    assert path.is_file(), f"missing {path}"
    with path.open("rb") as fh:
        return tomllib.load(fh)["capabilities"]


def test_modern_capabilities_toml_exists_and_parses():
    """The public `modern` template ships an all-False capabilities.toml.
    Unconditional — modern is present in both the private and public trees."""
    assert _read_caps_toml("modern") == {
        "freeze": False,
        "typography": False,
        "altmetric": False,
    }


@bespoke_required
def test_bespoke_capabilities_toml_exists_and_parses():
    """The private `bespoke` template ships an all-True capabilities.toml.
    Absent in a public modern-only tree, hence bespoke-gated."""
    assert _read_caps_toml("bespoke") == {
        "freeze": True,
        "typography": True,
        "altmetric": True,
    }


# ----------------------------------------------- active-template resolve ----


def _strip_template_key(ws):
    """Copy the active data dir into a neutral tmp workspace with any top-level
    `template:` key removed from meta.yml, then point the paths seam at it.
    Mirrors the `_force_modern` pattern below. Data-agnostic: asserts the
    CODE-LEVEL fallback, not a value read from the live corpus."""
    (ws / "data").mkdir(parents=True)
    shutil.copytree(paths.data_dir(), ws / "data", dirs_exist_ok=True)
    meta_path = ws / "data" / "meta.yml"
    # Strip only the TOP-LEVEL `template:` key (column 0) — never the indented
    # `footer.template` sub-key.
    kept = [
        line
        for line in meta_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("template:")
    ]
    meta_path.write_text("".join(kept), encoding="utf-8")
    paths.configure(data_dir=ws)


def test_active_template_defaults_to_disk_default(tmp_path):
    """With NO top-level `template:` key in meta.yml, resolution falls back to
    the disk-derived default (mirrors registry.typ's default-template): `bespoke`
    when the private bespoke template dir is present, else `modern`. Uses a
    neutral tmp workspace so the assertion holds regardless of the shipped
    meta.yml, and is layout-aware so it holds in both the private and public
    (bespoke-absent) trees."""
    ws = tmp_path / "ws"
    expected = "bespoke" if HAS_BESPOKE else "modern"
    try:
        _strip_template_key(ws)  # inside try so a raise still hits paths.reset()
        assert capabilities.active_template_name() == expected
    finally:
        paths.reset()


def test_current_matches_active_template_load():
    """Self-consistency invariant: `current()` is exactly the capabilities of
    the active template. Data-agnostic — holds under bespoke (all True) AND
    modern (all False)."""
    assert capabilities.current() == capabilities.load(capabilities.active_template_name())


# ------------------------------------------------ route + nav: bespoke ----


@bespoke_required
def test_bespoke_registers_all_gated_routes():
    app = create_app()
    eps = _endpoints(app)
    for ep in _GATED_ENDPOINTS:
        assert ep in eps, f"bespoke must register {ep}"
    for ep in _PUBLIC_ENDPOINTS:
        assert ep in eps, f"public route {ep} missing"


@bespoke_required
def test_bespoke_nav_shows_gated_links():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/").get_data(as_text=True)
    assert "Freeze CV" in body
    assert "Trackers" in body
    style_body = client.get("/style").get_data(as_text=True)
    assert "Typography" in style_body


# -------------------------------------------------- route + nav: modern ----


def _force_modern(ws):
    """Rewrite the tmp-workspace meta.yml to select the modern template and
    re-fire the paths hook so capabilities.current() re-resolves."""
    meta_path = ws / "data" / "meta.yml"
    meta_path.write_text("template: modern\n" + meta_path.read_text(), encoding="utf-8")
    paths.configure(data_dir=ws)  # re-fires on_configure -> capabilities re-resolves


def test_modern_omits_gated_routes(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    shutil.copytree(paths.data_dir(), ws / "data", dirs_exist_ok=True)
    try:
        _force_modern(ws)  # inside try so a raise still hits paths.reset()
        assert capabilities.current() == capabilities.Capabilities()
        app = create_app()
        eps = _endpoints(app)
        for ep in _GATED_ENDPOINTS:
            assert ep not in eps, f"modern must NOT register {ep}"
        for ep in _PUBLIC_ENDPOINTS:
            assert ep in eps, f"public route {ep} must stay registered under modern"
    finally:
        paths.reset()


def test_modern_nav_omits_gated_links_and_renders_clean(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    shutil.copytree(paths.data_dir(), ws / "data", dirs_exist_ok=True)
    try:
        _force_modern(ws)  # inside try so a raise still hits paths.reset()
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        # No BuildError despite the gated endpoints being unregistered.
        home = client.get("/")
        assert home.status_code == 200
        body = home.get_data(as_text=True)
        assert "Freeze CV" not in body
        assert ">Trackers<" not in body
        style = client.get("/style")
        assert style.status_code == 200
        assert "Typography" not in style.get_data(as_text=True)
        # An edit form (references the altmetric_resolve route) still renders.
        assert client.get("/publications/0/edit").status_code == 200
    finally:
        paths.reset()
