# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## 1.2.6

A `web` column and its own filter on the section list. Fixes a reported bug: the
`Show hidden` filter appeared to do nothing on the publications page.

### Fixed

- **`Show hidden` was inert on any section that does not use `highlighted:`.** It
  filters `highlighted: true` — the PDF gate — and in a corpus where no
  publication carries that field, the control could never reveal a row while a
  large and completely invisible `web: hide` split sat on the same page. A
  control that can never do anything is broken even when the code behind it is
  sound, so this is filed as a fix rather than a feature.

### Added

- **A `web` column on every section that carries the `web` field** (publications,
  presentations, teaching). Blank renders as a muted `auto`, because blank is a
  real third state — "the site exporter decides" — and an empty cell reads as
  `show`.
- **A `Show web: hide` filter**, defaulting to CHECKED so a list page opens on the
  full set exactly as it did before this existed. Unchecking narrows to what a
  website export would display. The column and the filter are declared together:
  a filter for a value you cannot see is the same defect one step removed.
- **`web_hidden` on every list row.** Only an explicit `hide` counts — blank means
  automatic, and what automatic resolves to belongs to the site exporter, which
  may default it off a sibling field. The engine deliberately does not guess;
  under-reporting a blank is honest, whereas inventing a default would put a
  number on the page that no exporter agrees with.
- The bulk-action hint now states that `highlighted:` and `web:` are two
  independent gates and that `web:` affects no PDF.

### Notes for hosts

No API change and no migration. A host that declares no `web` field on any section
sees no new column, no new control, and identical pages. `AUTHORSHIPS_VERSION` and
every export contract are untouched — this release does not touch export code.

## 1.2.5

A topic-tag field for publications, and a guard so a data-driven vocabulary can
never delete the field it configures. Additive for hosts: no API change, and the
new field is inert unless you declare a vocabulary.

### Added

- **`tags` on publications** — a checkbox set over a vocabulary the HOST declares
  in `data/meta.yml` under `tags:`, for grouping papers by subject. Inert in the
  Typst renderer; read by the editor's schema and available to a site exporter.
  Reuses the existing `audiences_set` field type rather than adding one: every
  site of that type is generic over `name`/`choices`, and everything semantic
  about audiences keys on the field NAME, so a differently-named field of the
  same type is inert for visibility. `JSON_FIELD_TYPES` is unchanged, and no
  template, JS, CSS or form-state edit was needed.
- **`build_variants.tag_choices(meta, load_data)`** — the vocabulary is
  `meta.tags` unioned with every tag the corpus already carries, declared order
  first then extras sorted. The union is what keeps a stored tag offered: the form
  posts only what is checked, so a value it cannot offer is a value the next save
  deletes. Note the union is therefore contaminated by the corpus BY DESIGN — a
  caller that needs the vocabulary as an authority must read `meta.tags` directly.

### Fixed

- **An empty `choices` no longer clears an `audiences_set` field.** `choices` for
  this type is data-driven, so it can legitimately resolve empty — a malformed
  `meta.yml`, a renamed key, an unreadable data dir. That state used to DELETE the
  field on the next ordinary save: the form rendered zero checkboxes, the JS wrote
  `[]` into the hidden input at mount, `validate_entry` skipped the empty optional
  value, and the apply handler popped the key. `js_mounted` was set, so the
  JS-mount sentinel never fired, and every test stayed green. For a corpus-wide
  vocabulary that repeats on every entry the user touches afterwards.

  The handler now leaves the stored value alone when `choices` is empty, which
  turns the failure from destructive into loud: the data survives and
  `_validate_audiences_set` reports each value as unknown, surfacing on
  `/validate`, `check_data --strict` and the build preflight. Provably inert for
  `audiences`/`hide-from`, whose vocabulary is a non-empty base set unioned with
  the corpus — asserted by a test rather than assumed.

## 1.2.4

Builds are killed properly and can actually time out. No API change, except that
`stream_subprocess` gains an optional `timeout_s`; hosts need no edits.

### Fixed

- **Killing a build now kills the whole process group, not just the shell.**
  `stream_subprocess` and `_run` spawned without `start_new_session`, so `proc.kill()`
  SIGKILLed `./build.sh` alone — and an untrappable signal cannot be forwarded to an
  already-forked `typst` (or, in a host that streams an export, to whatever that recipe
  spawned). Those children were orphaned and kept running, and kept WRITING, after the
  stream had reported "killed" and the `finally` had released the build lock; a user who
  retried then raced two writers over the same outputs with no lock held. The ordinary
  trigger is `GeneratorExit` — closing the tab or navigating away mid-build — not the
  timeout. Verified with a forking child and a positive control proving the grandchild
  really does survive when the group is left alone.
- **The build timeout could not fire on a silent child.** The deadline was evaluated
  only just after `readline` returned, and `readline` blocks forever on a child that
  says nothing, so a hung build was never capped at all. A reader thread feeding a queue
  lets the deadline run on a timer. It is checked BEFORE each read, so a continuously
  chatty child cannot starve it either — the symmetric failure, and the one that bites a
  full multi-variant build.
- **`_run` could hang outright on a timeout.** `subprocess.run(timeout=...)` kills only
  the direct child and then blocks in `communicate()` until every inherited pipe closes,
  so one orphaned grandchild holding stdout wedged the request thread. It now spawns
  through `Popen`, kills the group, and drains.
- **A killed stream is no longer reported as a success.** `ok` was `rc == 0` alone, and
  a killed child can still exit 0 in the race between the kill and the wait.

### Added

- `stream_subprocess(argv, cmd_str=None, timeout_s=None)` — `timeout_s` overrides
  `BUILD_TIMEOUT_S` for jobs that legitimately outlast a PDF build, such as a host that
  streams a full export-and-sync.
- `build_runner._kill_process_group(proc)`, which **refuses to signal its own process
  group**. A child spawned without `start_new_session` shares the caller's group, so an
  unguarded `killpg` would SIGTERM the editor itself; it degrades to a single-process
  kill instead. Not hypothetical — mutating the flag away to check the new tests were
  honest killed the entire pytest session, which is how the guard was found.

## 1.2.3

A reset now handles every file the corpus contains, and names the ones it
cannot. No API change; hosts that carry extra `data/*.yml` files should read
"Corpus files the engine has no schema for" in `docs/extending.md`.

### Fixed

- **`POST /reset` no longer leaves a host's corpus files behind while reporting
  a clean slate.** Both tree writers looped over `schemas.all_sections()`, so a
  data file the engine has no schema for was snapshotted and then left on disk
  carrying the *old* corpus's contents. Measured against a real corpus: three
  files survived a reset byte-for-byte, in both modes. The writers now iterate
  the example corpus's own `*.yml` listing — the example tree already
  single-sources every header, so it is the one place the engine can learn what
  a corpus contains. Every schema section must still have an example file, or
  `_corpus_yml_names` raises rather than silently skipping it.
- **A blank body now matches the example file's own root type.** All ten schema
  sections are sequences, so they are unchanged at `[]`. A mapping-rooted host
  file gets `{}`; `[]` there is not just odd-looking, since a loader that
  requires a mapping root raises on it — the "clean slate" would break the
  tooling it exists to reset.
- **The sidecar phase now deletes every `data/*.json` the sections phase did not
  write**, not just `publications_pubmed_sync.json` by name. Phase 1 snapshots
  with the same glob, so "was snapshotted first" holds by construction. The
  argument that had the pubmed sidecar deleted applies verbatim to any
  corpus-derived cache: leave one behind and a single click repopulates the
  fresh corpus with the old one's data.

### Added

- **A `phases.unmanaged` report** naming any `data/*.yml` no phase accounted
  for — a corpus file with no example counterpart, whose blank shape the engine
  cannot guess. Empty for both known corpora. It exists so the next such file
  surfaces on the reset page instead of surviving unmentioned, and the page
  renders it as a warning, not a footnote.
- **The reset page lists non-schema corpus files it rewrote.** They have no
  Backups URL, so the previous list — built from schema sections only — omitted
  them silently.

## 1.2.2

Bug fixes in the nav seam's path-overlap warning. No API change.

### Fixed

- **The overlap warning no longer names an engine path that does not exist.** Its
  basis was `reserved_keys()`, which also holds keys that ROUTES set explicitly
  (`trackers`, `qc_triage`, ...). Those are nav keys, not path segments — there is
  no engine rule at `/trackers`, and the engine never derives `trackers` from a
  path — so a host page there was told the engine's link would take its highlight,
  when in fact the host's own link was correctly current. The basis is now the exact
  set the engine's derivation walks. This is the same defect class 1.2.1 fixed, in
  the other half of the check.
- **A missing rule snapshot is reported instead of silently skipping the check.**
  An app not built by `create_app()`, or one whose `app.extensions` was cleared, got
  no overlap warnings and no explanation — a silent false negative that looks like a
  pass.
- **A foreign value on the seam's `app.extensions` key is reported.** Replacing it
  discards every registered entry and the snapshot, so the nav would quietly empty
  out with a green test suite.
- **The overlap check no longer latches on an empty first resolve**, and
  `register_nav` re-arms it. Previously, a first request arriving before the host's
  routes attached dropped every entry, latched the check, and it never ran again for
  the life of the app; and an entry contributed by a later route module was never
  checked at all.

### Changed

- `docs/extending.md` now states the three limits of the overlap warning (no exact
  matches, no engine leaves, quiet under a `SCRIPT_NAME` mount), so silence is not
  read as safety.
- `CONTRIBUTING.md` gains a release checklist. The "never move a published tag" rule
  previously existed only in a test docstring, and CI does not run on tag pushes;
  `tests/test_release_versions.py` now also checks the tag name when run on a tagged
  commit.

## 1.2.1

Bug fix in 1.2.0's nav seam. No API change.

### Fixed

- **The host/engine path-overlap warning no longer fires on a host's own
  sub-pages.** `cv_editor.nav` warns when a host nav entry's URL overlaps an
  engine path, because that makes the nav highlight the wrong link. The check
  walked the LIVE url map, so it could not tell a host's own routes from the
  engine's — and the most ordinary host shape, a landing page with children
  under it, logged a warning naming the host's own sub-page as "the engine
  page". `create_app()` now snapshots the engine's rules before any host
  attaches routes, and the check compares against that. Found by running a real
  host, not by reading the code.

## 1.2.0

Additive release adding a host-extension seam for nav entries. No breaking
changes; with nothing registered the rendered nav is byte-identical to 1.1.0.

### Added

- **Host-contributed nav entries** in the new `cv_editor.nav` module. A host app
  that wraps `create_app()` to add its own pages can now make them reachable from
  the editor's nav. The committed surface is exactly `NavEntry` (a frozen,
  keyword-only dataclass of `key` / `label` / `endpoint`), `register_nav(app,
  entries)`, and `reserved_keys()`; everything else in the module is `_`-prefixed
  and internal. Entries carry a Flask **endpoint name, not a URL**, so a host
  cannot inject a raw `href` — Werkzeug refuses any rule that does not begin with
  `/`, which makes a `javascript:`/`data:` target unrepresentable. Shape is
  validated eagerly (during the host's startup); endpoints resolve per request and
  a failure is dropped and logged once, never raised, because 25 templates extend
  `base.html`. `register_nav` appends, so a host split across route modules can
  have each contribute an entry. Contract in `docs/extending.md`.
- **`reserved_keys()`** documents the keys the engine owns — every CV section name
  plus every nav key the engine can set. It MAY GROW in a future minor release; a
  host is advised to assert its own keys against it in its own test suite.

### Fixed

- **Tools menu active state.** Five of the Tools panel's links (`PubMed sync`,
  `QC triage`, `Validate`, `Find & replace`, `Reset CV data`) never marked the
  "Tools" summary as active on their own pages. Their routes set
  `current_section` correctly; the Jinja-local `TOOLS` list that computes
  `tools_keys` was missing those five keys.

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
