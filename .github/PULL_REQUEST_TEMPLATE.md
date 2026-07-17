<!-- Thanks for contributing! For anything non-trivial, please open an issue
first so we can agree on the approach. See CONTRIBUTING.md. -->

## What this changes

A short description of the change and the motivation. Link the issue it
addresses (`Closes #123`).

## Which layer

- [ ] Typst engine / templates
- [ ] The web editor
- [ ] Tooling / build / CI
- [ ] Docs

## Checklist

- [ ] `make fmt-check` and `make lint` pass
- [ ] `make test` passes (run sequentially; some fidelity tests skip by design)
- [ ] `./build.sh` still renders the example corpus
- [ ] `bash scripts/ci_leak_check.sh` passes
- [ ] The example corpus stays fictional — no real personal data added
- [ ] If I changed a render flag or template, the editor's build-variants
      helper and any affected tests are updated in lockstep
- [ ] If I touched the `mk()` / eval boundary, I flagged it here (see
      `SECURITY.md`)

## Notes for reviewers

Anything that needs context — trade-offs, follow-ups, things you're unsure
about.
