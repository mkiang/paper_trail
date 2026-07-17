"""
Per-section field schemas. Each schema drives form generation,
validation, list-view columns, and the structure-aware navigation in
sections.py.

Field types understood by the form renderer:

    text          - single-line input
    textarea      - multi-line input (titles can wrap)
    int           - numeric input
    string        - quoted-string input (PMID, volume, issue — kept as
                    YAML string even when numeric)
    bool          - checkbox
    select        - dropdown of choices
    string_list   - editable list of strings (one per row)
    audiences_set - checkbox set over the fixed audience tags
    grant_amount  - text input; display strips a leading "\\$", store
                    re-adds it (single-quoted YAML)
    author_list   - dynamic author list editor (publications)
    typed_notes   - rich notes editor with type dropdown (publications)
    simple_notes  - simpler notes editor: list of {text, highlighted?}
    open_access_dict - paper/code/data toggles + URLs (publications)

Schema dict keys:

    file              - path under typst/
    label             - human-readable section name
    structure         - one of sections.STRUCTURES
    list_columns      - which fields show in the list-view table
    fields            - per-leaf-entry field schema
    cluster_fields    - per-cluster field schema (clusters /
                        subsections_of_clusters only)
    subsections       - allowed subsection names (list_of_subsections /
                        subsections_of_clusters only)
    default_subsection - default for "manual add" picker
    target_help       - one-line tooltip explaining the target picker
"""

from __future__ import annotations

from cv_editor import build_variants as _bv

# Audience vocabulary for the entry `audiences:` / `hide-from:` allowlists.
# DATA-DRIVEN: starts as the generic base set and is widened at import with
# every audience the corpus actually uses (see _widen_audiences_from_data at
# the end of this module), so the user's own audiences always validate and the
# edit form offers them. No personal/institution audience is hardcoded here.
# EDITOR-ONLY — the renderer's visible() (lib/flags.typ) is fully data-driven.
AUDIENCES = list(_bv.BASE_AUDIENCES)

# ---- shared field bits ----

_AUDIENCES_FIELD = {
    "name": "audiences",
    "type": "audiences_set",
    "label": "Audiences (allowlist; empty = visible to all)",
    "choices": AUDIENCES,
}
_HIDE_FROM_FIELD = {
    "name": "hide-from",
    "type": "audiences_set",
    "label": "Hide from (blocklist; wins over audiences)",
    "choices": AUDIENCES,
}
_HIGHLIGHTED_FIELD = {
    "name": "highlighted",
    "type": "bool",
    "label": "Hidden by default (gated by --input show_highlighted=true)",
}


# ---------- Publications ----------

PUBLICATIONS = {
    "file": "data/publications.yml",
    "label": "Publications",
    "structure": "list_of_subsections",
    "list_columns": ["year", "title", "first_author", "subsection"],
    "subsections": [
        "Peer-Reviewed Original Research",
        "Other Peer-reviewed Publications",
        "Other Scholarly Work (Not Peer-reviewed)",
    ],
    "default_subsection": "Peer-Reviewed Original Research",
    "fields": [
        {
            "name": "title",
            "type": "textarea",
            "required": True,
            "label": "Title",
            "placeholder": "Sentence case. Use _italics_, *bold*, --- for em-dash, -- for en-dash.",
        },
        {"name": "authors", "type": "author_list", "required": True, "label": "Authors"},
        {
            "name": "journal",
            "type": "text",
            "required": True,
            "label": "Journal",
            "placeholder": "e.g., JAMA Internal Medicine",
        },
        {"name": "year", "type": "int", "required": True, "label": "Year"},
        {
            "name": "month",
            "type": "int",
            "min": 1,
            "max": 12,
            "label": "Month",
            "placeholder": "1-12",
        },
        {"name": "day", "type": "int", "min": 1, "max": 31, "label": "Day", "placeholder": "1-31"},
        {"name": "volume", "type": "string", "label": "Volume"},
        {"name": "issue", "type": "string", "label": "Issue"},
        {
            "name": "pages",
            "type": "text",
            "label": "Pages",
            "placeholder": "e.g., 300-12 or e069008 or 776-778",
        },
        {
            "name": "doi",
            "type": "text",
            "regex": r"^10\.\d{4,9}/",
            "label": "DOI",
            "placeholder": "10.NNNN/...",
        },
        {
            "name": "epub_date",
            "type": "text",
            "label": "Epub date",
            "placeholder": "e.g., 2025 Sep 18  (free-form YYYY Mon DD)",
        },
        {
            "name": "pmid",
            "type": "string",
            "regex": r"^\d+$",
            "label": "PMID",
            "placeholder": "8-digit PubMed ID",
        },
        {
            "name": "pmcid",
            "type": "text",
            "regex": r"^PMC\d+$",
            "label": "PMCID",
            "placeholder": "PMC followed by digits",
        },
        {
            "name": "date_qualifier",
            "type": "text",
            "label": "Date qualifier",
            "placeholder": "rare; e.g., 'Special Issue' or 'Supplement'",
        },
        {"name": "open_access", "type": "open_access_dict", "label": "Open access"},
        {"name": "notes", "type": "typed_notes", "label": "Notes"},
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Presentations ----------

PRESENTATIONS = {
    "file": "data/presentations.yml",
    "label": "Presentations",
    "structure": "list_of_subsections",
    "list_columns": ["date", "title", "venue", "subsection"],
    # These ARE the section headings that render on the CV (the renderer reads
    # subsection names from the data, and these must match them exactly — see the
    # drift guard test_schema_subsections_cover_data). This is the single source
    # of truth: the edit-form dropdown, the bulk-move target list, and save
    # validation all read this list. To add/rename a subsection, edit it here AND
    # move any existing entries to the new name (the dropdown + validation adapt
    # automatically; insert_entry creates a new subsection group on first use).
    "subsections": [
        "Invited Presentations",
        "International Meetings (Podium Presentations)",
        "National and Regional Meetings (Podium Presentations)",
        "National and Regional Meetings (Poster Presentations)",
        "Non-Research Presentations",
    ],
    "default_subsection": "National and Regional Meetings (Podium Presentations)",
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "MM/YYYY (a future date hides the talk until it arrives)",
        },
        {
            "name": "authors",
            "type": "text",
            "label": "Authors",
            "placeholder": "Comma-separated; auto-bolds your name (use plain form, e.g. `Public JQ`)",
        },
        {
            "name": "title",
            "type": "textarea",
            "label": "Title",
            "placeholder": "Sentence case (optional)",
        },
        {
            "name": "venue",
            "type": "textarea",
            "required": True,
            "label": "Venue",
            "placeholder": "Conference name (auto-italicized) or institution",
        },
        {
            "name": "italic_venue",
            "type": "bool",
            "label": "Italicize venue (default true; uncheck for institution names)",
        },
        {
            "name": "location",
            "type": "text",
            "label": "Location",
            "placeholder": "City, State (used for invited presentations)",
        },
        {"name": "notes", "type": "simple_notes", "label": "Notes (sub-bullets)"},
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Research Support (grants) ----------

RESEARCH_SUPPORT = {
    "file": "data/research_support.yml",
    "label": "Research Support (Grants)",
    "structure": "flat_list",
    "list_columns": ["status", "date", "agency", "title", "role"],
    "fields": [
        {
            "name": "status",
            "type": "select",
            "required": True,
            "label": "Status",
            "choices": ["active", "pending", "previous"],
            "placeholder": "active grants must have an end date >= today (build will panic otherwise)",
        },
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date range",
            "placeholder": "MM/YYYY - MM/YYYY  (or 'MM/YYYY -' for open-ended)",
        },
        {"name": "agency", "type": "text", "required": True, "label": "Funding agency"},
        {
            "name": "project",
            "type": "text",
            "label": "Project / grant number",
            "placeholder": "e.g., R01AB123456 (quote bare-numeric IDs)",
        },
        {
            "name": "pi",
            "type": "text",
            "label": "PI name (when not sole PI)",
            "placeholder": "Surname only; combine with PI label below",
        },
        {
            "name": "pi_label",
            "type": "select",
            "label": "PI label (default 'PI')",
            "choices": ["", "PI", "MPI", "Co-PI", "Contact PI", "Sub-PI"],
        },
        {"name": "title", "type": "textarea", "required": True, "label": "Project title"},
        {
            "name": "role",
            "type": "text",
            "required": True,
            "label": "Your role",
            "placeholder": "e.g., Principal Investigator, Co-Investigator, Consultant",
        },
        {
            "name": "amount",
            "type": "grant_amount",
            "label": "Amount",
            "placeholder": "Type without the leading backslash; stored as '\\$XXX,XXX' in YAML",
        },
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Service ----------

SERVICE = {
    "file": "data/service.yml",
    "label": "Professional Service",
    "structure": "list_of_subsections",
    "list_columns": ["date", "role", "venue", "subsection"],
    "subsections": [
        "Editorial Service",
        "Service to Funding Agencies",
        "Service to National Organizations",
        "Service to Professional Organizations",
        "Service to the University",
        "Service to the Community",
    ],
    "default_subsection": "Service to Professional Organizations",
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "MM/YYYY  or  MM/YYYY - MM/YYYY  or  MM/YYYY -  (raw '*' for footnotes; future start hides it, future end renders open-ended until it passes)",
        },
        {
            "name": "role",
            "type": "text",
            "required": True,
            "label": "Role",
            "placeholder": "Italicized; use \\* for literal asterisk",
        },
        {
            "name": "venue",
            "type": "text",
            "label": "Venue",
            "placeholder": "Journal (editorial), agency (funding), organization (service)",
        },
        {
            "name": "extras",
            "type": "string_list",
            "label": "Inline continuations (extras)",
            "placeholder": "Each item renders as a continuation line in the same row (use \\* for asterisks)",
        },
        {"name": "notes", "type": "simple_notes", "label": "Notes (sub-bullets below the row)"},
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Teaching ----------

TEACHING = {
    "file": "data/teaching.yml",
    "label": "Teaching",
    "structure": "clusters",
    "list_columns": ["date", "role", "course", "institution"],
    "show_hidden_default": True,
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "MM/YYYY  or  MM/YYYY - MM/YYYY  or  YYYY  or  YYYY -  (future start hides it, future end renders open-ended until it passes)",
        },
        {
            "name": "role",
            "type": "text",
            "required": True,
            "label": "Role",
            "placeholder": "e.g., Faculty Mentor, Guest Lecturer (italicized)",
        },
        {"name": "course", "type": "textarea", "required": True, "label": "Course / activity name"},
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
    "cluster_fields": [
        {"name": "institution", "type": "text", "required": True, "label": "Institution"},
        {
            "name": "city",
            "type": "text",
            "label": "City",
            "placeholder": "City, State or City, COUNTRY",
        },
    ],
}


# ---------- Mentees ----------

MENTEES = {
    "file": "data/mentees.yml",
    "label": "Mentees",
    "structure": "flat_list",
    "list_columns": ["date", "role", "name", "institution"],
    "show_hidden_default": True,
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "Year or 'YYYY - YYYY' or 'YYYY -' (quote bare years like \"2025\"; future start hides it, future end renders open-ended until it passes)",
        },
        {
            "name": "role",
            "type": "text",
            "required": True,
            "label": "Role",
            "placeholder": "e.g., Doctoral Advisor, Dissertation Committee (italicized)",
        },
        {"name": "name", "type": "text", "required": True, "label": "Mentee name"},
        {
            "name": "institution",
            "type": "text",
            "required": True,
            "label": "Institution",
            "placeholder": "Wrapped in (parentheses) by the renderer",
        },
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Honors ----------

HONORS = {
    "file": "data/honors.yml",
    "label": "Honors & Awards",
    "structure": "flat_list",
    "list_columns": ["date", "award", "institution"],
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "Year or 'YYYY - YYYY' (quote bare years like \"2026\"; a future date hides the entry until it arrives)",
        },
        {
            "name": "award",
            "type": "textarea",
            "required": True,
            "label": "Award",
            "placeholder": "Italicized at render time",
        },
        {"name": "institution", "type": "text", "required": True, "label": "Awarding institution"},
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
}


# ---------- Education ----------

EDUCATION = {
    "file": "data/education.yml",
    "label": "Education",
    "structure": "clusters",
    "list_columns": ["date", "degree", "title", "institution"],
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "MM/YYYY or YYYY (a future date hides the entry until it arrives)",
        },
        {
            "name": "degree",
            "type": "text",
            "required": True,
            "label": "Degree",
            "placeholder": "e.g., ScD, MPH, BA  (italicized at render time)",
        },
        {"name": "title", "type": "text", "label": "Concentration / field"},
        {
            "name": "department",
            "type": "text",
            "label": "Department",
            "placeholder": "Optional second line below the degree row",
        },
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
    "cluster_fields": [
        {"name": "institution", "type": "text", "required": True, "label": "Institution"},
        {"name": "city", "type": "text", "label": "City"},
    ],
}


# ---------- Appointments ----------

APPOINTMENTS = {
    "file": "data/appointments.yml",
    "label": "Appointments",
    "structure": "subsections_of_clusters",
    "list_columns": ["date", "role", "program", "institution", "subsection"],
    "subsections": [
        "Faculty Appointments",
        "Academic Affiliations",
        "Postdoctoral and Fellowship Appointments",
    ],
    "default_subsection": "Academic Affiliations",
    "fields": [
        {
            "name": "date",
            "type": "text",
            "required": True,
            "label": "Date",
            "placeholder": "MM/YYYY or MM/YYYY - MM/YYYY or MM/YYYY - (future start hides it, future end renders open-ended until it passes)",
        },
        {
            "name": "role",
            "type": "text",
            "required": True,
            "label": "Role",
            "placeholder": "e.g., Assistant Professor (italicized)",
        },
        {"name": "program", "type": "text", "label": "Program / department / center"},
        _AUDIENCES_FIELD,
        _HIDE_FROM_FIELD,
        _HIGHLIGHTED_FIELD,
    ],
    "cluster_fields": [
        {"name": "institution", "type": "text", "required": True, "label": "Institution"},
        {"name": "city", "type": "text", "label": "City"},
    ],
}


# ---------- Meta (single-record header / footer / sections / build_variants) ----------
#
# The build_variants block deliberately stays in the schema as a raw
# textarea hint until V4 (Style/Variant editor). Header / contacts /
# self_bold / sections list are first-class in V2.

META = {
    "file": "data/meta.yml",
    "label": "Meta (header / footer / sections)",
    "structure": "single_record",
    "list_columns": [],
    "fields": [
        {
            "name": "name",
            "type": "text",
            "required": True,
            "label": "Name",
            "placeholder": "e.g., Jane Q Public",
        },
        {
            "name": "position",
            "type": "text",
            "label": "Position",
            "placeholder": "e.g., Assistant Professor",
        },
        {"name": "department", "type": "text", "label": "Department"},
        {"name": "institution", "type": "text", "label": "Institution"},
        {
            "name": "address",
            "type": "textarea",
            "label": "Address",
            "placeholder": "Multi-line postal address",
        },
        {"name": "email", "type": "text", "label": "Email"},
        {"name": "phone", "type": "text", "label": "Phone"},
        {"name": "website", "type": "text", "label": "Website"},
        {"name": "footer", "type": "textarea", "label": "Footer text"},
        {
            "name": "self_bold",
            "type": "text",
            "required": True,
            "label": "Self-bold name",
            "placeholder": "Exact author name to auto-bold across the CV (e.g., 'Public JQ')",
        },
        {
            "name": "sections",
            "type": "string_list",
            "label": "Section order (one per line)",
            "placeholder": "e.g., education, appointments, publications, ...",
        },
    ],
}


SCHEMAS = {
    "publications": PUBLICATIONS,
    "presentations": PRESENTATIONS,
    "research_support": RESEARCH_SUPPORT,
    "service": SERVICE,
    "teaching": TEACHING,
    "mentees": MENTEES,
    "honors": HONORS,
    "education": EDUCATION,
    "appointments": APPOINTMENTS,
    "meta": META,
}


def get(name: str) -> dict:
    return SCHEMAS[name]


def all_sections() -> list[str]:
    return list(SCHEMAS.keys())


# V20 (2026-05-18): fail-fast at import. Every `type:` referenced in
# any schema must have a registered FieldHandler. Catches typos before
# any route fires (used to silently fall through to `text` semantics).
from cv_editor.field_handlers import assert_schemas_covered  # noqa: E402

assert_schemas_covered(SCHEMAS)


def _widen_audiences_from_data() -> None:
    """Widen AUDIENCES in place with every audience the data actually uses
    (build-variant inputs + entry `audiences:`/`hide-from:`), so an audience
    the user already has always validates and the edit-form checkboxes offer
    it. Runs once at import against the configured data dir; falls back
    silently to the base set on any error (missing/malformed data). Mutates the
    list in place so the field dicts that hold `"choices": AUDIENCES` update
    too. EDITOR-ONLY — the renderer's visible() ignores this list."""
    try:
        from pathlib import Path

        from cv_editor import paths, yaml_io

        base = paths.data_dir()
        _, meta = yaml_io.load(base / "meta.yml")

        def _load(key):
            return yaml_io.load(base / Path(get(key)["file"]).name)[1]

        AUDIENCES[:] = list(_bv.audience_choices(meta or {}, _load))
    except Exception:
        pass


_widen_audiences_from_data()
