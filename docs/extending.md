# Extending the editor from a host app (`cv_editor.nav`)

A **host app** wraps `create_app()`, attaches its own routes, and serves the
result. That works today without any help from the engine — except for one thing:
the nav lives in `base.html`, which ships inside the installed package, so a host
had no way to make its own pages reachable from the UI. `cv_editor.nav` is that
seam. The engine learns "a host may contribute nav entries"; it never learns what
they are.

```python
from cv_editor.app import create_app
from cv_editor.nav import NavEntry, register_nav

app = create_app()
register_reports_routes(app)                    # your own pages
register_nav(app, [NavEntry(key="reports", label="Reports", endpoint="reports_index")])
```

With nothing registered the nav renders byte-for-byte as it did before the seam
existed — `tests/test_nav_seam.py` pins that against a golden captured from the
previous release.

## The committed surface

Exactly three names, and `cv_editor.nav.__all__` is the authority:

| Name | What it is |
|---|---|
| `NavEntry` | A frozen, **keyword-only** dataclass: `key`, `label`, `endpoint` |
| `register_nav(app, entries)` | Validates and appends entries to an app |
| `reserved_keys()` | The set of keys the engine owns (see below) |

Everything else in the module is `_`-prefixed and may change in any release,
including the resolved-entry type and the resolver itself. Do not import them.

**Construct with keywords.** `NavEntry` is `kw_only` so that field order is *not*
part of the contract — a future optional field can be added without breaking your
call site. `NavEntry("reports", "Reports", "reports_index")` raises.

## `endpoint` is an endpoint name, not a URL — and that is a security property

The engine calls `url_for(entry.endpoint)` itself and hands the template a
resolved path. That is deliberate. Werkzeug refuses to register any URL rule that
does not begin with `/` — `add_url_rule("javascript:alert(1)", ...)` raises — and
`url_for` with no `_external`/`_scheme` returns a server-relative path. So a
`javascript:` or `data:` target is **unrepresentable** through this seam, which is
why `base.html` interpolates the url into `href` with no filter.

Two consequences worth stating plainly:

- **Do not propose a field carrying a host-supplied URL.** That single change
  deletes the property above. If you need an off-site link, route it through your
  own redirect endpoint and register *that* endpoint.
- This is **not** the `safe_url` template filter. That filter returns `#` for
  anything without an `http(s)`/`mailto` scheme, so applying it here would blank
  every internal path the nav emits. It is not a filter this loop could ever have
  used.

**Labels are text, not markup.** They are autoescaped; the nav loop applies no
`|safe`. A label containing `<script>` renders inert.

## Marking your page as the active one

`key` doubles as the `current_section` value, which is what makes the nav entry
render as active. Pass it from every host route that renders a `base.html`
descendant:

```python
return render_template("reports.html", current_section="reports", ...)
```

An explicit `current_section=` kwarg always wins over the engine's inference, so
this is the reliable path and the one to prefer.

If you omit it, the engine falls back to matching `request.path` against your
entry's own resolved URL — exact match, or a `<url>/` prefix. That fallback is
**convenience, not contract**: it compares decoded paths, it considers your
longest registered URL first (so registering both `/reports` and
`/reports/monthly` resolves a sub-page to the sub-page), and it only runs when no
engine rule has already claimed the path. Which means it will not fire if your
page sits under a path the engine owns — see the next section.

## Reserved keys, and paths to avoid

`register_nav` refuses a `key` in `reserved_keys()`: every CV section name plus
every nav key the engine itself can set. A collision would light both your link
and the engine's on the engine's own page — the nav quietly lying about where you
are, with no error anywhere — so it is refused at registration, loudly, during
your startup.

**`reserved_keys()` may grow in any minor release.** A new CV section or a new
engine tool page widens it, and a host whose key becomes reserved will fail to
boot on upgrade. To turn that into a red test at pin-bump time instead of a
surprise at startup, assert your own keys in your own suite:

```python
def test_our_nav_keys_are_not_engine_reserved():
    assert not ({e.key for e in OUR_ENTRIES} & nav.reserved_keys())
```

**Paths** are not refused, because you own your own routes — but an overlap
produces the same lie, so the engine logs a warning once per app when it sees one.
Both directions matter. A host page under a prefix the engine *derives* a section
from (`/service/notes`) gets the engine's own link lit instead of yours; a host
entry that is a path prefix of engine pages (a `/qc` dashboard over the engine's
pages beneath `/qc`) lights *your* link on the engine's page. Pick a path root the
engine does not use.

Three limits on that warning, so you do not read silence as safety:

- It cannot fire on an **exact** path match, or on an engine page with no children
  beneath it. Most engine routes are such leaves.
- Under a `SCRIPT_NAME`/`APPLICATION_ROOT` mount it goes quiet, because resolved
  URLs carry the mount prefix and the engine's own rules do not.
- It needs the rule snapshot `create_app()` takes. If you build the app some other
  way, or clear `app.extensions`, the engine says so rather than checking nothing
  silently.

## What happens when something is wrong

The split is deliberate, because 25 templates extend `base.html` — a raise while
rendering the nav would 500 every page including `/`, which is the only surface
you could recover from.

| When | Where it surfaces |
|---|---|
| Malformed `NavEntry` field (non-`str`, empty, padded, blueprint-relative) | Raises at **construction**, in your startup |
| Not a `NavEntry`, a non-iterable batch, a duplicate or reserved key | Raises at **`register_nav`**, in your startup |
| Endpoint will not build (unregistered, or needs URL parameters) | Entry dropped, **logged once** per app |
| `url_for` returns a non-`str` (possible via your own `url_build_error_handlers`) | Entry dropped, **logged once** |
| Entry resolves to the application root | Entry dropped, **logged once** (a `/` url prefix-matches every path) |

Nothing in the render path raises. Nothing is dropped *silently* either — a
vanished nav entry with no explanation is the exact complaint this seam exists to
fix, so every drop writes one line. The log is `app.config["LOG_PATH"]`.

`register_nav` **appends**, so a host split across several route modules can have
each contribute its own entry. Registering a key that is already registered on
that app raises rather than overwriting.

Call order does not matter: fields and batches are validated when you call, and
endpoints are not resolved until a request, so registering before or after your
routes attach both work.
