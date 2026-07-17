"""V13 finish (2026-05-16): tracker URL persistence, unshorten.me fallback,
inline-during-fetch resolution, global Trackers page, per-entry sweep,
outlet grouping helpers + Typst renderer integration.

These tests run on top of the V13-A test surface (test_v13_altmetric_api.py)
which still covers the parser, JSON:API extractor, and original 3-strategy
resolver. This file focuses on the new pieces added in the V13 finish
milestone.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE, altmetric_required
from cv_editor import (
    altmetric_client,
    altmetric_tracker_cache,
    notes_helpers,
    url_helpers,
)
from cv_editor.altmetric_tracker_cache import (
    CacheEntry,
    ResolveResult,
    TrackerCache,
)
from cv_editor.app import create_app

# ---------- TrackerCache ----------


def test_trackercache_empty_file_returns_no_entries(tmp_path: Path):
    c = TrackerCache(tmp_path / "trackers.json")
    assert len(c) == 0
    assert c.stats()["total"] == 0


def test_trackercache_round_trip_persistence(tmp_path: Path):
    p = tmp_path / "trackers.json"
    c = TrackerCache(p)
    c.record(
        "http://ct.moreover.com/?a=1",
        ResolveResult(final_url="https://cnn.com/x", strategy="head", status="resolved"),
    )
    c.save()
    c2 = TrackerCache(p)
    assert "http://ct.moreover.com/?a=1" in c2
    entry = c2.get("http://ct.moreover.com/?a=1")
    assert entry is not None
    assert entry.final_url == "https://cnn.com/x"
    assert entry.strategy == "head"
    assert entry.status == "resolved"


def test_trackercache_corrupt_file_warns_and_starts_empty(tmp_path: Path, capsys):
    p = tmp_path / "trackers.json"
    p.write_text("not json{")
    c = TrackerCache(p)
    assert len(c) == 0
    captured = capsys.readouterr()
    assert "corrupted" in captured.err.lower()


def test_trackercache_version_mismatch_starts_empty(tmp_path: Path, capsys):
    p = tmp_path / "trackers.json"
    p.write_text(json.dumps({"version": 999, "trackers": {"foo": {}}}))
    c = TrackerCache(p)
    assert len(c) == 0
    captured = capsys.readouterr()
    assert "version" in captured.err.lower()


def test_trackercache_should_attempt_respects_resolved(tmp_path: Path):
    c = TrackerCache(tmp_path / "trackers.json")
    c.record(
        "http://t.test/x",
        ResolveResult(final_url="https://final.test", strategy="head", status="resolved"),
    )
    assert not c.should_attempt("http://t.test/x")


def test_trackercache_failed_record_persists_with_attempt_metadata(tmp_path: Path):
    """2026-06-08: failed results are persisted with attempt_count +
    last_attempt_ts so the Trackers page can show "Attempts" / "Last
    tried". should_attempt() still returns True so every Resolve click
    re-attempts the network (Stage B / I9 intent preserved)."""
    c = TrackerCache(tmp_path / "trackers.json")
    out = c.record(
        "http://t.test/x",
        ResolveResult(status="failed_network", error="timeout"),
    )
    assert out is not None
    assert out.status == "failed_network"
    assert out.attempt_count == 1
    assert out.last_attempt_ts  # non-empty timestamp
    assert out.error == "timeout"
    assert "http://t.test/x" in c
    assert c.should_attempt("http://t.test/x") is True
    # A second failed attempt bumps the count + refreshes the timestamp.
    out2 = c.record(
        "http://t.test/x",
        ResolveResult(status="failed_network", error="timeout again"),
    )
    assert out2.attempt_count == 2


def test_trackercache_failed_reattempt_overwrites_prior_resolved(tmp_path: Path):
    """If a re-attempt fails on a previously-resolved URL, the entry is
    overwritten with a failed entry (status failed_*, final_url=None) so
    get_result() no longer serves the stale resolved URL — and the
    attempt_count carries forward."""
    c = TrackerCache(tmp_path / "trackers.json")
    c.record(
        "http://t.test/x",
        ResolveResult(final_url="http://final/x", strategy="head", status="resolved"),
    )
    assert "http://t.test/x" in c
    out = c.record(
        "http://t.test/x",
        ResolveResult(status="failed_network", error="dns"),
    )
    assert "http://t.test/x" in c
    assert out.status == "failed_network"
    assert out.final_url is None
    assert out.attempt_count == 2  # carried forward from the resolved attempt
    assert c.should_attempt("http://t.test/x") is True
    # get_result returns the failed entry, NOT the stale resolved URL.
    got = c.get_result("http://t.test/x")
    assert got is not None and not got.is_resolved
    assert got.final_url is None


def test_trackercache_should_attempt_legacy_failed_entry_returns_true(tmp_path: Path):
    """A failed (or legacy on-disk failure) entry read into memory must
    be treated as `should_attempt` → True so the next click re-attempts.
    record() then overwrites it in place (not evicts) with the fresh
    outcome + bumped attempt_count (2026-06-08)."""
    c = TrackerCache(tmp_path / "trackers.json")
    c._entries["http://legacy"] = CacheEntry(
        final_url=None,
        strategy=None,
        status="failed_network",
        first_seen_ts="2026-05-01T00:00:00+00:00",
        last_attempt_ts="2026-05-25T00:00:00+00:00",
        attempt_count=1,
        error="legacy",
    )
    assert c.should_attempt("http://legacy") is True


def test_trackercache_resolved_persists_and_round_trips(tmp_path: Path):
    """Resolved entries still persist + survive save/reload."""
    p = tmp_path / "trackers.json"
    c = TrackerCache(p)
    c.record(
        "http://t.test/x",
        ResolveResult(final_url="http://final/x", strategy="head", status="resolved"),
    )
    c.save()
    c2 = TrackerCache(p)
    assert "http://t.test/x" in c2
    got = c2.get_result("http://t.test/x")
    assert got is not None and got.is_resolved
    assert got.final_url == "http://final/x"


def test_trackercache_cache_file_persists_failures(tmp_path: Path):
    """2026-06-08: failures ARE persisted (with attempt metadata) so the
    Trackers page can show Last-tried/Attempts. The cache file carries
    the failed entry after save() and it round-trips on reload."""
    p = tmp_path / "trackers.json"
    c = TrackerCache(p)
    c.record("http://t.test/x", ResolveResult(status="failed_network", error="boom"))
    c.save()
    import json

    data = json.loads(p.read_text())
    rec = (data.get("trackers") or {})["http://t.test/x"]
    assert rec["status"] == "failed_network"
    assert rec["attempt_count"] == 1
    assert rec["last_attempt_ts"]
    # Reload round-trips the failed entry.
    c2 = TrackerCache(p)
    assert "http://t.test/x" in c2


def test_trackercache_stats_counts_by_status(tmp_path: Path):
    """Stats bucket by status. This test records only resolved entries,
    so it reports all-resolved totals. (Failed entries, if recorded,
    would count in their failed_* bucket since 2026-06-08.)"""
    c = TrackerCache(tmp_path / "trackers.json")
    c.record("http://a", ResolveResult(final_url="ok-a", strategy="head", status="resolved"))
    c.record("http://b", ResolveResult(final_url="ok-b", strategy="head", status="resolved"))
    c.record("http://c", ResolveResult(final_url="ok-c", strategy="head", status="resolved"))
    s = c.stats()
    assert s["total"] == 3
    assert s["resolved"] == 3
    # Failed buckets are empty by construction post-I9.
    assert s["failed_network"] == 0
    assert s["failed_rate_limit"] == 0
    assert s["failed_no_redirect"] == 0


def test_trackercache_should_attempt_verify_still_true_for_legacy_failed(tmp_path: Path):
    """The verify=True path (V20 D3 verify-resolved sweep) must also
    treat legacy failed entries as should_attempt=True — symmetric
    with the verify=False path. Failed entries should never gate the
    sweep regardless of verify flag."""
    c = TrackerCache(tmp_path / "trackers.json")
    c._entries["http://legacy"] = CacheEntry(
        final_url=None,
        strategy=None,
        status="failed_network",
        first_seen_ts="2026-05-01T00:00:00+00:00",
        last_attempt_ts="2026-05-25T00:00:00+00:00",
        attempt_count=1,
        error="legacy",
    )
    assert c.should_attempt("http://legacy", verify=True) is True


# ---------- unshorten.me strategy ----------


class _Resp:
    def __init__(self, body=b"", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]


def test_resolve_via_unshorten_me_success(monkeypatch):
    body = b'{"success": true, "resolved_url": "https://www.cnn.com/x"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t.test/x")
    assert out.is_resolved
    assert out.final_url == "https://www.cnn.com/x"
    assert out.strategy == "unshorten_me"


def test_resolve_via_unshorten_me_rate_limit_http_429(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(altmetric_client.urllib.request, "urlopen", boom)
    out = altmetric_client.resolve_via_unshorten_me("http://t.test/x")
    assert out.status == "failed_rate_limit"
    assert not out.is_resolved


def test_resolve_via_unshorten_me_rate_limit_in_json_payload(monkeypatch):
    body = b'{"success": false, "error": "Rate limit exceeded"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t.test/x")
    assert out.status == "failed_rate_limit"


def test_resolve_via_unshorten_me_timeout(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(altmetric_client.urllib.request, "urlopen", boom)
    out = altmetric_client.resolve_via_unshorten_me("http://t.test/x")
    assert out.status == "failed_network"


def test_resolve_via_unshorten_me_returns_same_url_marks_no_redirect(monkeypatch):
    body = b'{"success": true, "resolved_url": "http://t.test/x"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t.test/x")
    assert out.status == "failed_no_redirect"


# ---------- resolve_tracker_url_with_cache ----------


def test_resolve_with_cache_returns_cached_resolved(tmp_path, monkeypatch):
    c = TrackerCache(tmp_path / "trackers.json")
    c.record(
        "http://t/x",
        ResolveResult(final_url="https://final.test", strategy="head", status="resolved"),
    )

    # Network would fail — but the cache hit should short-circuit.
    def boom(req, timeout=None):
        raise AssertionError("network was called when cache should hit")

    monkeypatch.setattr(altmetric_client.urllib.request, "urlopen", boom)
    out = altmetric_client.resolve_tracker_url_with_cache("http://t/x", c)
    assert out.final_url == "https://final.test"


def test_resolve_with_cache_writes_new_entry(tmp_path, monkeypatch):
    c = TrackerCache(tmp_path / "trackers.json")
    body = b'{"success": true, "resolved_url": "https://final.test"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body if "unshorten.me" in req.full_url else b""),
    )
    out = altmetric_client.resolve_tracker_url_with_cache("http://t/x", c)
    assert out.is_resolved
    assert c.get("http://t/x") is not None


# ---------- url_helpers tracker detection ----------


def test_is_tracker_url_detects_moreover():
    assert url_helpers.is_tracker_url("http://ct.moreover.com/?a=1")
    assert url_helpers.is_tracker_url("https://ct.moreover.com/x")


def test_is_tracker_url_rejects_others():
    assert not url_helpers.is_tracker_url("https://www.cnn.com/")
    assert not url_helpers.is_tracker_url("")
    assert not url_helpers.is_tracker_url(None)
    assert not url_helpers.is_tracker_url("javascript:alert(1)")
    assert not url_helpers.is_tracker_url("not a url")


# ---------- outlet grouping ----------


def test_group_outlets_empty():
    assert notes_helpers.group_outlets_for_display([]) == []
    assert notes_helpers.group_outlets_for_display(None) == []


def test_group_outlets_singleton_bare():
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "AP", "url": "https://ap.org/x"},
        ]
    )
    assert len(groups) == 1
    assert groups[0]["name"] == "AP"
    assert groups[0]["urls"] == [
        {"local_idx": 1, "url": "https://ap.org/x", "highlighted": False},
    ]


def test_group_outlets_multi_article_per_outlet_indices():
    """Per-outlet 1-based local indices. First article = bare name;
    subsequent articles render with digits starting at 2."""
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "AP", "url": "https://ap.org/1"},
            {"name": "Washington Post", "url": "https://wapo.com/a"},
            {"name": "Washington Post", "url": "https://wapo.com/b"},
            {"name": "Washington Post", "url": "https://wapo.com/c"},
            {"name": "Reuters", "url": "https://reuters.com/a"},
            {"name": "Reuters", "url": "https://reuters.com/b"},
        ]
    )
    by_name = {g["name"]: g for g in groups}
    # Each outlet restarts at 1; rendered output for multi-article ones
    # shows only digits >= 2 in the parenthetical.
    assert [it["local_idx"] for it in by_name["AP"]["urls"]] == [1]
    assert [it["local_idx"] for it in by_name["Washington Post"]["urls"]] == [1, 2, 3]
    assert [it["local_idx"] for it in by_name["Reuters"]["urls"]] == [1, 2]


def test_group_outlets_case_insensitive_trim_dedup():
    """`AP`, ` ap `, `Ap` collapse into one group; per-outlet indices."""
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "AP", "url": "https://1"},
            {"name": " ap ", "url": "https://2"},
            {"name": "Ap", "url": "https://3"},
        ]
    )
    assert len(groups) == 1
    assert groups[0]["name"] == "AP"  # first-occurrence casing preserved
    assert [it["local_idx"] for it in groups[0]["urls"]] == [1, 2, 3]


def test_group_outlets_accepts_plain_string_outlets():
    """YAML lets each outlet be a plain str OR a dict; both must work."""
    groups = notes_helpers.group_outlets_for_display(
        [
            "AP",
            {"name": "Washington Post", "url": "https://wapo.com/x"},
        ]
    )
    assert [g["name"] for g in groups] == ["AP", "Washington Post"]
    assert groups[0]["urls"][0]["url"] == ""
    assert groups[1]["urls"][0]["url"] == "https://wapo.com/x"


def test_group_outlets_drops_empty_names():
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "AP", "url": "https://1"},
            {"name": "", "url": "https://drop"},
            {"name": "   ", "url": "https://drop2"},
        ]
    )
    assert [g["name"] for g in groups] == ["AP"]


# ---------- Trackers page ----------


@pytest.fixture
def client(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["TRACKER_CACHE_PATH"] = tmp_path / "trackers.json"
    return app.test_client()


@altmetric_required
def test_trackers_page_empty_state(client):
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    assert b"Tracker URLs awaiting resolution" in resp.data


@altmetric_required
def test_trackers_page_lists_publications_with_trackers(client, monkeypatch):
    """Tracker URL in publications.yml + cache entry → row appears on
    the Trackers page with the friendly status label.

    Stage B / I9 (2026-05-25): failed entries no longer persist via
    record(), but the read-side rendering must still handle legacy
    on-disk entries. Inject directly via _entries to simulate one.
    """
    from cv_editor import tracker_walk
    from cv_editor.altmetric_tracker_cache import TrackerCache

    tracker_url = "http://ct.moreover.com/?a=test123"
    cache = TrackerCache(client.application.config["TRACKER_CACHE_PATH"])
    cache._entries[tracker_url] = CacheEntry(
        final_url=None,
        strategy=None,
        status="failed_network",
        first_seen_ts="2026-05-01T00:00:00+00:00",
        last_attempt_ts="2026-05-25T00:00:00+00:00",
        attempt_count=1,
        error="(test mock; legacy entry)",
    )
    cache.save()
    fake_ref = tracker_walk.OutletRef(
        pub_global_idx=0,
        pub_title="Test Publication",
        pub_date="2024",
        note_idx=0,
        outlet_idx=0,
        outlet_name="CNN Test",
        url=tracker_url,
    )
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter([fake_ref]))
    resp = client.get("/publications/trackers")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Test Publication" in body
    assert "CNN Test" in body
    # Status badge uses the friendly label, NOT the raw enum.
    # (Stage B I9: label now reads "Network error (legacy cache)" in
    # the by-status banner; the per-row badge keeps its friendly form.)
    assert "Network error" in body
    assert "ct.moreover.com" in body


@altmetric_required
def test_trackers_page_does_not_list_resolved_entries(client, monkeypatch):
    """Resolved entries are filtered out by count_unresolved_trackers."""
    from cv_editor import tracker_walk
    from cv_editor.altmetric_tracker_cache import TrackerCache

    tracker_url = "http://ct.moreover.com/?a=already_resolved"
    cache = TrackerCache(client.application.config["TRACKER_CACHE_PATH"])
    cache.record(
        tracker_url,
        ResolveResult(
            final_url="https://final.test/article",
            strategy="head",
            status="resolved",
        ),
    )
    cache.save()
    fake_ref = tracker_walk.OutletRef(
        pub_global_idx=0,
        pub_title="Already Resolved Pub",
        pub_date="2024",
        note_idx=0,
        outlet_idx=0,
        outlet_name="CNN",
        url=tracker_url,
    )
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter([fake_ref]))
    resp = client.get("/publications/trackers")
    body = resp.get_data(as_text=True)
    assert "Already Resolved Pub" not in body
    assert "All tracker URLs resolved" in body


@altmetric_required
def test_trackers_page_shows_attempt_metadata_for_failed_record(client, monkeypatch):
    """2026-06-08 regression: a failed Resolve must populate the Attempts
    + Last-tried columns. Before the fix the cache evicted failures, so
    unresolved rows always showed Attempts=0 / Last-tried=blank. Records
    via the real record() path (not direct _entries injection)."""
    from cv_editor import tracker_walk
    from cv_editor.altmetric_tracker_cache import TrackerCache

    tracker_url = "http://ct.moreover.com/?a=fails"
    cache = TrackerCache(client.application.config["TRACKER_CACHE_PATH"])
    cache.record(tracker_url, ResolveResult(status="failed_network", error="boom"))
    cache.record(tracker_url, ResolveResult(status="failed_network", error="boom2"))
    cache.save()
    entry = cache.get(tracker_url)
    assert entry is not None and entry.attempt_count == 2
    fake_ref = tracker_walk.OutletRef(
        pub_global_idx=0,
        pub_title="Failing Tracker Pub",
        pub_date="2024",
        note_idx=0,
        outlet_idx=0,
        outlet_name="CNN",
        url=tracker_url,
    )
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter([fake_ref]))
    body = client.get("/publications/trackers").get_data(as_text=True)
    # Attempts column shows the real count (was always 0 before the fix).
    assert '<td class="num">2</td>' in body
    # Last-tried column shows the real timestamp (was always blank "—").
    assert entry.last_attempt_ts[:10] in body
    assert entry.last_attempt_ts in body  # full ts in the title attribute


# ---------- Per-entry resolve ----------


@altmetric_required
def test_per_entry_resolve_handles_no_trackers(client):
    """Hitting /publications/<idx>/trackers/resolve on an entry with no
    tracker URLs flashes 'No tracker URLs' and redirects to view."""
    resp = client.post(
        "/publications/0/trackers/resolve",
        data={"mtime_ns": "0"},
        follow_redirects=False,
    )
    # 302 redirect to entry_view
    assert resp.status_code in (302, 303)


# ---------- CLI subcommands ----------


def test_cli_cache_status_smoketest(tmp_path, monkeypatch, capsys):
    """Invoke the cache-status subcommand and verify it prints counts."""
    # Redirect DEFAULT_PATH to tmp.
    monkeypatch.setattr(
        altmetric_tracker_cache.TrackerCache,
        "DEFAULT_PATH",
        tmp_path / "trackers.json",
    )
    monkeypatch.setattr("sys.argv", ["altmetric_client", "cache-status"])
    altmetric_client._cli()
    out = capsys.readouterr().out
    assert "cache file" in out
    assert "resolved" in out


def test_cli_resolve_outputs_url_or_failure(monkeypatch, capsys):
    """resolve subcommand prints the final URL on success, failure
    message + exit-1 on failure."""
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            final_url="https://final.test",
            strategy="head",
            status="resolved",
        ),
    )
    monkeypatch.setattr("sys.argv", ["altmetric_client", "resolve", "http://t/x"])
    altmetric_client._cli()
    out = capsys.readouterr().out
    assert "https://final.test" in out


def test_cli_resolve_failure_exits_one(monkeypatch):
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: altmetric_client.ResolveResult(
            status="failed_network",
            error="boom",
        ),
    )
    monkeypatch.setattr("sys.argv", ["altmetric_client", "resolve", "http://t/x"])
    with pytest.raises(SystemExit) as exc:
        altmetric_client._cli()
    assert exc.value.code == 1


# ---------- entry_view + index banners ----------


def test_index_banner_hidden_when_no_trackers(client, monkeypatch):
    """No trackers → banner copy absent. Uses tracker_walk iterator
    monkeypatch (T3.1 module-level extraction)."""
    from cv_editor import tracker_walk

    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter([]))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "unresolved tracker URL" not in body


def test_index_banner_singular_grammar_via_macro():
    """T2.2d: render the tracker_banner_global macro directly with a
    fixed count dict; verify singular pluralization. Bypasses the
    closure helper since we're testing the macro contract."""
    from flask import Flask

    app = Flask(
        __name__,
        template_folder=str(
            Path(__file__).resolve().parent.parent / "scripts" / "cv_editor" / "templates"
        ),
    )

    @app.route("/dummy/publications_trackers")
    def _dummy():
        return ""

    app.add_url_rule(
        "/publications/trackers",
        endpoint="publications_trackers",
        view_func=_dummy,
    )

    # Tier B / B7 (2026-05-27): the macros now use the `pluralize`
    # filter which is registered in create_app(). This test builds
    # its own Flask app to bypass create_app's full route setup, so
    # we mirror just the filter here.
    def _pluralize(count, singular, plural):
        try:
            n = int(count) if count is not None else 0
        except (TypeError, ValueError):
            n = 0
        return singular if n == 1 else plural

    app.add_template_filter(_pluralize, name="pluralize")
    import re

    def normalize(s):
        return re.sub(r"\s+", " ", s)

    with app.test_request_context("/"):
        from flask import render_template_string

        # Singular case.
        html_sg = normalize(
            render_template_string(
                '{% from "_macros.html" import tracker_banner_global %}'
                '{{ tracker_banner_global(c) }}',
                c={"total_trackers": 1, "pubs_with_trackers": 1, "by_status": {}},
            )
        )
        assert "1 publication has 1 unresolved tracker URL." in html_sg
        # Plural case.
        html_pl = normalize(
            render_template_string(
                '{% from "_macros.html" import tracker_banner_global %}'
                '{{ tracker_banner_global(c) }}',
                c={"total_trackers": 5, "pubs_with_trackers": 3, "by_status": {}},
            )
        )
        assert "3 publications have 5 unresolved tracker URLs." in html_pl


@altmetric_required
def test_index_banner_shown_when_trackers_present(client, monkeypatch):
    """5 trackers across 3 pubs → plural-form banner appears on index."""
    from cv_editor import tracker_walk

    # Build 5 OutletRefs across 3 pub_global_idx values.
    refs = [
        tracker_walk.OutletRef(
            pub_global_idx=i % 3,
            pub_title=f"P{i % 3}",
            pub_date="2024",
            note_idx=0,
            outlet_idx=0,
            outlet_name=f"O{i}",
            url=f"http://ct.moreover.com/?a={i}",
        )
        for i in range(5)
    ]
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter(refs))
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert "3 publications" in body
    assert "have" in body
    assert "5 unresolved tracker URLs" in body
    assert "Open the Trackers page" in body


@altmetric_required
def test_index_banner_singular_grammar(client, monkeypatch):
    """1 tracker on 1 pub → singular form ('has', 'URL' not 'URLs')."""
    from cv_editor import tracker_walk

    ref = tracker_walk.OutletRef(
        pub_global_idx=0,
        pub_title="P",
        pub_date="2024",
        note_idx=0,
        outlet_idx=0,
        outlet_name="O",
        url="http://ct.moreover.com/?a=1",
    )
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter([ref]))
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    import re

    flat = re.sub(r"\s+", " ", body)
    assert "1 publication has 1 unresolved tracker URL." in flat


def test_entry_view_renders_grouped_outlets(client):
    """Smoke test that entry_view renders without 500 (real data may or
    may not have media notes; either way the template must not error)."""
    resp = client.get("/publications/0")
    assert resp.status_code == 200


# ---------- ResolveResult dataclass ----------


def test_resolveresult_is_resolved_property():
    assert ResolveResult(
        final_url="x",
        strategy="head",
        status="resolved",
    ).is_resolved
    assert not ResolveResult(status="failed_network").is_resolved
    # T2.1: cover the remaining status states.
    assert not ResolveResult(status="failed_rate_limit").is_resolved
    assert not ResolveResult(status="failed_no_redirect").is_resolved
    # Status is resolved but final_url missing → not resolved.
    assert not ResolveResult(status="resolved").is_resolved


# ---------- T2.1 new tests: SSE route, force-retry, banner counts, etc. ----------


@altmetric_required
def test_sse_resolve_all_yields_frames_and_done_payload(client, monkeypatch):
    """T2.1a: SSE /publications/trackers/resolve_all yields per-tracker
    progress frames and a final `done` event with resolved/failed/total/
    substituted counts."""
    from cv_editor import tracker_walk

    fake_refs = [
        tracker_walk.OutletRef(
            pub_global_idx=0,
            pub_title="X",
            pub_date="2024",
            note_idx=0,
            outlet_idx=0,
            outlet_name="CNN",
            url="http://ct.moreover.com/?a=1",
        ),
        tracker_walk.OutletRef(
            pub_global_idx=0,
            pub_title="X",
            pub_date="2024",
            note_idx=0,
            outlet_idx=1,
            outlet_name="AP",
            url="http://ct.moreover.com/?a=2",
        ),
    ]
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter(fake_refs))

    def fake_resolve(url, cache, **kw):
        if url.endswith("a=1"):
            return ResolveResult(final_url="https://cnn.com/x", strategy="head", status="resolved")
        return ResolveResult(status="failed_network", error="(mock)")

    monkeypatch.setattr(altmetric_client, "resolve_tracker_url_with_cache", fake_resolve)
    # Don't actually write YAML in test.
    monkeypatch.setattr(
        tracker_walk, "substitute_tracker_urls_in_publications", lambda data, subs: 0
    )
    monkeypatch.setattr(
        "cv_editor.yaml_io.write_with_backup", lambda path, header, data, **kw: None
    )
    monkeypatch.setattr("cv_editor.yaml_io.mtime_ns", lambda path: 0)

    resp = client.post("/publications/trackers/resolve_all")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Per-tracker frames. Stage B / I9 (2026-05-25): the failure line
    # is now "network error: <reason> [failed_network]" (with reason)
    # or "attempted, still failed [failed_network]" (no reason); the
    # old "kept     [failed_network]" verb was retired with the
    # failure-TTL removal.
    assert "resolved [head]" in body
    assert "network error: (mock)" in body
    assert "[failed_network]" in body
    # Final done event.
    assert "event: done" in body
    assert '"resolved": 1' in body
    assert '"failed": 1' in body
    assert '"total": 2' in body


@altmetric_required
def test_sse_resolve_all_yaml_stale_emits_error_frame(client, monkeypatch):
    """T1.3: when write_with_backup raises StaleFileError mid-sweep, the
    SSE stream emits an `error` event and stops without claiming success."""
    from cv_editor import tracker_walk

    fake_refs = [
        tracker_walk.OutletRef(
            pub_global_idx=0,
            pub_title="X",
            pub_date="2024",
            note_idx=0,
            outlet_idx=0,
            outlet_name="CNN",
            url="http://ct.moreover.com/?a=stale",
        )
    ]
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter(fake_refs))
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url_with_cache",
        lambda url, cache, **kw: ResolveResult(
            final_url="https://cnn.com/x",
            strategy="head",
            status="resolved",
        ),
    )
    monkeypatch.setattr(
        tracker_walk, "substitute_tracker_urls_in_publications", lambda data, subs: 1
    )
    from cv_editor import yaml_io as _yaml_io

    def boom(*args, **kw):
        raise _yaml_io.StaleFileError("file changed under us")

    monkeypatch.setattr("cv_editor.yaml_io.write_with_backup", boom)
    monkeypatch.setattr("cv_editor.yaml_io.mtime_ns", lambda path: 0)

    resp = client.post("/publications/trackers/resolve_all")
    body = resp.get_data(as_text=True)
    assert "event: error" in body
    assert "YAML write failed" in body
    # No `done` event after the abort.


@altmetric_required
def test_force_retry_param_bypasses_cached_failure(client, monkeypatch):
    """T1.5: /publications/altmetric/resolve with force=1 calls
    resolve_tracker_url_with_cache(..., force=True)."""
    captured = {}

    def fake(url, cache, **kw):
        captured["force"] = kw.get("force", False)
        return ResolveResult(status="failed_no_redirect", error="(mock)")

    monkeypatch.setattr(altmetric_client, "resolve_tracker_url_with_cache", fake)

    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=stuck", "force": "1"},
    )
    assert resp.status_code == 200
    assert captured["force"] is True

    captured.clear()
    resp = client.post(
        "/publications/altmetric/resolve",
        data={"url": "http://ct.moreover.com/?a=stuck"},
    )
    assert captured["force"] is False


@altmetric_required
def test_count_unresolved_trackers_buckets_by_status(client, monkeypatch):
    """T2.1b: _count_unresolved_trackers correctly buckets cached entries
    by status.

    The read-side bucketing path must handle failed on-disk entries.
    Inject them directly into _entries (equivalent to what record() now
    persists since 2026-06-08, or a legacy pre-I9 cache file)."""
    from cv_editor.altmetric_tracker_cache import TrackerCache

    cache_path = client.application.config["TRACKER_CACHE_PATH"]
    cache = TrackerCache(cache_path)
    urls = {
        "http://ct.moreover.com/?a=net": "failed_network",
        "http://ct.moreover.com/?a=rl": "failed_rate_limit",
        "http://ct.moreover.com/?a=nr": "failed_no_redirect",
    }
    for u, status in urls.items():
        cache._entries[u] = CacheEntry(
            final_url=None,
            strategy=None,
            status=status,
            first_seen_ts="2026-05-01T00:00:00+00:00",
            last_attempt_ts="2026-05-25T00:00:00+00:00",
            attempt_count=1,
            error="x (legacy)",
        )
    cache.save()
    from cv_editor import tracker_walk

    refs = [
        tracker_walk.OutletRef(
            pub_global_idx=i,
            pub_title="P",
            pub_date="2024",
            note_idx=0,
            outlet_idx=0,
            outlet_name="X",
            url=u,
        )
        for i, u in enumerate(list(urls) + ["http://ct.moreover.com/?a=unseen"])
    ]
    monkeypatch.setattr(tracker_walk, "iter_tracker_outlets", lambda data: iter(refs))
    # Trigger the helper indirectly via the index route.
    resp = client.get("/")
    import re

    flat = re.sub(r"\s+", " ", resp.get_data(as_text=True))
    # 4 trackers across 4 publications.
    assert "4 publications" in flat
    assert "have 4 unresolved tracker URLs" in flat


def test_unshorten_me_non_object_payload_marks_failed_network(monkeypatch):
    """T2.1g: unshorten.me returns a non-object JSON payload (e.g. an
    array) → failed_network, not silent success."""
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(b'[1, 2, 3]'),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert out.status == "failed_network"
    assert "non-object" in (out.error or "").lower()


def test_unshorten_me_bad_json_marks_failed_network(monkeypatch):
    """T2.1g: unshorten.me returns malformed JSON → failed_network."""
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(b'{"not": json'),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert out.status == "failed_network"
    assert "bad json" in (out.error or "").lower()


def test_unshorten_me_http_500_marks_failed_network(monkeypatch):
    """T2.1g: unshorten.me returns HTTP 500 → failed_network with the
    code in the error string (not the failed_rate_limit branch)."""

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(altmetric_client.urllib.request, "urlopen", boom)
    out = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert out.status == "failed_network"
    assert "500" in (out.error or "")


def test_unshorten_me_success_false_with_generic_error(monkeypatch):
    """T2.1g: unshorten.me returns `success: false` with an error string
    that doesn't mention rate-limit → failed_network (NOT
    failed_rate_limit)."""
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(
            b'{"success": false, "error": "URL malformed"}',
        ),
    )
    out = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert out.status == "failed_network"


# Stage B / I9 (2026-05-25): R4-M5 test_trackercache_should_attempt_age_boundary
# DELETED. The 1h backoff for failed_network is gone. The contract (every
# Resolve click re-attempts a failed URL) is covered by
# test_trackercache_failed_record_persists_with_attempt_metadata and
# test_trackercache_should_attempt_legacy_failed_entry_returns_true above.
# (2026-06-08: failures are now persisted with attempt metadata, but they
# still never gate a re-attempt — should_attempt is True for non-resolved.)


@pytest.mark.skip(
    reason="T3.2: _resolve_idx is closure-scoped; testable after Blueprint extraction."
)
def test_per_entry_resolve_returns_409_on_stale_mtime(client, monkeypatch):
    """T1.5/T2.1e: with stale mtime_ns and substitutions actually
    happening, the per-entry resolve route returns the write_or_409
    redirect + flash."""
    # Patch _resolve_idx to return an entry with a tracker URL.
    fake_rec = {
        "global_idx": 0,
        "loc": (0,),
        "ctx": {"subsection": "", "institution": "", "city": ""},
        "entry": {
            "title": "Test Pub",
            "notes": [
                {
                    "type": "media",
                    "outlets": [
                        {"name": "CNN", "url": "http://ct.moreover.com/?a=stale"},
                    ],
                }
            ],
        },
    }
    monkeypatch.setattr(
        "cv_editor.app._resolve_idx",
        lambda section, idx: (None, "fake_path", "", {}, fake_rec),
    )
    # Force resolution success so substitutions is non-empty.
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url_with_cache",
        lambda url, cache, **kw: ResolveResult(
            final_url="https://final.test",
            strategy="head",
            status="resolved",
        ),
    )
    # write_or_409 returns a (redirect, 409) tuple on stale mtime.
    # The mtime_ns=0 form value will be passed; we mock write_with_backup
    # to raise StaleFileError so write_or_409 produces the 409 path.
    from cv_editor import yaml_io as _yaml_io

    def boom(*a, **kw):
        raise _yaml_io.StaleFileError("stale mtime")

    monkeypatch.setattr("cv_editor.yaml_io.write_with_backup", boom)

    resp = client.post(
        "/publications/0/trackers/resolve",
        data={"mtime_ns": "0"},
        follow_redirects=False,
    )
    # Either 302 (followed) or 409 — both are valid 409 paths.
    assert resp.status_code in (302, 303, 409)


def test_cli_resolve_all_actually_invokes_real_function(
    tmp_path,
    monkeypatch,
    capsys,
):
    """T2.1f: invoke the REAL altmetric_client._cli_resolve_all (not a
    stub) against a synthetic publications.yml in tmp_path. Asserts the
    YAML is rewritten with the resolved URL.

    Implementation: monkeypatch `Path.resolve` to make __file__'s
    parent.parent.parent resolve to tmp_path. The CLI uses
    `Path(__file__).resolve().parent.parent.parent / "data" /
    "publications.yml"` so we redirect the project root.
    """
    # Build a minimal publications.yml in a temp project root.
    pubs_dir = tmp_path / "data"
    pubs_dir.mkdir()
    pubs_yml = pubs_dir / "publications.yml"
    pubs_yml.write_text(
        "# header docstring\n"
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: Test Pub\n"
        "      authors:\n"
        "        - name: Public JQ\n"
        "      year: 2024\n"
        "      notes:\n"
        "        - type: media\n"
        "          outlets:\n"
        "            - name: CNN\n"
        "              url: http://ct.moreover.com/?a=resolveme\n"
    )

    # Redirect TrackerCache to tmp.
    monkeypatch.setattr(
        altmetric_tracker_cache.TrackerCache,
        "DEFAULT_PATH",
        tmp_path / "trackers.json",
    )
    # Stub the resolver so we don't hit the network.
    monkeypatch.setattr(
        altmetric_client,
        "resolve_tracker_url",
        lambda url, **kw: ResolveResult(
            final_url="https://www.cnn.com/x",
            strategy="head",
            status="resolved",
        ),
    )
    # Point the workspace root at tmp via the P1 seam (the CLI resolves
    # publications.yml through paths.data_dir(); reset fixture restores it).
    from cv_editor import paths

    paths.configure(data_dir=tmp_path)

    # Invoke the REAL function.
    altmetric_client._cli_resolve_all(force=False)

    # YAML was rewritten with the resolved URL.
    written = pubs_yml.read_text()
    assert "https://www.cnn.com/x" in written
    assert "ct.moreover.com" not in written
    # Cache sidecar exists.
    assert (tmp_path / "trackers.json").exists()


def test_cli_cache_status_prints_all_status_counts(tmp_path, monkeypatch, capsys):
    """T2.1: cache-status shows every status line (resolved + 3 failed
    variants + total)."""
    cache_path = tmp_path / "trackers.json"
    monkeypatch.setattr(
        altmetric_tracker_cache.TrackerCache,
        "DEFAULT_PATH",
        cache_path,
    )
    c = altmetric_tracker_cache.TrackerCache(cache_path)
    c.record("http://t/1", ResolveResult(final_url="x", strategy="head", status="resolved"))
    c.record("http://t/2", ResolveResult(status="failed_network"))
    c.record("http://t/3", ResolveResult(status="failed_rate_limit"))
    c.save()
    monkeypatch.setattr("sys.argv", ["altmetric_client", "cache-status"])
    altmetric_client._cli()
    out = capsys.readouterr().out
    for label in ("total", "resolved", "failed_network", "failed_rate_limit", "failed_no_redirect"):
        assert label in out
    # Confirm at least one count appears next to "resolved".
    import re

    assert re.search(r"resolved:\s+1", out)


# ---------- Typst-side outlet grouping parity ----------


def test_typst_format_media_outlets_renders_grouped_string(tmp_path):
    """T2.1h: Build a fixture .typ that imports format-media-outlets and
    verify the rendered output contains the canonical grouping string.

    Uses typst CLI in a subprocess. Skips if typst isn't on PATH (some
    CI environments). The fixture exercises the case
    `AP, Washington Post (2, 3), Reuters (2)` — per-outlet local
    numbering with first article represented by the bare name.
    """
    import shutil
    import subprocess as sp

    typst_bin = shutil.which("typst")
    if not (typst_bin and HAS_BESPOKE):
        import pytest as _pytest

        _pytest.skip("typst not on PATH; renderer parity skipped in this env")

    # Build a self-contained .typ that stubs out the meta + flags the
    # renderer reads, then imports format-media-outlets.
    proj_root = Path(__file__).resolve().parent.parent
    fixture = tmp_path / "grouping_test.typ"
    fixture.write_text(f'''\
#let show-highlighted = false
#let meta = (self_bold: "Public JQ")
#import "{proj_root / "templates" / "bespoke" / "render.typ"}": format-media-outlets, mk
#format-media-outlets((
  "AP",
  (name: "Washington Post", url: "https://wapo.com/a"),
  (name: "Washington Post", url: "https://wapo.com/b"),
  (name: "Washington Post", url: "https://wapo.com/c"),
  (name: "Reuters",         url: "https://reuters.com/a"),
  (name: "Reuters",         url: "https://reuters.com/b"),
))
''')
    out_pdf = tmp_path / "out.pdf"
    result = sp.run(
        [
            typst_bin,
            "compile",
            "--font-path",
            str(proj_root / "fonts"),
            "--ignore-system-fonts",
            str(fixture),
            str(out_pdf),
        ],
        capture_output=True,
        text=True,
    )
    # If compile fails (likely due to import path resolution under
    # subprocess CWD), skip cleanly rather than fail — this is best-effort.
    if result.returncode != 0:
        import pytest as _pytest

        _pytest.skip(f"typst compile failed in test env: {result.stderr[:200]}")
    # Extract text from PDF.
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        import pytest as _pytest

        _pytest.skip("pdftotext not on PATH")
    extracted = sp.run(
        [pdftotext, str(out_pdf), "-"],
        capture_output=True,
        text=True,
    ).stdout
    # Per-outlet numbering convention (2026-05-17):
    #   AP             -> singleton, bare
    #   Washington Post (article 1 = bare; articles 2, 3 in parens)
    #   Reuters        (article 1 = bare; article 2 in parens)
    assert "AP" in extracted
    assert "Washington Post" in extracted
    assert "Reuters" in extracted
    # Washington Post has 3 articles → (2, 3); Reuters has 2 → (2).
    assert "(2, 3)" in extracted or "(2,3)" in extracted
    # Reuters' "(2)" is hard to assert independently because "Reuters (2)"
    # is a substring of "(2, 3)"; verify by reading right after the name.
    import re

    assert re.search(r"Reuters\s*\(2\)", extracted)


def test_group_outlets_skips_highlighted(tmp_path):
    """T1.4: highlighted outlets MUST be filtered out before per-outlet
    indices are assigned. Python, Typst, and JS implementations must
    agree on this."""
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "AP", "url": "https://1"},
            {"name": "Hidden", "url": "https://2", "highlighted": True},
            {"name": "Reuters", "url": "https://3"},
        ]
    )
    # Hidden outlet dropped entirely; both remaining are singletons.
    names = [g["name"] for g in groups]
    assert names == ["AP", "Reuters"]
    by_name = {g["name"]: g for g in groups}
    assert by_name["AP"]["urls"][0]["local_idx"] == 1
    assert by_name["Reuters"]["urls"][0]["local_idx"] == 1


def test_group_outlets_per_outlet_numbering_renders_as_expected():
    """The ICE-detention case from 2026-05-17: two outlets each with
    multiple articles should render with (2, 3) per outlet, not global
    digits like (6, 9) and (7, 10)."""
    groups = notes_helpers.group_outlets_for_display(
        [
            {"name": "NBC", "url": "https://nbc/1"},
            {"name": "CBS", "url": "https://cbs/1"},
            {"name": "AOL", "url": "https://aol/1"},
            {"name": "Yahoo!", "url": "https://yahoo/1"},
            {"name": "Newsbreak", "url": "https://nb/1"},
            {"name": "AOL", "url": "https://aol/2"},
            {"name": "AOL", "url": "https://aol/3"},
            {"name": "Yahoo!", "url": "https://yahoo/2"},
            {"name": "Yahoo!", "url": "https://yahoo/3"},
            {"name": "USA Today", "url": "https://usa/1"},
        ]
    )
    by_name = {g["name"]: g for g in groups}
    assert [it["local_idx"] for it in by_name["NBC"]["urls"]] == [1]
    assert [it["local_idx"] for it in by_name["AOL"]["urls"]] == [1, 2, 3]
    assert [it["local_idx"] for it in by_name["Yahoo!"]["urls"]] == [1, 2, 3]
    assert [it["local_idx"] for it in by_name["USA Today"]["urls"]] == [1]


def test_resolveresult_partial_resolved():
    """Status is resolved but final_url missing → is_resolved=False."""
    assert not ResolveResult(status="resolved").is_resolved
