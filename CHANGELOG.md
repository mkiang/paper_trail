# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## 1.1.0

Additive release supporting an external static-site export of the CV. No
breaking changes.

### Added

- **Website-export editor fields.** `web` (a `show`/`hide`/blank select
  controlling website visibility), `slides` (a presentation slide URL/path),
  and `paper_pdf` (a hosted paper PDF path). All three are read only by an
  external site exporter and are inert in the Typst renderer.
- **Bulk website visibility** on the publications list: "Show on website" /
  "Hide from website" bulk actions that set `web` on the selected entries.
- **Public export API** in `cv_editor.export_core`: stable `mk`, `plain`,
  `emphasis`, `strong`, `sup`, `link`, `self_bold_terms`, `visible`, and
  `entry_visible` names (aliasing the internal helpers) so an out-of-package
  exporter can reuse the engine's markup conversion and leak-guard visibility
  logic without importing private symbols.
- **"Commit pending edits" button** (`POST /commit`): stages the
  editor-managed workspace files plus a freshly-regenerated `publications.bib`
  and makes one local git commit. The add-set is a positive allowlist (never
  `git add -A`, never QC/URL report churn); the route refuses a detached HEAD,
  is inert outside a git worktree, and commits locally only (no push).

## 1.0.0

The repository is now the **development home** for the engine and a full local
editor, and accepts pull requests. This is a large, breaking change from the
generated-snapshot 0.1.0.

### Added

- **Local Flask editor** (`cv-editor` / `python -m cv_editor`): structured CRUD
  for every CV section, citation import (BibTeX / NLM-Vancouver / DOI / PMID),
  a live build console, QC against PubMed/Crossref, PubMed sync, citation
  counts, URL verification, and Markdown/HTML export.
- Python package `paper-trail`. Install from a checkout with `pip install -e .`
  and run the editor there — it operates on the working tree's `data/`. (A
  bare non-editable install works for library use; the editor expects a
  checkout or `CV_EDITOR_*` environment pointing at your data.)
- A bundled fictional example corpus reachable inside an installed wheel.
- CI: lint, a structural leak gate, the test suite, a render smoke, and a
  latest-Typst canary.
- `SECURITY.md` documenting the `mk()` / footer eval trust boundary.

### Changed (breaking)

- **`modern` template contract is now data-injection.** Templates export
  `setup` and `render(meta:, section-data:)`; the consumer (`cv.typ`) owns every
  `yaml()` load and injects the data. The template reads no files itself. A
  `cv.typ` written against 0.1.0's self-loading template will not work
  unchanged.
- The Typst engine moved under `src/` (entrypoint `src/lib.typ`), so the
  repository is installable as a Typst `@local` package.
- Requires Python 3.11+ (was 3.9+).

### Preserved

- The `0.1.0` git tag remains for anyone pinned to the generated snapshot.

## 0.1.0

- Initial public release: the `modern` Typst CV template + a fictional example
  corpus + minimal build tooling, generated one-way from a private source repo
  (no editor, no Python package).
