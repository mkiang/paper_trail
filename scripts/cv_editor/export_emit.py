"""Emitters: walk an export_core.Document and wrap its structure in a target
syntax (M5 5b). All inline strings in the model are ALREADY target-converted by
export_core; the emitters only add headings/lists/line-breaks — they never escape
or convert body/heading text. Markdown + HTML live in one module (they share
entry-rendering shape) and are named distinctly from the `scripts/` CLIs to avoid
an import shadow.

The ONE exception to "emitters never escape": `Header.contacts` values are RAW
(verbatim email/phone/website) because they carry link semantics. `_html_contact`
escapes + scheme-guarded-linkifies them for HTML; the Markdown emitter inlines them
as plain text. Everything else (names, affiliations, section/subsection titles,
cluster city, entry bodies + sub-rows) is target-correct from the model.

KNOWN MIDDLE-FIDELITY LIMITATION (Markdown): when a renderer-emphasized field
(award / role / degree / venue) ALSO contains _italic_ markup in its content, the
outer italic wrap nests `_.._` inside `_.._`, which Markdown renders ambiguously.
HTML handles nested `<em>` fine. Bold-inside-italic (`*bold*` inside an italic-wrapped
field) is valid in both. Rare in real CV data; documented rather than worked around.
"""

from __future__ import annotations

import html

from cv_editor import export_core, markup_convert

# ---------- Markdown ----------


def _md_entry(e) -> list[str]:
    line = f"- **{e.date}** — {e.body}" if e.date else f"- {e.body}"
    out = [line]
    out.extend(f"  - {r}" for r in e.sub_rows)
    return out


def render_markdown(doc: "export_core.Document") -> str:
    L: list[str] = []
    h = doc.header
    L.append(f"# {h.name}")
    if h.affiliations:
        L.append("")
        L.append("  \n".join(h.affiliations))  # two-space MD hard breaks
    if h.contacts:
        L.append("")
        L.append(" · ".join(f"{lbl}: {val}" for lbl, val in h.contacts))
    for sec in doc.sections:
        L.append("")
        L.append(f"## {sec.title}")
        for sub in sec.subsections:
            if sub.title:
                L.append("")
                L.append(f"### {sub.title}")
            for e in sub.entries:
                L.extend(_md_entry(e))
            for cl in sub.clusters:
                L.append("")
                head = f"**{cl.institution}**" + (f", {cl.city}" if cl.city else "")
                L.append(head)
                L.append("")
                for e in cl.entries:
                    L.extend(_md_entry(e))
    return "\n".join(L).rstrip() + "\n"


# ---------- HTML ----------

_HTML_CSS = """\
  :root { --ink:#1a1a1a; --muted:#555; --rule:#ddd; --link:#33489e; }
  * { box-sizing: border-box; }
  body { font-family: Georgia, "Times New Roman", serif; color: var(--ink);
         max-width: 46rem; margin: 2.5rem auto; padding: 0 1.25rem; line-height: 1.5; }
  header.cv-header { margin-bottom: 1.5rem; }
  h1 { font-size: 1.9rem; margin: 0 0 .25rem; }
  .affiliations { margin: .25rem 0; color: var(--muted); }
  .contacts { margin: .35rem 0 0; font-size: .9rem; color: var(--muted); }
  section { margin-top: 1.75rem; }
  h2 { font-size: 1.2rem; border-bottom: 1px solid var(--rule); padding-bottom: .2rem; margin: 0 0 .6rem; }
  h3 { font-size: 1rem; color: var(--muted); margin: 1rem 0 .4rem; font-weight: 600; }
  ul.entries { list-style: none; margin: 0; padding: 0; }
  ul.entries > li { margin: 0 0 .55rem; }
  .date { font-weight: 600; white-space: nowrap; }
  ul.sub-rows { list-style: none; margin: .15rem 0 0 1.25rem; padding: 0; font-size: .92rem; color: var(--muted); }
  .cluster { margin: .5rem 0 .8rem; }
  .institution { margin: 0 0 .3rem; }
  .city { font-weight: 400; color: var(--muted); }
  a { color: var(--link); text-decoration: none; }
  a:hover { text-decoration: underline; }
  sup { font-size: .7em; }
  footer { margin-top: 2.5rem; border-top: 1px solid var(--rule); padding-top: .6rem;
           font-size: .78rem; color: var(--muted); }"""


def _html_contact(label: str, value: str) -> str:
    """Render one (label, RAW value) contact pair to HTML. Email -> mailto: link
    (inherently safe scheme); Website -> scheme-guarded href (unsafe -> plain text,
    never a live javascript: link in a published file); anything else -> plain text.
    Label + value are both escaped."""
    label_e = html.escape(str(label), quote=False)
    value_e = html.escape(str(value), quote=False)
    if label == "Email":
        href = "mailto:" + str(value).strip()
        return f'{label_e}: <a href="{html.escape(href, quote=True)}">{value_e}</a>'
    if label == "Website":
        safe = markup_convert._link_target(str(value))
        if safe is not None:
            return f'{label_e}: <a href="{html.escape(safe, quote=True)}">{value_e}</a>'
    return f"{label_e}: {value_e}"


def _html_entry(e) -> list[str]:
    """One entry as <li> lines. body + sub_rows are already HTML; date is escaped."""
    inner = f'<span class="date">{e.date}</span> &mdash; {e.body}' if e.date else e.body
    if not e.sub_rows:
        return [f"      <li>{inner}</li>"]
    out = [f"      <li>{inner}", '        <ul class="sub-rows">']
    out.extend(f"          <li>{r}</li>" for r in e.sub_rows)
    out.append("        </ul>")
    out.append("      </li>")
    return out


def render_html(doc: "export_core.Document", *, variant: str | None = None) -> str:
    """Standalone, self-contained HTML CV (inline CSS, no external assets — freeze
    ethos). The view variant is surfaced in a leading HTML comment so a rendered
    page is self-evidently the public default view, not a leaky one. `variant=None`
    uses the resolved name threaded on the Document by build_model."""
    variant = variant or doc.variant
    h = doc.header
    L: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f"  <title>{h.name} &mdash; Curriculum Vitae</title>",
        "  <style>",
        _HTML_CSS,
        "  </style>",
        "</head>",
        "<body>",
        f"  <!-- Generated by cv-editor export (M5 5b). View variant: "
        f"{html.escape(str(variant), quote=False)}. -->",
        '  <header class="cv-header">',
        f"    <h1>{h.name}</h1>",
    ]
    if h.affiliations:
        L.append(f'    <p class="affiliations">{"<br>".join(h.affiliations)}</p>')
    if h.contacts:
        contacts = " &middot; ".join(_html_contact(lbl, val) for lbl, val in h.contacts)
        L.append(f'    <p class="contacts">{contacts}</p>')
    L.append("  </header>")
    L.append("  <main>")
    for sec in doc.sections:
        L.append("    <section>")
        L.append(f"      <h2>{sec.title}</h2>")
        for sub in sec.subsections:
            if sub.title:
                L.append(f"      <h3>{sub.title}</h3>")
            if sub.entries:
                L.append('      <ul class="entries">')
                for e in sub.entries:
                    L.extend(_html_entry(e))
                L.append("      </ul>")
            for cl in sub.clusters:
                L.append('      <div class="cluster">')
                city = f' <span class="city">, {cl.city}</span>' if cl.city else ""
                L.append(
                    f'        <p class="institution"><strong>{cl.institution}</strong>{city}</p>'
                )
                L.append('        <ul class="entries">')
                for e in cl.entries:
                    L.extend(_html_entry(e))
                L.append("        </ul>")
                L.append("      </div>")
        L.append("    </section>")
    L.append("  </main>")
    L.append(
        f"  <footer>Public CV view ({html.escape(str(variant), quote=False)}). "
        "Generated from structured CV data.</footer>"
    )
    L.append("</body>")
    L.append("</html>")
    return "\n".join(L) + "\n"
