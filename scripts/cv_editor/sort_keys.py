"""Date-aware sort key helpers for the CV editor's list views.

Both server-side default sort and client-side column sort consume the
strings these functions produce via the row's `data-sort-value`. The key
property: string compare on the normalized form must agree with
chronological compare on the underlying date.

Why this exists (V17 polish): MM/YYYY date strings sort wrong with naive
string compare ('03/2026' < '12/2025' lexically) and wrong with
parseFloat (3 < 12 numerically). We normalize to YYYYMM-derived keys
that work under both string compare and `localeCompare(numeric: true)`.

Extracted from app.py in V17-D so the helpers are unit-testable without
instantiating the Flask app.
"""

from __future__ import annotations


def parse_yyyymm(token):
    """Parse 'MM/YYYY' or 'YYYY' into integer YYYYMM. None on failure."""
    token = (str(token) if token is not None else "").strip()
    if not token:
        return None
    if "/" in token:
        try:
            m, y = token.split("/", 1)
            return int(y) * 100 + int(m)
        except (ValueError, AttributeError):
            return None
    try:
        return int(token) * 100
    except (TypeError, ValueError):
        return None


def date_sort_norm(s) -> str:
    """Normalize a date string into a string-comparable sort key.

    End-date dominates so reverse-chronological sort surfaces grants
    by closing date first. Open-ended ranges ('MM/YYYY -') sort to
    the top of descending order via a 999999 sentinel.

    Examples: 'MM/YYYY' -> 'YYYYMM_YYYYMM',
              'MM/YYYY - MM/YYYY' -> 'END_START',
              'MM/YYYY -' -> '999999_START',
              'YYYY' -> 'YYYY00_YYYY00'.
    """
    s = (str(s) if s is not None else "").strip()
    if not s:
        return ""
    if " - " in s:
        a, b = s.split(" - ", 1)
        start = parse_yyyymm(a)
        end = parse_yyyymm(b) if b.strip() else 999999
        if start is not None and end is not None:
            return f"{end:06d}_{start:06d}"
    if s.endswith(" -"):
        start = parse_yyyymm(s[:-2])
        if start is not None:
            return f"999999_{start:06d}"
    v = parse_yyyymm(s)
    if v is not None:
        return f"{v:06d}_{v:06d}"
    return ""


def year_month_sort_norm(year, month=None, day=None) -> str:
    """Normalize publication year/month/day into 'YYYYMMDD' for sort."""

    def _i(x):
        try:
            return int(x) if x is not None and str(x).strip() != "" else 0
        except (TypeError, ValueError):
            return 0

    return f"{_i(year):04d}{_i(month):02d}{_i(day):02d}"
