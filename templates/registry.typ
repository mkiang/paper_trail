// Template registry — the publish seam. The ONLY file that static-imports a
// template module. To add a template, drop it under src/<name>/ (re-exported by
// src/lib.typ), import it here, and add it to the `templates` dict. Select at
// build time with `--input template=<name>` (overrides a `template:` key in
// data/meta.yml, which overrides `default-template`).
//
// Template CONTRACT: each template module exports
//   setup                       — the document show-rule, applied by the consumer
//                                 as `#show: tpl.setup.with(meta: meta)`
//   render(meta:, section-data:) — the full CV body (header + sections in
//                                 meta.sections order), a pure function of the
//                                 injected data (the consumer owns every yaml()).

// `modern` is re-exported by the package hub (src/lib.typ), which also carries
// the shared flag contract. Importing through the hub keeps the seam single.
#import "../src/lib.typ": modern

#let default-template = "modern"

#let templates = (
  modern: modern,
)

// Canonical section-key vocabulary — the valid values for meta.yml `sections:`.
// The consumer (cv.typ) validates meta.sections against this and uses it as the
// fallback when `sections:` is omitted, so an unknown/typo key gets a friendly
// "Valid keys: [...]" panic BEFORE any data load, and the template always
// receives a full section-data dict. Add a key here when adding a section.
#let section-keys = (
  "education",
  "appointments",
  "publications",
  "presentations",
  "research_support",
  "service",
  "teaching",
  "honors",
  "mentees",
)

// Resolved template NAME: --input template=<name> overrides meta.yml's optional
// top-level `template:`, which overrides `default-template`. Takes the loaded
// `meta` dict from the consumer so the registry does no yaml() of its own.
#let resolve-name(meta) = {
  let name = sys.inputs.at(
    "template",
    default: meta.at("template", default: default-template),
  )
  if name not in templates {
    panic(
      "Unknown template \"" + name + "\". Valid templates: "
        + repr(templates.keys()) + ". Set via --input template=<name> "
        + "or a `template:` key in data/meta.yml.",
    )
  }
  name
}

// Resolved template MODULE.
#let resolve(meta) = templates.at(resolve-name(meta))
