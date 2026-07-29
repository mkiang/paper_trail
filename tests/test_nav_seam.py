"""1.2.0 nav seam — host-contributed nav entries (`cv_editor/nav.py`).

The load-bearing property is that a HOST BUG CANNOT TAKE DOWN THE EDITOR. 25
templates extend `base.html`, so a raise out of the context processor 500s all of
them including `/`, which is the only recovery surface. Hence the two-stage
design under test here: shape raises EAGERLY in `register_nav` (host startup), and
render-time failure is caught, logged once, and dropped.

The golden `tests/fixtures/nav_no_extras.html` was captured from `main` at
`1cfca34` (v1.1.0) BEFORE any seam edit, so the byte-identity test below asserts
that the seam changed nothing for a host that registers nothing — not merely that
the current output equals itself. Regenerate only for a DELIBERATE nav change:
    python tests/test_nav_seam.py --regenerate-golden
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cv_editor import capabilities, nav
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "nav_no_extras.html"


def _app(entries=None):
    app = create_app()
    app.config["TESTING"] = True
    if entries is not None:
        nav.register_nav(app, entries)
    return app


def _nav_block(body: str) -> str:
    i, j = body.find("<nav>"), body.find("</nav>")
    assert i >= 0 and j > i, "no <nav> block in the rendered page"
    return body[i : j + 6]


def _tools_details(body: str) -> str:
    """The Tools `<details>` opening tag, where `is-current` lands on the summary."""
    i = body.find('aria-label="Editor tools"')
    assert i > 0, "Tools menu not rendered"
    return body[body.rfind("<details", 0, i) : i]


# A shape fixture for the registration tests, where the endpoint is never
# resolved. Note it points at `index` (url "/"), which `resolve` REFUSES at render
# time — see test_an_entry_resolving_to_the_root_url_is_refused. Render tests use
# `_host_app` and its /hostpage endpoint instead.
CURATION = nav.NavEntry(key="curation", label="Curation", endpoint="index")


# ---- the byte-identity guarantee: an unused seam costs zero bytes ----------


@pytest.mark.skipif(
    capabilities.current().freeze,
    reason="golden captured under the public `modern` template (all capabilities False)",
)
def test_nav_with_nothing_registered_matches_the_golden():
    """A host that registers nothing must get byte-identical nav markup.

    This is an EXACT compare on purpose. Jinja's `trim_blocks`/`lstrip_blocks` are
    both False, so a `{% for %}` on its own lines emits its surrounding newline and
    indentation EVEN WHEN THE LOOP NEVER RUNS — the loop and the comment above
    `TOOLS` both needed `{%-`/`{#-` markers to hold this, and a substring
    assertion would not have noticed. (Measured: 7 bytes of drift from the comment
    alone.)
    """
    body = _app().test_client().get("/").get_data(as_text=True)
    assert _nav_block(body) == GOLDEN.read_text(encoding="utf-8"), (
        "nav markup drifted with no host entries registered. If intentional:\n"
        "  python tests/test_nav_seam.py --regenerate-golden"
    )


def _host_app(entries, path="/hostpage"):
    """An app with a HOST-REGISTERED page, which is the real use case.

    No engine page can exercise the `current_section` URL-prefix fallback: every
    unclaimed HTML path is already claimed by a section prefix, and the pages that
    could be are set explicitly by their routes. So the fallback is only reachable
    through a host route — exactly what it exists for.
    """
    app = create_app()
    app.config["TESTING"] = True

    @app.route(path)
    def hostpage():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}host page{% endblock %}"
        )

    nav.register_nav(app, entries)
    return app


# ---- shape: every hostile input raises AT REGISTRATION --------------------
#
# One parametrised case per shape measured to 500 every page under the first
# draft's resolver, which unpacked `(key, label, endpoint)` in the `for` header,
# outside its own try. Each must now raise here, in the host's startup, where the
# traceback names the host's code.

_HOSTILE = [
    ("two_tuple", ("curation", "Curation"), TypeError),
    ("four_tuple", ("curation", "Curation", "index", "extra"), TypeError),
    ("bare_string", "curation", TypeError),
    ("dict", {"key": "curation"}, TypeError),
    ("none", None, TypeError),
    ("endpoint_none", nav.NavEntry("curation", "Curation", None), ValueError),
    ("endpoint_int", nav.NavEntry("curation", "Curation", 7), ValueError),
    ("empty_label", nav.NavEntry("curation", "", "index"), ValueError),
    ("blank_key", nav.NavEntry("   ", "Curation", "index"), ValueError),
]


@pytest.mark.parametrize("name,entry,exc", _HOSTILE, ids=[c[0] for c in _HOSTILE])
def test_register_nav_rejects_a_hostile_entry_shape(name, entry, exc):
    with pytest.raises(exc):
        nav.register_nav(_app(), [entry])


def test_register_nav_rejects_a_non_iterable_batch():
    with pytest.raises(TypeError):
        nav.register_nav(_app(), 42)


def test_register_nav_rejects_a_duplicate_key():
    with pytest.raises(ValueError, match="duplicate key"):
        nav.register_nav(_app(), [CURATION, nav.NavEntry("curation", "Other", "index")])


@pytest.mark.parametrize("key", ["publications", "meta", "style", "reset", "qc_triage"])
def test_register_nav_rejects_a_reserved_key(key):
    # A host reusing an engine key lights BOTH links on the engine's own page —
    # the nav lying about where you are, with no error anywhere.
    with pytest.raises(ValueError, match="reserved"):
        nav.register_nav(_app(), [nav.NavEntry(key, "Mine", "index")])


def test_reserved_keys_covers_sections_and_engine_nav_keys():
    reserved = nav.reserved_keys()
    assert {"publications", "meta"} <= reserved, "CV section names must be reserved"
    assert {"style", "trackers", "validate", "reset"} <= reserved, "engine nav keys too"
    assert "curation" not in reserved


def test_a_rejected_batch_does_not_partially_register():
    app = _app()
    with pytest.raises(ValueError):
        nav.register_nav(app, [CURATION, nav.NavEntry("meta", "Bad", "index")])
    assert nav.registered(app) == (), "validation must complete before anything is stored"


# ---- render time: nothing raises, everything is logged --------------------


def test_a_registered_entry_renders_in_the_tools_menu():
    app = _host_app([nav.NavEntry("curation", "Curation", "hostpage")])
    block = _nav_block(app.test_client().get("/").get_data(as_text=True))
    assert ">Curation</a>" in block
    assert 'href="/hostpage"' in block


def test_an_entry_resolving_to_the_root_url_is_refused(caplog):
    # A url of "/" prefix-matches every path, so it would mark its key current on
    # every page in the app. Refused at resolution and logged.
    app = _app([CURATION])  # endpoint `index` -> "/"
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Curation" not in _nav_block(resp.get_data(as_text=True))
    assert any("root url" in r.getMessage() for r in caplog.records)


def test_registration_order_relative_to_routes_does_not_matter():
    # `_host_app` registers the nav AFTER adding its route; do the reverse here.
    app = create_app()
    app.config["TESTING"] = True
    nav.register_nav(app, [nav.NavEntry("curation", "Curation", "hostpage")])

    @app.route("/hostpage")
    def hostpage():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}host page{% endblock %}"
        )

    assert 'href="/hostpage"' in _nav_block(app.test_client().get("/").get_data(as_text=True))


def test_an_unbuildable_endpoint_is_dropped_without_a_500(caplog):
    app = _app([nav.NavEntry("curation", "Curation", "no_such_endpoint")])
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Curation" not in _nav_block(resp.get_data(as_text=True))
    assert any("no_such_endpoint" in r.getMessage() for r in caplog.records)


def test_a_parameterised_endpoint_is_dropped_without_a_500(caplog):
    # `section_list` needs a <section>, so url_for() with no args raises BuildError.
    app = _app([nav.NavEntry("curation", "Curation", "section_list")])
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Curation" not in _nav_block(resp.get_data(as_text=True))


def test_an_unbuildable_endpoint_logs_once_not_once_per_request(caplog):
    # The log is a real file on disk; a permanently-broken entry must not append a
    # line on every request forever.
    app = _app([nav.NavEntry("curation", "Curation", "no_such_endpoint")])
    client = app.test_client()
    with caplog.at_level(logging.WARNING):
        client.get("/")
        client.get("/")
        client.get("/publications")
    hits = [r for r in caplog.records if "no_such_endpoint" in r.getMessage()]
    assert len(hits) == 1, f"expected one warning, got {len(hits)}"


def test_a_resolver_that_raises_does_not_500_the_editor(caplog, monkeypatch):
    """The belt-and-braces catch in `inject_helpers`.

    `nav.resolve` is documented not to raise, but the context processor is where
    the damage WOULD land: a raise there 500s all 25 templates that extend
    base.html, `/` included, which is the only recovery surface. So the caller
    catches too, and this is that guard's gate.
    """

    def boom(_app):
        raise RuntimeError("host resolver exploded")

    monkeypatch.setattr(nav, "resolve", boom)
    app = _app([CURATION])
    with caplog.at_level(logging.ERROR):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "<nav>" in resp.get_data(as_text=True)
    assert any("host-contributed entries failed" in r.getMessage() for r in caplog.records)


def test_resolve_outside_a_request_context_is_empty():
    assert nav.resolve(_app([CURATION])) == []


def test_a_label_is_autoescaped():
    app = _host_app([nav.NavEntry("curation", "<script>alert(1)</script>", "hostpage")])
    block = _nav_block(app.test_client().get("/").get_data(as_text=True))
    assert "<script>alert(1)</script>" not in block
    assert "&lt;script&gt;" in block


# ---- rider R2: the Tools summary highlights on its own pages --------------


@pytest.mark.parametrize(
    "path,key",
    [
        ("/pubmed_sync", "pubmed_sync"),
        ("/qc/triage", "qc_triage"),
        ("/validate", "validate"),
        ("/replace", "replace"),
        ("/reset", "reset"),
        ("/citations", "citations"),
        ("/style", "style"),
    ],
)
def test_tools_summary_is_current_on_every_tools_page(path, key):
    """Five of the twelve Tools links could not light their own summary.

    Their routes DO set `current_section` explicitly — the gap was that the
    Jinja-local `TOOLS` list, which exists only to compute `tools_keys`, named
    five of the twelve panel links. Measured before the fix: only /citations and
    /style highlighted.
    """
    if key in ("freeze", "trackers") and not capabilities.current().freeze:
        pytest.skip("capability-gated link not registered under this template")
    body = _app().test_client().get(path).get_data(as_text=True)
    assert "is-current" in _tools_details(body), f"{path}: Tools summary not marked current"


def test_the_current_section_fallback_marks_a_host_page_current():
    app = _host_app([nav.NavEntry("curation", "Curation", "hostpage")])
    block = _nav_block(app.test_client().get("/hostpage").get_data(as_text=True))
    assert '>Curation</a>' in block
    assert 'class="is-current"' in block
    assert 'aria-current="page"' in block


def test_the_fallback_also_matches_a_host_subpath():
    app = _host_app([nav.NavEntry("curation", "Curation", "hostpage")])

    @app.route("/hostpage/deeper")
    def deeper():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}deeper{% endblock %}"
        )

    block = _nav_block(app.test_client().get("/hostpage/deeper").get_data(as_text=True))
    assert 'aria-current="page"' in block, "a sub-path of a host entry must still be current"


def test_a_host_entry_also_lights_the_tools_summary():
    # The `tools_keys` union. Without it the link works but its menu never shows
    # as active — the one silent-degradation case for a host entry.
    app = _host_app([nav.NavEntry("curation", "Curation", "hostpage")])
    body = app.test_client().get("/hostpage").get_data(as_text=True)
    assert "is-current" in _tools_details(body)


def test_a_host_entry_does_not_light_the_tools_summary_on_an_engine_page():
    app = _host_app([nav.NavEntry("curation", "Curation", "hostpage")])
    body = app.test_client().get("/education").get_data(as_text=True)
    assert "is-current" not in _tools_details(body)


def test_resolve_orders_longest_url_first():
    """Registration order must not decide prefix matching; url length must.

    Both entries resolve to a real, non-root url, and they are registered
    SHORTEST-FIRST so the assertion fails if the sort is dropped. (An earlier
    version of this test paired a real url with `index` — whose "/" is refused
    outright — leaving one url, so the sort was trivially satisfied and a mutant
    that deleted it survived.)
    """
    app = _app()
    nav.register_nav(
        app,
        [
            nav.NavEntry("shorter", "Shorter", "qc_triage_view"),  # /qc/triage
            nav.NavEntry("longer", "Longer", "urls_verify_view"),  # /urls/verify
        ],
    )
    with app.test_request_context("/"):
        resolved = nav.resolve(app)
    assert [r.url for r in resolved] == ["/urls/verify", "/qc/triage"]


def test_a_parent_entry_does_not_shadow_its_own_child_page():
    """The concrete reason the sort exists: /hostpage registered before
    /hostpage/deeper must not claim the deeper page's own nav entry."""
    app = _host_app([nav.NavEntry("parent", "Parent", "hostpage")])

    @app.route("/hostpage/deeper")
    def deeper():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}deeper{% endblock %}"
        )

    nav.register_nav(
        app,
        [
            nav.NavEntry("parent", "Parent", "hostpage"),
            nav.NavEntry("child", "Child", "deeper"),
        ],
    )
    block = _nav_block(app.test_client().get("/hostpage/deeper").get_data(as_text=True))
    # Exactly one entry is current, and the marker is on the CHILD's anchor.
    assert block.count('aria-current="page"') == 1
    child_anchor = block[block.find('href="/hostpage/deeper"') :]
    child_anchor = child_anchor[: child_anchor.find("</a>")]
    assert 'aria-current="page"' in child_anchor, "the parent entry shadowed its own child"


if __name__ == "__main__":
    import sys

    if "--regenerate-golden" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        html = _app().test_client().get("/").get_data(as_text=True)
        GOLDEN.write_text(_nav_block(html), encoding="utf-8")
        print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes)")
