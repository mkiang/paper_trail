// Byte-diff probe for the flatten feature (Part 3).
// Mirrors cv.typ's dispatch, but renders EVERY body section via lib/emit.typ
// (emit-section → eval of literal markup) instead of the render-* path.
// The header still uses render-header (not the edit target).
//
// If the emitters are byte-faithful, compiling this probe produces glyph
// positions identical to compiling cv.typ with the same --input flags.
// tests/test_flatten.py runs that comparison for the cv + everything variants.
//
// Compile with the typst/ root as project root:
//   typst compile --root . --font-path fonts --ignore-system-fonts \
//       --input <variant flags> tests/flatten_probe.typ out.pdf

#import "../templates/bespoke/lib/styles.typ": setup, section, subsection, institution
#import "../templates/bespoke/render.typ": meta, render-header, linkify-dois
#import "../templates/bespoke/lib/entry.typ": entry
#import "../templates/bespoke/lib/talk.typ": talk
#import "../templates/bespoke/lib/grant.typ": grant
#import "../templates/bespoke/lib/publication.typ": pub-entry
#import "../templates/bespoke/emit.typ": emit-section

#show: setup.with(meta: meta)

#render-header(meta)

// Identifiers referenced by the emitted markup must be in eval scope; markup
// builtins (emph/super/link/text/strong/rgb/linebreak) are available without it.
#let _emit-scope = (
  section: section,
  subsection: subsection,
  institution: institution,
  entry: entry,
  talk: talk,
  grant: grant,
  pub-entry: pub-entry,
  linkify-dois: linkify-dois,
)

#let order = meta.at("sections", default: ())
#for (i, key) in order.enumerate() {
  let before = if i == 0 { 30pt } else { 24pt }
  let data = yaml("../data/" + key + ".yml")
  eval(emit-section(key, data, before: before), mode: "markup", scope: _emit-scope)
}
