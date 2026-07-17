"""Whole-corpus, load-time data validation — "friendly validation-on-load" (M5).

DISTINCT from `cv_editor/validate.py`, which validates ONE entry's FORM payload
on the save path. This module walks every `data/*.yml` file as it sits on disk
and surfaces build/save-breaking and data-quality issues with file + line + field
located messages. It REUSES (does not reinvent) the existing guards:

  * ruamel round-trip parse — the full file is parsed (the header is pure
    comments), so `.lc` line numbers are TRUE file lines.
  * `schemas` — per-section field defs + structure.
  * `validate.validate_entry` -> `field_handlers.FIELD_HANDLERS[t].validate` —
    the one source of truth for per-field rules (required, regex, int range,
    select choices). On-disk shapes are adapted to the form shape those handlers
    expect (bare-string authors -> `{name: ...}`).
  * `validate.grant_end_date_warning` — active-grant past-end (date reuse).
  * `yaml_io._validate_publications_data` — the authors-shape invariant
    (gotcha #58); surfaced here as a load-time Error instead of a write refusal.

Scope is deliberately narrow (build/save-breaking + a few high-signal data-quality
classes) to avoid false-positive fatigue. Canonical-variant judgments (journal /
author name variants) stay with `qc_publications.py` — this module makes NO network
calls and never imports qc. It is deterministic, offline, and fast, so it can run
in the `build.sh` preflight (WARN-only) and on the editor index.

Severity tiers: ERROR (would break the build or a save) and WARNING (data quality;
renders but is probably wrong). An INFO/cosmetic tier is intentionally deferred —
it would duplicate the QC report and drown the signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from cv_editor import schemas, validate, yaml_io
from cv_editor.sections import flatten

ERROR = "error"
WARNING = "warning"

# Field types whose values are rendered through Typst `mk()` (eval markup), so an
# unescaped `$` opens math mode and breaks/garbles the build.
_MARKUP_FIELD_TYPES = frozenset({"text", "textarea"})

# A `$` not immediately preceded by a backslash. Grant amounts (`grant_amount`
# type, stored as `\$...`) are excluded because they are escaped and not of a
# markup field type.
_BARE_DOLLAR = re.compile(r"(?<!\\)\$")

# Candidate fields, in priority order, for a human-readable entry label.
_LABEL_FIELDS = (
    "title",
    "award",
    "course",
    "degree",
    "role",
    "name",
    "agency",
    "venue",
)


@dataclass(frozen=True)
class Issue:
    """One located validation finding. `line` is 1-based or None (best-effort:
    present for entry/field nodes that ruamel tracked, None for parse failures
    on scalars). `file` is the canonical `data/<x>.yml` display path.
    `global_idx` is the entry's flat index (for an editor jump-to-edit link), or
    None for file-level issues (parse failure, missing file, authors-shape)."""

    severity: str  # ERROR | WARNING
    section: str  # schema key, e.g. "publications"
    file: str  # "data/publications.yml"
    line: int | None
    entry_label: str
    field: str | None
    message: str
    global_idx: int | None = None


def _rt_yaml() -> YAML:
    """Round-trip loader (gives `.lc` line tracking). Mirrors yaml_io's knobs."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    return y


def _entry_line(entry) -> int | None:
    lc = getattr(entry, "lc", None)
    if lc is not None and getattr(lc, "line", None) is not None:
        return lc.line + 1
    return None


def _field_line(entry, name: str) -> int | None:
    """1-based line of `name:` within an entry, falling back to the entry line."""
    lc = getattr(entry, "lc", None)
    data = getattr(lc, "data", None) if lc is not None else None
    if isinstance(data, dict) and name in data:
        # data[name] = (key_line, key_col, val_line, val_col)
        return data[name][0] + 1
    return _entry_line(entry)


def _entry_label(entry, global_idx: int) -> str:
    if isinstance(entry, dict):
        for f in _LABEL_FIELDS:
            v = entry.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip()[:80]
    return f"entry #{global_idx + 1}"


def _adapt_for_validate(ftype: str, value):
    """Map an on-disk value to the FORM shape `FIELD_HANDLERS[t].validate`
    expects. Only `author_list` differs: on disk an author may be a bare
    string, but the validator does `(a or {}).get("name")` and would crash."""
    if ftype == "author_list" and isinstance(value, list):
        return [a if isinstance(a, dict) else {"name": str(a)} for a in value]
    return value


def check_file(section_key: str, path: Path) -> list[Issue]:
    """Validate one data file against its schema. Read-only."""
    sch = schemas.get(section_key)
    canonical = sch["file"]
    out: list[Issue] = []

    if not path.exists():
        return [Issue(ERROR, section_key, canonical, None, "(file)", None, "data file not found")]

    raw = path.read_text()
    try:
        # Parse the FULL file (header is comment-only) so `.lc` lines are true.
        data = _rt_yaml().load(raw)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        return [
            Issue(
                ERROR,
                section_key,
                canonical,
                line,
                "(file)",
                None,
                f"YAML will not parse: {getattr(e, 'problem', str(e))}",
            )
        ]

    if data is None:
        return out  # empty section file — nothing to validate

    # Authors-shape invariant (gotcha #58): delegate to the write-path guard.
    if section_key == "publications":
        try:
            yaml_io._validate_publications_data(data)
        except yaml_io.CorruptedShapeError as e:
            out.append(Issue(ERROR, section_key, canonical, None, "(authors)", "authors", str(e)))

    fields = sch.get("fields", [])
    by_name = {f["name"]: f for f in fields}

    for rec in flatten(data, sch["structure"]):
        entry = rec["entry"]
        if not isinstance(entry, dict):
            continue
        gidx = rec["global_idx"]
        label = _entry_label(entry, gidx)

        def add(severity: str, field: str | None, message: str) -> None:
            out.append(
                Issue(
                    severity,
                    section_key,
                    canonical,
                    _field_line(entry, field) if field else _entry_line(entry),
                    label,
                    field,
                    message,
                    gidx,
                )
            )

        # (a) Reuse the per-entry field validator (required + regex + int range
        # + select). Adapt author_list to the form shape it expects.
        form_shaped = {
            name: _adapt_for_validate(by_name[name]["type"], v)
            for name, v in entry.items()
            if name in by_name
        }
        for fname, msg in validate.validate_entry(form_shaped, fields).items():
            add(WARNING, fname, "required field is empty" if msg == "required" else msg)

        # (b) Build-breaker + data-quality scans the per-field validator can't see.
        for fname, f in by_name.items():
            v = entry.get(fname)
            if v is None:
                continue
            ftype = f["type"]
            # Unescaped `$` in a markup field opens Typst math mode -> build break.
            if ftype in _MARKUP_FIELD_TYPES and isinstance(v, str) and _BARE_DOLLAR.search(v):
                add(ERROR, fname, r"unescaped '$' opens Typst math mode — write '\$'")
            # Quoted-numeric coercion: pmid/volume/issue are `string` type but
            # got loaded as a bare number (unquoted in YAML).
            if ftype == "string" and isinstance(v, int) and not isinstance(v, bool):
                add(
                    WARNING,
                    fname,
                    f"stored as a number; quote it ({fname}: '{v}') so YAML keeps it a string",
                )

        # (c) Active-grant past-end (reuse validate.grant_end_date_warning).
        if section_key == "research_support":
            warn = validate.grant_end_date_warning(entry)
            if warn:
                add(WARNING, "date", warn)

    return out


def check_data(data_dir: Path | str | None = None) -> list[Issue]:
    """Validate every section file under `data_dir` (default: the real data/).
    Returns a flat list of Issues (callers bucket by severity)."""
    base = Path(data_dir) if data_dir is not None else yaml_io.DATA
    out: list[Issue] = []
    for key in schemas.all_sections():
        fname = Path(schemas.get(key)["file"]).name
        out.extend(check_file(key, base / fname))
    return out


def summarize(issues: list[Issue]) -> dict[str, int]:
    """Counts by severity, e.g. {'error': 1, 'warning': 4}."""
    counts = {ERROR: 0, WARNING: 0}
    for i in issues:
        counts[i.severity] = counts.get(i.severity, 0) + 1
    return counts


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == ERROR for i in issues)
