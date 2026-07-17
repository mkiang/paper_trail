# Template capabilities (`capabilities.toml`)

Some editor features only make sense for a template that ships the assets
they need. The **freeze/flatten** workspace tool, the **typography-knob**
editor, and the **Altmetric** trackers page are all examples: they depend on
layout internals that the shipped `modern` template doesn't provide. Rather
than hard-code "these features are off," the editor reads a small per-template
descriptor and gates the features from there.

## Where it lives

Each template directory may carry a `capabilities.toml`:

```
templates/
  modern/
    capabilities.toml
```

The editor discovers the descriptor for the **active** template — the one
`templates/registry.typ` resolves (default `modern`, overridable with
`--input template=<name>` or a top-level `template:` key in `data/meta.yml`).
Typst never reads this file; it is editor-only metadata.

## Schema

```toml
[capabilities]
freeze = false      # freeze -> flatten workspace tool
typography = false  # advanced typography-knob editor
altmetric = false   # Altmetric trackers + Explorer deep-link
```

Every key is a boolean. The shipped `modern` template declares all three
`false`, so those routes never register and their nav links never appear.

## Fail-safe defaults

A missing file, a missing key, or a non-boolean value all default to
**`false`**. A template that doesn't explicitly claim a capability doesn't get
it — so adding a new template can only *reduce* the feature surface until you
opt in, never silently widen it.

Note the string-vs-bool trap: `freeze = "false"` is a non-boolean (a truthy
string), so it is treated as **absent** and resolves to `false` — not as an
enabled capability. Use a real TOML boolean.

## Adding a capability

The loader (`scripts/cv_editor/capabilities.py`) reads only the keys named on
the `Capabilities` dataclass. To add a new gated feature:

1. Add a field to `Capabilities` (defaulting `False`).
2. Register the feature's routes conditionally on
   `capabilities.current().<field>`.
3. Gate the nav link the same way.
4. Add the key to any template's `capabilities.toml` that should enable it.

Because discovery is keyed to the active template and recomputed whenever the
editor is reconfigured, a per-request template override resolves the right
descriptor without a restart.

## Why gate instead of delete?

The gated features are genuinely useful to a downstream template that provides
the necessary layout hooks — for example, a private template a maintainer keeps
in their own consumer repo. Shipping the code (dormant) rather than stripping it
keeps one codebase for everyone: the public `modern` template runs lean, and a
capable template turns the extra tools on with a one-line descriptor. Tests for
the gated features skip automatically when no capable template is present, so a
clean `modern`-only checkout stays green.
