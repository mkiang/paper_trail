"""Derived guards for the field-type registry's coupled sites (gotcha #96).

Adding or removing a field `type:` touches NINE places and only ONE of them is
enforced today (`assert_schemas_covered`, which checks a handler exists). The
rest fail silently or fall through to a plain text input -- which is how a
mapping came to be rendered into a `<textarea>` as its Python repr.

These tests DERIVE the coupling instead of restating it, so a tenth site cannot
be forgotten without going red.

TWO RULES FOR EVERYTHING IN HERE:

* Resolve package files through `cv_editor_pkg_dir()`, never a repo-relative
  path. The consuming private repo has no `scripts/cv_editor/` -- it installs
  the wheel -- so a repo-relative glob finds nothing there, computes an empty
  set, and passes vacuously.
* Assert the sweep found something. A glob that matches zero files makes every
  set-comparison below trivially true.
"""

from __future__ import annotations

import re

from _engine_guards import cv_editor_pkg_dir
from cv_editor.field_handlers import FIELD_HANDLERS, JSON_FIELD_TYPES
from cv_editor.schemas import META, META_ALLOWED_FIELD_TYPES, SCHEMAS

# Types each template's `{% else %}` branch legitimately covers. Declared here,
# beside the guard, because the naive "every type needs a branch" version fails
# today for good reasons: a plain text input IS the right widget for `text` and
# `string`, and the read-only view renders scalars uniformly.
FALLBACK_TYPES = {
    "entry_edit.html": frozenset({"text", "string"}),
    "entry_view.html": frozenset({"text", "textarea", "int", "select", "string"}),
}

# Each JSON-backed type's mount root, as declared in entry_edit.js's own header
# comment. A type with a handler and a template branch but no mount silently
# discards every edit to it, with the whole server-side suite green.
JSON_TYPE_MOUNTS = {
    "author_list": "#authors-editor",
    "open_access_dict": "#oa-editor",
    "typed_notes": "#notes-editor",
    "simple_notes": ".simple-notes-editor",
    "string_list": ".string-list-editor",
    "audiences_set": ".audiences-set",
}

_TYPE_TEST_RE = re.compile(r"f\.type\s*==\s*'([a-z_]+)'")


def _template_types(name: str) -> set[str]:
    path = cv_editor_pkg_dir() / "templates" / name
    assert path.is_file(), f"{name} not found at {path} — the sweep would be vacuous"
    text = path.read_text()
    assert len(text) > 500, f"{name} is suspiciously small ({len(text)} bytes)"
    return set(_TYPE_TEST_RE.findall(text))


def test_the_sweep_is_not_vacuous():
    """Self-guard: the globs resolve and the registry is populated."""
    assert cv_editor_pkg_dir().is_dir()
    assert (cv_editor_pkg_dir() / "templates").is_dir()
    assert len(FIELD_HANDLERS) >= 13, f"registry has only {len(FIELD_HANDLERS)} types"
    for name in FALLBACK_TYPES:
        assert _template_types(name), f"{name} yielded no `f.type ==` branches"


def test_every_field_type_has_a_widget_branch_or_a_declared_fallback():
    for name, fallback in FALLBACK_TYPES.items():
        branched = _template_types(name)
        missing = set(FIELD_HANDLERS) - branched - fallback
        assert not missing, (
            f"{name} has no branch for {sorted(missing)} and they are not in its "
            f"declared FALLBACK_TYPES. Each would render through the `{{% else %}}` "
            f"plain-text input, which for a non-scalar prints its Python repr into "
            f"the widget — gotcha #96. Add a branch, or add the type to "
            f"FALLBACK_TYPES here with a reason."
        )


def test_no_template_branches_on_a_type_that_does_not_exist():
    """The other direction: a removed type leaves dead template branches."""
    for name in FALLBACK_TYPES:
        stray = _template_types(name) - set(FIELD_HANDLERS)
        assert not stray, (
            f"{name} branches on {sorted(stray)}, which is not a registered field "
            f"type. Either the registry entry was removed and this branch is dead, "
            f"or the branch has a typo and silently never fires."
        )


def test_every_json_field_type_has_a_declared_js_mount():
    assert set(JSON_TYPE_MOUNTS) == set(JSON_FIELD_TYPES), (
        f"JSON_TYPE_MOUNTS is out of step with JSON_FIELD_TYPES: "
        f"only-in-map={sorted(set(JSON_TYPE_MOUNTS) - set(JSON_FIELD_TYPES))}, "
        f"only-in-registry={sorted(set(JSON_FIELD_TYPES) - set(JSON_TYPE_MOUNTS))}. "
        f"A JSON-backed type with no mount discards every edit to it while the "
        f"whole server-side suite stays green."
    )
    js = (cv_editor_pkg_dir() / "static" / "entry_edit.js").read_text()
    assert len(js) > 5000, "entry_edit.js is suspiciously small — vacuous sweep"
    for ftype, selector in JSON_TYPE_MOUNTS.items():
        assert selector in js, f"{ftype}'s mount root {selector!r} is absent from entry_edit.js"


def test_meta_uses_only_the_types_meta_view_can_render():
    """meta_view.html branches on `string_list` and `textarea` only.

    Anything else in META renders through its `<code>{{ v }}</code>` else --
    printing a Python repr on the page the owner lands on. `footer` is a mapping
    and is edited by /meta/footer for exactly this reason; it must not come back
    as a field.
    """
    types = {f["type"] for f in META["fields"]}
    assert types <= META_ALLOWED_FIELD_TYPES, (
        f"META declares {sorted(types - META_ALLOWED_FIELD_TYPES)}, which "
        f"meta_view.html cannot render. This is gotcha #96: `address` was "
        f"`textarea` over a list and `footer` `textarea` over a mapping."
    )
    assert not any(f["name"] == "footer" for f in META["fields"]), (
        "`footer` is a MAPPING and is edited at /meta/footer. Putting it back on "
        "the Meta form re-creates the scalar-widget-over-a-mapping bug."
    )


def test_shape_classes_cover_every_registered_type():
    """A type with no shape class would KeyError inside the render-time guard."""
    from cv_editor.field_handlers import SHAPE_CLASSES

    missing = set(FIELD_HANDLERS) - set(SHAPE_CLASSES)
    assert not missing, (
        f"{sorted(missing)} have handlers but no SHAPE_CLASSES entry. "
        f"`shape_mismatch` would raise KeyError while rendering any form "
        f"carrying one — a 500 on the edit page."
    )


def test_renderer_required_is_only_used_on_list_types():
    """The flag's implementation lives in `_apply_string_list`.

    Setting it on a scalar field would be inert and read as protection that is
    not there.
    """
    for section, sch in SCHEMAS.items():
        for f in sch.get("fields", []):
            if f.get("renderer_required"):
                assert f["type"] == "string_list", (
                    f"{section}.{f['name']} is renderer_required but typed "
                    f"{f['type']!r}; only _apply_string_list honours the flag."
                )
