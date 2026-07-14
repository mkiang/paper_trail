// Modern template -- section renderers + shared helpers.
//
// Self-contained: imports ONLY lib/flags.typ (the flag/visibility
// contract every template must honor) plus this template's own
// styles.typ. Must NOT import anything from a separate template  --
// its lib/ (styles.typ, typography.typ, entry.typ, talk.typ,
// grant.typ, publication.typ) encodes the private reference look
// (see templates/registry.typ's publish-seam note).
//
// ============================================================================
// V1 SIMPLIFICATIONS (omitted features -- all gated by flags that
// default OFF in lib/flags.typ, so omitting them changes no default
// build; document here per the modern-template build instructions):
//
//   * Publication typed notes (`notes:` on a publication entry --
//     commentary / letter / response / media / editorial /
//     contributions / note sub-bullets). Gated by show-notes
//     (master gate) plus show-contributions / show-media /
//     show-hidden-media / show-media-urls for specific types. NOT
//     implemented -- publication entries render only the citation
//     line.
//   * Open-access sub-bullets (`open_access: {code, data, paper}`).
//     Gated by show-oa. NOT implemented.
//   * "(Cited by N)" citation-count tag. Gated by show-citations.
//     NOT implemented -- data/citation_counts.json is never read.
//   * Media-outlet grouping/dedup display logic. Subsumed by the
//     publication-notes omission above.
//   * Font Awesome contact icons (meta.contacts[].glyph/font/size).
//     NOT rendered -- modern shows each contact's `text` only
//     (as a link when a URL is present or inferable, plain text
//     otherwise), joined with " | ". This keeps the template
//     genuinely self-contained: Libertinus Serif only, no Font
//     Awesome font dependency.
//   * strict-dates active-grant end-date validation. NOT
//     implemented -- an active grant with a past end date renders
//     as authored (no build-failing panic). Matches lib/flags.typ's
//     lenient (non-strict) default behavior.
//
//   Presentations' and service's generic `notes:` sub-bullets (plain
//   string or `{text, highlighted}` dicts) ARE implemented -- they
//   are not gated by any of the flags above, only by per-note
//   `highlighted` + show-highlighted, which modern always honors.
// ============================================================================

#import "../../lib/flags.typ": visible, show-highlighted, show-dollars, show-pending
#import "styles.typ": section, subsection, cluster, row, name-size, body-size, muted, rule-color

#let meta = yaml("../../data/meta.yml")

// ============================================================================
// Self-bolding + inline markup
// ============================================================================

// Normalize meta.self_bold into a tuple of terms (string, list, or
// missing -> always a tuple).
#let self-bold-terms = {
  let raw = meta.at("self_bold", default: ())
  if type(raw) == str {
    if raw == "" { () } else { (raw,) }
  } else {
    raw
  }
}

// Inline-markup helper. YAML strings -> parsed Typst content. Wraps
// every self_bold term in `*...*` before evaluating so the author's
// own name renders bold wherever it appears (author lists,
// presentation titles, etc.) without manual markup in every row.
#let mk(s) = {
  let processed = s
  for term in self-bold-terms {
    processed = processed.replace(term, "*" + term + "*")
  }
  eval(processed, mode: "markup")
}

// ============================================================================
// Visibility
// ============================================================================

// Entry-level visibility: `highlighted: true` hides an entry unless
// show-highlighted; otherwise delegates to flags.typ's visible()
// using the entry's own audiences/hide-from (falling back to the
// cluster/subsection defaults the caller cascades down).
#let entry-visible(e, sub-audiences: (), sub-hide-from: ()) = {
  if e.at("highlighted", default: false) and not show-highlighted {
    return false
  }
  visible(
    audiences: e.at("audiences", default: sub-audiences),
    hide-from: e.at("hide-from", default: sub-hide-from),
  )
}

// ============================================================================
// Date parsing / formatting / sorting
// ============================================================================

// Typographic hyphen -> en-dash normalization for display:
//   "2012 - 2016"  -> "2012 - 2016" (en-dash)
//   "01/2026 -"    -> "01/2026 -"   (en-dash, open-ended)
#let format-date(s) = {
  let r = s.replace(" - ", " \u{2013} ")
  if r.ends-with(" -") {
    r.slice(0, r.len() - 2) + " \u{2013}"
  } else {
    r
  }
}

#let format-month-year(s) = {
  let parts = s.split("/")
  let d = datetime(year: int(parts.at(1)), month: int(parts.at(0)), day: 1)
  d.display("[month repr:long] [year]")
}

#let _parse-date-part(p) = {
  let p = p.trim()
  if p == "" { return (99999, 13) }
  if p.contains("/") {
    let bits = p.split("/")
    return (int(bits.at(1)), int(bits.at(0)))
  }
  return (int(p), 0)
}

// Sort key for reverse-chronological ordering. Handles "MM/YYYY",
// "MM/YYYY - MM/YYYY", "YYYY", "YYYY - YYYY", and open-ended
// "... -" forms (which sort first). Strips footnote-marker
// characters authors may append to a date (e.g. "2025*").
#let date-sort-key(s) = {
  let raw = s.replace("\u{2013}", "-").replace("*", "")
    .replace("\u{2020}", "").replace("\u{2021}", "")
  let ongoing = raw.ends-with(" -")
  let core = if ongoing { raw.slice(0, raw.len() - 2).trim() } else { raw }
  let parts = core.split(" - ")
  let start = _parse-date-part(parts.first())
  let end = if ongoing {
    (99999, 13)
  } else if parts.len() > 1 {
    _parse-date-part(parts.last())
  } else {
    start
  }
  (-end.at(0), -end.at(1), -start.at(0), -start.at(1))
}

// Grant sort key: start-date descending only (grants sort within
// their status group by when they began, not when they end).
#let grant-start-key(s) = {
  let start = _parse-date-part(s.replace("\u{2013}", "-").split(" - ").first())
  (-start.at(0), -start.at(1))
}

// Generic sub-bullet note (presentations / service): plain string or
// `{text, highlighted?}` dict. NOT the publication typed-notes system
// (see V1 SIMPLIFICATIONS) -- only gated by per-note highlighted.
#let format-generic-note(n) = {
  if type(n) == str { return mk(n) }
  if n.at("highlighted", default: false) and not show-highlighted { return none }
  mk(n.text)
}

// ============================================================================
// Author flags (publications) -- glyphs + footnote text lifted
// VERBATIM from the author-flag spec.
// ============================================================================

#let _author-flags = (
  ("co_first", "\u{2020}"),
  ("co_senior", "\u{2021}"),
  ("group_authorship", "\u{25CA}"),
)

#let _author-parts(a) = {
  let out = (name: if type(a) == str { a } else { a.name })
  for f in _author-flags {
    let key = f.at(0)
    out.insert(key, if type(a) == str { false } else { a.at(key, default: false) })
  }
  out
}

// Compose the authors line. Auto-applies dagger/double-dagger/lozenge
// superscripts and returns which flags were seen so the citation can
// append the matching footnote sentence(s).
#let format-authors(authors) = {
  let seen = (co_first: false, co_senior: false, group_authorship: false)
  let body = []
  for (i, a) in authors.enumerate() {
    let parts = _author-parts(a)
    if i > 0 { body += [, ] }
    body += mk(parts.name)
    for f in _author-flags {
      let key = f.at(0)
      let glyph = f.at(1)
      if parts.at(key) {
        body += super[#glyph]
        seen.insert(key, true)
      }
    }
  }
  (
    content: body,
    has-co-first: seen.co_first,
    has-co-senior: seen.co_senior,
    has-group-authorship: seen.group_authorship,
  )
}

// Compose the PubMed-style date/volume/issue/pages suffix.
#let format-pub-date(e) = {
  let months = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")
  let parts = (str(e.year),)
  if "month" in e { parts.push(months.at(e.month - 1)) }
  if "day" in e { parts.push(str(e.day)) }
  let date-str = parts.join(" ")
  let tail = ""
  if "volume" in e {
    tail += ";" + e.volume
    if "issue" in e { tail += "(" + e.issue + ")" }
    if "pages" in e { tail += ":" + e.pages }
  } else if "pages" in e {
    tail += "; " + e.pages
  }
  if "date_qualifier" in e {
    tail += " \u{2014} " + e.date_qualifier
  }
  date-str + tail
}

// Full citation body for one publication entry.
#let render-pub-body(e) = {
  let af = format-authors(e.authors)
  let body = af.content
  body += [. ]
  let title = if e.title.ends-with(".") { e.title.slice(0, -1) } else { e.title }
  body += mk(title)
  body += [. ]
  body += emph(mk(e.journal))
  body += [. ]
  body += format-pub-date(e)
  body += [. ]
  if "doi" in e {
    body += [doi: ]
    body += link("https://doi.org/" + e.doi)[#e.doi]
    body += [. ]
  }
  if "epub_date" in e {
    body += [Epub #e.epub_date. ]
  }
  if "pmid" in e {
    body += [PubMed PMID: ]
    body += link("https://pubmed.ncbi.nlm.nih.gov/" + str(e.pmid) + "/")[#e.pmid]
    if "pmcid" in e {
      body += [; PubMed Central PMCID: ]
      body += link("https://www.ncbi.nlm.nih.gov/pmc/articles/" + str(e.pmcid) + "/")[#e.pmcid]
      body += [.]
    } else {
      body += [.]
    }
  } else if "pmcid" in e {
    body += [PubMed Central PMCID: ]
    body += link("https://www.ncbi.nlm.nih.gov/pmc/articles/" + str(e.pmcid) + "/")[#e.pmcid]
    body += [.]
  }
  if af.has-co-first {
    body += [ ] + super[\u{2020}] + [First authors contributed equally.]
  }
  if af.has-co-senior {
    body += [ ] + super[\u{2021}] + [Senior authors contributed equally.]
  }
  if af.has-group-authorship {
    body += [ ] + super[\u{25CA}] + [Group authorship.]
  }
  body
}

// ============================================================================
// Header
// ============================================================================

// Infer a clickable URL from contact text when no explicit `link:` is
// given: email -> mailto:, http(s):// -> as-is, ORCID iD pattern ->
// https://orcid.org/{id}. Returns none if nothing matches.
#let infer-link(t) = {
  if t.contains("@") and not t.contains(" ") { return "mailto:" + t }
  if t.starts-with("http://") or t.starts-with("https://") { return t }
  if t.match(regex("^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$")) != none {
    return "https://orcid.org/" + t
  }
  none
}

#let render-header() = {
  block(above: 0pt, below: 6pt, text(size: name-size, weight: "bold", meta.name))
  block(above: 0pt, below: 8pt, text(size: body-size, {
    mk(meta.position)
    linebreak()
    mk(meta.department)
    linebreak()
    mk(meta.institution)
    for line in meta.address {
      linebreak()
      mk(line)
    }
  }))
  block(above: 0pt, below: 10pt, text(size: 9.5pt, fill: muted, {
    for (i, c) in meta.contacts.enumerate() {
      if i > 0 { [ | ] }
      let url = c.at("link", default: infer-link(c.text))
      if url != none { link(url, c.text) } else { c.text }
    }
  }))
  line(length: 100%, stroke: 0.6pt + rule-color)
  v(4pt)
}

// ============================================================================
// Section body renderers (take already-loaded YAML)
// ============================================================================

// Education: clustered by institution; both clusters and entries
// auto-sort reverse-chronologically.
#let render-education(data) = {
  let cluster-key(c) = c.entries.map(e => date-sort-key(e.date)).sorted().first()
  for c in data.sorted(key: cluster-key) {
    cluster(c.institution, city: c.at("city", default: none))
    for e in c.entries.sorted(key: e => date-sort-key(e.date)) {
      if not entry-visible(
        e,
        sub-audiences: c.at("audiences", default: ()),
        sub-hide-from: c.at("hide-from", default: ()),
      ) { continue }
      let title = e.at("title", default: none)
      row(marker: format-date(e.date), {
        emph(mk(e.degree))
        if title != none [, #mk(title)]
      })
      let department = e.at("department", default: none)
      if department != none {
        row(marker: "", mk(department))
      }
    }
  }
}

// Appointments: subsections -> clusters -> entries. Subsection/cluster
// order is author-controlled; entries auto-sort within each cluster.
#let render-appointments(data) = {
  for sub in data {
    let sub-audiences = sub.at("audiences", default: ())
    let sub-hide-from = sub.at("hide-from", default: ())
    let visible-clusters = sub.clusters.map(c => {
      let kept = c.entries.filter(e => entry-visible(
        e,
        sub-audiences: c.at("audiences", default: sub-audiences),
        sub-hide-from: c.at("hide-from", default: sub-hide-from),
      ))
      (cluster: c, entries: kept)
    }).filter(c => c.entries.len() > 0)
    if visible-clusters.len() == 0 { continue }

    subsection(sub.subsection)
    for c in visible-clusters {
      let cl = c.cluster
      cluster(cl.institution, city: cl.at("city", default: none))
      for e in c.entries.sorted(key: e => date-sort-key(e.date)) {
        let program = e.at("program", default: none)
        row(marker: format-date(e.date), {
          emph(mk(e.role))
          if program != none [, #mk(program)]
        })
      }
    }
  }
}

// Publications: subsections (author-ordered) of entries (auto-sorted
// by year -> month -> day desc). Reverse-numbered per subsection.
#let render-publications(data) = {
  for sub in data {
    let sub-audiences = sub.at("audiences", default: ())
    let sub-hide-from = sub.at("hide-from", default: ())
    let entries = sub.entries.filter(e => entry-visible(
      e, sub-audiences: sub-audiences, sub-hide-from: sub-hide-from,
    )).sorted(key: e => (
      -e.year, -e.at("month", default: 0), -e.at("day", default: 0),
    ))
    if entries.len() == 0 { continue }

    subsection(sub.subsection)
    let total = entries.len()
    for (i, e) in entries.enumerate() {
      row(marker: str(total - i) + ".", above: 9pt, render-pub-body(e))
    }
  }
}

// Presentations: subsections of uniform-schema entries; reverse-
// numbered per subsection (resets each subsection, like publications).
#let render-presentations(data) = {
  for sub in data {
    let sub-audiences = sub.at("audiences", default: ())
    let sub-hide-from = sub.at("hide-from", default: ())
    let entries = sub.entries.filter(e => entry-visible(
      e, sub-audiences: sub-audiences, sub-hide-from: sub-hide-from,
    )).sorted(key: e => date-sort-key(e.date))
    if entries.len() == 0 { continue }

    subsection(sub.subsection)
    let total = entries.len()
    for (i, e) in entries.enumerate() {
      let notes = e.at("notes", default: ())
        .map(format-generic-note).filter(n => n != none)
      row(marker: str(total - i) + ".", above: 9pt, notes: notes, {
        let authors = e.at("authors", default: none)
        let title = e.at("title", default: none)
        let italic-v = e.at("italic_venue", default: true)
        let location = e.at("location", default: none)
        if authors != none [#mk(authors), ]
        if title != none {
          let t = if title.ends-with(".") { title.slice(0, -1) } else { title }
          [#mk(t). ]
        }
        if italic-v { emph(mk(e.venue)) } else { mk(e.venue) }
        [ (#format-month-year(e.date))]
        if location != none [. #mk(location).]
      })
    }
  }
}

// Research Support: flat list of grants grouped by status
// (pending -> active -> previous). Pending gated by show-pending.
// Agency/title/role render as plain text (not mk()'d) -- matches the
// schema's "renders bold/plain" wording; only `amount` carries markup
// (its `'\$X'` escape needs mk() to render a literal $).
#let render-research-support(data) = {
  let by-status = (pending: (), active: (), previous: ())
  for g in data { by-status.at(g.status).push(g) }
  let order = ("pending", "active", "previous")
  let titles = (
    pending: "Pending Support",
    active: "Active Support",
    previous: "Previous Support",
  )
  for status in order {
    if status == "pending" and not show-pending { continue }
    let group = by-status.at(status).filter(g => entry-visible(g))
    if group.len() == 0 { continue }

    subsection(titles.at(status))
    for g in group.sorted(key: g => grant-start-key(g.date)) {
      let project = g.at("project", default: none)
      let pi = g.at("pi", default: none)
      let pi-label = g.at("pi_label", default: "PI")
      row(marker: format-date(g.date), above: 10pt, {
        strong(g.agency)
        if project != none or pi != none [
          ~(#{
            if project != none [#project]
            if project != none and pi != none [;~]
            if pi != none [#pi-label: #pi]
          })
        ]
        linebreak()
        [#emph[Title:] "#g.title"]
        linebreak()
        [
          #emph[Role:] #g.role
          #if "amount" in g and show-dollars [~~(#mk(g.amount))]
        ]
      })
    }
  }
}

// Professional Service: subsections of entries plus an optional
// per-subsection "ad hoc reviewer" journal list.
#let render-service(data) = {
  for sub in data {
    let entries = sub.at("entries", default: ())
      .filter(e => entry-visible(e))
      .sorted(key: e => date-sort-key(e.date))
    let has-ahr = "ad_hoc_reviewer" in sub
    if entries.len() == 0 and not has-ahr { continue }

    subsection(sub.subsection)
    for e in entries {
      let extras = e.at("extras", default: ())
      let notes = e.at("notes", default: ())
        .map(format-generic-note).filter(n => n != none)
      row(
        marker: format-date(e.date),
        above: if extras.len() > 0 { 9pt } else { 5pt },
        notes: notes,
        {
          emph(mk(e.role))
          if "venue" in e [, #mk(e.venue)]
          for line in extras {
            linebreak()
            mk(line)
          }
        },
      )
    }
    if has-ahr {
      let ahr = sub.ad_hoc_reviewer
      let sorted-journals = ahr.journals.sorted(key: j => lower(j))
      row(marker: ahr.label, above: 9pt, sorted-journals.map(j => mk(j)).join([ \u{2022} ]))
    }
  }
}

// Teaching Experience: clustered by institution; entries auto-sort
// reverse-chronologically within each cluster.
#let render-teaching(data) = {
  for c in data {
    let entries = c.at("entries", default: ())
      .filter(e => entry-visible(e))
      .sorted(key: e => date-sort-key(e.date))
    if entries.len() == 0 { continue }

    cluster(c.institution, city: c.at("city", default: none))
    for t in entries {
      row(marker: format-date(t.date), [#emph(mk(t.role)), #mk(t.course)])
    }
  }
}

// Honors & Awards: flat list, auto-sorted reverse-chronologically.
#let render-honors(data) = {
  for h in data.sorted(key: e => date-sort-key(e.date)) {
    if not entry-visible(h) { continue }
    row(
      marker: format-date(h.at("date", default: "")),
      [#emph(mk(h.award)), #mk(h.institution)],
    )
  }
}

// Mentees: flat list, auto-sorted reverse-chronologically.
// "_role_, name (institution)".
#let render-mentees(data) = {
  for m in data.sorted(key: e => date-sort-key(e.date)) {
    if not entry-visible(m) { continue }
    row(
      marker: format-date(m.at("date", default: "")),
      [#emph(mk(m.role)), #mk(m.name) (#mk(m.institution))],
    )
  }
}

// ============================================================================
// Per-section "is there anything visible at all" checks, used to skip
// a section (heading included) entirely rather than render a
// dangling header over empty content. Each predicate mirrors the
// cascade the corresponding render-<section> above actually applies.
// ============================================================================

#let _any-clusters-visible(data) = data.any(c => c.entries.any(e => entry-visible(
  e,
  sub-audiences: c.at("audiences", default: ()),
  sub-hide-from: c.at("hide-from", default: ()),
)))

#let _any-appointments-visible(data) = data.any(sub => {
  let sa = sub.at("audiences", default: ())
  let sh = sub.at("hide-from", default: ())
  sub.clusters.any(c => c.entries.any(e => entry-visible(
    e,
    sub-audiences: c.at("audiences", default: sa),
    sub-hide-from: c.at("hide-from", default: sh),
  )))
})

#let _any-subsection-entries-visible(data) = data.any(sub => {
  let sa = sub.at("audiences", default: ())
  let sh = sub.at("hide-from", default: ())
  sub.entries.any(e => entry-visible(e, sub-audiences: sa, sub-hide-from: sh))
})

#let _any-research-support-visible(data) = data.any(g => entry-visible(g)
  and (g.status != "pending" or show-pending))

#let _any-service-visible(data) = data.any(sub => sub.at("entries", default: ())
  .any(e => entry-visible(e)) or "ad_hoc_reviewer" in sub)

#let _any-teaching-visible(data) = data.any(c => c.at("entries", default: ())
  .any(e => entry-visible(e)))

#let _any-flat-visible(data) = data.any(e => entry-visible(e))

// ============================================================================
// Per-section dispatch: load YAML, skip (heading included) if nothing
// visible, else render the heading + body. `before` lets template.typ
// apply the first-section 30pt-before override. All 9 sections share
// this shape (path, title, any-visible predicate, body renderer), so
// one small dispatcher replaces 9 near-identical wrapper bodies.
// ============================================================================

#let _dispatch-section(path, title, any-visible, render-fn, before: 24pt) = {
  let data = yaml(path)
  if not any-visible(data) { return }
  section(title, before: before)
  render-fn(data)
}

#let render-section-education(before: 24pt) = _dispatch-section(
  "../../data/education.yml", [Education],
  _any-clusters-visible, render-education, before: before,
)
#let render-section-appointments(before: 24pt) = _dispatch-section(
  "../../data/appointments.yml", [Professional Appointments],
  _any-appointments-visible, render-appointments, before: before,
)
#let render-section-publications(before: 24pt) = _dispatch-section(
  "../../data/publications.yml", [Scholarly Publications],
  _any-subsection-entries-visible, render-publications, before: before,
)
#let render-section-presentations(before: 24pt) = _dispatch-section(
  "../../data/presentations.yml", [Presentations],
  _any-subsection-entries-visible, render-presentations, before: before,
)
#let render-section-research-support(before: 24pt) = _dispatch-section(
  "../../data/research_support.yml", [Research Support],
  _any-research-support-visible, render-research-support, before: before,
)
#let render-section-service(before: 24pt) = _dispatch-section(
  "../../data/service.yml", [Professional Service],
  _any-service-visible, render-service, before: before,
)
#let render-section-teaching(before: 24pt) = _dispatch-section(
  "../../data/teaching.yml", [Teaching Experience],
  _any-teaching-visible, render-teaching, before: before,
)
#let render-section-honors(before: 24pt) = _dispatch-section(
  "../../data/honors.yml", [Honors #emph[&] Awards],
  _any-flat-visible, render-honors, before: before,
)
#let render-section-mentees(before: 24pt) = _dispatch-section(
  "../../data/mentees.yml", [Mentees],
  _any-flat-visible, render-mentees, before: before,
)
