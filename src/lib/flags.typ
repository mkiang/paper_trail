// Build-time flags read from `typst compile --input key=value`.
// All sys.inputs values are strings; parse explicitly where needed.

#let audience = sys.inputs.at("audience", default: "full")
// NOTE: `review` is currently UNCONSUMED by the render pipeline — its only user
// was the `draft()` helper, removed in P2-bespoke. The `review` build variant
// still sets `--input review=true`, but its visible effect comes from the
// separate `show_future` flag, not this one. Kept as a public flag surface;
// revisit (drop, or re-wire to a highlight helper) in a later cleanup.
#let review = sys.inputs.at("review", default: "false") == "true"

// Some institution-internal CVs cannot include grant dollar amounts. Set this via
// --input show_dollars=false to suppress them across the document.
// Defaults to true (dollar amounts visible) for most renders.
#let show-dollars = sys.inputs.at("show_dollars", default: "true") == "true"

// Pending Support is hidden by default — most external CVs don't list
// pending grants. Opt in for internal drafts via --input show_pending=true.
#let show-pending = sys.inputs.at("show_pending", default: "false") == "true"

// Per-publication open-access glyph row (code / data / paper). Hidden by
// default. Opt in for public-facing CVs with --input show_oa=true.
#let show-oa = sys.inputs.at("show_oa", default: "false") == "true"

// Per-publication "(Cited by N)" tag. Hidden by default. Opt in for
// grant-prep / internal / everything PDFs with --input show_citations=true.
// Reads counts from `data/citation_counts.json` (committed snapshot,
// regenerated via `scripts/fetch_citation_counts.py`).
#let show-citations = sys.inputs.at("show_citations", default: "false") == "true"

// Per-publication "Contributions:" sub-bullet describing authorial role.
// Hidden by default. Opt in for internal/draft CVs that want this with
// --input show_contributions=true.
#let show-contributions = sys.inputs.at("show_contributions", default: "false") == "true"

// Master gate for ALL per-publication typed-note sub-bullets (commentary,
// letter, response, media, editorial, contributions, note). Hidden by default.
// Opt in with --input show_notes=true (some variants do, e.g. internal + everything).
// Sits ABOVE the per-type gates: media still also needs show_media, and
// contributions still also needs show_contributions, even when show_notes=true.
// Does NOT affect the OA "Reproducibility:" bullets (show_oa) or the
// "(Cited by N)" tag (show_citations).
#let show-notes = sys.inputs.at("show_notes", default: "false") == "true"

// Per-publication "Selected media coverage:" sub-bullet for outlets NOT
// marked `highlighted: true`. Hidden by default — most variants omit press
// coverage. Opt in with --input show_media=true (some variants, e.g. internal + everything
// variants do). Note: as of 2026-05-26 this flag no longer surfaces hidden
// (highlighted) outlets; pair with show_hidden_media to see those.
#let show-media = sys.inputs.at("show_media", default: "false") == "true"

// Per-publication media outlets marked `highlighted: true` in YAML — the
// user-curated "hidden" pile. Hidden by default. Opt in with
// --input show_hidden_media=true (the everything variant does). When both
// show_media and show_hidden_media are off, the whole media sub-bullet is
// suppressed; when only one is on, only that slice renders.
#let show-hidden-media = sys.inputs.at("show_hidden_media", default: "false") == "true"

// Stage D / I6 (2026-05-25): hyperlink media outlet names to their press
// URLs. Default true (current behavior — outlets render as clickable
// links). Set --input show_media_urls=false to render outlet names as
// plain text instead. Useful for paper-routed variants where clickable
// links don't help, or variants with many outlets where accidental
// clicks would be annoying. Has no effect when show-media is false
// (the outlets aren't rendered at all in that case).
#let show-media-urls = sys.inputs.at("show_media_urls", default: "true") == "true"

// Entries marked `highlighted: true` in YAML correspond to items that
// were yellow-highlighted in the original Word source — typically items
// pending review or that the author hasn't finalized. They're hidden by
// default. Opt in with --input show_highlighted=true to see them
// (useful when finalizing a CV variant).
#let show-highlighted = sys.inputs.at("show_highlighted", default: "false") == "true"

// Date-conditional entries (appointments, service, teaching, education,
// honors, mentees, presentations) whose START date is in the future are
// HIDDEN by default so fixed-term items can be entered whenever convenient and
// appear automatically once they begin. A closed range whose END is in the
// future renders as open-ended ("Start –") until the end passes, then shows the
// full range. Set --input show_future=true (the review + everything variants do)
// to REVEAL hidden future entries AND show their literal entered dates (no
// collapse) — a preview/QC aid. Grants and publications are NOT date-gated.
//
// The reference "now" is Typst's datetime.today() (compile-machine clock),
// overridable with --input today=YYYY-MM-DD for deterministic tests and
// reproducible builds (SOURCE_DATE_EPOCH does NOT pin datetime.today()). The
// date logic lives in templates/bespoke/render.typ (render-today / active-form /
// start-in-future); mirrored in emit.typ.
#let show-future = sys.inputs.at("show_future", default: "false") == "true"

// An "active" grant whose end date is in the past is a data-freshness
// inconsistency — either the status wasn't flipped to "previous", or it's a
// no-cost extension whose YAML end date is stale. Default LENIENT (false):
// render the grant in place under Active Support with its literal dates (no
// build failure; the past end date is visible). Set --input strict_dates=true
// to HARD-FAIL the build instead — a freshness guard for the owner's own QC.
// See render-research-support in lib/render.typ (mirrored in lib/emit.typ).
#let strict-dates = sys.inputs.at("strict_dates", default: "false") == "true"

// Returns true if an entry should be shown for the current audience.
// Two visibility controls (both optional, both lists):
//   - audiences:  allowlist. Empty = universal. Non-empty = show only when
//                 current audience matches one of these tags.
//   - hide-from:  blocklist. Always wins. Show everywhere EXCEPT these.
//
// `audience == "full"` shows everything (allowlist), but `hide-from` still
// applies — so on the `full` build you can preview a public-CV exclusion
// by setting audience=public-health (or it stays visible by default).
//
// The blocklist is the convenience case: hiding one honor from a single
// CV variant is `hide-from: [public-health]` (one entry) instead of
// `audiences: [academic, industry, internal]` (every audience except one).
#let visible(audiences: (), hide-from: ()) = {
  if hide-from.contains(audience) { return false }
  audience == "full" or audiences.len() == 0 or audiences.contains(audience)
}
