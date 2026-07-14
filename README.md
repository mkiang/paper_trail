# paper_trail

A YAML-driven [Typst](https://typst.app) engine for typesetting an academic
CV. All content lives in `data/*.yml`; the Typst files are layout only. One
source produces multiple PDF variants (e.g. a full CV and a public-facing cut)
from a single edit.

**No fonts to install.** The default `modern` template uses Libertinus Serif,
which ships inside the Typst binary — a fresh clone renders with zero setup.

## Quick start

Render a single PDF with just Typst (no Python needed):

```sh
typst compile cv.typ out.pdf
```

Or build every variant defined in `data/meta.yml` (needs Python + PyYAML):

```sh
./build.sh          # writes output/<variant>.pdf for each build_variant
```

Then edit `data/*.yml` and re-run. Each data file opens with a header
docstring describing its own schema — that is the reference; start there.
To add a publication, copy an existing entry block in
`data/publications.yml` and change the fields.

## Templates

Selected with `--input template=<name>` or a top-level `template:` key in
`data/meta.yml`. The registry (`templates/registry.typ`) lists the available
templates; `modern` is the default.

### Flags honored by `modern`

| `--input` flag | effect |
|---|---|
| `audience=<tag>` | show only entries whose `audiences:` list is empty or contains `<tag>`; `full` (default) shows all |
| `show_dollars=false` | hide grant dollar amounts (default: shown) |
| `show_pending=true` | show Pending Support (default: hidden) |
| `show_highlighted=true` | show entries marked `highlighted: true` (default: hidden) |

Per-entry `hide-from: [<tag>]` always hides an entry from that audience.
`modern` is a deliberately simple template — see the "V1 SIMPLIFICATIONS"
comment block atop `templates/modern/render.typ` for features it does not (yet)
render.

## Example data is fictional

Everything under `data/` is placeholder content for a made-up person, **Jane
Q. Public**. The DOIs (`10.9999/...`), PMIDs, phone number (`555-867-5309`),
and the ORCID iD (`0000-0002-1825-0097`, ORCID's public "Josiah Carberry"
sandbox identity) are all fake and identify no real person or work. Replace it
with your own.

**Before publishing your compiled CV, review it for anything you don't want
public** — this tool has no built-in PII scanner.

## Fonts & licensing

- Code: MIT (see `LICENSE`).
- Body font: Libertinus Serif (SIL OFL 1.1), bundled with Typst — nothing to
  install or redistribute here.
- Contact icons: `modern` renders contacts as plain text; it does not draw
  Font Awesome icons.

## Requirements

- [Typst](https://github.com/typst/typst) **0.15.x** (pinned in CI).
- For `./build.sh`: Python 3.9+ with PyYAML.

## Built with Claude

This project was built with [Claude](https://www.anthropic.com/claude),
Anthropic's AI assistant.
