"""M2 behavior-fingerprint guard (2026-05-29).

This is the safety net for the M2 app.py decomposition. The 4-critic
pre-impl critique concluded a URL-map snapshot ALONE is necessary but
NOT sufficient: a Jinja filter can silently vanish (templates render
`#`), the CSRF before_request can drop (every cross-origin POST stops
403ing — a security regression), or the `current_section` context
processor can break (nav highlight dies) while the URL map stays
byte-identical.

So this file freezes FOUR things and asserts them against the live app:
  (a) the full URL map (rule, endpoint, methods) == committed baseline
  (b) the 4 template filters are registered
  (c) the CSRF Origin-check before_request is registered AND a
      cross-origin POST 403s (route never runs — safe)
  (d) ~15 golden (request -> status [+ body substring]) tuples, incl.
      the nav-highlight that proves inject_helpers still computes
      `current_section`

Run this (sub-second) after EVERY extraction step. If the decomposition
is behavior-identical, this never changes. The baseline is regenerated
+ committed per extraction commit so a subtle miss shows as a git diff.

NOTE: read-only by construction (GETs + 404s + a CSRF-blocked POST that
aborts before the handler). The publications.yml corruption canary in
conftest.py backs this up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cv_editor import capabilities
from cv_editor.app import create_app

# P5.5: the committed url-map baseline was generated with the BESPOKE template
# active (all capabilities True), so it is a superset. Under a public `modern`
# template the freeze/typography/altmetric routes are NOT registered, so we
# filter the corresponding baseline rows out at compare time when the matching
# capability is off. Mirror the endpoint-name sets from tests/test_p5_capabilities.py
# (imported so the two can't drift). Private tree (all caps True) -> nothing
# filtered -> the comparison is unchanged.
from test_p5_capabilities import (
    _ALTMETRIC_ENDPOINTS,
    _FREEZE_ENDPOINTS,
    _TYPOGRAPHY_ENDPOINTS,
)

PROJ_ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "_url_map_baseline.json"


def _gated_endpoints_off() -> set:
    """Endpoint NAMES whose routes are not registered under the active
    template because the gating capability is off. Empty in the private tree."""
    caps = capabilities.current()
    off = set()
    if not caps.freeze:
        off |= set(_FREEZE_ENDPOINTS)
    if not caps.typography:
        off |= set(_TYPOGRAPHY_ENDPOINTS)
    if not caps.altmetric:
        off |= set(_ALTMETRIC_ENDPOINTS)
    return off


# Filters that templates depend on (a missing one renders "#" or raises).
REQUIRED_FILTERS = {"safe_url", "id_url", "altmetric_url", "pluralize"}

# Golden read-only requests:
#   (method, path, expected_status, substring|None, capname|None).
# Substrings are deliberately minimal + stable. These hit every feature
# surface so a route that fails to register (or a context-processor break)
# shows up immediately. `capname` names the capability gating a route (P5):
# when that capability is off the route is unregistered and the path 404s.
# It's None for the 14 ungated rows. Private tree (all caps True) -> every
# gated row still expects 200 -> unchanged.
GOLDEN = [
    ("GET", "/", 200, "CV Editor", None),
    ("GET", "/healthz", 200, None, None),
    ("GET", "/publications", 200, "is-current", None),  # nav highlight => inject_helpers ok
    ("GET", "/presentations", 200, None, None),
    ("GET", "/meta", 200, None, None),
    ("GET", "/style", 200, None, None),
    ("GET", "/freeze", 200, None, "freeze"),
    ("GET", "/search?q=public", 200, None, None),
    ("GET", "/qc/triage", 200, None, None),
    ("GET", "/pubmed_sync", 200, None, None),
    ("GET", "/citations", 200, None, None),
    ("GET", "/publications/trackers", 200, None, "altmetric"),
    ("GET", "/urls/verify", 200, None, None),
    ("GET", "/no_such_section", 404, None, None),  # require_section abort(404)
    ("GET", "/publications/0", 200, None, None),  # entry_view (real data has idx 0)
    ("GET", "/publications/0/edit", 200, None, None),  # entry_edit
]


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


# ----- (a) URL map identity -------------------------------------------


def _current_url_map(app) -> list:
    return sorted(
        [
            r.rule,
            r.endpoint,
            ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))),
        ]
        for r in app.url_map.iter_rules()
    )


def test_url_map_matches_committed_baseline(app):
    assert BASELINE.exists(), (
        "tests/_url_map_baseline.json is missing. Regenerate it from the "
        "current app and commit it (it is the route-identity ground truth)."
    )
    baseline = json.loads(BASELINE.read_text())
    current = _current_url_map(app)
    # Drop baseline rows for routes gated off under the active template (P5.5).
    off = _gated_endpoints_off()
    baseline = [row for row in baseline if row[1] not in off]
    # Compare as sets of tuples for a readable diff on mismatch.
    b = {tuple(x) for x in baseline}
    c = {tuple(x) for x in current}
    missing = b - c
    added = c - b
    assert not missing and not added, (
        f"URL map drifted from baseline.\n"
        f"  REMOVED (in baseline, not in app): {sorted(missing)}\n"
        f"  ADDED   (in app, not in baseline): {sorted(added)}\n"
        f"If this change is intentional, regenerate tests/_url_map_baseline.json."
    )


# ----- (b) template filters -------------------------------------------


def test_required_jinja_filters_registered(app):
    have = set(app.jinja_env.filters)
    missing = REQUIRED_FILTERS - have
    assert not missing, f"template filters missing after refactor: {missing}"


# ----- (c) CSRF before_request ----------------------------------------


def test_csrf_before_request_is_registered(app):
    names = {fn.__name__ for fns in app.before_request_funcs.values() for fn in fns}
    assert "_csrf_origin_check" in names, (
        "the CSRF Origin-check before_request vanished — cross-origin POSTs "
        "would no longer 403 (security regression)."
    )


def test_cross_origin_post_is_blocked_403():
    # Non-TESTING app so the CSRF check is active. The handler never runs
    # (abort(403) fires in before_request), so /quit does NOT shut down.
    app = create_app()  # TESTING unset => CSRF active
    client = app.test_client()
    resp = client.post(
        "/quit",
        headers={"Origin": "http://evil.example.com"},
        data={},
    )
    assert resp.status_code == 403


# ----- (d) golden request/response tuples -----------------------------


@pytest.mark.parametrize("method,path,status,substr,capname", GOLDEN)
def test_golden_responses(app, method, path, status, substr, capname):
    client = app.test_client()
    resp = client.open(path, method=method)
    # A capability-gated route 404s when its capability is off (P5.5). All caps
    # True in the private tree -> expected_status == status -> unchanged.
    expected_status = 404 if capname and not getattr(capabilities.current(), capname) else status
    assert resp.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {resp.status_code}"
    )
    if substr is not None and expected_status == status:
        body = resp.get_data(as_text=True)
        assert substr in body, f"{method} {path}: missing expected substring {substr!r}"
