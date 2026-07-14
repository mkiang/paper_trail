// Build-time flags read from `typst compile --input key=value`.
// All sys.inputs values are strings; parse explicitly.
//
// The `modern` template honors exactly these flags. (Additional flags used by
// other templates would be declared here too.)

// Which audience a variant targets. `full` shows everything; otherwise an
// entry is shown only if its `audiences:` list is empty or contains this value,
// and never if its `hide-from:` list contains it. See visible() below.
#let audience = sys.inputs.at("audience", default: "full")

// Grant dollar amounts. Default on; set --input show_dollars=false to suppress
// (some institutions restrict amounts on internal CVs).
#let show-dollars = sys.inputs.at("show_dollars", default: "true") == "true"

// Pending Support. Hidden by default; opt in with --input show_pending=true.
#let show-pending = sys.inputs.at("show_pending", default: "false") == "true"

// Entries marked `highlighted: true` (e.g. items not yet finalized) are hidden
// by default; opt in with --input show_highlighted=true.
#let show-highlighted = sys.inputs.at("show_highlighted", default: "false") == "true"

// Returns true if an entry should be shown for the current audience.
//   audiences: allowlist (empty = universal).
//   hide-from: blocklist (always wins).
#let visible(audiences: (), hide-from: ()) = {
  if hide-from.contains(audience) { return false }
  audience == "full" or audiences.len() == 0 or audiences.contains(audience)
}
