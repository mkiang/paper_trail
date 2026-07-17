// paper-trail package entrypoint — the AGGREGATING RE-EXPORT HUB.
//
// The shared engine lives here. The hub re-exports:
//   * the `modern` public template MODULE (dot-access `modern.render` /
//     `modern.setup`);
//   * the shared flag / visibility contract (`flags.typ` is pure
//     `sys.inputs` + `visible()`, ZERO file I/O, so it resolves regardless of
//     how a distribution lays out the engine, and the import PATH cannot enter
//     rendered bytes — proven byte-identical by the delta-oracle).
// `modern` reads NO files at module scope, so it compiles as a package
// entrypoint (which can't escape its own root). The flags import paths below
// are relative to THIS file's location in the shipped layout; a distribution
// that relocates the engine adjusts them at assembly time.
#import "modern/template.typ" as modern
#import "lib/flags.typ": *
