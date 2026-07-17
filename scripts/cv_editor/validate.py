"""
Schema-driven server-side validation. Returns dict {field: error_msg}
for any failures. Empty dict means OK.

Field types:
  text, textarea, string  - non-empty if required; otherwise optional
  int                     - parseable int; in [min, max] if specified
  bool                    - parsed from form (presence = true)
  select                  - value must be in the schema's `choices` list
  string_list             - list of strings; required = at least one
                            non-empty entry
  audiences_set           - set of strings, each in `choices`
  grant_amount            - free text; stored single-quoted with leading
                            backslash-dollar
  author_list             - non-empty if required; each row has a name
  typed_notes             - dict-shaped notes
  simple_notes            - list of {text, highlighted} dicts; empty rows
                            silently dropped
  open_access_dict        - {paper, code, data} per-key {enabled, url}

Active-grant validation: research_support entries with status=active must
have a date string with an end date >= today. The renderer panics
otherwise; we surface this in the form as a non-blocking warning so users
notice before they hit Save.
"""

from __future__ import annotations

import re
from datetime import date as _date


def validate_entry(form_data: dict, fields: list[dict]) -> dict[str, str]:
    """Schema-driven validation. Returns dict {field: error_msg}.

    V20 (2026-05-18): the per-type validation logic now lives in
    `cv_editor.field_handlers.FIELD_HANDLERS[ftype].validate`. This
    function handles the type-agnostic concerns (required + empty
    check) and dispatches.
    """
    from cv_editor.field_handlers import FIELD_HANDLERS

    errors: dict[str, str] = {}
    for f in fields:
        name = f["name"]
        ftype = f["type"]
        v = form_data.get(name)
        required = f.get("required", False)

        # Empty check (required fields).
        if required and (v is None or v == "" or v == [] or v == {}):
            errors[name] = "required"
            continue
        if v in (None, "", [], {}):
            continue  # optional and empty — skip type checks

        err = FIELD_HANDLERS[ftype].validate(v, f)
        if err:
            errors[name] = err

    return errors


def parse_pages_for_storage(s: str) -> str:
    """Pages stored verbatim. The renderer handles display formatting."""
    return s.strip() if s else s


# ---- date-range parsing for active-grant warning ----

_MONTH_YEAR = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$")
_YEAR = re.compile(r"^\s*(\d{4})\s*$")


def _parse_endpoint(s: str) -> _date | None:
    """Parse 'MM/YYYY' or 'YYYY' to a date (last day of that month/year)."""
    if not s:
        return None
    s = s.strip()
    m = _MONTH_YEAR.match(s)
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
        # Last day of month: 1st of next month minus 1.
        if mo == 12:
            return _date(yr, 12, 31)
        from datetime import timedelta

        return _date(yr, mo + 1, 1) - timedelta(days=1)
    m = _YEAR.match(s)
    if m:
        return _date(int(m.group(1)), 12, 31)
    return None


def grant_end_date_warning(form_data: dict) -> str | None:
    """Returns a warning message if status=active and the date range's end
    point is in the past. Non-blocking — the caller flashes a banner.

    Recognized date shapes:
        "MM/YYYY - MM/YYYY"   end = MM/YYYY
        "MM/YYYY -"           open-ended (no warning)
        "YYYY - YYYY"         end = YYYY
        "MM/YYYY"             single-point grant — end = the same date
    """
    status = (form_data.get("status") or "").strip()
    if status != "active":
        return None
    date_str = (form_data.get("date") or "").strip()
    if not date_str:
        return None
    if date_str.endswith("-") or date_str.endswith("- "):
        return None  # open-ended

    if "-" in date_str:
        _, _, right = date_str.partition("-")
        endpoint = _parse_endpoint(right)
    else:
        endpoint = _parse_endpoint(date_str)

    if endpoint is None:
        return None  # unparseable — don't block; renderer will catch
    if endpoint < _date.today():
        return (
            f"Active grant with end date {endpoint.isoformat()} is in the past — "
            "Typst build will panic. Move to status: previous, or extend the date."
        )
    return None


# ---- date-conditional rendering status (editor discoverability) ----
#
# Sections whose entries are date-gated in the renderer (future START hides
# the entry; future END renders open-ended "Start –"). MUST stay in sync with
# the `date-gated: true` call sites in templates/bespoke/render.typ. The
# renderer is the SOURCE OF TRUTH for the actual behavior; the helpers below
# are a COARSE, boundary-tolerant mirror used only to surface editor hints,
# so month-boundary precision may differ (Python uses last-day-of-month for
# ends via _parse_endpoint; Typst uses first-of-month). Phrase hints loosely.

DATE_GATED_SECTIONS = frozenset(
    {
        "appointments",
        "service",
        "teaching",
        "education",
        "honors",
        "mentees",
        "presentations",
    }
)

_MARKERS = "*†‡"


def _parse_start(s: str) -> _date | None:
    """Parse the START of a date string ('MM/YYYY', 'YYYY', range, or
    open-ended '... -') to a date (first of that month/year). None if
    unparseable/empty. Coarse mirror of render.typ:parse-start-date."""
    if not s:
        return None
    core = s.replace("–", "-").strip()
    if core.endswith("-"):
        core = core[:-1].strip()
    first = core.split(" - ")[0].strip().rstrip(_MARKERS).strip()
    m = _MONTH_YEAR.match(first)
    if m:
        return _date(int(m.group(2)), int(m.group(1)), 1)
    m = _YEAR.match(first)
    if m:
        return _date(int(m.group(1)), 1, 1)
    return None


def date_conditional_status(date_str: str, *, today: _date | None = None) -> str | None:
    """Classify a date string for the date-conditional render feature.

    Returns:
      "future_start" -> start date is in the future (entry hidden from normal
                        builds until then; revealed only under show_future)
      "future_end"   -> closed range whose end is still in the future (renders
                        open-ended "Start –" until the end passes)
      None           -> normal (single/past/already-open-ended date)

    Coarse mirror of templates/bespoke/render.typ (start-in-future /
    active-form). The renderer is the source of truth.
    """
    if not date_str:
        return None
    today = today or _date.today()
    start = _parse_start(date_str)
    if start is not None and start > today:
        return "future_start"
    norm = date_str.replace("–", "-").strip()
    if not norm.endswith("-") and " - " in norm:
        _, _, right = norm.rpartition(" - ")
        endpoint = _parse_endpoint(right.strip().rstrip(_MARKERS).strip())
        if endpoint is not None and endpoint >= today:
            return "future_end"
    return None


def _other_gate_summary(entry: dict) -> str:
    """One-line summary of any NON-date visibility gate on an entry, so the
    date badge doesn't imply the entry will simply appear on its start date
    when another gate also hides it. Empty string if none."""
    bits = []
    if entry.get("highlighted"):
        bits.append("highlighted (needs show_highlighted)")
    aud = entry.get("audiences")
    if aud:
        bits.append("audiences: " + ", ".join(str(a) for a in aud))
    hide = entry.get("hide-from")
    if hide:
        bits.append("hide-from: " + ", ".join(str(h) for h in hide))
    if not bits:
        return ""
    return "Also gated by " + "; ".join(bits) + "."


def date_gate_note(entry: dict, section_key: str, *, today: _date | None = None) -> str | None:
    """Boundary-tolerant human hint for a date-gated entry, or None. Fires only
    for DATE_GATED_SECTIONS. Appends any other active visibility gate so the
    badge is a complete visibility summary."""
    if section_key not in DATE_GATED_SECTIONS:
        return None
    date_str = (entry.get("date") or "").strip()
    status = date_conditional_status(date_str, today=today)
    if status is None:
        return None
    start = _parse_start(date_str)
    if status == "future_start":
        base = "Start date is in the future — hidden from normal builds until it arrives"
        if start is not None:
            base += f" (around {start.strftime('%b %Y')})"
    else:
        base = 'End date is in the future — renders as open-ended ("Start –") until it passes'
    msg = base + "."
    extra = _other_gate_summary(entry)
    if extra:
        msg += " " + extra
    return msg
