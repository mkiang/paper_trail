// Modern template -- visual primitives (page setup, colors, typography
// constants, low-level layout helpers). Self-contained: imports ONLY
// this file's own constants -- no imports from lib/ or
// templates/bespoke/ (see render.typ for the V1 SIMPLIFICATIONS block
// and templates/registry.typ for the publish-seam contract).
//
// Single column, hanging-indent entries via a two-column grid (a
// right-aligned "marker" column -- date or entry number -- plus a
// left-aligned body column that wraps naturally). Libertinus Serif
// throughout (embedded in the typst binary; no external font files
// needed, so this template compiles even after templates/bespoke/ and
// fonts/ are both deleted).

// ---- Font ------------------------------------------------------------
#let body-font = "Libertinus Serif"

// ---- Colors ------------------------------------------------------------
#let ink = rgb("#1a1a1a")     // body text
#let muted = rgb("#5c5c5c")   // dates, entry numbers, footer
#let rule-color = rgb("#3a3a3a")

// ---- Sizes ---------------------------------------------------------------
#let name-size = 24pt
#let section-size = 11.5pt
#let subsection-size = 10pt
#let body-size = 10.5pt
#let date-size = 9pt
#let footer-size = 8.5pt

// ---- Grid geometry -------------------------------------------------------
#let date-col-width = 1.05in
#let col-gutter = 0.15in
#let body-indent = date-col-width + col-gutter

// setup(): page + base text/paragraph rules. Applied as
// `#show: tpl.setup.with(meta: tpl.meta)` from template.typ.
//
// Footer mirrors meta.footer (template string with a `{date}`
// substitution + date_format + show_on_first_page) -- same schema
// bespoke reads, implemented independently here (no shared code, per
// the publish-seam constraint).
#let setup(meta: none, body) = {
  let footer-content = {
    let date-str = datetime.today().display(
      meta.footer.at("date_format", default: "[month repr:long] [year]"),
    )
    eval(meta.footer.template.replace("{date}", date-str), mode: "markup")
  }
  let show-on-first = meta.footer.at("show_on_first_page", default: false)

  set page(
    paper: "us-letter",
    margin: 2cm,
    footer: context {
      let page-num = counter(page).get().first()
      if page-num > 1 or show-on-first {
        align(right, text(
          size: footer-size,
          fill: muted,
          [#footer-content  |  #page-num of #counter(page).final().first()],
        ))
      }
    },
  )
  set text(font: body-font, size: body-size, fill: ink, hyphenate: false)
  set par(leading: 0.62em, justify: false)
  set block(above: 6pt, below: 0pt)
  show link: underline
  body
}

// section(title): top-level heading -- uppercase, letter-tracked, ruled.
// `sticky: true` keeps the heading from being orphaned at a page bottom.
#let section(title, before: 18pt) = {
  block(above: before, below: 6pt, sticky: true, {
    text(size: section-size, weight: "bold", tracking: 1.2pt, upper(title))
    v(3pt)
    line(length: 100%, stroke: 0.6pt + rule-color)
  })
}

// subsection(title): mid-level heading -- small caps, no rule.
#let subsection(title) = {
  block(above: 12pt, below: 5pt, sticky: true, text(
    size: subsection-size,
    weight: "bold",
  )[#smallcaps(title)])
}

// cluster(name, city): bold institution / organization line, indented
// to the body column so it lines up with entry text below it.
#let cluster(name, city: none) = {
  block(above: 8pt, below: 2pt, pad(left: body-indent, {
    text(weight: "bold", name)
    if city != none [, #city]
  }))
}

// row(marker, body, ...): the workhorse two-column grid. `marker` is
// either a formatted date or an entry number ("12."); it right-aligns
// in a fixed column while `body` wraps naturally in the remaining
// width -- the hanging indent falls straight out of the grid cell,
// no manual line-break bookkeeping needed. `notes` renders each item
// as an indented bulleted sub-line below the row.
#let row(marker: "", above: 5pt, notes: (), body) = {
  block(above: above, grid(
    columns: (date-col-width, 1fr),
    column-gutter: col-gutter,
    row-gutter: 0pt,
    align: (right + top, left + top),
    text(size: date-size, fill: muted, marker),
    body,
  ))
  for (i, note) in notes.enumerate() {
    block(
      above: if i == 0 { 5pt } else { 3pt },
      pad(left: body-indent + 0.15in, grid(
        columns: (10pt, 1fr),
        column-gutter: 0pt,
        align: (left + top, left + top),
        text(fill: muted)[\u{2022}],
        note,
      )),
    )
  }
}
