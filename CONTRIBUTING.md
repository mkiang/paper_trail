# Contributing

Thanks for your interest! This repository is the **development home** for the
paper_trail engine (the `modern` Typst template) and its local editor. Pull
requests are welcome.

## Setup

```sh
make install        # editable install + pytest/ruff into .venv
make doctor         # verify typst + deps
make test           # run the suite
make lint fmt-check  # ruff
```

You need [Typst](https://github.com/typst/typst) **0.15.x** on your PATH.

For the architecture, the module map, and the load-bearing conventions, read
[`docs/developing.md`](docs/developing.md). Template feature-gating is
documented in [`docs/capabilities.md`](docs/capabilities.md).

## Ground rules

- **Open an issue first** for anything non-trivial, so we can agree on the
  approach before you invest time.
- **Keep the example corpus fictional.** Everything under `data/` describes
  "Jane Q. Public" and must never contain a real person's identifiers. Tests
  enforce this (`tests/test_m5_sample_data.py`).
- **Read `SECURITY.md`.** CV data and footer strings are evaluated as Typst
  markup (`mk()`), which can read local files into the PDF. Don't add features
  that widen that boundary without flagging it.
- **The leak gate (`scripts/ci_leak_check.sh`) runs in CI and will fail a PR**
  that adds an identifier-shaped string (grant #, DOI, PMID, ORCID, phone,
  email) or a maintainer-specific token outside the allowlisted example data.
  If a line is a deliberate, safe exception (e.g. a privacy-guard assertion, or
  an intentional fictional value the scanner can't tell apart), append a
  `# leak-allow` comment on that line and say why in the PR. Use it sparingly —
  it's an escape hatch, not a silencer for real content.
- **Some tests skip by design.** Rendering-fidelity tests that need a template
  or fonts not shipped here skip automatically; that is expected, not a failure.

## Before you open a PR

```sh
make fmt fmt-check lint test
./build.sh
bash scripts/ci_leak_check.sh   # the structural gate CI also runs
```

CI runs lint, the leak gate, the test suite, a render smoke, and a
latest-Typst canary. A green run is required before merge.
