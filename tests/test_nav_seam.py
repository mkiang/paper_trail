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
import re
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
# resolved. Note it points at `index` (url "/"), which `_resolve` REFUSES at render
# time — see test_an_entry_resolving_to_the_root_url_is_refused. Render tests use
# `_host_app` and its /hostpage endpoint instead.
REPORTS = nav.NavEntry(key="reports", label="Reports", endpoint="index")


# ---- the byte-identity guarantee: an unused seam costs zero bytes ----------


@pytest.mark.skipif(
    capabilities.current().freeze or capabilities.current().altmetric,
    reason="golden captured under the public `modern` template; the nav gates on "
    "capabilities.freeze and .altmetric independently, so check both rather than "
    "relying on the two ship-templates keeping them in lockstep",
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


def _plain_page():
    from flask import render_template_string

    return render_template_string("{% extends 'base.html' %}{% block content %}x{% endblock %}")


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


# ---- shape: every hostile input raises during the host's STARTUP ----------

# Not a NavEntry at all -> refused by the BATCH check in register_nav. Each of
# these 500ed every page under the first draft's resolver, which unpacked
# `(key, label, endpoint)` in the `for` header, outside its own try.
_NON_ENTRY = [
    ("two_tuple", ("reports", "Reports")),
    ("four_tuple", ("reports", "Reports", "index", "extra")),
    ("bare_string", "reports"),
    ("dict", {"key": "reports"}),
    ("none", None),
]


@pytest.mark.parametrize("name,entry", _NON_ENTRY, ids=[c[0] for c in _NON_ENTRY])
def test_register_nav_rejects_a_non_entry(name, entry):
    with pytest.raises(TypeError):
        nav.register_nav(_app(), [entry])


# A NavEntry with a bad FIELD is refused at CONSTRUCTION (`__post_init__`), so the
# traceback lands on the host's own construction site rather than its register_nav
# call. Factories, not values: building these at module scope would raise at import.
_BAD_FIELDS = [
    ("endpoint_none", lambda: nav.NavEntry(key="reports", label="Reports", endpoint=None)),
    ("endpoint_int", lambda: nav.NavEntry(key="reports", label="Reports", endpoint=7)),
    ("empty_label", lambda: nav.NavEntry(key="reports", label="", endpoint="index")),
    ("blank_key", lambda: nav.NavEntry(key="   ", label="Reports", endpoint="index")),
    ("padded_key", lambda: nav.NavEntry(key=" reports ", label="Reports", endpoint="index")),
    ("padded_label", lambda: nav.NavEntry(key="reports", label="Reports ", endpoint="index")),
    ("relative_endpoint", lambda: nav.NavEntry(key="reports", label="C", endpoint=".page")),
]


@pytest.mark.parametrize("name,make", _BAD_FIELDS, ids=[c[0] for c in _BAD_FIELDS])
def test_nav_entry_rejects_a_bad_field_at_construction(name, make):
    with pytest.raises(ValueError):
        make()


def test_a_padded_key_is_refused_rather_than_silently_dead():
    # " education " would pass a `.strip()`-only check, then evade the reserved-key
    # exact match, and then never equal any `current_section` -> a silently dead
    # entry instead of a refusal.
    with pytest.raises(ValueError, match="whitespace"):
        nav.NavEntry(key=" education ", label="Mine", endpoint="index")


def test_nav_entry_is_keyword_only():
    # Field order must never become contractual.
    with pytest.raises(TypeError):
        nav.NavEntry("reports", "Reports", "index")


def test_register_nav_rejects_a_non_iterable_batch():
    with pytest.raises(TypeError):
        nav.register_nav(_app(), 42)


def test_registering_an_empty_batch_is_a_no_op():
    app = _app([])
    assert nav._registered(app) == ()
    with app.test_request_context("/"):
        assert nav._resolve(app) == []


def test_a_generator_batch_is_accepted_and_fully_consumed():
    # The signature says Iterable, and a generator is consumed once into a tuple —
    # so a second render cannot silently see an exhausted iterator.
    app = _app()
    nav.register_nav(app, (e for e in [nav.NavEntry(key="g", label="G", endpoint="index")]))
    assert [e.key for e in nav._registered(app)] == ["g"]
    assert [e.key for e in nav._registered(app)] == ["g"]


def test_register_nav_rejects_a_duplicate_key_within_one_batch():
    with pytest.raises(ValueError, match="already registered"):
        nav.register_nav(
            _app(), [REPORTS, nav.NavEntry(key="reports", label="Other", endpoint="index")]
        )


@pytest.mark.parametrize("key", ["publications", "meta", "style", "reset", "qc_triage"])
def test_register_nav_rejects_a_reserved_key(key):
    # A host reusing an engine key lights BOTH links on the engine's own page —
    # the nav lying about where you are, with no error anywhere.
    with pytest.raises(ValueError, match="reserved"):
        nav.register_nav(_app(), [nav.NavEntry(key=key, label="Mine", endpoint="index")])


def test_reserved_keys_covers_sections_and_engine_nav_keys():
    reserved = nav.reserved_keys()
    assert {"publications", "meta"} <= reserved, "CV section names must be reserved"
    assert {"style", "trackers", "validate", "reset"} <= reserved, "engine nav keys too"
    assert "reports" not in reserved


def test_reserved_keys_covers_every_current_section_literal_in_the_engine():
    """Drift guard. `_ENGINE_NAV_KEYS` is hand-maintained, and a new tool page whose
    key is forgotten re-opens the collision the reserved set exists to close. A
    spot-check of four keys would stay green through that, so grep the package."""
    pkg = ROOT / "scripts" / "cv_editor"
    found = set()
    for py in pkg.rglob("*.py"):
        found |= set(re.findall(r'current_section=["\']([a-z_]+)["\']', py.read_text()))
    assert found, "grep found no current_section literals — the pattern must have changed"
    missing = found - nav.reserved_keys()
    assert not missing, f"engine keys missing from reserved_keys(): {sorted(missing)}"


def test_path_derived_nav_keys_is_the_single_source_used_by_app():
    """`app.py`'s derivation must READ the tuple, not repeat the literal."""
    src = (ROOT / "scripts" / "cv_editor" / "app.py").read_text()
    assert "nav._PATH_DERIVED_NAV_KEYS" in src
    assert '"style", "freeze", "search", "urls", "citations", "pubmed_sync"' not in src


def test_the_public_surface_is_exactly_three_names():
    """Pins what 1.2.0 commits under SemVer. Everything else is `_`-prefixed and
    free to change. Mirrors `tests/test_export_public_api.py`."""
    assert nav.__all__ == ["NavEntry", "register_nav", "reserved_keys"]
    public = {n for n in vars(nav) if not n.startswith("_")}
    # module-level imports are not part of the surface; only our own definitions
    ours = {n for n in public if getattr(vars(nav)[n], "__module__", None) == "cv_editor.nav"}
    assert ours == set(nav.__all__), f"undeclared public name(s): {sorted(ours - set(nav.__all__))}"


def test_register_nav_appends_rather_than_replacing():
    """A host decomposed into several route modules registers once per module. The
    first draft REBOUND the tuple, so only the last call's batch survived — silently,
    with no error, no log and no test — so any host that grows a second route
    module is one refactor away from losing entries."""
    app = _app()
    nav.register_nav(app, [nav.NavEntry(key="one", label="One", endpoint="index")])
    nav.register_nav(app, [nav.NavEntry(key="two", label="Two", endpoint="index")])
    assert [e.key for e in nav._registered(app)] == ["one", "two"]


def test_register_nav_refuses_a_key_already_registered_by_an_earlier_call():
    app = _app()
    nav.register_nav(app, [nav.NavEntry(key="one", label="One", endpoint="index")])
    with pytest.raises(ValueError, match="already registered"):
        nav.register_nav(app, [nav.NavEntry(key="one", label="Again", endpoint="index")])


def test_a_rejected_batch_leaves_earlier_entries_intact():
    app = _app()
    nav.register_nav(app, [nav.NavEntry(key="one", label="One", endpoint="index")])
    with pytest.raises(ValueError):
        nav.register_nav(app, [nav.NavEntry(key="meta", label="Bad", endpoint="index")])
    assert [e.key for e in nav._registered(app)] == ["one"]


def test_a_rejected_batch_does_not_partially_register():
    app = _app()
    with pytest.raises(ValueError):
        nav.register_nav(app, [REPORTS, nav.NavEntry(key="meta", label="Bad", endpoint="index")])
    assert nav._registered(app) == (), "validation must complete before anything is stored"


# ---- render time: nothing raises, everything is logged --------------------


def test_a_registered_entry_renders_in_the_tools_menu():
    app = _host_app([nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])
    block = _nav_block(app.test_client().get("/").get_data(as_text=True))
    assert ">Reports</a>" in block
    assert 'href="/hostpage"' in block


def test_an_entry_resolving_to_the_root_url_is_refused(caplog):
    # A url of "/" prefix-matches every path, so it would mark its key current on
    # every page in the app. Refused at resolution and logged.
    app = _app([REPORTS])  # endpoint `index` -> "/"
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Reports" not in _nav_block(resp.get_data(as_text=True))
    assert any("root url" in r.getMessage() for r in caplog.records)


def test_registration_order_relative_to_routes_does_not_matter():
    # `_host_app` registers the nav AFTER adding its route; do the reverse here.
    app = create_app()
    app.config["TESTING"] = True
    nav.register_nav(app, [nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])

    @app.route("/hostpage")
    def hostpage():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}host page{% endblock %}"
        )

    assert 'href="/hostpage"' in _nav_block(app.test_client().get("/").get_data(as_text=True))


def test_an_unbuildable_endpoint_is_dropped_without_a_500(caplog):
    app = _app([nav.NavEntry(key="reports", label="Reports", endpoint="no_such_endpoint")])
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Reports" not in _nav_block(resp.get_data(as_text=True))
    assert any("no_such_endpoint" in r.getMessage() for r in caplog.records)


def test_a_parameterised_endpoint_is_dropped_without_a_500(caplog):
    # `section_list` needs a <section>, so url_for() with no args raises BuildError.
    app = _app([nav.NavEntry(key="reports", label="Reports", endpoint="section_list")])
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "Reports" not in _nav_block(resp.get_data(as_text=True))
    # The logging half of the contract, which this test previously opened caplog
    # for and then never asserted — it would have passed with the warn deleted.
    assert any("section_list" in r.getMessage() for r in caplog.records)


def test_a_non_str_url_is_dropped_without_a_500(caplog):
    """N1 checkpoint HIGH. Flask returns whatever a host's `url_build_error_handler`
    returns and never type-checks it (`flask/sansio/app.py`). A 3-element list
    passed the old `len(url) < 2` guard, and the fallback's `r.url.rstrip(...)` then
    raised AttributeError OUT of the context processor — 500ing all 25 templates
    that extend base.html, `/` included. Reproduced before the fix."""
    app = _app([nav.NavEntry(key="reports", label="Reports", endpoint="no_such_endpoint")])
    app.url_build_error_handlers.append(lambda error, endpoint, values: ["not", "a", "string"])
    with caplog.at_level(logging.WARNING):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert any("not str" in r.getMessage() for r in caplog.records)


def test_an_unbuildable_endpoint_logs_once_not_once_per_request(caplog):
    # The log is a real file on disk; a permanently-broken entry must not append a
    # line on every request forever.
    app = _app([nav.NavEntry(key="reports", label="Reports", endpoint="no_such_endpoint")])
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

    monkeypatch.setattr(nav, "_resolve", boom)
    app = _app([REPORTS])
    with caplog.at_level(logging.ERROR):
        resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "<nav>" in resp.get_data(as_text=True)
    assert any("host-contributed entries failed" in r.getMessage() for r in caplog.records)


def test_resolve_outside_a_request_context_is_empty():
    assert nav._resolve(_app([REPORTS])) == []


def test_a_label_is_autoescaped():
    app = _host_app(
        [nav.NavEntry(key="reports", label="<script>alert(1)</script>", endpoint="hostpage")]
    )
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
    # The two capability-gated links (freeze, trackers) are deliberately absent
    # from the table: their routes are not registered under the public `modern`
    # template, so they 404 here. An earlier draft carried a skip-guard for them
    # that could never fire, which read as coverage it did not provide.
    body = _app().test_client().get(path).get_data(as_text=True)
    assert "is-current" in _tools_details(body), f"{path}: Tools summary not marked current"


def test_the_current_section_fallback_marks_a_host_page_current():
    app = _host_app([nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])
    block = _nav_block(app.test_client().get("/hostpage").get_data(as_text=True))
    assert '>Reports</a>' in block
    assert 'class="is-current"' in block
    assert 'aria-current="page"' in block


def test_the_fallback_also_matches_a_host_subpath():
    app = _host_app([nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])

    @app.route("/hostpage/deeper")
    def deeper():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}deeper{% endblock %}"
        )

    block = _nav_block(app.test_client().get("/hostpage/deeper").get_data(as_text=True))
    assert 'aria-current="page"' in block, "a sub-path of a host entry must still be current"


def test_an_encoded_url_still_matches_the_decoded_request_path():
    """`url_for` percent-encodes; `request.path` is already decoded by Werkzeug.
    Comparing the encoded form means a host page whose rule contains a space or any
    non-ASCII character NEVER lights its nav entry — silently, which is the exact
    "I don't see this, where is it?" symptom the seam exists to remove."""
    app = create_app()
    app.config["TESTING"] = True

    @app.route("/host page/café")
    def spacey():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}spacey{% endblock %}"
        )

    nav.register_nav(app, [nav.NavEntry(key="spacey", label="Spacey", endpoint="spacey")])
    with app.test_request_context("/host page/café"):
        resolved = nav._resolve(app)
    assert resolved[0].url != resolved[0].match_path, "url must stay encoded for href"
    assert "%20" in resolved[0].url and "%20" not in resolved[0].match_path

    block = _nav_block(app.test_client().get("/host page/café").get_data(as_text=True))
    assert 'aria-current="page"' in block, "an encoded host url never matched its own page"


def test_a_host_entry_also_lights_the_tools_summary():
    # The `tools_keys` union. Without it the link works but its menu never shows
    # as active — the one silent-degradation case for a host entry.
    app = _host_app([nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])
    body = app.test_client().get("/hostpage").get_data(as_text=True)
    assert "is-current" in _tools_details(body)


def test_a_host_entry_does_not_light_the_tools_summary_on_an_engine_page():
    app = _host_app([nav.NavEntry(key="reports", label="Reports", endpoint="hostpage")])
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
            nav.NavEntry(key="shorter", label="Shorter", endpoint="qc_triage_view"),  # /qc/triage
            nav.NavEntry(key="longer", label="Longer", endpoint="urls_verify_view"),  # /urls/verify
        ],
    )
    with app.test_request_context("/"):
        resolved = nav._resolve(app)
    assert [r.url for r in resolved] == ["/urls/verify", "/qc/triage"]


def test_a_parent_entry_does_not_shadow_its_own_child_page():
    """The concrete reason the sort exists: /hostpage registered before
    /hostpage/deeper must not claim the deeper page's own nav entry."""
    app = _host_app([])

    @app.route("/hostpage/deeper")
    def deeper():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'base.html' %}{% block content %}deeper{% endblock %}"
        )

    nav.register_nav(
        app,
        [
            nav.NavEntry(key="parent", label="Parent", endpoint="hostpage"),
            nav.NavEntry(key="child", label="Child", endpoint="deeper"),
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


# ---- doc drift: the contract is only usable if the doc still states it -------


DOC = ROOT / "docs" / "extending.md"


def test_the_doc_names_every_committed_symbol():
    """A renamed public symbol must not leave `docs/extending.md` silently stale —
    the doc IS the contract for a host that cannot read our tests."""
    text = DOC.read_text(encoding="utf-8")
    for name in nav.__all__:
        assert name in text, f"docs/extending.md does not mention {name}"


def test_the_doc_does_not_resurrect_the_safe_url_framing():
    """N1's checkpoint caught this claim in the module docstring: `safe_url` returns
    '#' for anything without an http(s)/mailto scheme, so applying it to the nav
    would blank every internal path. The doc must ground the endpoints-not-URLs
    property in Werkzeug's rule check, and must say so only to warn against it."""
    text = DOC.read_text(encoding="utf-8")
    assert "Werkzeug refuses" in text, "the real grounding must be stated"
    assert "not** the `safe_url`" in text, "the doc must warn off the safe_url framing"


def test_the_doc_states_the_growable_reserved_set_and_the_append_semantics():
    text = DOC.read_text(encoding="utf-8")
    assert "may grow in any minor release" in text.lower()
    assert "appends" in text, "batch semantics must be stated in one word"
    assert "current_section" in text, "the active-page obligation must be stated"


# ---- host sub-pages are not engine pages ------------------------------------


def test_a_host_landing_page_with_sub_pages_warns_about_nothing(caplog):
    """Found by running the real host, not by reading the code.

    The path-overlap warning walked the LIVE url map, so a host's own sub-pages
    were indistinguishable from engine pages and the most ordinary host shape —
    a landing page with children under it — logged a warning claiming its own
    `/host/child` was "the engine page". The engine now compares against a
    snapshot of its own rules taken before any host attached routes.
    """
    app = create_app()
    app.config["TESTING"] = True

    def _page():
        from flask import render_template_string

        return render_template_string("{% extends 'base.html' %}{% block content %}x{% endblock %}")

    for rule, endpoint in (("/hostroot", "hostroot"), ("/hostroot/child", "hostchild")):
        app.add_url_rule(rule, endpoint, _page)
    nav.register_nav(app, [nav.NavEntry(key="hostroot", label="Host", endpoint="hostroot")])

    with caplog.at_level(logging.WARNING):
        assert app.test_client().get("/").status_code == 200
    overlap = [r.getMessage() for r in caplog.records if "path prefix" in r.getMessage()]
    assert not overlap, f"warned about the host's own sub-page: {overlap}"


def test_an_entry_shadowing_a_real_engine_page_still_warns(caplog):
    """The other half: the check must keep its teeth. `/qc` is a plausible host
    dashboard path, and the engine really does serve /qc/report under it."""
    app = create_app()
    app.config["TESTING"] = True

    def _page():
        from flask import render_template_string

        return render_template_string("{% extends 'base.html' %}{% block content %}x{% endblock %}")

    app.add_url_rule("/qc", "host_qc", _page)
    nav.register_nav(app, [nav.NavEntry(key="hostqc", label="QC", endpoint="host_qc")])
    with caplog.at_level(logging.WARNING):
        app.test_client().get("/")
    assert any("path prefix" in r.getMessage() for r in caplog.records), (
        "an entry over a real engine page must still warn"
    )


# ---- the overlap check must not lie, and must not go quiet ------------------


def test_a_host_page_under_an_explicit_nav_key_does_not_warn(caplog):
    """N6 checkpoint. 1.2.1 fixed one false-positive family and left another.

    The direction-1 basis was `reserved_keys()`, which also holds keys that ROUTES
    pass explicitly (`trackers`, `qc_triage`, …). Those are nav keys, not path
    segments: there is no engine rule at `/trackers`, and `inject_helpers` never
    derives `trackers` from a path. So a host page there was told it "sits under
    the engine path /trackers" and that the engine's link "will light on that page
    instead" — both false, and the host's own link was in fact `is-current`.
    """
    app = create_app()
    app.config["TESTING"] = True
    app.add_url_rule("/trackers", "hosttrackers", _plain_page)
    nav.register_nav(app, [nav.NavEntry(key="hosttrackers", label="Host", endpoint="hosttrackers")])
    assert not any(
        r.rule == "/trackers" for r in app.url_map.iter_rules() if r.endpoint != "hosttrackers"
    )
    with caplog.at_level(logging.WARNING):
        body = app.test_client().get("/trackers").get_data(as_text=True)
    assert not [r.getMessage() for r in caplog.records if "sits under" in r.getMessage()]
    i = body.find(">Host</a>")
    assert 'aria-current="page"' in body[body.rfind("<a", 0, i) : i], "the host link IS current"


def test_a_host_page_under_a_derived_section_still_warns(caplog):
    # The other half: /service really is derived from the path, and the engine's
    # link really does steal the highlight, so this must keep its teeth.
    app = create_app()
    app.config["TESTING"] = True
    app.add_url_rule("/service/notes", "hostnotes", _plain_page)
    nav.register_nav(app, [nav.NavEntry(key="hostnotes", label="Host", endpoint="hostnotes")])
    with caplog.at_level(logging.WARNING):
        app.test_client().get("/service/notes")
    assert any("derives its own current_section" in r.getMessage() for r in caplog.records)


def test_a_missing_engine_snapshot_is_reported_not_silently_skipped(caplog):
    """Without this, an app not built by `create_app()` (or one whose extensions
    were cleared) gets NO overlap warnings and no explanation — a silent false
    negative, which is worse than the false positive 1.2.1 removed because it
    still looks like the check ran."""
    app = create_app()
    app.config["TESTING"] = True
    app.add_url_rule("/qc", "host_qc", _plain_page)
    app.extensions.clear()
    nav.register_nav(app, [nav.NavEntry(key="hostqc", label="QC", endpoint="host_qc")])
    with caplog.at_level(logging.WARNING):
        app.test_client().get("/")
    assert any("no engine url-rule snapshot" in r.getMessage() for r in caplog.records)


def test_a_foreign_value_on_the_extensions_key_is_reported(caplog):
    """Replacing it discards every registered entry and the snapshot. That must not
    be silent — the nav would simply empty out with a green suite."""
    app = create_app()
    app.config["TESTING"] = True
    nav.register_nav(app, [nav.NavEntry(key="reports", label="Reports", endpoint="index")])
    app.extensions["cv_editor_nav"] = {"someone": "else"}
    with caplog.at_level(logging.WARNING):
        assert nav._registered(app) == ()
    assert any("not the seam's own state" in r.getMessage() for r in caplog.records)


def test_the_overlap_check_is_not_latched_by_an_empty_first_resolve():
    """It runs once per app. If the first request lands before the host's routes
    attach, every entry is dropped, `resolved` is empty, and latching there meant
    the check never ran again for the life of the app — silently.

    Asserted on the latch itself rather than end-to-end, because Flask forbids
    `add_url_rule` after the first request, so the "routes attach later" shape
    cannot be staged through the test client.
    """
    app = create_app()
    app.config["TESTING"] = True
    nav.register_nav(app, [nav.NavEntry(key="hostqc", label="QC", endpoint="never_registered")])
    app.test_client().get("/")  # entry drops -> nothing resolved
    assert nav._state(app).collisions_checked is False, "an empty resolve must not latch the check"


def test_an_entry_registered_after_the_first_request_is_still_checked(caplog):
    app = create_app()
    app.config["TESTING"] = True
    app.add_url_rule("/qc", "host_qc", _plain_page)
    nav.register_nav(app, [nav.NavEntry(key="first", label="First", endpoint="urls_verify_view")])
    app.test_client().get("/")  # latches on the first entry
    nav.register_nav(app, [nav.NavEntry(key="hostqc", label="QC", endpoint="host_qc")])
    with caplog.at_level(logging.WARNING):
        app.test_client().get("/")
    assert any("path prefix of the engine page" in r.getMessage() for r in caplog.records)


def test_derivable_prefixes_are_a_strict_subset_of_reserved_keys():
    derivable, reserved = nav._derivable_path_prefixes(), nav.reserved_keys()
    assert derivable < reserved, "the overlap basis must be narrower than the key basis"
    assert not (derivable & set(nav._EXPLICIT_NAV_KEYS)), (
        "explicitly-passed keys are not path segments and must not be an overlap basis"
    )
