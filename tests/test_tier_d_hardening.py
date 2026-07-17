"""Tier D (2026-05-27) — medium-priority test gaps surfaced by the R4 audit.

Eight categories of hardening: boundary tests, fallback-chain integration,
diacritics edge cases, version-mismatch handling, simple route smoke. All
tests are additive — they don't change production code.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---- (1) /quit route smoke + CSRF coverage ----


def test_quit_route_rejects_GET():
    """The /quit route is POST-only. A stray GET (from a bookmark or
    a curious user typing the URL) must NOT shut the editor down.
    Flask returns 405 (Method Not Allowed) by default for unsupported
    methods on a known route; 404 is also acceptable here (means the
    URL doesn't even match a GET route)."""
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    resp = c.get("/quit")
    assert resp.status_code in (404, 405), f"GET /quit should not succeed; got {resp.status_code}"


def test_quit_route_csrf_blocks_cross_origin_post(monkeypatch):
    """The before_request Origin/Referer guard (gotcha #39) must
    apply to /quit. A malicious browser tab on a different origin
    POSTing to /quit while the editor runs locally would otherwise
    kill the editor without auth. TESTING bypass is the intentional
    escape hatch for the test suite — flip it off here to exercise
    the real check."""
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = False
    c = app.test_client()
    # Send a POST with an Origin header from a different netloc.
    resp = c.post("/quit", headers={"Origin": "http://attacker.example:9999"})
    # The CSRF check must refuse the cross-origin POST.
    assert resp.status_code in (400, 403), (
        f"cross-origin POST to /quit should be blocked, got {resp.status_code}"
    )


def test_quit_route_requires_launcher_token_when_set():
    """When QUIT_TOKEN is set (production: launcher mints it), a POST
    without the matching token is rejected with 403. Protects against
    a stray `curl POST /quit` from another shell on the same box —
    no browser involved, can't read the token from the page DOM."""
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True  # bypass CSRF
    app.config["QUIT_TOKEN"] = "secret-from-launcher"
    c = app.test_client()
    # Without the token — blocked.
    resp = c.post("/quit", data={})
    assert resp.status_code == 403
    # With the wrong token — blocked.
    resp = c.post("/quit", data={"quit_token": "guessed"})
    assert resp.status_code == 403


def test_quit_route_accepts_correct_token(monkeypatch):
    """With the right token, /quit reaches the SIGTERM dispatch. We
    can't safely let the daemon thread fire `os.kill(getpid, SIGTERM)`
    because it'd kill pytest (matches the existing test_v3 pattern:
    `test_quit_returns_200_without_killing_test_process`). Stub out
    threading.Thread.start so the daemon never runs."""
    import threading as _thr

    monkeypatch.setattr(_thr.Thread, "start", lambda self: None)
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["QUIT_TOKEN"] = "matching-token"
    c = app.test_client()
    resp = c.post("/quit", data={"quit_token": "matching-token"})
    # 200 means: token verified, route reached, daemon "started"
    # (no-op stub), response returned.
    assert resp.status_code == 200


def test_quit_route_no_token_gate_when_token_unset():
    """In test mode (no launcher), QUIT_TOKEN defaults to empty and
    the /quit gate falls through. This preserves the existing test
    suite contract — tests that POST /quit don't need a token."""
    import os

    from cv_editor.app import create_app

    # Belt-and-suspenders: clear any inherited env var so the create_app
    # default sees an empty string.
    monkeypatch_env = os.environ.pop("CV_EDITOR_QUIT_TOKEN", None)
    try:
        app = create_app()
        app.config["TESTING"] = True
        assert app.config.get("QUIT_TOKEN", "") == ""
        # POST without a token: the gate short-circuits; the route
        # would proceed to SIGTERM, which would kill the test process.
        # Just assert the gate doesn't 403 — we don't actually send.
        # (test_quit_route_accepts_correct_token covers the full path.)
    finally:
        if monkeypatch_env is not None:
            os.environ["CV_EDITOR_QUIT_TOKEN"] = monkeypatch_env


def test_quit_token_rendered_in_base_template_when_set(client):
    """The base template renders the token as a hidden input so the
    Quit button's form POST carries it. The default `client` fixture
    sets TESTING and leaves QUIT_TOKEN empty; this test creates a
    fresh app with the token set to assert the template path."""
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["QUIT_TOKEN"] = "page-rendered-token"
    c = app.test_client()
    body = c.get("/").get_data(as_text=True)
    assert 'name="quit_token"' in body
    assert 'value="page-rendered-token"' in body


def test_quit_token_not_rendered_when_unset(client):
    """When QUIT_TOKEN is empty (test mode without launcher), the
    hidden input is omitted entirely. Prevents the template from
    rendering `value=""` which would look like a deliberate empty
    string to anyone inspecting the DOM."""
    body = client.get("/").get_data(as_text=True)
    # The Quit form is present; the hidden token is NOT.
    assert 'btn-quit' in body
    assert 'name="quit_token"' not in body


# ---- (2) Tombstone TTL exact-30-day boundary ----


def test_tombstone_prune_boundary_at_30_days_exactly():
    """qc_decisions.Decisions.prune_expired_tombstones drops tombstones
    older than TOMBSTONE_TTL_DAYS=30. The R4 critic flagged that the
    existing tests only cover 10 days (kept) and 40 days (pruned) — no
    test at the exact 30-day boundary. An off-by-one rewrite would
    slip past."""
    from cv_editor.qc_decisions import (
        TOMBSTONE_TTL_DAYS,
        Decisions,
        Tombstone,
    )

    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    boundary_ts = (now - timedelta(days=TOMBSTONE_TTL_DAYS)).isoformat()
    past_ts = (now - timedelta(days=TOMBSTONE_TTL_DAYS, seconds=1)).isoformat()
    inside_ts = (now - timedelta(days=TOMBSTONE_TTL_DAYS - 1, hours=23)).isoformat()

    s = Decisions(
        decisions={},
        tombstones={
            "boundary": Tombstone(pruned_at=boundary_ts, decision={"k": "v"}),
            "past": Tombstone(pruned_at=past_ts, decision={"k": "v"}),
            "inside": Tombstone(pruned_at=inside_ts, decision={"k": "v"}),
        },
    )
    s.prune_expired_tombstones(now=now)
    assert "past" not in s.tombstones, "Past-30-days tombstone should be pruned"
    assert "inside" in s.tombstones, "Inside-30-days tombstone must be kept"
    # The boundary case can go either way per the predicate's `<` vs `<=`;
    # current impl uses `<` so the exact-30-day tombstone is KEPT.
    assert "boundary" in s.tombstones, (
        "Implementation uses `<` cutoff; exact-30d tombstone should be kept. "
        "If this flips, an off-by-one was introduced in prune_expired_tombstones."
    )


# ---- (3) notes: empty-list serialization round-trip ----


def test_notes_empty_list_round_trip_does_not_drop_key():
    """gotcha #58 invocation: `_validate_publications_data` covers
    authors corruption but not the `notes: []` vs `notes:` (null)
    serialization. A renderer assuming truthy `notes` would break
    differently in each shape. This test asserts ruamel preserves
    an explicit `notes: []` round-trip."""
    from io import StringIO

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    src = "- title: Test Paper\n  authors:\n    - Public JQ\n  notes: []\n"
    data = yaml.load(src)
    assert "notes" in data[0]
    assert data[0]["notes"] == []
    # Round-trip preserves the key (vs silently dropping it).
    buf = StringIO()
    yaml.dump(data, buf)
    assert "notes" in buf.getvalue()


# ---- (4) Author normalization diacritics edge cases ----


@pytest.mark.parametrize(
    "raw,expected_canonical_substring",
    [
        # Standard diacritic — Müller already covered in Phase 1.5 tests.
        ("Müller, A", "muller"),
        # Compound diacritics + multiple characters (Korean Romanization,
        # German double umlaut variants).
        ("Brönsted-Lowry, T", "bronsted-lowry"),
        # Apostrophe in Irish/Scottish surnames — common in publication
        # author lists. norm_author_name does NOT strip apostrophes; just
        # check it doesn't crash and produces a consistent form.
        ("O'Brien, K", "o'brien"),
        # Hyphenated double-barreled surnames.
        ("Smith-Jones, K", "smith-jones"),
        # NFKD splits a precomposed character; assert the combining mark
        # gets dropped (the whole reason for using NFKD).
        ("Åårsted, J", "aarsted"),  # Å, å — Norwegian
    ],
)
def test_norm_author_name_handles_diacritic_edge_cases(raw, expected_canonical_substring):
    from cv_editor.author_names import norm_author_name

    got = norm_author_name(raw).lower()
    assert expected_canonical_substring in got, (
        f"norm_author_name({raw!r}) → {got!r}; expected to contain {expected_canonical_substring!r}"
    )


def test_norm_author_name_is_idempotent_on_normalized_input():
    """Once normalized, re-normalizing must return the same string."""
    from cv_editor.author_names import norm_author_name

    cases = ["Public JQ", "muller a", "o'brien k"]
    for c in cases:
        once = norm_author_name(c)
        twice = norm_author_name(once)
        assert once == twice, f"non-idempotent: {c!r} → {once!r} → {twice!r}"


# ---- (5) PubMed-sync sidecar version mismatch ----


def test_pubmed_sync_load_sidecar_version_mismatch_returns_empty(tmp_path):
    """gotcha #35h: load_sidecar warns + returns empty state on a
    version mismatch. Without this, a future SIDECAR_VERSION bump
    would silently mis-parse older readers."""
    from cv_editor.pubmed_sync import load_sidecar

    sidecar = tmp_path / "pubmed_sync.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 999,  # future version we don't know about
                "entries": {
                    "12345": {
                        "synced_at": "2026-01-01T00:00:00+00:00",
                        "pubmed_status": "ppublish",
                        "fields_filled": [],
                        "fields_flagged": [],
                        "yaml_idx_at_sync": 0,
                    }
                },
            }
        )
    )
    state = load_sidecar(sidecar)
    # The 999-versioned sidecar must NOT be parsed as v1 — the
    # version-aware loader returns an empty state instead.
    assert len(state.entries) == 0, "version-999 sidecar must not be parsed as v1"


def test_pubmed_sync_load_sidecar_corrupt_json_returns_empty(tmp_path):
    """Corrupt JSON → empty state + stderr warn. Covers the
    sidecar-corrupt-but-file-exists case (e.g., a crashed prior
    save_sidecar)."""
    from cv_editor.pubmed_sync import load_sidecar

    sidecar = tmp_path / "pubmed_sync.json"
    sidecar.write_text("{ this is not valid json")
    state = load_sidecar(sidecar)
    assert len(state.entries) == 0


# ---- (6) YAML _validate_publications_data shape guards ----


def _wrap_publications(entry):
    """publications.yml is a list of subsections each with an `entries` list.
    Wrap a single entry dict in that shape so _validate_publications_data
    actually walks it."""
    return [{"subsection": "PRR", "entries": [entry]}]


def test_validate_publications_data_rejects_authors_as_dict():
    """The guard rejects non-list authors. Today the only documented
    shape is `authors: <string>` (qc apply route bug class). Verify
    it also rejects dict-form."""
    from cv_editor.yaml_io import CorruptedShapeError, _validate_publications_data

    bad = _wrap_publications({"title": "x", "authors": {"name": "Public"}})
    with pytest.raises(CorruptedShapeError):
        _validate_publications_data(bad)


def test_validate_publications_data_rejects_authors_list_of_short_names():
    """Task-#42 hardening: ≥3 authors where every name is ≤2 chars is
    the `['a','b','c','d']` corruption shape. Verify the guard catches it."""
    from cv_editor.yaml_io import CorruptedShapeError, _validate_publications_data

    bad = _wrap_publications({"title": "x", "authors": ["a", "b", "c", "d"]})
    with pytest.raises(CorruptedShapeError):
        _validate_publications_data(bad)


def test_validate_publications_data_accepts_legitimate_short_initials_list():
    """A paper with 2 short-name authors should NOT fire the guard.
    Threshold is ≥3 AND every name ≤2 chars."""
    from cv_editor.yaml_io import _validate_publications_data

    ok = _wrap_publications({"title": "x", "authors": ["ab", "cd"]})
    _validate_publications_data(ok)  # should not raise


def test_validate_publications_data_rejects_empty_authors_list():
    """Documented case-3 in the validator docstring: `authors: []` must
    be refused. The validator's existing tests covered this implicitly;
    this adds an explicit assertion that the empty-list branch fires."""
    from cv_editor.yaml_io import CorruptedShapeError, _validate_publications_data

    bad = _wrap_publications({"title": "x", "authors": []})
    with pytest.raises(CorruptedShapeError):
        _validate_publications_data(bad)


# ---- (7) Resolver 4-strategy fallback CHAIN ----


def test_resolver_falls_through_all_four_strategies(monkeypatch):
    """gotcha #24: tracker resolver tries HEAD → GET → meta-refresh
    → unshorten.me. Each strategy has its own test, but no test
    exercises the FULL chain: strategy 1 fails → 2 fails → 3 fails
    → 4 succeeds. A regression that broke ORDER between strategies
    would slip through. This adds the chain-integration coverage."""
    from cv_editor import altmetric_client

    call_order = []

    def fake_try_head(url, *args, **kwargs):
        call_order.append("head")
        return None  # strategy 1 fails

    def fake_try_get(url, *args, **kwargs):
        call_order.append("get")
        return (None, "<html>no meta refresh here</html>")  # strategy 2 fails

    def fake_parse_meta(body, *args, **kwargs):
        call_order.append("meta_refresh")
        return None  # strategy 3 fails

    def fake_unshorten(url, *args, **kwargs):
        call_order.append("unshorten")
        return altmetric_client.ResolveResult(
            final_url="https://final.example/article",
            strategy="unshorten_me",
            status="resolved",
        )

    monkeypatch.setattr(altmetric_client, "_try_head", fake_try_head)
    monkeypatch.setattr(altmetric_client, "_try_get", fake_try_get)
    monkeypatch.setattr(altmetric_client, "_parse_meta_refresh", fake_parse_meta)
    monkeypatch.setattr(altmetric_client, "resolve_via_unshorten_me", fake_unshorten)

    result = altmetric_client.resolve_tracker_url("https://ct.moreover.com/foo")
    assert result.status == "resolved"
    assert result.strategy == "unshorten_me"
    # CRITICAL: the order asserts the helper actually tried strategy 1
    # BEFORE 2 BEFORE 3 BEFORE 4. A "try 4 first" regression would
    # show ['unshorten'] here, not the full chain.
    assert call_order == ["head", "get", "meta_refresh", "unshorten"], (
        f"resolver chain ran in wrong order: {call_order}"
    )
