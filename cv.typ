// Consumer entrypoint: loads the CV data, resolves the active template
// (--input template=<name> -> meta.yml `template:` -> registry default), and
// injects the data into it. Template modules export `setup` + a data-injection
// `render(meta:, section-data:)` — the contract lives in templates/registry.typ.
//
// cv.typ owns every `yaml()` load. It passes `meta` plus a per-section
// `section-data` dict into the template, which is a pure function of that
// injected data (no template reads files itself).

#import "templates/registry.typ": resolve, section-keys

#let meta = yaml("data/meta.yml")
#let tpl = resolve(meta)

// Load each section listed in meta.sections so the template can render from
// injected data. Falls back to the canonical section-keys when `sections:` is
// omitted. Each key is validated against section-keys FIRST so a typo yields a
// friendly "Valid keys: [...]" panic rather than a bare "file not found".
#let section-data = {
  let d = (:)
  for key in meta.at("sections", default: section-keys) {
    if key not in section-keys {
      panic(
        "Unknown section key \"" + key + "\" in data/meta.yml `sections:`. "
          + "Valid keys: " + repr(section-keys) + ".",
      )
    }
    d.insert(key, yaml("data/" + key + ".yml"))
  }
  d
}

#show: tpl.setup.with(meta: meta)

#tpl.render(meta: meta, section-data: section-data)
