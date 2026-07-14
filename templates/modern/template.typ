// Modern template entrypoint -- implements the template CONTRACT
// (templates/registry.typ): exports `meta`, `setup`, `render()`.
//
// PUBLIC template: this is the intended open-source default look.
// Self-contained -- imports ONLY lib/flags.typ plus this directory's
// own render.typ/styles.typ. Must compile after a separate template  is
// deleted (the publish seam; see templates/registry.typ).
//
// Module scope: imports + data loads only -- no asserts/panics.

#import "render.typ": meta, render-header, render-section-education, render-section-appointments, render-section-publications, render-section-presentations, render-section-research-support, render-section-service, render-section-teaching, render-section-honors, render-section-mentees
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
#let render() = {
  render-header()
  let order = meta.at("sections", default: sections.keys())
  for (i, key) in order.enumerate() {
    let render-fn = sections.at(key, default: none)
    if render-fn == none {
      panic(
        "Unknown section key \"" + key + "\" in data/meta.yml `sections:`. "
          + "Valid keys: " + repr(sections.keys()) + ".",
      )
    }
    if i == 0 {
      render-fn(before: 30pt)
    } else {
      render-fn()
    }
  }
}
