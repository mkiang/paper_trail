# Security

## Reporting

Please report suspected vulnerabilities by opening a GitHub security advisory
(Security → Report a vulnerability) rather than a public issue. We aim to
acknowledge reports within a few days.

## Trust model — the `mk()` / footer eval boundary

paper_trail renders your CV **data** (`data/*.yml`) into a PDF via Typst. To
support inline markup (`*bold*`, `_italic_`, `---` em-dashes, `\$` amounts),
the `modern` template passes selected string fields through Typst's
`eval(..., mode: "markup")`:

- `src/modern/render.typ` — the `mk()` helper evaluates data-derived strings as
  Typst markup.
- `src/modern/styles.typ` — the page footer template is evaluated the same way.

**Implication:** evaluated Typst markup can call `read()` and `image()`, which
read files from the local filesystem into the compiled PDF. A CV data file (or
footer string) is therefore **trusted input with the same privileges as the
person running the build**. Treat `data/*.yml` and any `--input` you pass as
code you are willing to run.

**Do not** compile a `data/` tree, a footer, or `--input` values you received
from an untrusted third party without reviewing them first — the same caution
you would apply to running a downloaded script.

### For maintainers / CI

The CI render jobs are hardened accordingly (see `.github/workflows/ci.yml`):

- render runs on `pull_request` (never `pull_request_target`), so a fork PR
  cannot read repository secrets;
- `permissions: contents: read` — no write token in the render environment;
- no secrets are exposed to the render step;
- the render step assumes no network egress is required (a compile that tries
  to reach the network is treated as suspicious).

## No built-in PII scanner

paper_trail does not scan your compiled CV for personal information you may not
want public. Review your output before publishing it.
