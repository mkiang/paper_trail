"""Host-contributed nav entries (the nav seam, 1.2.0).

A HOST APP — one that wraps ``create_app()`` to add its own private pages —
registers them here so the engine's nav can link them without knowing what they
are. The engine learns "a host may contribute entries"; it never learns which.

    from cv_editor.nav import NavEntry, register_nav

    app = create_app()
    ...attach your own routes...
    register_nav(app, [NavEntry(key="ext", label="Curation", endpoint="ext_index")])

TWO-STAGE VALIDATION, and the split is the whole design.

  * SHAPE is checked EAGERLY, in ``register_nav``, so a malformed entry raises
    during the host's own startup where the traceback points at the host's code.
  * The ENDPOINT is resolved LAZILY, per request, in ``resolve``. 25 templates
    extend ``base.html``, so a raise out of the context processor 500s all of
    them INCLUDING ``/`` — the only recovery surface. Anything that can go wrong
    at render time is caught and logged once, never raised.

Splitting it this way also makes call ORDER irrelevant: register before or after
your routes attach, either works, because nothing is resolved until a request.

Entries carry an ENDPOINT NAME, not a URL. That is deliberate, and it is a
security property: a host cannot hand the template a raw ``href``, so a
``javascript:`` or ``data:`` URI cannot reach the nav and bypass the ``safe_url``
filter convention. Labels are autoescaped by Jinja — the nav loop applies no
``|safe``.

WHY NOT ``app.config``. Every ``app.config`` override in this engine is
underscore-prefixed or documented test-only (``_VERIFY_HEAD_PROBE``,
``EXPORT_DATA_DIR``, …); ``app.config`` is also a namespace shared with Flask's
own reserved keys. A host-facing surface follows the engine's other host-facing
convention instead — a typed object plus a ``register_*`` function, as in
``register_<feature>_routes(app, <Feature>Deps(...))`` — and keeps its state in
``app.extensions``, which is what Flask reserves for exactly this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import NamedTuple

from flask import Flask, has_request_context, url_for
from werkzeug.routing import BuildError

from cv_editor import schemas

_EXT_KEY = "cv_editor_nav"
_WARNED_KEY = "cv_editor_nav_warned"

# Nav keys the ENGINE itself can put in `current_section` — either derived from
# `request.path` in `inject_helpers` or passed explicitly by a route. A host
# reusing one would light BOTH its own link and the engine's on the engine's own
# page: the nav quietly lying about where you are, with no error anywhere. So a
# collision is refused at registration instead.
_ENGINE_NAV_KEYS = frozenset(
    {
        "style",
        "freeze",
        "search",
        "urls",
        "citations",
        "pubmed_sync",
        "trackers",
        "qc_triage",
        "validate",
        "replace",
        "reset",
    }
)


def reserved_keys() -> frozenset[str]:
    """Keys a host may NOT use: every CV section name plus the engine's nav keys."""
    return frozenset(schemas.SCHEMAS) | _ENGINE_NAV_KEYS


@dataclass(frozen=True)
class NavEntry:
    """One host-contributed nav destination.

    ``key`` is what the host's own routes should pass as ``current_section`` (or
    what the URL-prefix fallback in ``inject_helpers`` will derive) so the entry
    renders as the active page. ``endpoint`` is a Flask endpoint name — see the
    module docstring on why it is not a URL.
    """

    key: str
    label: str
    endpoint: str


class ResolvedNav(NamedTuple):
    """A ``NavEntry`` with its endpoint resolved for this request."""

    key: str
    label: str
    url: str


def register_nav(app: Flask, entries: Sequence[NavEntry]) -> None:
    """Validate ``entries`` and store them on ``app`` for lazy resolution.

    Raises ``TypeError`` / ``ValueError`` on a malformed batch — deliberately, and
    here rather than at render time, so the failure lands in the host's startup
    instead of 500ing every page that extends ``base.html``.

    Calling this twice REPLACES the previous batch; there is one nav list per app.
    """
    try:
        items = list(entries)
    except TypeError as exc:
        raise TypeError(
            f"register_nav: entries must be an iterable of NavEntry, got {type(entries).__name__}"
        ) from exc
    reserved = reserved_keys()
    seen: set[str] = set()
    for i, entry in enumerate(items):
        if not isinstance(entry, NavEntry):
            raise TypeError(f"register_nav: entry {i} is {type(entry).__name__}, expected NavEntry")
        for field in fields(NavEntry):
            value = getattr(entry, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"register_nav: entry {i} ({entry.key!r}): "
                    f"{field.name} must be a non-empty str, got {value!r}"
                )
        if entry.key in seen:
            raise ValueError(f"register_nav: entry {i}: duplicate key {entry.key!r} in this batch")
        if entry.key in reserved:
            raise ValueError(
                f"register_nav: entry {i}: key {entry.key!r} is reserved by the engine "
                "— it would light both its link and the engine's on the engine's own page"
            )
        seen.add(entry.key)
    app.extensions[_EXT_KEY] = tuple(items)


def registered(app: Flask) -> tuple[NavEntry, ...]:
    """The validated entries registered on ``app`` (empty when none are)."""
    return app.extensions.get(_EXT_KEY) or ()


def resolve(app: Flask) -> list[ResolvedNav]:
    """Registered entries with their URLs, LONGEST URL FIRST.

    Longest-first matters for the ``current_section`` fallback that consumes this:
    it takes the first prefix match, so a host registering both ``/ext`` and
    ``/ext/orcid`` would otherwise see every sub-page resolve to ``/ext``.

    An entry whose endpoint cannot build (unregistered, or parameterised) is
    DROPPED and logged once — never raised, and never silently. A silent drop
    reproduces the very complaint this seam exists to fix ("I don't see this,
    where is it?"), so the operator gets a line in the log.

    Returns ``[]`` outside a request context: ``url_for`` needs one, and the
    sibling ``current_section`` derivation already anticipates a request-less
    render.
    """
    if not has_request_context():
        return []
    out: list[ResolvedNav] = []
    for entry in registered(app):
        try:
            url = url_for(entry.endpoint)
        except BuildError:
            _warn_once(
                app,
                f"nav: endpoint {entry.endpoint!r} will not build; dropping {entry.key!r}",
            )
            continue
        if len(url) < 2:
            # A url of "/" prefix-matches every path, so it would mark its key
            # current on every page in the app.
            _warn_once(app, f"nav: refusing root url {url!r} for entry {entry.key!r}")
            continue
        out.append(ResolvedNav(entry.key, entry.label, url))
    out.sort(key=lambda r: len(r.url), reverse=True)
    return out


def _warn_once(app: Flask, message: str) -> None:
    """Log ``message`` once per app instance.

    A permanently-broken entry would otherwise write one line per request
    forever, and the log is a real file on disk.
    """
    warned = app.extensions.setdefault(_WARNED_KEY, set())
    if message in warned:
        return
    warned.add(message)
    app.logger.warning(message)
