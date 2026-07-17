"""Discover, resolve, and validate the typography knobs for the editor UI.

Single source of truth is `templates/bespoke/lib/typography.typ`: each knob `#let`
carries a trailing `// @ty group=<G> label="<L>"` annotation. We parse those rather
than maintaining a parallel Python list (the drift `author_flags.py` was built to
prevent). A knob without an annotation is a bug caught by tests/test_advanced_typography.py.

The editor persists overrides to `data/meta.yml` under a `typography:` block keyed by the
bare knob name (e.g. `body_leading`, `name_color`). `templates/bespoke/lib/typography.typ`
reads that block as the middle resolution tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cv_editor import paths

# P5 (paper_trail inversion): this module is the `typography` CAPABILITY's
# implementation. It assumes the bespoke template's knob file and is only ever
# reached when the typography capability is present (i.e. bespoke is the active
# template — see cv_editor/capabilities.py + style_routes gating). No behaviour
# change for the private repo; a public modern-only tree simply never calls it.
TYPST_ROOT = paths.project_root()  # engine: templates/bespoke/lib/typography.typ
TYPOGRAPHY_TYP = TYPST_ROOT / "templates" / "bespoke" / "lib" / "typography.typ"


@paths.on_configure
def _refresh_paths() -> None:
    global TYPST_ROOT, TYPOGRAPHY_TYP
    TYPST_ROOT = paths.project_root()
    TYPOGRAPHY_TYP = TYPST_ROOT / "templates" / "bespoke" / "lib" / "typography.typ"


# #let body-leading = _len("ty_body_leading", 0.58em)  // @ty group=Body label="Body line spacing"
_KNOB_RE = re.compile(
    r'#let\s+[\w-]+\s*=\s*(_len|_color|_int|_str)\(\s*"(ty_\w+)"\s*,\s*(.+?)\)\s*'
    r'//\s*@ty\s+group=(\S+)\s+label="([^"]*)"'
)
# any knob-shaped #let (to detect ones MISSING the annotation)
_ANY_KNOB_RE = re.compile(r'#let\s+[\w-]+\s*=\s*(?:_len|_color|_int|_str)\(\s*"(ty_\w+)"')

_HEX_RE = re.compile(r"#?[0-9A-Fa-f]{6}")
_LEN_RE = re.compile(r"^\d*\.?\d+(pt|in|em|mm|cm)$")
_COLOR_IN_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")
_INT_RE = re.compile(r"^\d+$")

_KIND_FROM_HELPER = {"_len": "len", "_color": "color", "_int": "int", "_str": "str"}


@dataclass
class Knob:
    key: str  # ty_body_leading
    meta_key: str  # body_leading
    kind: str  # len | color | int | str
    default: str  # normalised string form ("0.58em", "#000000", "300", "Libertinus Serif")
    group: str
    label: str


def _normalise_default(kind: str, raw: str) -> str:
    raw = raw.strip()
    if kind == "color":
        m = _HEX_RE.search(raw)
        return ("#" + m.group(0).lstrip("#")).upper() if m else raw
    if kind == "str":
        return raw.strip().strip('"')
    return raw  # len / int verbatim


def discover_knobs(path: Path = TYPOGRAPHY_TYP) -> list[Knob]:
    text = path.read_text()
    knobs: list[Knob] = []
    annotated_keys: set[str] = set()
    for line in text.splitlines():
        m = _KNOB_RE.search(line)
        if not m:
            continue
        helper, key, raw_default, group, label = m.groups()
        kind = _KIND_FROM_HELPER[helper]
        knobs.append(
            Knob(
                key=key,
                meta_key=key[3:],
                kind=kind,
                default=_normalise_default(kind, raw_default),
                group=group,
                label=label,
            )
        )
        annotated_keys.add(key)

    # Drift guard: every knob-shaped #let must be annotated.
    all_keys = {m.group(1) for m in _ANY_KNOB_RE.finditer(text)}
    missing = all_keys - annotated_keys
    if missing:
        raise ValueError(
            f"typography.typ knobs missing a `// @ty group= label=` annotation: {sorted(missing)}"
        )
    return knobs


def grouped_knobs(knobs: list[Knob] | None = None) -> dict[str, list[Knob]]:
    knobs = knobs if knobs is not None else discover_knobs()
    groups: dict[str, list[Knob]] = {}
    for k in knobs:
        groups.setdefault(k.group, []).append(k)
    return groups


def resolve_current(knobs: list[Knob], meta_typography: dict | None) -> list[dict]:
    """Overlay the saved meta.typography block onto code defaults. Returns one dict per
    knob with the effective value + is_modified (override present and != default)."""
    meta_typography = meta_typography or {}
    out = []
    for k in knobs:
        override = meta_typography.get(k.meta_key)
        has = override is not None and str(override).strip() != ""
        value = str(override) if has else k.default
        # Normalise a colour override only if it's actually valid hex — otherwise
        # show the user's invalid input verbatim on the error re-render (don't mangle
        # "not-a-colour" into "#NOT-A-COLOUR").
        if k.kind == "color" and has and _COLOR_IN_RE.match(value.strip()):
            value = "#" + value.strip().lstrip("#").upper()
        out.append(
            {
                "key": k.key,
                "meta_key": k.meta_key,
                "kind": k.kind,
                "group": k.group,
                "label": k.label,
                "default": k.default,
                "value": value,
                "is_modified": has and value != k.default,
            }
        )
    return out


def validate_value(kind: str, value: str) -> tuple[bool, str, str]:
    """Return (ok, normalised_value, error). Empty value means 'reset to default'
    (handled by the caller dropping the key), so empty is OK here."""
    v = (value or "").strip()
    if v == "":
        return True, "", ""
    if kind == "len":
        return (
            (True, v, "")
            if _LEN_RE.match(v)
            else (False, v, "expected a length like 11pt / 0.58em / 0.9in")
        )
    if kind == "color":
        if not _COLOR_IN_RE.match(v):
            return False, v, "expected a 6-digit hex colour like #404040"
        return True, "#" + v.lstrip("#").upper(), ""
    if kind == "int":
        return (True, v, "") if _INT_RE.match(v) else (False, v, "expected a whole number")
    return True, v, ""  # str: passthrough
