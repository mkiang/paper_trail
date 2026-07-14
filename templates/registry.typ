// Template registry. To add a template, drop it under templates/<name>/,
// import it here, and add it to the `templates` dict. Select at build time
// with `--input template=<name>` (overrides a `template:` key in
// data/meta.yml, which overrides `default-template`).
//
// A template module must export:
//   meta     — the loaded data/meta.yml dict
//   setup    — the document show-rule, applied as
//              `#show: tpl.setup.with(meta: tpl.meta)`
//   render() — the CV body (header + sections in meta.sections order)

#import "modern/template.typ" as modern

#let default-template = "modern"

#let templates = (
  modern: modern,
)

#let resolve-name() = {
  let meta = yaml("../data/meta.yml")
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

#let resolve() = templates.at(resolve-name())
