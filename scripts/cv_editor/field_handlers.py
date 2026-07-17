"""Field-type dispatch table (V20, 2026-05-18 — B2 refactor).

Each schema field has a `type:` ("text", "int", "author_list", etc.).
Three parallel if/elif chains used to dispatch over this:
    app.py:_form_to_entry        — form payload → CommentedMap entry
    validate.py:validate_entry   — form payload → error dict
    templates/entry_edit.html    — type → input widget

The first two collapse into a single `FIELD_HANDLERS` dict keyed by
type name; the template chain stays scope-coupled to the JS extraction
(B3) because it's tied to the inline-JS dispatch.

Threshold to extract: V8's `open_access_dict` was the 13th field type
(per V5-D R2-H4 / V18-A-D R2-M4 deferred-refactor note). V20 finally
ships it.

Design:

* Each handler is a tiny `FieldHandler` dataclass exposing `apply`
  (mutates the in-progress CommentedMap entry from form data) and
  `validate` (returns an error message or None).
* `apply` mutates the entry directly rather than returning a value,
  since a handler may need to write auxiliary keys beyond its own
  field name. A "return value or DELETE sentinel" API can't express
  that without callbacks.
* `validate` runs only on non-empty values. The required+empty case
  is handled by the dispatcher in `validate.validate_entry`, not by
  per-handler code.
* Both signatures are stable: `(form, field, entry)` for apply,
  `(value, field)` for validate.
* `schemas.py` imports `FIELD_HANDLERS` at module load and asserts
  every field type is registered (fail-fast on a typo'd `"type":`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import yaml as pyyaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from cv_editor import author_names, notes_helpers

# ---- apply handlers (form, field_schema, entry → mutates entry) -----


def _apply_int(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    v = form.get(name)
    if v in (None, "", "None"):
        entry.pop(name, None)
        return
    try:
        entry[name] = int(v)
    except (TypeError, ValueError):
        # Defensive (M1, 2026-05-29): the save route runs validate_entry
        # (which rejects non-integers) BEFORE this, so a bad value only
        # reaches here via non-validated callers like the pending-form
        # re-render (app.py:_form_to_entry). Drop it rather than 500;
        # the validation pass surfaces the real error to the user.
        entry.pop(name, None)


def _apply_bool(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    if form.get(name):
        entry[name] = True
    else:
        entry.pop(name, None)


def _apply_string(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    v = form.get(name)
    if v in (None, ""):
        entry.pop(name, None)
    else:
        entry[name] = str(v).strip()


def _apply_select(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    v = form.get(name)
    sv = str(v).strip() if v is not None else ""
    if sv == "":
        entry.pop(name, None)
    else:
        entry[name] = sv


def _apply_string_list(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    items = [s.strip() for s in (form.get(name) or []) if (s or "").strip()]
    if items:
        entry[name] = CommentedSeq(items)
    else:
        entry.pop(name, None)


def _apply_audiences_set(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    items = [s for s in (form.get(name) or []) if s]
    if items:
        seq = CommentedSeq(items)
        seq.fa.set_flow_style()
        entry[name] = seq
    else:
        entry.pop(name, None)


def _apply_grant_amount(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    v = form.get(name)
    if v in (None, ""):
        entry.pop(name, None)
        return
    sv = str(v).strip()
    # Strip a leading $ or \$, then re-add the literal backslash-dollar
    # so single-quoted YAML stores the escape the renderer's mk() needs.
    if sv.startswith(r"\$"):
        body = sv[2:]
    elif sv.startswith("$"):
        body = sv[1:]
    else:
        body = sv
    entry[name] = "\\$" + body


def _apply_author_list(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    raw = form.get(name) or []
    # Task #30 defensive guard (2026-05-25): if upstream handed us a
    # string (e.g., from a malformed authors_json or a YAML stub with
    # `authors: a; b; c; d`), do NOT iterate it character-by-character.
    # Coerce into a single-name list so the user can edit + re-save
    # without producing N one-char author rows.
    if isinstance(raw, str):
        raw = [{"name": raw}]
    elif not isinstance(raw, list):
        raw = []
    authors = CommentedSeq()
    for a in raw:
        if not isinstance(a, dict):
            a = {"name": str(a)}
        if not (a.get("name") or "").strip():
            continue
        authors.append(author_names.form_to_yaml_author(a))
    entry[name] = authors


def _apply_typed_notes(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    yaml_seq = notes_helpers.notes_form_to_yaml(form.get(name) or [])
    if yaml_seq:
        entry[name] = yaml_seq
    else:
        entry.pop(name, None)


def _apply_simple_notes(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    yaml_seq = notes_helpers.simple_notes_form_to_yaml(form.get(name) or [])
    if yaml_seq:
        entry[name] = yaml_seq
    else:
        entry.pop(name, None)


def _apply_open_access_dict(form: dict, f: dict, entry: CommentedMap) -> None:
    name = f["name"]
    yaml_oa = notes_helpers.open_access_form_to_yaml(form.get(name) or {})
    if yaml_oa:
        entry[name] = yaml_oa
    else:
        entry.pop(name, None)


def _apply_text(form: dict, f: dict, entry: CommentedMap) -> None:
    """Shared handler for "text" and "textarea" — same semantics."""
    name = f["name"]
    v = form.get(name)
    if v in (None, ""):
        entry.pop(name, None)
    else:
        entry[name] = str(v).strip()


# ---- validate handlers (value, field_schema → str | None) -----------


def _validate_int(v: Any, f: dict) -> str | None:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return "must be an integer"
    lo, hi = f.get("min"), f.get("max")
    if lo is not None and iv < lo:
        return f"must be >= {lo}"
    if hi is not None and iv > hi:
        return f"must be <= {hi}"
    return None


def _validate_string_regex(v: Any, f: dict) -> str | None:
    """Shared between text / textarea / string / grant_amount."""
    import re

    sv = str(v)
    rx = f.get("regex")
    if rx and not re.search(rx, sv):
        return f"must match {rx}"
    return None


def _validate_select(v: Any, f: dict) -> str | None:
    choices = f.get("choices") or []
    if str(v) not in [str(c) for c in choices]:
        nonblank = [str(c) for c in choices if c != ""]
        return f"must be one of: {', '.join(nonblank)}"
    return None


def _validate_string_list(v: Any, f: dict) -> str | None:
    if not isinstance(v, list):
        return "must be a list"
    if f.get("required") and not any((s or "").strip() for s in v):
        return "at least one entry required"
    return None


def _validate_audiences_set(v: Any, f: dict) -> str | None:
    if not isinstance(v, list):
        return "must be a list"
    allowed = set(f.get("choices") or [])
    for item in v:
        if item not in allowed:
            return f"unknown audience: {item}"
    return None


def _validate_author_list(v: Any, f: dict) -> str | None:
    if not isinstance(v, list) or not v:
        return "at least one author required"
    for i, a in enumerate(v):
        n = ((a or {}).get("name") or "").strip()
        if not n:
            return f"author #{i + 1} has no name"
    return None


def _validate_typed_notes(v: Any, f: dict) -> str | None:
    try:
        parsed = pyyaml.safe_load(v) if isinstance(v, str) else v
    except pyyaml.YAMLError as e:
        return f"invalid YAML: {e}"
    if parsed is not None and not isinstance(parsed, list):
        return "must be a YAML list (or empty)"
    return None


def _validate_simple_notes(v: Any, f: dict) -> str | None:
    if not isinstance(v, list):
        return "must be a list"
    return None


def _validate_open_access_dict(v: Any, f: dict) -> str | None:
    if not isinstance(v, dict):
        return "must be a dict"
    return None


def _validate_noop(v: Any, f: dict) -> str | None:
    return None


# ---- registry -------------------------------------------------------


@dataclass(frozen=True)
class FieldHandler:
    name: str  # type-name string (for debugging + repr)
    apply: Callable[[dict, dict, CommentedMap], None]
    validate: Callable[[Any, dict], str | None]
    # V20-cleanup T2 (2026-05-18): is_json + empty_factory replace
    # `_JSON_FIELD_TYPES` and `_empty_json_value` (previously hand-
    # maintained in app.py). Single source of truth — when a new
    # JSON-shaped field type lands, only this registry changes.
    is_json: bool = False
    empty_factory: Callable[[], Any] | None = None


FIELD_HANDLERS: dict[str, FieldHandler] = {
    "int": FieldHandler("int", _apply_int, _validate_int),
    "bool": FieldHandler("bool", _apply_bool, _validate_noop),
    "string": FieldHandler("string", _apply_string, _validate_string_regex),
    "select": FieldHandler("select", _apply_select, _validate_select),
    "string_list": FieldHandler(
        "string_list",
        _apply_string_list,
        _validate_string_list,
        is_json=True,
        empty_factory=list,
    ),
    "audiences_set": FieldHandler(
        "audiences_set",
        _apply_audiences_set,
        _validate_audiences_set,
        is_json=True,
        empty_factory=list,
    ),
    "grant_amount": FieldHandler(
        "grant_amount",
        _apply_grant_amount,
        _validate_string_regex,
    ),
    "author_list": FieldHandler(
        "author_list",
        _apply_author_list,
        _validate_author_list,
        is_json=True,
        empty_factory=list,
    ),
    "typed_notes": FieldHandler(
        "typed_notes",
        _apply_typed_notes,
        _validate_typed_notes,
        is_json=True,
        empty_factory=list,
    ),
    "simple_notes": FieldHandler(
        "simple_notes",
        _apply_simple_notes,
        _validate_simple_notes,
        is_json=True,
        empty_factory=list,
    ),
    "open_access_dict": FieldHandler(
        "open_access_dict",
        _apply_open_access_dict,
        _validate_open_access_dict,
        is_json=True,
        empty_factory=dict,
    ),
    "text": FieldHandler("text", _apply_text, _validate_string_regex),
    "textarea": FieldHandler("textarea", _apply_text, _validate_string_regex),
}


# Derived from the registry — single source of truth.
JSON_FIELD_TYPES: frozenset[str] = frozenset(k for k, h in FIELD_HANDLERS.items() if h.is_json)


def empty_json_value(ftype: str) -> Any:
    """Empty value for a JSON-hidden field type. Wraps the handler's
    `empty_factory` so callers don't have to know about the dispatch
    table internals."""
    handler = FIELD_HANDLERS[ftype]
    if handler.empty_factory is None:
        raise ValueError(f"empty_json_value called on non-JSON type {ftype!r}")
    return handler.empty_factory()


def assert_schemas_covered(schemas: dict) -> None:
    """Fail-fast guard: every `type:` referenced in any schema must
    have a registered handler. Call once at import time from
    schemas.py. Catches typos in schema declarations before any route
    fires (worth doing — `_form_to_entry` used to fall through to the
    `else: text` branch on unknown types, silently coercing).
    """
    unknown: list[str] = []
    for section, sch in schemas.items():
        for f in sch.get("fields", []):
            ftype = f.get("type")
            if ftype not in FIELD_HANDLERS:
                unknown.append(f"{section}.{f.get('name')}={ftype!r}")
    if unknown:
        raise ImportError(
            "Unknown field type(s) in schemas: "
            + ", ".join(unknown)
            + f". Registered types: {sorted(FIELD_HANDLERS)}",
        )
