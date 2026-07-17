"""Tests for V20 D3 — TrackerCache resolved TTL + /verify_resolved sweep."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _engine_guards import altmetric_required
from cv_editor.altmetric_tracker_cache import (
    RESOLVED_TTL_DAYS,
    CacheEntry,
    TrackerCache,
)
from cv_editor.app import create_app

# ---- cache-level behavior --------------------------------------------


def _make_resolved_entry(*, age_days: int) -> CacheEntry:
    last_attempt = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    return CacheEntry(
        final_url="https://example.com/article",
        strategy="head",
        status="resolved",
        first_seen_ts=last_attempt,
        last_attempt_ts=last_attempt,
        attempt_count=1,
        error=None,
    )


def test_resolved_ttl_default_is_30_days():
    assert RESOLVED_TTL_DAYS == 30


def test_should_attempt_default_skips_resolved(tmp_path):
    """verify=False (default) preserves pre-V20 behavior — resolved
    is permanent at page-render."""
    cache = TrackerCache(tmp_path / "trackers.json")
    cache._entries["http://t.example/abc"] = _make_resolved_entry(age_days=999)
    assert cache.should_attempt("http://t.example/abc") is False


def test_should_attempt_verify_skips_fresh_resolved(tmp_path):
    """verify=True still skips when within the TTL window."""
    cache = TrackerCache(tmp_path / "trackers.json")
    cache._entries["http://t.example/abc"] = _make_resolved_entry(age_days=15)
    assert cache.should_attempt("http://t.example/abc", verify=True) is False


def test_should_attempt_verify_allows_stale_resolved(tmp_path):
    """verify=True re-checks once past the 30-day TTL."""
    cache = TrackerCache(tmp_path / "trackers.json")
    cache._entries["http://t.example/abc"] = _make_resolved_entry(age_days=45)
    assert cache.should_attempt("http://t.example/abc", verify=True) is True


def test_stale_resolved_yields_stale_only(tmp_path):
    cache = TrackerCache(tmp_path / "trackers.json")
    cache._entries["http://t.example/fresh"] = _make_resolved_entry(age_days=5)
    cache._entries["http://t.example/stale"] = _make_resolved_entry(age_days=60)
    # Failed status entries don't show up in stale_resolved
    cache._entries["http://t.example/dead"] = CacheEntry(
        final_url=None,
        strategy=None,
        status="failed_network",
        first_seen_ts="2020-01-01T00:00:00+00:00",
        last_attempt_ts="2020-01-01T00:00:00+00:00",
        attempt_count=1,
    )
    stale = list(cache.stale_resolved())
    urls = [u for u, _ in stale]
    assert urls == ["http://t.example/stale"]


def test_touch_resolved_refreshes_last_attempt(tmp_path):
    cache = TrackerCache(tmp_path / "trackers.json")
    entry = _make_resolved_entry(age_days=60)
    old_ts = entry.last_attempt_ts
    cache._entries["http://t.example/abc"] = entry
    cache.touch_resolved("http://t.example/abc")
    assert cache._entries["http://t.example/abc"].last_attempt_ts != old_ts
    # And it's no longer stale
    assert not list(cache.stale_resolved())


def test_touch_resolved_noop_on_unresolved(tmp_path):
    """Sanity: touching a failed entry doesn't accidentally promote it."""
    cache = TrackerCache(tmp_path / "trackers.json")
    cache._entries["http://t.example/dead"] = CacheEntry(
        final_url=None,
        strategy=None,
        status="failed_network",
        first_seen_ts="2020-01-01T00:00:00+00:00",
        last_attempt_ts="2020-01-01T00:00:00+00:00",
        attempt_count=1,
    )
    cache.touch_resolved("http://t.example/dead")
    assert cache._entries["http://t.example/dead"].status == "failed_network"
    assert cache._entries["http://t.example/dead"].last_attempt_ts == "2020-01-01T00:00:00+00:00"


# ---- route-level sweep -----------------------------------------------


@pytest.fixture
def client_with_stale_cache(tmp_path):
    """Flask test client with a tmp tracker-cache pre-populated with one
    stale + one fresh resolved entry."""
    cache_path = tmp_path / "trackers.json"

    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = cache_path

    # Pre-populate
    cache = TrackerCache(cache_path)
    cache._entries["http://t.example/stale1"] = _make_resolved_entry(age_days=60)
    cache._entries["http://t.example/stale2"] = _make_resolved_entry(age_days=120)
    cache._entries["http://t.example/fresh"] = _make_resolved_entry(age_days=5)
    cache.save()

    return app, cache_path


@altmetric_required
def test_verify_resolved_sweep_touches_alive_urls(client_with_stale_cache):
    """When the HEAD probe says alive, touch_resolved is called."""
    app, cache_path = client_with_stale_cache

    probed: list[str] = []

    def fake_probe(url, *, timeout=10):
        probed.append(url)
        return True  # all alive

    app.config["_VERIFY_HEAD_PROBE"] = fake_probe
    client = app.test_client()
    resp = client.post("/publications/trackers/verify_resolved")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "verifying 2 resolved URL(s)" in body  # only the stale ones
    assert "2 verified, 0 dead" in body

    # Reload cache and confirm stale entries are no longer stale
    after = TrackerCache(cache_path)
    assert list(after.stale_resolved()) == []
    # Fresh entry untouched (timestamps may differ, but it's still fresh)
    fresh_age = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(
            after._entries["http://t.example/fresh"].last_attempt_ts.replace("Z", "+00:00")
        )
    ).days
    assert fresh_age < RESOLVED_TTL_DAYS


@altmetric_required
def test_verify_resolved_sweep_logs_dead_urls(client_with_stale_cache):
    """When the HEAD probe says dead, it surfaces in the SSE log and
    the cache entry is NOT touched (TTL still stale; user fixes YAML)."""
    app, cache_path = client_with_stale_cache

    def fake_probe(url, *, timeout=10):
        return False  # everything dead

    app.config["_VERIFY_HEAD_PROBE"] = fake_probe
    client = app.test_client()
    resp = client.post("/publications/trackers/verify_resolved")
    body = resp.get_data(as_text=True)
    assert "0 verified, 2 dead" in body
    assert "manual fix needed" in body

    # Stale entries are STILL stale (TTL not reset on failure)
    after = TrackerCache(cache_path)
    assert len(list(after.stale_resolved())) == 2


@altmetric_required
def test_verify_resolved_empty_when_no_stale(tmp_path):
    """Sweep over empty stale-resolved set produces a benign log."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "empty.json"
    client = app.test_client()
    resp = client.post("/publications/trackers/verify_resolved")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "verifying 0 resolved URL(s)" in body
    assert "0 verified, 0 dead" in body


# ---- template-render coverage (post-impl review HIGH) ----------------


@altmetric_required
def test_trackers_page_renders_verify_button_when_stale(client_with_stale_cache):
    """The "Verify resolved trackers" button must appear when the
    cache has at least one resolved entry past the TTL.
    """
    app, _ = client_with_stale_cache
    client = app.test_client()
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "data-verify-resolved" in body
    assert "Verify resolved trackers" in body
    # Banner copy reads the stale count.
    assert "resolved tracker URL" in body


@altmetric_required
def test_trackers_page_hides_verify_button_when_no_stale(tmp_path):
    """When stale_resolved_count is 0, the banner + button must be absent.

    Note: the literal string `data-verify-resolved` does still appear
    in the page (inside the BuildConsole.attach JS that handles the
    button click — defensive guard via `document.querySelector`). The
    button + banner copy below are what surface to the user, so check
    for the banner-specific text instead.
    """
    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "fresh.json"
    # Seed cache with ONLY fresh resolved entries.
    cache = TrackerCache(tmp_path / "fresh.json")
    cache._entries["http://t.example/fresh"] = _make_resolved_entry(age_days=5)
    cache.save()
    client = app.test_client()
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Verify resolved trackers" not in body
    # The "≥30 days old" string is unique to the stale-resolved banner;
    # the unresolved-tracker banner uses different copy.
    assert "30 days old" not in body
    # The page-level guard string is in the JS; that's fine.
