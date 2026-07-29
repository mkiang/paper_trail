# paper_trail

A multi-variant, YAML-driven [Typst](https://typst.app) engine for an academic
CV, plus a local web editor. All content lives in `data/*.yml`; the Typst files
are layout only. One source produces multiple PDF variants — a full CV, a
public-facing cut, an audience-tailored version — from a single edit.

**No fonts to install.** The `modern` template uses Libertinus Serif, bundled
inside the Typst binary, so a fresh clone renders with zero setup.

## Quick start

Render a single PDF with just Typst (no Python needed):

```sh
typst compile --ignore-system-fonts cv.typ out.pdf
```

Build every variant defined in `data/meta.yml` (needs Python + PyYAML):

```sh
./build.sh          # writes output/<variant>.pdf for each build_variant
```

Then edit `data/*.yml` and re-run. Each data file opens with a header docstring
describing its own schema — that is the reference; start there. To add a
publication, copy an existing entry block in `data/publications.yml`.

## The editor

paper_trail ships a local Flask editor for structured editing — CRUD for every
section, citation import (BibTeX / NLM-Vancouver / DOI / PMID), a live build
console, QC checks against PubMed/Crossref, PubMed sync, citation counts, and
Markdown/HTML export.

```sh
make install        # editable install + dev tooling into .venv
make doctor         # check your environment (typst, deps, data)
cv-editor           # launch the editor (opens a browser tab)
```

## Templates & flags

Select a template with `--input template=<name>` or a top-level `template:` key
in `data/meta.yml`. The registry (`templates/registry.typ`) lists what is
available; `modern` is the default.

| `--input` flag | effect |
|---|---|
| `audience=<tag>` | show only entries whose `audiences:` list is empty or contains `<tag>`; `full` (default) shows all |
| `show_dollars=false` | hide grant dollar amounts (default: shown) |
| `show_pending=true` | show Pending Support (default: hidden) |
| `show_highlighted=true` | show entries marked `highlighted: true` (default: hidden) |

Per-entry `hide-from: [<tag>]` always hides an entry from that audience.

## Example data is fictional

Everything under `data/` is placeholder content for a made-up person, **Jane
Q. Public**. The DOIs (`10.9999/...`), PMIDs, phone number, and ORCID iD
(ORCID's public "Josiah Carberry" sandbox identity) are fake and identify no
real person or work. Replace it with your own.

**Before publishing your compiled CV, review it for anything you don't want
public** — this tool has no built-in PII scanner. See `SECURITY.md` for the
`mk()` eval trust model (your data is evaluated as Typst markup).

## Licensing

- Code: MIT (see `LICENSE`).
- Body font: Libertinus Serif (SIL OFL 1.1), bundled with Typst — nothing to
  install or redistribute here.

## Requirements

- [Typst](https://github.com/typst/typst) **0.15.x** (pinned in CI).
- Python 3.11+ (editor + `./build.sh`).

## Extending it from your own app

You can wrap `create_app()`, attach your own Flask routes, and serve the result —
the editor is a normal Flask app. `cv_editor.nav` lets those pages appear in the
editor's nav without the engine knowing anything about them:

```python
from cv_editor.app import create_app
from cv_editor.nav import NavEntry, register_nav

app = create_app()
register_nav(app, [NavEntry(key="reports", label="Reports", endpoint="reports_index")])
```

Entries carry an endpoint *name*, not a URL, so a host can never inject a raw
`href`. Full contract — the committed surface, reserved keys, and what happens when
an entry is malformed — in `docs/extending.md`.

## Contributing

PRs welcome — this repository is the development home for the engine and the
editor. See `CONTRIBUTING.md`, plus `docs/developing.md` (architecture),
`docs/capabilities.md` (per-template feature gating), and `docs/extending.md`
(host-contributed nav entries).

## Built with Claude

This project was built with [Claude](https://www.anthropic.com/claude),
Anthropic's AI assistant.
