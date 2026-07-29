"""Host-contributed nav entries (the nav seam, 1.2.0).

A HOST APP — one that wraps ``create_app()`` to add its own pages — registers them
here so the engine's nav can link them without knowing what they are. The engine
learns "a host may contribute entries"; it never learns which.

    from cv_editor.nav import NavEntry, register_nav

    app = create_app()
    ...attach your own routes...
    register_nav(app, [NavEntry(key="reports", label="Reports", endpoint="reports_index")])

PUBLIC SURFACE — ``NavEntry``, ``register_nav``, ``reserved_keys``, and nothing
else. Everything prefixed with ``_`` is internal and may change in any release;
``tests/test_nav_seam.py`` pins the list, mirroring the convention in
``export_core.py``'s public-API block. Construct ``NavEntry`` with KEYWORDS: it is
``kw_only`` precisely so field order never becomes part of the contract.

TWO-STAGE VALIDATION, and the split is the whole design.

  * SHAPE is checked EAGERLY — per field in ``NavEntry.__post_init__`` (so the
    traceback lands on the host's own construction site) and per batch in
    ``register_nav``. Either way the failure surfaces during the host's startup.
  * The ENDPOINT is resolved LAZILY, per request, in ``_resolve``. 25 templates
    extend ``base.html``, so a raise out of the context processor 500s all of them
    INCLUDING ``/`` — the only recovery surface. Anything that can go wrong at
    render time is caught and logged once, never raised.

Splitting it this way also makes call ORDER irrelevant: register before or after
your routes attach, either works, because nothing resolves until a request.

Entries carry an ENDPOINT NAME, not a URL, and that is a security property.
Werkzeug refuses to register any rule that does not begin with ``/`` (a
``javascript:`` rule raises at ``add_url_rule``), and ``url_for`` with no
``_external``/``_scheme`` returns a server-relative path — so a host-supplied
scheme is UNREPRESENTABLE here, which is why ``base.html`` interpolates the url
into ``href`` with no filter. This is NOT the ``safe_url`` template filter: that
one returns ``#`` for anything without an ``http(s)``/``mailto`` scheme, so it
would blank every internal path the nav emits. Do not "restore" it here. Labels
are autoescaped by Jinja; the nav loop applies no ``|safe``.

Do not add a field carrying a host-supplied URL — that single change deletes the
property above. Route an external link through your own redirect endpoint.

WHY ``app.extensions`` AND NOT ``app.config``. ``app.config`` in this engine holds
scalars and filesystem paths — the ``_DEFAULT_PATHS`` knob registry and
``QUIT_TOKEN`` — and no key there holds an object. It is also a namespace shared
with Flask's own reserved keys. ``app.extensions`` is what Flask reserves for
extension state, so that is where a typed object belongs. (This module is the
engine's FIRST host-facing runtime surface. The ``register_*_routes`` functions
elsewhere look similar but are ``create_app()``'s internal decomposition, called
from nowhere else, so there was no host-facing precedent to copy.)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from urllib.parse import unquote

from flask import Flask, has_request_context, request
from werkzeug.routing import BuildError

__all__ = ["NavEntry", "register_nav", "reserved_keys"]

_EXT_KEY = "cv_editor_nav"

# THE SINGLE SOURCE for the nav keys the engine derives from `request.path`.
# `app.py`'s `inject_helpers` imports this rather than repeating the literal — a
# second copy silently goes stale, and a stale copy re-opens the very key
# collision `reserved_keys()` exists to close.
_PATH_DERIVED_NAV_KEYS: tuple[str, ...] = (
    "style",
    "freeze",
    "search",
    "urls",
    "citations",
    "pubmed_sync",
)

# Nav keys no path derives — routes pass these to `render_template` explicitly.
# `tests/test_nav_seam.py` greps every `current_section=` literal in the package
# and fails if one is missing from the union below.
_EXPLICIT_NAV_KEYS: tuple[str, ...] = (
    "trackers",
    "qc_triage",
    "validate",
    "replace",
    "reset",
)


def reserved_keys() -> frozenset[str]:
    """Keys a host may NOT use: every CV section name plus the engine's nav keys.

    PART OF THE PUBLIC CONTRACT, and it MAY GROW in any minor release — a new CV
    section or a new engine tool page widens it. A host whose key becomes reserved
    gets a ``ValueError`` from ``register_nav`` and does not boot. To turn that
    upgrade-time surprise into a red test at pin-bump time, assert your own keys
    against this function in your own suite.
    """
    # Imported lazily: `schemas` reads the YAML corpus at import time, and a host
    # doing `from cv_editor.nav import NavEntry` should not trigger a corpus read
    # before it has configured its data dir.
    from cv_editor import schemas

    return (
        frozenset(schemas.SCHEMAS)
        | frozenset(_PATH_DERIVED_NAV_KEYS)
        | frozenset(_EXPLICIT_NAV_KEYS)
    )


def _derivable_path_prefixes() -> frozenset[str]:
    """First path segments ``inject_helpers`` derives a ``current_section`` from.

    A STRICT SUBSET of ``reserved_keys()``: it excludes ``_EXPLICIT_NAV_KEYS``,
    which routes pass as a kwarg and which no path produces. Used only by the
    path-overlap warning, so that warning cannot name a nonexistent engine path.
    """
    from cv_editor import schemas

    return frozenset(schemas.SCHEMAS) | frozenset(_PATH_DERIVED_NAV_KEYS)


@dataclass(frozen=True, kw_only=True)
class NavEntry:
    """One host-contributed nav destination.

    ``key`` is what the host's own routes should pass as ``current_section`` so the
    entry renders as the active page (the URL-prefix fallback in ``app.py`` covers
    a host that does not). ``endpoint`` is a Flask endpoint name — see the module
    docstring on why it is not a URL.

    Fields are validated here rather than in ``register_nav`` so the traceback
    points at the construction site. Keyword-only: field order is not contractual.
    """

    key: str
    label: str
    endpoint: str

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"NavEntry.{f.name} must be a non-empty str, got {value!r}")
            if value != value.strip():
                # `key` is compared to `current_section` by exact string equality,
                # so " reports " would never match and the entry would be silently
                # dead — and it would also evade the reserved-key check.
                raise ValueError(
                    f"NavEntry.{f.name} must not have leading or trailing whitespace, got {value!r}"
                )
        if self.endpoint.startswith("."):
            # Flask resolves a leading dot against the CURRENT request's blueprint,
            # so a relative endpoint would build on some pages and BuildError on
            # others. A nav entry must resolve identically everywhere.
            raise ValueError(
                f"NavEntry.endpoint must be absolute, not blueprint-relative, got {self.endpoint!r}"
            )


@dataclass(frozen=True)
class _ResolvedNav:
    """A ``NavEntry`` resolved for this request. Internal."""

    key: str
    label: str
    # `url` is percent-encoded, as it goes into `href`. `match_path` is decoded and
    # slash-normalised, for comparison against `request.path` (which Werkzeug has
    # already decoded). They differ for any rule containing a space or a non-ASCII
    # character, and comparing the encoded form would silently never match.
    url: str
    match_path: str


@dataclass
class _NavState:
    """All seam state for one app, under one ``app.extensions`` key."""

    entries: tuple[NavEntry, ...] = ()
    warned: set[str] = field(default_factory=set)
    collisions_checked: bool = False
    # The ENGINE's own url rules, snapshotted by `_snapshot_engine_rules` at the end
    # of `create_app()` — i.e. before any host has attached its routes. Without
    # this, a host's own sub-pages are indistinguishable from engine pages, and the
    # overlap check below warns about the most ordinary shape there is: a landing
    # page with children under it.
    engine_rules: frozenset[str] = frozenset()


def _snapshot_engine_rules(app: Flask) -> None:
    """Record which url rules belong to the ENGINE. Internal; call at the end of
    ``create_app()``, before a host attaches anything."""
    _state(app).engine_rules = frozenset(r.rule for r in app.url_map.iter_rules())


def _state(app: Flask) -> _NavState:
    st = app.extensions.get(_EXT_KEY)
    if not isinstance(st, _NavState):
        if st is not None:
            # Something else is occupying our key — a host squatting it, a test
            # helper, a future engine storing something different. Replacing it
            # discards every registered entry AND the engine-rule snapshot, so the
            # nav would just empty out. Say so; a silent drop is the failure this
            # module exists to remove, not one to add.
            app.logger.warning(
                "nav: app.extensions[%r] held a %s, not the seam's own state — "
                "replacing it, and any registered entries are lost",
                _EXT_KEY,
                type(st).__name__,
            )
        st = _NavState()
        app.extensions[_EXT_KEY] = st
    return st


def register_nav(app: Flask, entries: Iterable[NavEntry]) -> None:
    """Validate ``entries`` and APPEND them to ``app``'s nav for lazy resolution.

    APPENDS rather than replaces, so a host decomposed into several route modules
    can have each contribute its own entry. Replacing would keep only the last
    call's batch, silently — and a silent drop is the failure this seam exists to
    remove, not one to add. A key already registered on this app is therefore a
    ``ValueError``, not an overwrite; registering the same batch twice is an error
    rather than a no-op, which is what a startup path should want.

    Raises ``TypeError`` / ``ValueError`` on a malformed batch — deliberately, and
    here rather than at render time, so the failure lands in the host's startup
    instead of 500ing every page that extends ``base.html``.
    """
    try:
        items = list(entries)
    except TypeError as exc:
        raise TypeError(
            f"register_nav: entries must be an iterable of NavEntry, got {type(entries).__name__}"
        ) from exc
    st = _state(app)
    reserved = reserved_keys()
    seen = {e.key for e in st.entries}
    for i, entry in enumerate(items):
        if not isinstance(entry, NavEntry):
            raise TypeError(f"register_nav: entry {i} is {type(entry).__name__}, expected NavEntry")
        if entry.key in seen:
            raise ValueError(
                f"register_nav: entry {i}: key {entry.key!r} is already registered on this app"
            )
        if entry.key in reserved:
            raise ValueError(
                f"register_nav: entry {i}: key {entry.key!r} is reserved by the engine "
                "— it would light both its link and the engine's on the engine's own page"
            )
        seen.add(entry.key)
    # Extend only after the whole batch validates, so a bad batch leaves the
    # previously registered entries untouched.
    st.entries = st.entries + tuple(items)
    # Re-arm the path-overlap check. It runs once per app, and a host decomposed
    # into route modules may register after the first request has already been
    # served; without this, a late entry is never checked.
    st.collisions_checked = False


def _registered(app: Flask) -> tuple[NavEntry, ...]:
    """The validated entries registered on ``app``. Internal."""
    return _state(app).entries


def _resolve(app: Flask) -> list[_ResolvedNav]:
    """Registered entries with their URLs, LONGEST MATCH PATH FIRST. Internal.

    Longest-first matters for the ``current_section`` fallback that consumes this:
    it takes the first prefix match, so a host registering both ``/reports`` and
    ``/reports/monthly`` would otherwise see every sub-page resolve to the parent.

    An entry is DROPPED and logged once when its endpoint cannot build
    (unregistered or parameterised), when ``url_for`` returns a non-``str`` (a
    host-installed ``url_build_error_handler`` may return anything and Flask does
    not type-check it), or when it resolves to the application root (which
    prefix-matches every path). Never raised, and never silently: a silent drop
    reproduces the very complaint this seam exists to fix.

    Returns ``[]`` outside a request context — ``url_for`` needs one, and the
    sibling ``current_section`` derivation already anticipates a request-less render.
    """
    if not has_request_context():
        return []
    st = _state(app)
    out: list[_ResolvedNav] = []
    for entry in st.entries:
        try:
            url = app.url_for(entry.endpoint)
        except BuildError:
            _warn_once(
                app, f"nav: endpoint {entry.endpoint!r} will not build; dropping {entry.key!r}"
            )
            continue
        if not isinstance(url, str):
            _warn_once(
                app,
                f"nav: url_for({entry.endpoint!r}) returned {type(url).__name__}, not str; "
                f"dropping {entry.key!r}",
            )
            continue
        if url.rstrip("/") == request.script_root.rstrip("/"):
            _warn_once(app, f"nav: refusing application-root url {url!r} for entry {entry.key!r}")
            continue
        out.append(_ResolvedNav(entry.key, entry.label, url, unquote(url).rstrip("/") or "/"))
    out.sort(key=lambda r: len(r.match_path), reverse=True)
    # Latch only once something actually resolved. If the first request lands
    # before the host's routes attach, every entry is dropped, `out` is empty, and
    # latching here would mean the collision check never runs for the life of the
    # app — silently. `register_nav` also clears the latch when it appends, so an
    # entry contributed by a later route module still gets checked.
    if out and not st.collisions_checked:
        st.collisions_checked = True
        _warn_on_url_collisions(app, out)
    return out


def _warn_on_url_collisions(app: Flask, resolved: list[_ResolvedNav]) -> None:
    """Warn when a host url overlaps an engine path, in either direction. Internal.

    A key collision is refused at registration because it makes the nav lie about
    where you are. A PATH overlap produces the identical lie and cannot be refused
    — the host owns its own routes — so it is reported instead. Computed once per
    app, on the first resolve, because it walks the url map.

    Direction 1 — a host page under a prefix the engine DERIVES a section from
    (``/service/notes``): ``inject_helpers`` runs its derivation first and lights
    the engine's own link on the host's page. The basis is deliberately the exact
    set that derivation walks — the CV section names plus
    ``_PATH_DERIVED_NAV_KEYS`` — and NOT ``reserved_keys()``, which also contains
    keys routes set explicitly (``trackers``, ``qc_triage``, …). Those are nav keys,
    not path segments: there is no engine rule at ``/trackers``, and the derivation
    never produces ``trackers`` from a path, so warning about a host page there
    named an engine path that does not exist and predicted a theft that does not
    happen. Reported at N6's checkpoint, having survived the 1.2.1 fix.

    Direction 2 — a host url that is a path prefix of engine pages (a ``/qc``
    dashboard over the engine's pages beneath ``/qc``): the host's link lights on
    the engine's page. Which page gets named is whichever sorts first; do not rely
    on a particular one.

    Neither half can fire on an EXACT path match or on an engine leaf with no
    children, so silence here is not evidence of no overlap.
    """
    # ONLY the engine's own rules, snapshotted before the host attached anything.
    # Using the live url map here would treat the host's own sub-pages as engine
    # pages and warn about a landing page with children under it — the most
    # ordinary host shape there is.
    engine_rules = _state(app).engine_rules
    if not engine_rules:
        # Empty means `create_app()` never ran `_snapshot_engine_rules` on this app
        # (built some other way, or `app.extensions` was cleared). Direction 2 would
        # then be a no-op that LOOKS like it passed — a silent false negative, worse
        # than the false positive 1.2.1 removed. Say so instead.
        _warn_once(
            app,
            "nav: no engine url-rule snapshot on this app, so host/engine path "
            "overlaps cannot be checked — was it built by create_app()?",
        )
        return
    engine_paths = sorted(rule for rule in engine_rules if "<" not in rule and rule != "/")
    derived_prefixes = sorted(_derivable_path_prefixes())
    for r in resolved:
        for prefix in derived_prefixes:
            if r.match_path == f"/{prefix}" or r.match_path.startswith(f"/{prefix}/"):
                _warn_once(
                    app,
                    f"nav: entry {r.key!r} at {r.url!r} sits under /{prefix}, which the "
                    "engine derives its own current_section from — the engine's link "
                    "will light on that page instead of yours",
                )
        for engine_path in engine_paths:
            if engine_path.startswith(r.match_path + "/"):
                _warn_once(
                    app,
                    f"nav: entry {r.key!r} at {r.url!r} is a path prefix of the engine page "
                    f"{engine_path} — it will light on that page too",
                )
                break


def _warn_once(app: Flask, message: str) -> None:
    """Log ``message`` once per app instance.

    A permanently-broken entry would otherwise write one line per request forever,
    and the log is a real file on disk.
    """
    warned = _state(app).warned
    if message in warned:
        return
    warned.add(message)
    app.logger.warning(message)
