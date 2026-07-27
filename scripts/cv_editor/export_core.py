"""Target-agnostic CV export model (M5 5b) — MIDDLE fidelity, public-default view.

Builds a structured `Document` (sections -> subsections -> clusters/entries) of
ALREADY-RESOLVED inline strings for ONE target (html | md). The Markdown and HTML
emitters (`export_markdown.py` / `export_html.py`) walk this model and wrap the
structure (headings, lists) in target syntax — all the Typst-parity logic lives
here so the emitters stay dumb.

MIDDLE fidelity (user-confirmed 2026-05-30): reproduce author lists WITH co-first
(†)/co-senior(‡)/group(◊) markers + footnote sentences + self-bold of meta.self_bold;
proper date/journal/volume/issue/pages assembly; markup conversion. DEFER: exact
per-subsection reverse numbering, OA sub-bullets, typed notes, media-outlet grouping,
citation counts, pending grants, the review highlight (all gated off under the
public default variant anyway). Layout/fonts/spacing are the emitter's.

VIEW = the default build variant (first in meta.yml). The visibility predicate is
a LITERAL port of lib/flags.typ:visible + templates/bespoke/render.typ:entry-visible — a public web
CV must NOT leak hide-from / highlighted / audience-restricted entries; a string-shape
drift guard + a behavioral truth-table pin it (tests/test_m5_export_core.py).

REUSE (not re-implemented): markup_convert (Typst markup -> html/md), author_flags
(glyphs + footnotes, drift-guarded vs render.typ), author_names (str/dict authors),
sort_keys (reverse-chrono), schemas (section files/structure). Grant agency/title/
role/project/pi are LITERAL text (not markup, not self-bolded) — only `amount` goes
through mk() raw (its '\\$X' YAML value -> literal "$X", matching render.typ + grant.typ;
NOT the csv_export $-stripping helper). Cluster institution names (teaching/education/
appointments) are LITERAL too (render's `institution()` is text(weight:bold), no mk).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

from cv_editor import (
    author_flags,
    author_names,
    build_variants,
    markup_convert,
    schemas,
    sort_keys,
    yaml_io,
)

HTML = markup_convert.HTML
MD = markup_convert.MD

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Section display titles. Mirrors the M3 canonical map (tests/test_m3_section_titles
# .py); honors' render-side `#emph[&]` (italic ampersand) is simplified to a plain
# "&" here (a typographic frill, out of MIDDLE scope). Drift-guarded by a parity test.
SECTION_TITLES = {
    "education": "Education",
    "appointments": "Professional Appointments",
    "publications": "Scholarly Publications",
    "presentations": "Presentations",
    "research_support": "Research Support",
    "service": "Professional Service",
    "teaching": "Teaching Experience",
    "honors": "Honors & Awards",
    "mentees": "Mentees",
}


# ----- resolved flags for a variant -----


@dataclass(frozen=True)
class ExportFlags:
    """Resolved lib/flags.typ values for the chosen build variant. Defaults match
    the flags.typ public-facing profile (audience=public-health; show_dollars +
    show_media_urls ON; everything else OFF)."""

    audience: str = "public-health"
    review: bool = False
    show_dollars: bool = True
    show_pending: bool = False
    show_oa: bool = False
    show_citations: bool = False
    show_contributions: bool = False
    show_notes: bool = False
    show_media: bool = False
    show_hidden_media: bool = False
    show_media_urls: bool = True
    show_highlighted: bool = False
    strict_dates: bool = False


_FLAG_DEFAULTS = {
    "review": False,
    "show_dollars": True,
    "show_pending": False,
    "show_oa": False,
    "show_citations": False,
    "show_contributions": False,
    "show_notes": False,
    "show_media": False,
    "show_hidden_media": False,
    "show_media_urls": True,
    "show_highlighted": False,
    "strict_dates": False,
}


def resolve_flags(meta: dict, variant: str | None = None) -> ExportFlags:
    """Resolve a build_variants entry's inputs over the flags.typ defaults. Inputs
    arrive as the editor stores them (strings/bools); booleans parse from 'true'.
    `variant=None` resolves to the default variant (first in meta.yml)."""
    variant = variant or build_variants.default_variant_name(meta)
    inputs = {}
    for v in meta.get("build_variants") or []:
        if v.get("filename") == variant:
            inputs = v.get("inputs") or {}
            break
    audience = str(inputs.get("audience", "public-health"))

    def _b(key):
        if key in inputs:
            val = inputs[key]
            return val is True or str(val).strip().lower() == "true"
        return _FLAG_DEFAULTS[key]

    return ExportFlags(audience=audience, **{k: _b(k) for k in _FLAG_DEFAULTS})


# ----- document model -----


@dataclass(frozen=True)
class Entry:
    date: str  # formatted date column ("" for publications)
    body: str  # final target inline string
    sub_rows: tuple = ()  # date-less continuation lines (dept / extras / grant rows)


@dataclass(frozen=True)
class Cluster:
    institution: str  # final target inline string (emitter adds emphasis weight)
    city: str | None
    entries: tuple = ()


@dataclass(frozen=True)
class Subsection:
    title: str | None  # secondary header, or None for a flat section
    entries: tuple = ()
    clusters: tuple = ()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    subsections: tuple = ()


@dataclass(frozen=True)
class Header:
    name: str
    affiliations: tuple = ()
    # (label, RAW value) pairs. The value is the verbatim email/phone/website —
    # the ONLY model string NOT pre-converted for the target, because it carries
    # link semantics (mailto:/href) the emitter resolves + escapes. MD inlines it
    # as text; HTML linkifies + escapes it (see export_emit._html_contact).
    contacts: tuple = ()


@dataclass(frozen=True)
class Document:
    target: str
    header: Header
    sections: tuple = field(default_factory=tuple)
    variant: str = ""  # resolved build-variant name (for the emitter's view label)


# ----- inline helpers (target-aware) -----


def _plain(s, target: str) -> str:
    """Literal text: HTML-escape for HTML, verbatim for MD. No markup, no self-bold."""
    s = "" if s is None else str(s)
    return html.escape(s, quote=False) if target == HTML else s


def _self_bold_terms(meta_self_bold) -> list[str]:
    """meta.self_bold -> list of literal terms (mirrors render.typ _self-bold-terms)."""
    if isinstance(meta_self_bold, str):
        return [meta_self_bold] if meta_self_bold.strip() else []
    if isinstance(meta_self_bold, (list, tuple)):
        return [str(t) for t in meta_self_bold if str(t).strip()]
    return []


def _mk(s, target: str, self_bold: list[str]) -> str:
    """Export analog of render.typ mk(): self-bold pre-pass, then markup_convert.
    NEVER evals. Self-bold wraps each literal term in *...* so markup_convert turns
    it into <strong>/**."""
    s = "" if s is None else str(s)
    for term in self_bold:
        if term:
            s = s.replace(term, f"*{term}*")
    return markup_convert.to_html(s) if target == HTML else markup_convert.to_markdown(s)


def _em(inline: str, target: str) -> str:
    """Wrap an ALREADY-converted inline string in emphasis."""
    return f"<em>{inline}</em>" if target == HTML else f"_{inline}_"


def _strong(inline: str, target: str) -> str:
    return f"<strong>{inline}</strong>" if target == HTML else f"**{inline}**"


def _sup(glyph: str, target: str) -> str:
    """Author/footnote marker: <sup> in HTML, bare glyph in MD (decision: no native
    MD superscript; bare glyphs read fine and avoid renderer-fragile raw <sup>)."""
    return f"<sup>{glyph}</sup>" if target == HTML else glyph


def _link(url, label, target: str) -> str:
    """Scheme-guarded link (reuses markup_convert._link_target). Unsafe scheme ->
    plain label (never a live javascript: link in a published file)."""
    safe = markup_convert._link_target(str(url))
    if target == HTML:
        label_e = html.escape(str(label), quote=False)
        if safe is None:
            return label_e
        return f'<a href="{html.escape(safe, quote=True)}">{label_e}</a>'
    if safe is None:
        return str(label)
    return f"[{label}]({safe})"


# ----- visibility (LITERAL port of lib/flags.typ:visible + render.typ:entry-visible) -----


def _visible(audiences, hide_from, audience: str) -> bool:
    # hide-from wins, checked FIRST (flags.typ:93).
    if audience in (hide_from or []):
        return False
    return audience == "full" or len(audiences or []) == 0 or audience in audiences


def _entry_visible(
    e: dict, audience: str, show_highlighted: bool, sub_audiences=None, sub_hide_from=None
) -> bool:
    # highlighted gate is PRIOR to the audience delegate (render.typ:57).
    if e.get("highlighted", False) and not show_highlighted:
        return False
    aud = e.get("audiences", sub_audiences if sub_audiences is not None else [])
    hide = e.get("hide-from", sub_hide_from if sub_hide_from is not None else [])
    return _visible(aud, hide, audience)


# ----- public export API (stable names for out-of-package exporters) -----
#
# The underscored helpers above are the internal implementation used by this
# module's own `build_*` formatters. An external consumer (e.g. a website JSON
# exporter that reuses this engine's markup + leak-guard visibility logic)
# should import these PUBLIC names instead of reaching for the private ones.
# They are thin aliases — identical behaviour, a supported surface — so a future
# refactor of the private internals can't silently break a downstream exporter.
# Keep every name a downstream exporter depends on listed here.
mk = _mk
plain = _plain
emphasis = _em
strong = _strong
sup = _sup
link = _link
self_bold_terms = _self_bold_terms
visible = _visible
entry_visible = _entry_visible


# ----- dates (ports of render.typ format-date / format-month-year) -----


def _format_date(s) -> str:
    """Port of render.typ:format-date — hyphen range -> en-dash, passthrough else."""
    s = "" if s is None else str(s)
    if " - " in s:
        return s.replace(" - ", " – ")
    if s.endswith(" -"):
        return s[:-2] + " –"
    return s


def _format_month_year(s) -> str:
    """Port of render.typ:format-month-year (presentations): MM/YYYY -> 'Month YYYY'."""
    s = "" if s is None else str(s)
    if "/" in s:
        mm, _, yy = s.partition("/")
        try:
            return f"{_MONTHS_LONG[int(mm) - 1]} {int(yy)}"
        except (ValueError, IndexError):
            return s
    return s


def _by_date_desc(items, date_of):
    """Reverse-chronological via the editor's established normalizer (sort_keys)."""
    return sorted(items, key=lambda x: sort_keys.date_sort_norm(date_of(x)), reverse=True)


# ----- publication citation body (port of _render-pub-body, MIDDLE subset) -----


def format_publication(e: dict, target: str, self_bold: list[str], flags: ExportFlags) -> str:
    authors = author_names.normalize_authors_for_render(e.get("authors") or [])
    segs, seen = [], set()
    for a in authors:
        form = author_names.author_to_form(a)
        seg = _mk(form.get("name", ""), target, self_bold)
        for fl in author_flags.AUTHOR_FLAGS:
            if form.get(fl.key):
                seg += _sup(fl.glyph, target)
                seen.add(fl.key)
        segs.append(seg)
    body = ", ".join(segs)
    if body:
        body += ". "
    if "title" in e:
        body += _mk(e["title"], target, self_bold) + ". "
    if "journal" in e:
        body += _em(_mk(e["journal"], target, self_bold), target) + ". "
    body += _pub_date_tail(e, target) + ". "
    if "doi" in e:
        body += "doi: " + _link("https://doi.org/" + str(e["doi"]), str(e["doi"]), target) + ". "
    if "epub_date" in e:
        body += "Epub " + _plain(e["epub_date"], target) + ". "
    body += _pub_id_block(e, target)
    for fl in author_flags.AUTHOR_FLAGS:
        if fl.key in seen:
            body += " " + _sup(fl.glyph, target) + fl.footnote
    return body.rstrip()


def _pub_date_tail(e: dict, target: str) -> str:
    def p(x):
        return _plain(x, target)

    parts = []
    if "year" in e:
        parts.append(p(e["year"]))
    if "month" in e:
        try:
            parts.append(_MONTHS[int(e["month"]) - 1])
        except (ValueError, IndexError, TypeError):
            pass
    if "day" in e:
        parts.append(p(e["day"]))
    tail = " ".join(parts)
    if "volume" in e:
        tail += ";" + p(e["volume"])
        if "issue" in e:
            tail += "(" + p(e["issue"]) + ")"
        if "pages" in e:
            tail += ":" + p(e["pages"])
    elif "pages" in e:
        tail += "; " + p(e["pages"])
    if "date_qualifier" in e:
        tail += " — " + p(e["date_qualifier"])
    return tail


def _pub_id_block(e: dict, target: str) -> str:
    has_pmid, has_pmcid = "pmid" in e, "pmcid" in e

    def pmcid_link():
        return _link(
            "https://www.ncbi.nlm.nih.gov/pmc/articles/" + str(e["pmcid"]) + "/",
            str(e["pmcid"]),
            target,
        )

    if has_pmid:
        out = "PubMed PMID: " + _link(
            "https://pubmed.ncbi.nlm.nih.gov/" + str(e["pmid"]) + "/", str(e["pmid"]), target
        )
        return out + ("; PubMed Central PMCID: " + pmcid_link() + "." if has_pmcid else ".")
    if has_pmcid:
        return "PubMed Central PMCID: " + pmcid_link() + "."
    return ""


# ----- section builders -----


def _section(key: str, subsections: list, target: str) -> Section | None:
    subs = tuple(s for s in subsections if s is not None)
    if not subs:
        return None
    # SECTION_TITLES carries a literal "&" ("Honors & Awards"); _plain escapes it
    # for HTML and leaves it verbatim for MD (so the emitters never escape — see
    # export_emit.py's contract).
    return Section(key=key, title=_plain(SECTION_TITLES.get(key, key), target), subsections=subs)


def build_publications(data, target, audience, self_bold, flags) -> Section | None:
    subs = []
    for sub in data or []:
        sa, sh = sub.get("audiences"), sub.get("hide-from")
        ents = [
            e
            for e in (sub.get("entries") or [])
            if _entry_visible(e, audience, flags.show_highlighted, sa, sh)
        ]
        if not ents:
            continue
        ents.sort(
            key=lambda e: sort_keys.year_month_sort_norm(
                e.get("year"), e.get("month"), e.get("day")
            ),
            reverse=True,
        )
        rows = tuple(Entry("", format_publication(e, target, self_bold, flags)) for e in ents)
        subs.append(
            Subsection(title=_plain(sub.get("subsection", ""), target) or None, entries=rows)
        )
    return _section("publications", subs, target)


def _simple_note_rows(notes, target, self_bold, show_highlighted) -> list[str]:
    """Visible simple-notes -> continuation lines (highlighted notes dropped unless
    show_highlighted). DEFER nested-bullet styling; honor the gate (render.typ:195)."""
    out = []
    for n in notes or []:
        if isinstance(n, dict):
            if n.get("highlighted") and not show_highlighted:
                continue
            text = n.get("text") or ""
        else:
            text = n
        text = str(text).strip()
        if text:
            out.append(_mk(text, target, self_bold))
    return out


def build_presentations(data, target, audience, self_bold, flags) -> Section | None:
    subs = []
    for sub in data or []:
        sa, sh = sub.get("audiences"), sub.get("hide-from")
        ents = [
            e
            for e in (sub.get("entries") or [])
            if _entry_visible(e, audience, flags.show_highlighted, sa, sh)
        ]
        if not ents:
            continue
        ents = _by_date_desc(ents, lambda e: e.get("date", ""))
        rows = []
        for e in ents:
            body = ""
            if e.get("authors"):
                body += _mk(e["authors"], target, self_bold) + ", "
            if e.get("title"):
                title = str(e["title"])
                if title.endswith("."):
                    title = title[:-1]
                body += _mk(title, target, self_bold) + ". "
            venue = _mk(e.get("venue", ""), target, self_bold)
            body += _em(venue, target) if e.get("italic_venue", True) else venue
            body += " (" + _plain(_format_month_year(e.get("date", "")), target) + ")"
            if e.get("location"):
                body += ". " + _mk(e["location"], target, self_bold) + "."
            subr = _simple_note_rows(e.get("notes"), target, self_bold, flags.show_highlighted)
            rows.append(Entry("", body, tuple(subr)))
        subs.append(
            Subsection(title=_plain(sub.get("subsection", ""), target) or None, entries=tuple(rows))
        )
    return _section("presentations", subs, target)


def build_research_support(data, target, audience, self_bold, flags) -> Section | None:
    groups = [("active", "Active Support")]
    if flags.show_pending:
        groups.insert(0, ("pending", "Pending Support"))
    groups.append(("previous", "Previous Support"))
    entries = [g for g in (data or []) if _entry_visible(g, audience, flags.show_highlighted)]
    subs = []
    for status, title in groups:
        grp = [g for g in entries if str(g.get("status", "")) == status]
        if not grp:
            continue
        grp = _by_date_desc(grp, lambda g: g.get("date", ""))
        rows = []
        for g in grp:
            agency = _strong(_plain(g.get("agency", ""), target), target)
            paren = []
            if g.get("project"):
                paren.append(_plain(g["project"], target))
            if g.get("pi"):
                paren.append(
                    _plain(g.get("pi_label", "PI"), target) + ": " + _plain(g["pi"], target)
                )
            row1 = agency + (" (" + "; ".join(paren) + ")" if paren else "")
            row2 = _em("Title:", target) + ' "' + _plain(g.get("title", ""), target) + '"'
            row3 = _em("Role:", target) + " " + _plain(g.get("role", ""), target)
            if g.get("amount") and flags.show_dollars:
                # Faithful to grant.typ: the raw '\$X' YAML value goes straight
                # through mk() -> literal "$X" (render.typ shows "($1,000,000)").
                # Do NOT route through csv_export._format_grant_amount (it strips
                # the $ for spreadsheets, which dropped the currency sign here).
                amt = _mk(g["amount"], target, self_bold)
                row3 += "  (" + amt + ")"
            rows.append(Entry(_plain(_format_date(g.get("date", "")), target), row1, (row2, row3)))
        subs.append(Subsection(title=_plain(title, target), entries=tuple(rows)))
    return _section("research_support", subs, target)


def build_service(data, target, audience, self_bold, flags) -> Section | None:
    subs = []
    for sub in data or []:
        ents = [
            e
            for e in (sub.get("entries") or [])
            if _entry_visible(e, audience, flags.show_highlighted)
        ]
        rows = []
        for e in _by_date_desc(ents, lambda e: e.get("date", "")):
            body = _em(_mk(e.get("role", ""), target, self_bold), target)
            if e.get("venue"):
                body += ", " + _mk(e["venue"], target, self_bold)
            subr = [_mk(x, target, self_bold) for x in (e.get("extras") or []) if str(x).strip()]
            subr += _simple_note_rows(e.get("notes"), target, self_bold, flags.show_highlighted)
            rows.append(Entry(_plain(_format_date(e.get("date", "")), target), body, tuple(subr)))
        ahr = sub.get("ad_hoc_reviewer")
        if ahr:
            journals = sorted((str(j) for j in (ahr.get("journals") or [])), key=str.lower)
            body = " • ".join(_mk(j, target, self_bold) for j in journals)
            rows.append(Entry(_plain(ahr.get("label", ""), target), body))
        if rows:
            subs.append(
                Subsection(
                    title=_plain(sub.get("subsection", ""), target) or None, entries=tuple(rows)
                )
            )
    return _section("service", subs, target)


def _build_clusters(
    data,
    target,
    audience,
    self_bold,
    flags,
    entry_body,
    *,
    cascade: bool,
    sort_clusters: bool = False,
):
    """Shared cluster walker (teaching / education). cascade=False => cluster
    audiences do NOT inherit to entries (the teaching trap, render.typ:337).
    sort_clusters=True => reorder clusters newest-first by their most-recent
    entry date (render-education does this; render-teaching preserves YAML order).
    The institution name is rendered LITERALLY (render's `institution()` helper is
    `text(weight:"bold", school)`, no mk()), so it goes through _plain, not _mk."""
    src = list(data or [])
    if sort_clusters:
        # Mirror render-education's cluster-key: the cluster's most-recent entry.
        src.sort(
            key=lambda c: max(
                (sort_keys.date_sort_norm(e.get("date", "")) for e in (c.get("entries") or [])),
                default="",
            ),
            reverse=True,
        )
    clusters = []
    for cl in src:
        ca = cl.get("audiences") if cascade else None
        ch = cl.get("hide-from") if cascade else None
        ents = [
            e
            for e in (cl.get("entries") or [])
            if _entry_visible(e, audience, flags.show_highlighted, ca, ch)
        ]
        if not ents:
            continue
        ents = _by_date_desc(ents, lambda e: e.get("date", ""))
        rows = tuple(entry_body(e) for e in ents)
        clusters.append(
            Cluster(
                _plain(cl.get("institution", ""), target),
                (_plain(cl["city"], target) if cl.get("city") else None),
                rows,
            )
        )
    return clusters


def build_teaching(data, target, audience, self_bold, flags) -> Section | None:
    def body(e):
        b = (
            _em(_mk(e.get("role", ""), target, self_bold), target)
            + ", "
            + _mk(e.get("course", ""), target, self_bold)
        )
        return Entry(_plain(_format_date(e.get("date", "")), target), b)

    cl = _build_clusters(data, target, audience, self_bold, flags, body, cascade=False)
    return _section("teaching", [Subsection(title=None, clusters=tuple(cl))] if cl else [], target)


def build_education(data, target, audience, self_bold, flags) -> Section | None:
    def body(e):
        b = _em(_mk(e.get("degree", ""), target, self_bold), target)
        if e.get("title"):
            b += ", " + _mk(e["title"], target, self_bold)
        subr = (_mk(e["department"], target, self_bold),) if e.get("department") else ()
        return Entry(_plain(_format_date(e.get("date", "")), target), b, subr)

    cl = _build_clusters(
        data, target, audience, self_bold, flags, body, cascade=True, sort_clusters=True
    )
    return _section("education", [Subsection(title=None, clusters=tuple(cl))] if cl else [], target)


def build_appointments(data, target, audience, self_bold, flags) -> Section | None:
    subs = []
    for sub in data or []:
        sa, sh = sub.get("audiences"), sub.get("hide-from")
        clusters = []
        for cl in sub.get("clusters") or []:
            ca = cl.get("audiences", sa)
            ch = cl.get("hide-from", sh)
            ents = [
                e
                for e in (cl.get("entries") or [])
                if _entry_visible(e, audience, flags.show_highlighted, ca, ch)
            ]
            if not ents:
                continue
            ents = _by_date_desc(ents, lambda e: e.get("date", ""))
            rows = []
            for e in ents:
                b = _em(_mk(e.get("role", ""), target, self_bold), target)
                if e.get("program"):
                    b += ", " + _mk(e["program"], target, self_bold)
                rows.append(Entry(_plain(_format_date(e.get("date", "")), target), b))
            clusters.append(
                Cluster(
                    _plain(cl.get("institution", ""), target),
                    (_plain(cl["city"], target) if cl.get("city") else None),
                    tuple(rows),
                )
            )
        if clusters:
            subs.append(
                Subsection(
                    title=_plain(sub.get("subsection", ""), target) or None,
                    clusters=tuple(clusters),
                )
            )
    return _section("appointments", subs, target)


def build_honors(data, target, audience, self_bold, flags) -> Section | None:
    ents = [e for e in (data or []) if _entry_visible(e, audience, flags.show_highlighted)]
    if not ents:
        return None
    rows = tuple(
        Entry(
            _plain(_format_date(e.get("date", "")), target),
            _em(_mk(e.get("award", ""), target, self_bold), target)
            + ", "
            + _mk(e.get("institution", ""), target, self_bold),
        )
        for e in _by_date_desc(ents, lambda e: e.get("date", ""))
    )
    return _section("honors", [Subsection(title=None, entries=rows)], target)


def build_mentees(data, target, audience, self_bold, flags) -> Section | None:
    ents = [e for e in (data or []) if _entry_visible(e, audience, flags.show_highlighted)]
    if not ents:
        return None
    rows = tuple(
        Entry(
            _plain(_format_date(e.get("date", "")), target),
            _em(_mk(e.get("role", ""), target, self_bold), target)
            + ", "
            + _mk(e.get("name", ""), target, self_bold)
            + " ("
            + _mk(e.get("institution", ""), target, self_bold)
            + ")",
        )
        for e in _by_date_desc(ents, lambda e: e.get("date", ""))
    )
    return _section("mentees", [Subsection(title=None, entries=rows)], target)


_BUILDERS = {
    "publications": build_publications,
    "presentations": build_presentations,
    "research_support": build_research_support,
    "service": build_service,
    "teaching": build_teaching,
    "education": build_education,
    "appointments": build_appointments,
    "honors": build_honors,
    "mentees": build_mentees,
}


def build_header(meta: dict, target: str) -> Header:
    self_bold = _self_bold_terms(meta.get("self_bold"))
    affs = tuple(
        _mk(x, target, self_bold)
        for x in (meta.get("position"), meta.get("department"), meta.get("institution"))
        if x
    )
    contacts = []
    for label, key in (("Email", "email"), ("Phone", "phone"), ("Website", "website")):
        if meta.get(key):
            contacts.append((label, str(meta[key])))
    return Header(
        name=_mk(meta.get("name", ""), target, self_bold),
        affiliations=affs,
        contacts=tuple(contacts),
    )


def build_model(data_dir, *, target: str, variant: str | None = None) -> Document:
    """Load every section file under `data_dir` and build the export Document for
    `target` (html|md) under the named build variant. `variant=None` resolves to
    the default variant (first in meta.yml = the public default view)."""
    base = Path(data_dir)
    _, meta = yaml_io.load(base / "meta.yml")
    meta = meta or {}
    variant = variant or build_variants.default_variant_name(meta)
    flags = resolve_flags(meta, variant)
    self_bold = _self_bold_terms(meta.get("self_bold"))
    header = build_header(meta, target)

    order = meta.get("sections") or list(_BUILDERS)
    sections = []
    for key in order:
        if key not in _BUILDERS:
            raise ValueError(f"unknown section key in meta.sections: {key!r}")
        _, data = yaml_io.load(base / Path(schemas.get(key)["file"]).name)
        sec = _BUILDERS[key](data, target, flags.audience, self_bold, flags)
        if sec is not None:
            sections.append(sec)
    return Document(target=target, header=header, sections=tuple(sections), variant=variant)
