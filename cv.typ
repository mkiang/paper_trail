// Entrypoint: resolve the active template and render the document.
// A template module exports `meta`, `setup`, and `render()` — see
// templates/registry.typ. Select a template with `--input template=<name>`
// or a top-level `template:` key in data/meta.yml.

#import "templates/registry.typ": resolve

#let tpl = resolve()

#show: tpl.setup.with(meta: tpl.meta)

#tpl.render()
