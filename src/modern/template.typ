// Modern template entrypoint -- implements the template CONTRACT
// (templates/registry.typ): exports `setup`, `render()`.
//
// PUBLIC template: this is the intended open-source default look.
// Self-contained -- imports ONLY lib/flags.typ plus this directory's
// own render.typ/styles.typ, and reads ZERO files at module scope so it
// compiles as a Typst PACKAGE (P6a; a package can't escape its own root).
// Must compile after templates/bespoke/ is deleted (the publish seam;
// see templates/registry.typ).
//
// Module scope: imports only -- no data loads, no asserts/panics.

#import "render.typ": make-mk, render-header, render-section-education, render-section-appointments, render-section-publications, render-section-presentations, render-section-research-support, render-section-service, render-section-teaching, render-section-honors, render-section-mentees
#import "styles.typ": setup

// Dispatch table: meta.yml `sections:` key -> the section's
// render-section-<key>(before:) function. Add a new key here when
// adding a new section.
#let sections = (
  education: render-section-education,
  appointments: render-section-appointments,
  publications: render-section-publications,
  presentations: render-section-presentations,
  research_support: render-section-research-support,
  service: render-section-service,
  teaching: render-section-teaching,
  honors: render-section-honors,
  mentees: render-section-mentees,
)

// Full CV body. Header (name + affiliation + contacts) always renders
// first, then each body section in meta.sections order (falls back to
// sections.keys() if omitted). The first section gets a 30pt-before
// override to match the header-to-first-section convention;
// subsequent sections use each section()'s default (18pt before).
//
// Sections OMITTED from meta.sections are not rendered. A section key
// not in `sections` above panics with the full list of valid keys.
//
// Data-injection contract (P6a): `meta` + `section-data` are REQUIRED --
// the consumer (cv.typ) owns every yaml() load and injects both. modern
// reads NO files (packageable). Passing none is a contract violation and
// asserts below. The per-section data + the `mk` closure (built here via
// make-mk so self_bold is folded in once) are threaded into every
// section wrapper.
#let render(meta: none, section-data: none) = {
  assert(meta != none, message: "modern.render: `meta` is required (the consumer must inject it)")
  assert(
    section-data != none,
    message: "modern.render: `section-data` is required (the consumer must inject it)",
  )
  let mk = make-mk(meta)
  render-header(meta, mk)
  let order = meta.at("sections", default: sections.keys())
  for (i, key) in order.enumerate() {
    let render-fn = sections.at(key, default: none)
    if render-fn == none {
      panic(
        "Unknown section key \"" + key + "\" in data/meta.yml `sections:`. "
          + "Valid keys: " + repr(sections.keys()) + ".",
      )
    }
    let data = section-data.at(key)
    if i == 0 {
      render-fn(data, mk, before: 30pt)
    } else {
      render-fn(data, mk)
    }
  }
}
