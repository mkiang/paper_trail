"""Per-template editor capabilities (paper_trail inversion, P5).

A per-template ``capabilities.toml`` gates whether three optional feature
clusters register their ROUTES and show their nav links:

  * ``freeze``     — the freeze -> flatten workspace tool (bespoke-only).
  * ``typography`` — the advanced typography-knob editor.
  * ``altmetric``  — the Altmetric trackers page + Explorer deep-link.

The private ``bespoke`` template declares all three TRUE (so the daily-driver
editor behaves EXACTLY as it always has); the public ``modern`` template
declares them FALSE. A missing file or missing key defaults FALSE — fail-safe:
a template that doesn't declare a capability doesn't get it.

Discovery is from PROJECT_ROOT (the engine root), NOT the workspace: the
descriptor lives beside the Typst template under
``templates/<name>/capabilities.toml``. Typst never reads it — it is editor-only
metadata. The active template mirrors ``templates/registry.typ`` resolution
WITHOUT importing Typst (see ``active_template_name``).

``current()`` caches the resolved ``Capabilities`` in a module global that a
``@paths.on_configure`` hook recomputes on every ``configure()``/``reset()`` —
mirroring the ``yaml_io`` / ``scaffold`` cached-globals pattern so a per-test
``configure()`` re-resolves. In the private repo the active template is
``bespoke`` -> ``current()`` returns all-True -> the editor is unchanged.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields

import yaml

from cv_editor import paths


@dataclass(frozen=True)
class Capabilities:
    freeze: bool = False
    typography: bool = False
    altmetric: bool = False


_KNOWN_KEYS = tuple(f.name for f in fields(Capabilities))


def load(template_name: str) -> Capabilities:
    """Read ``templates/<template_name>/capabilities.toml`` and return its
    ``[capabilities]`` table as a ``Capabilities`` (only known keys whose
    value is a genuine ``bool``; anything unknown/missing/non-bool defaults
    False). Any error (missing dir/file, parse failure) returns all-False."""
    try:
        path = paths.templates_dir() / template_name / "capabilities.toml"
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
        table = doc.get("capabilities") or {}
        # Accept only GENUINE booleans; a non-bool value (e.g. the string
        # "false", which is truthy) is treated as absent -> default False.
        return Capabilities(
            **{k: table[k] for k in _KNOWN_KEYS if k in table and isinstance(table[k], bool)}
        )
    except Exception:
        return Capabilities()


def default_template_name() -> str:
    """The default template when meta.yml carries no top-level ``template:`` key,
    mirroring ``templates/registry.typ``'s ``default-template`` WITHOUT importing
    Typst. This module ships to BOTH the private tree (default ``bespoke``) and
    the public tree (default ``modern``), so the default is DERIVED FROM DISK —
    ``bespoke`` when the private bespoke template dir is present, else ``modern``
    — never a hardcoded literal that would be wrong for one of the two trees."""
    try:
        if (paths.templates_dir() / "bespoke").is_dir():
            return "bespoke"
    except Exception:
        pass
    return "modern"


def active_template_name() -> str:
    """The active template, mirroring ``templates/registry.typ`` resolution
    WITHOUT importing Typst: ``meta.yml``'s optional top-level ``template:``,
    else :func:`default_template_name`.

    Same contract as ``build_flattened.resolve_template`` minus the per-variant
    ``--input template=`` override (a build-time concern; capabilities describe
    the workspace's default template, not one PDF variant)."""
    try:
        meta_path = paths.data_dir() / "meta.yml"
        with meta_path.open(encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}
        name = meta.get("template")
        if name:
            return str(name).strip()
    except Exception:
        pass
    return default_template_name()


# Cached resolution, refreshed on every configure()/reset() so a per-test
# configure() re-resolves (mirrors yaml_io/scaffold). Defined before the hook
# so _refresh() can assign it.
_current: Capabilities | None = None


@paths.on_configure
def _refresh() -> None:
    global _current
    _current = load(active_template_name())


def current() -> Capabilities:
    """The active template's capabilities. All-True in the private repo
    (active template = ``bespoke``)."""
    if _current is None:  # defensive — the on_configure hook fires at import
        _refresh()
    return _current
