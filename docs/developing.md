# Developing paper_trail

Orientation for contributors working on the engine or the editor. Read
`CONTRIBUTING.md` first for setup and ground rules; this document covers the
architecture and the conventions that aren't obvious from the code.

## Two layers

paper_trail is two cooperating layers with a clean seam between them:

1. **The Typst engine.** All CV content lives in `data/*.yml`; the Typst files
   under `src/` and `templates/` are layout only. The consumer `cv.typ` loads
   every YAML file, validates the section order, and injects the data into the
   selected template. Templates render purely from injected data — they read no
   files themselves, which is what makes the engine packageable.
2. **The editor.** A local Flask app (`scripts/cv_editor/`) that does structured
   CRUD on the YAML, plus citation import, QC, sync, and export. The editor
   *produces* the YAML the engine consumes; it never renders the PDF itself
   (it shells out to `typst`).

Keep the seam clean: the editor should never encode layout decisions, and the
templates should never assume the editor exists.

## The publish seam

`templates/registry.typ` is the only file that statically imports template
modules. It exports the default template name, the template table, the
canonical section vocabulary, and the `resolve(meta)` / `resolve-name(meta)`
helpers. Adding or removing a template is a one-file change here. Typst imports
are static, so a registry entry pointing at a missing directory is a compile
error — which is exactly why the seam is a single file.

Template selection precedence: `--input template=<name>` overrides a top-level
`template:` in `data/meta.yml`, which overrides the registry default.

## Render flags

Flags live in `src/lib/flags.typ` and ride a variant's `inputs:` block. The
audience flag (`audience=<tag>`) filters entries by their `audiences:` list;
per-entry `hide-from: [<tag>]` always hides. Boolean flags (grant dollars,
pending support, highlighted entries, and so on) default off unless a variant
turns them on. When you add a flag to `flags.typ`, mirror its default in the
editor's build-variants helper so the Style editor's checkbox matches the
renderer.

## The editor package

`scripts/cv_editor/` is a Flask app decomposed into feature route modules. The
app shell in `app.py` builds shared helper closures and calls a series of
`register_<feature>_routes(app, deps)` functions; each feature module ships a
`<Feature>Deps` dataclass that carries exactly the dependencies its routes
need. Shared state (locks, caches, kickers) is passed by reference, so the same
object is visible to every consumer. When you add a feature route, add it to the
matching `*_routes.py` module — don't define feature routes back in `app.py`.

Highlights of the module map:

| Concern | Module |
|---|---|
| Round-trip YAML writes | `yaml_io.py` |
| Per-section field schemas | `schemas.py` |
| Structure-aware navigation | `sections.py` |
| Form field <-> YAML dispatch | `field_handlers.py` |
| Per-entry / save-path validation | `validate.py` |
| Whole-corpus load-time validation | `data_check.py` |
| Citation import + enrichment | `citation_parse.py`, `bibtex_parse.py`, `enrichment.py`, `preprint.py`, `orcid_client.py` |
| Markdown / HTML export | `export_core.py`, `export_emit.py`, `markup_convert.py` |
| Blank / example scaffolding + reset | `scaffold.py`, `example_build.py` |
| Template capability gating | `capabilities.py` |

## Load-bearing conventions

- **Atomic writes.** Every write to `data/*.yml` funnels through
  `yaml_io.write_with_backup`: acquire a file lock, re-read the comment header
  inside the lock, dump the body, fsync the temp file, parse-verify, then
  `os.replace` and fsync the parent directory. Don't bypass it.
- **Stale-form 409.** Edit forms post the file's `mtime_ns` at render time; the
  write path re-checks it. Two tabs editing the same file means the second save
  gets a 409 and a reload prompt.
- **Structure-aware navigation.** Sections come in a few shapes (flat lists,
  lists of subsections, clusters). Use `sections.flatten` / `sections.locate`
  instead of open-coding `data[i]["entries"][j]`.
- **Section-generic routes.** CRUD flows through `/<section>/...` handlers.
  Section-specific flows (citation import, preprint promotion, style variants)
  get their own route names.
- **SSE for long jobs.** Streamed builds and sweeps go through one shared
  subprocess-streaming primitive and one client-side console. Don't hand-roll a
  new event-stream wrapper.
- **Self-bolding.** The renderer auto-bolds every occurrence of the configured
  author name (`meta.self_bold`). Store the name plain in YAML — never with
  markup.

## Security boundary

CV data and footer strings are evaluated as Typst markup, which can read local
files into the rendered PDF. Treat YAML body strings as **code, not data**:
sanitize anything pasted from a third-party source before saving. See
`SECURITY.md` for the full model. This is why CI renders untrusted PR code with
a read-only token and no secrets — never switch the render job to a workflow
trigger that exposes them.

## Testing

```sh
python -m pytest -q -p no:randomly
```

Run the suite **sequentially** — tests share the on-disk `data/*.yml`, so two
concurrent runs corrupt each other. Rendering-fidelity tests that need a
template or fonts not shipped in this repo **skip by design**; that is expected,
not a failure. Pure-Python drift guards (schema/renderer parity, section-title
maps, and so on) always run.

## The leak gate

`scripts/ci_leak_check.sh` runs in CI and enforces structural invariants — no
font files, no private template directory, no local filesystem paths, no
maintainer name or institution tokens outside `LICENSE`, and no personal profile
URLs. It is a *structural* backstop, not a semantic one: it can't recognize an
unseen real identifier, so keep the example corpus fictional (see
`CONTRIBUTING.md`) and never commit real personal data.
Run it locally before opening a PR:

```sh
bash scripts/ci_leak_check.sh
```
