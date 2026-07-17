"""
Build-variant helpers for the Style editor (V4).

Each variant in `data/meta.yml` → `build_variants:` is a `{filename, inputs}`
record. The `inputs:` dict maps Typst `--input` keys to string-encoded
values; the renderer reads them from `sys.inputs` in `lib/flags.typ`.

This module:
- normalizes form ↔ YAML conversions (CommentedMap-aware so meta.yml's
  comments and YAML conventions docstring survive round-trips),
- enumerates display chips for the list view ("audience: academic",
  "show_dollars: off", etc.),
- builds the `typst compile` argv to compile just one variant,
- approximates an impact preview (per-section count of entries that a
  given audience + show_highlighted combination would render) without
  actually invoking Typst.
"""

from __future__ import annotations

import re
from typing import Iterable

from ruamel.yaml.comments import CommentedMap

# Renderer defaults — must match lib/flags.typ. If you change a default
# there, mirror it here. (V4-D code review flagged this duplication; it's
# tolerable while the renderer + editor stay in lockstep.)
#
# The audience vocabulary is DATA-DRIVEN: a generic base set, widened with
# whatever audiences the corpus actually uses (build-variant inputs + entry
# audiences/hide-from). Nothing personal or institution-specific is baked in
# here — see audience_choices(). EDITOR-ONLY: the renderer's visibility
# predicate (lib/flags.typ:visible) is fully data-driven and never reads this.
BASE_AUDIENCES = ("full", "academic", "industry", "public-health")
DEFAULT_AUDIENCE = "full"

# Generic fallback build-variant name when meta.yml defines none (a blank/
# example corpus). The live default is always the FIRST build variant.
DEFAULT_VARIANT_FALLBACK = "cv"


def default_variant_name(meta: dict | None = None) -> str:
    """The default build-variant filename = the FIRST entry in meta.yml's
    `build_variants:` list, else the generic DEFAULT_VARIANT_FALLBACK. This is
    the shared quick-rebuild / staleness-gauge / export / freeze default; it
    hardcodes no personal variant name."""
    for v in (meta or {}).get("build_variants") or []:
        name = (v.get("filename") or "").strip()
        if name:
            return name
    return DEFAULT_VARIANT_FALLBACK


def audience_choices(meta: dict | None = None, load_data=None) -> tuple[str, ...]:
    """Audience vocabulary = BASE_AUDIENCES ∪ every audience present in the
    data, so an audience the user already uses always validates and the editor
    offers it. Sources: each build variant's `inputs.audience`, and — when a
    `load_data(section_key) -> data` callback is supplied — every entry's
    `audiences:` / `hide-from:` list across sections. Order is stable: base
    first, then any extras sorted. EDITOR-ONLY (does not affect the PDF)."""
    extras: set[str] = set()
    for v in (meta or {}).get("build_variants") or []:
        aud = str((v.get("inputs") or {}).get("audience") or "").strip()
        if aud:
            extras.add(aud)
    if load_data is not None:
        from cv_editor import schemas as _schemas

        for key in _schemas.all_sections():
            if key == "meta":
                continue
            try:
                data = load_data(key)
            except Exception:
                continue
            structure = _schemas.get(key)["structure"]
            for entry in _walk_entries(data, structure):
                if not isinstance(entry, dict):
                    continue
                for field_name in ("audiences", "hide-from"):
                    for a in entry.get(field_name) or []:
                        a = str(a).strip()
                        if a:
                            extras.add(a)
    extras -= set(BASE_AUDIENCES)
    return BASE_AUDIENCES + tuple(sorted(extras))


BOOLEAN_INPUTS = (
    "review",
    "show_pending",
    "show_oa",
    "show_citations",
    "show_contributions",
    "show_notes",
    "show_media",
    "show_hidden_media",
    "show_highlighted",
    "show_future",
)
# Default-FALSE renderer flags (rendered off unless --input <key>=true).
# `BOOLEAN_INPUTS` is "default false; checkbox unchecked == renderer default".

# Default-TRUE renderer flags (rendered on unless --input <key>=false).
# Carve-out from BOOLEAN_INPUTS because the persistence semantics differ:
# - default_form: prefill checked
# - form_to_variant: persist ONLY when unchecked (elide the common case)
# - variant_to_form: missing key reads as checked
# - variant_chips: chip ONLY when explicitly off
# - app.py:style_save: form-build block MUST read the field explicitly
#   (this list was a 5-touchpoint carve-out before the 2026-05-25
#   post-batch refactor; extracting it here prevents a recurrence of
#   Stage D's CRITICAL bug where style_save forgot to read
#   show_media_urls and the checkbox was silently inert).
DEFAULT_TRUE_INPUTS = (
    "show_dollars",
    "show_media_urls",
)

FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---- form <-> YAML ----


def default_form() -> dict:
    """Return the form-shaped dict for a brand-new variant (all flags off,
    audience unset). Used by `/style/new`."""
    out = {"filename": "", "audience": ""}
    for key in DEFAULT_TRUE_INPUTS:
        out[key] = True
    for key in BOOLEAN_INPUTS:
        out[key] = False
    return out


_KNOWN_INPUT_KEYS = frozenset({"audience", *DEFAULT_TRUE_INPUTS, *BOOLEAN_INPUTS})


def form_to_variant(
    form: dict, existing: CommentedMap | None = None, *, audiences: Iterable[str] | None = None
) -> CommentedMap:
    """Convert the editor's form dict into a CommentedMap suitable for
    placement into meta.yml's `build_variants:` list.

    `audience == "full"` is preserved as explicit `audience: full` rather
    than being elided (V4 reviewer-A HIGH#2): some YAML variants
    historically carry `audience: full` to signal intent, distinct from
    omitting the key. The form's empty string `""` continues to mean
    "omit the key entirely."

    Reviewer-1 LOW V5-D: if `existing` is provided, unknown input keys
    (e.g., a custom renderer flag the form doesn't surface yet) are
    preserved on round-trip. Form-controlled keys are still authoritative.
    """
    variant = existing if isinstance(existing, CommentedMap) else CommentedMap()
    variant["filename"] = (form.get("filename") or "").strip()
    # Start from existing inputs to preserve any unknown keys; clear
    # form-controlled keys so checkboxes can unset them.
    existing_inputs = (
        variant.get("inputs") if isinstance(variant.get("inputs"), CommentedMap) else None
    )
    inputs = CommentedMap()
    if existing_inputs is not None:
        for k, v in existing_inputs.items():
            if k not in _KNOWN_INPUT_KEYS:
                inputs[k] = v
    aud = (form.get("audience") or "").strip()
    choices = tuple(audiences) if audiences is not None else audience_choices()
    if aud in choices:
        inputs["audience"] = aud
    for key in BOOLEAN_INPUTS:
        if form.get(key):
            inputs[key] = True
    # Default-true flags: persist ONLY when explicitly unchecked. Keeping
    # the common case absent avoids littering meta.yml with redundant
    # `show_dollars: true` / `show_media_urls: true` keys.
    for key in DEFAULT_TRUE_INPUTS:
        if not form.get(key, True):
            inputs[key] = False
    variant["inputs"] = inputs
    return variant


def variant_to_form(variant) -> dict:
    """Convert a stored variant dict into the form-friendly shape."""
    inputs = variant.get("inputs") or {}
    out = {
        "filename": str(variant.get("filename", "")),
        "audience": str(inputs.get("audience", "")) if inputs.get("audience") else "",
    }
    # Default-true: missing key reads as checked; explicit False reads as unchecked.
    for key in DEFAULT_TRUE_INPUTS:
        out[key] = inputs.get(key, True) is not False
    for key in BOOLEAN_INPUTS:
        out[key] = bool(inputs.get(key))
    return out


def validate_form(
    form: dict,
    *,
    existing_filenames: Iterable[str] = (),
    audiences: Iterable[str] | None = None,
) -> dict[str, str]:
    """Returns {field: error} for any failed checks. `audiences` is the allowed
    audience vocabulary (default: audience_choices() = the generic base set);
    callers pass the data-widened set so the user's own audiences validate."""
    errors: dict[str, str] = {}
    fname = (form.get("filename") or "").strip()
    if not fname:
        errors["filename"] = "required"
    elif not FILENAME_RE.match(fname):
        errors["filename"] = (
            "must start with a-z/0-9 and contain only lowercase letters, digits, '-', '_'"
        )
    elif fname in set(existing_filenames):
        errors["filename"] = f"a variant already uses this filename ({fname}.pdf)"
    aud = (form.get("audience") or "").strip()
    choices = tuple(audiences) if audiences is not None else audience_choices()
    if aud and aud not in choices:
        errors["audience"] = f"must be one of: {', '.join(choices)} (or blank)"
    return errors


# ---- display chips ----


def variant_chips(variant) -> list[dict]:
    """Per-variant summary chips for the list view. Each chip is
    {label, kind} where kind ∈ {audience, on, off}."""
    chips: list[dict] = []
    inputs = variant.get("inputs") or {}
    aud = inputs.get("audience")
    if aud:
        chips.append({"label": f"audience: {aud}", "kind": "audience"})
    for key in BOOLEAN_INPUTS:
        if inputs.get(key):
            chips.append({"label": key.replace("_", " "), "kind": "on"})
    # Default-true flags: chip ONLY when EXPLICITLY false. Default-true
    # is silent (most variants will be silent here). The "is False"
    # check (not falsy) is intentional — missing key = default-true.
    for key in DEFAULT_TRUE_INPUTS:
        if inputs.get(key) is False:
            chips.append({"label": f"{key}: off", "kind": "off"})
    if not chips:
        chips.append({"label": "default (no flags set)", "kind": "default"})
    return chips


# ---- impact preview ----


def is_visible(entry, audience: str) -> bool:
    """Mirror lib/flags.typ visible(audiences, hide-from). entry can be any
    dict with optional `audiences` / `hide-from` keys."""
    hide_from = entry.get("hide-from") or []
    if audience in hide_from:
        return False
    audiences = entry.get("audiences") or []
    return audience == "full" or not audiences or audience in audiences


def _walk_entries(data, structure: str):
    """Yield every leaf entry for the four structures, ignoring meta /
    single_record sections."""
    from cv_editor import sections

    if structure == "single_record" or data is None:
        return
    for rec in sections.flatten(data, structure):
        yield rec["entry"]


def impact_preview(load_data, audience: str, show_highlighted: bool) -> dict:
    """Approximate what a build with these flags would emit.

    `load_data(key)` is a callback returning just the data tree for the
    given section. Schema is looked up via `schemas.get(key)`. Returns
    `{per_section: {key: {visible, total, label, error?}}, total_visible,
    total_total, audience, show_highlighted}`.

    Failures to load a section's YAML record an `error` row instead of
    silently dropping the section (so the preview never lies about coverage).
    """
    import yaml as _pyyaml

    from cv_editor import schemas as _schemas

    per_section: dict[str, dict[str, int | str]] = {}
    total_visible = 0
    total_total = 0
    audience = audience or DEFAULT_AUDIENCE
    for key in _schemas.all_sections():
        if key == "meta":
            continue
        sch = _schemas.get(key)
        try:
            data = load_data(key)
        except (FileNotFoundError, OSError, _pyyaml.YAMLError) as e:
            per_section[key] = {
                "visible": 0,
                "total": 0,
                "label": sch["label"],
                "error": f"could not load: {type(e).__name__}: {e}",
            }
            continue
        visible = 0
        total = 0
        for entry in _walk_entries(data, sch["structure"]):
            total += 1
            if not is_visible(entry, audience):
                continue
            if entry.get("highlighted") and not show_highlighted:
                continue
            visible += 1
        per_section[key] = {"visible": visible, "total": total, "label": sch["label"]}
        total_visible += visible
        total_total += total
    return {
        "per_section": per_section,
        "total_visible": total_visible,
        "total_total": total_total,
        "audience": audience,
        "show_highlighted": show_highlighted,
    }


# ---- typst compile argv for a single variant ----


class InvalidVariantError(ValueError):
    """Raised when a variant is unsafe to build (e.g., its filename failed
    re-validation against FILENAME_RE). Surfaces at build time so a
    hand-edited meta.yml can't sneak a path-traversal filename past the
    form validator."""


def variant_inputs_map(variant) -> dict[str, str]:
    """Return a variant's `inputs` as a {key: string-value} dict, encoded the
    way Typst's `--input` expects (bools → "true"/"false"). Shared by
    `variant_typst_argv` (build) and the freeze flatten path."""
    out: dict[str, str] = {}
    for k, v in (variant.get("inputs") or {}).items():
        out[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
    return out


def variant_typst_argv(variant) -> list[str]:
    """Build the `typst compile` argv for one variant. The caller is
    responsible for cwd=ROOT and lock acquisition. Filename is re-validated
    here (defense-in-depth) so a hand-edited meta.yml can't introduce a
    path-traversal output path."""
    filename = (variant.get("filename") or "").strip()
    if not filename or not FILENAME_RE.match(filename):
        raise InvalidVariantError(
            f"variant filename {filename!r} is invalid; must match {FILENAME_RE.pattern}"
        )
    cmd = [
        "typst",
        "compile",
        "--font-path",
        "fonts",
        "--ignore-system-fonts",
        "cv.typ",
        f"output/{filename}.pdf",
    ]
    for k, sv in variant_inputs_map(variant).items():
        cmd.extend(["--input", f"{k}={sv}"])
    return cmd
