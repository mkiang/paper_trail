# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

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
