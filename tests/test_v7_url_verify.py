"""V7: URL health-check script + /urls/verify editor surface."""

from __future__ import annotations

import socket
import ssl
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cv_editor import paths, schemas  # noqa: E402
from cv_editor import verify_urls as vu  # noqa: E402
from cv_editor.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------- URL collection ----------------


def _make_entry(**kwargs):
    e = CommentedMap()
    for k, v in kwargs.items():
        e[k] = v
    return e


def _make_pubs_data(entries):
    """Wrap entries in the publications.yml shape (list_of_subsections)."""
    data = CommentedSeq()
    sub = CommentedMap()
    sub["subsection"] = "Peer-Reviewed Original Research"
    sub["entries"] = CommentedSeq(entries)
    data.append(sub)
    return data


def test_collect_doi_pmid_pmcid():
    e = _make_entry(doi="10.9999/nae.2024.2858", pmid="90002858", pmcid="PMC9002858")
    data = _make_pubs_data([e])
    sch = schemas.get("publications")
    entries = list(vu.collect_publication_urls(data, sch))
    urls = {x.url for x in entries}
    assert "https://doi.org/10.9999/nae.2024.2858" in urls
    assert "https://pubmed.ncbi.nlm.nih.gov/90002858/" in urls
    assert "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9002858/" in urls
    # Source labels embed field name + global_idx.
    sources = {x.source for x in entries}
    assert any(":doi" in s for s in sources)
    assert any(":pmid" in s for s in sources)
    assert any(":pmcid" in s for s in sources)


def test_collect_preprint_doi():
    e = _make_entry(preprint_doi="10.48550/arXiv.2401.12345")
    data = _make_pubs_data([e])
    entries = list(vu.collect_publication_urls(data, schemas.get("publications")))
    assert entries[0].url == "https://doi.org/10.48550/arXiv.2401.12345"
    assert ":preprint_doi" in entries[0].source


def test_collect_open_access_url_only_for_string():
    oa = CommentedMap()
    oa["paper"] = "https://osf.io/foo"
    oa["code"] = True  # placeholder, no URL
    oa["data"] = None
    e = _make_entry(open_access=oa)
    data = _make_pubs_data([e])
    entries = list(vu.collect_publication_urls(data, schemas.get("publications")))
    urls = [x.url for x in entries]
    assert "https://osf.io/foo" in urls
    # True / None values should not produce URL entries.
    assert not any("True" in u or "None" in u for u in urls)
    assert sum(1 for x in entries if "open_access" in x.source) == 1


def test_collect_outlet_urls_dict_form_only():
    notes = CommentedSeq()
    media = CommentedMap()
    media["type"] = "media"
    outlets = CommentedSeq()
    outlets.append("CNN")  # plain string, no URL
    outlet_dict = CommentedMap()
    outlet_dict["name"] = "NYT"
    outlet_dict["url"] = "https://nytimes.com/article/foo"
    outlets.append(outlet_dict)
    outlets.append({"name": "Reuters", "highlighted": True})  # no URL key
    media["outlets"] = outlets
    notes.append(media)
    e = _make_entry(notes=notes)
    data = _make_pubs_data([e])
    entries = list(vu.collect_publication_urls(data, schemas.get("publications")))
    urls = [x.url for x in entries]
    assert "https://nytimes.com/article/foo" in urls
    assert len([u for u in urls if "nytimes" in u]) == 1


def test_collect_meta_urls(tmp_path):
    meta_yaml = tmp_path / "meta.yml"
    meta_yaml.write_text(
        "self_bold: Public JQ\n"
        "contacts:\n"
        "  - glyph: web\n"
        "    text: https://example.com\n"
        "  - glyph: email\n"
        "    text: jq.public@example.com\n"
    )
    entries = list(vu.collect_meta_urls(meta_yaml))
    assert len(entries) == 1
    assert entries[0].url == "https://example.com"
    assert "contacts[0]" in entries[0].source


def test_collect_meta_urls_missing_file(tmp_path):
    assert list(vu.collect_meta_urls(tmp_path / "absent.yml")) == []


# ---------------- cache ----------------


def _make_result(
    url="https://doi.org/foo",
    status=200,
    category="ok",
    checked_at=None,
    final_url=None,
    error=None,
):
    return vu.CheckResult(
        url=url,
        status=status,
        final_url=final_url or url,
        error=error,
        category=category,
        checked_at=checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        method_used="HEAD",
    )


def test_cache_miss_returns_none(tmp_path):
    cache = vu.UrlCache(tmp_path, ttl_days=30)
    assert cache.get_fresh("https://nope.example") is None


def test_cache_hit_within_ttl(tmp_path):
    cache = vu.UrlCache(tmp_path, ttl_days=30)
    cache.set(_make_result())
    got = cache.get_fresh("https://doi.org/foo")
    assert got is not None
    assert got.status == 200


def test_cache_miss_after_ttl_expiry(tmp_path):
    cache = vu.UrlCache(tmp_path, ttl_days=30)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    cache.set(_make_result(checked_at=old_iso))
    assert cache.get_fresh("https://doi.org/foo") is None


def test_cache_does_not_store_failures(tmp_path):
    cache = vu.UrlCache(tmp_path, ttl_days=30)
    cache.set(_make_result(status=404, category="4xx"))
    assert cache.get_fresh("https://doi.org/foo") is None


def test_cache_handles_corrupt_file(tmp_path):
    cache = vu.UrlCache(tmp_path, ttl_days=30)
    cache._path("https://doi.org/foo").write_text("not-json{{{")
    assert cache.get_fresh("https://doi.org/foo") is None


# ---------------- HTTP check (mocked) ----------------


class _FakeResp:
    """urllib.urlopen context-manager stub."""

    def __init__(self, status, url):
        self.status = status
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_url_head_200(monkeypatch):
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append((req.get_method(), req.full_url))
        return _FakeResp(200, req.full_url)

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://doi.org/x")
    assert r.status == 200
    assert r.category == "ok"
    assert r.method_used == "HEAD"
    assert captured == [("HEAD", "https://doi.org/x")]


def test_check_url_head_405_falls_back_to_get(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)
        return _FakeResp(200, req.full_url)

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://publisher.test/x")
    assert r.status == 200
    assert r.method_used == "GET"
    assert calls == ["HEAD", "GET"]


def test_check_url_404(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://doi.org/bad")
    assert r.status == 404
    assert r.category == "4xx"
    assert "404" in (r.error or "")


def test_check_url_timeout(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://slow.test")
    assert r.status == 0
    assert r.category == "timeout"


def test_check_url_dns(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://nope.example")
    assert r.status == 0
    assert r.category == "dns"


def test_check_url_ssl(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ssl.SSLError("BAD_CERT"))

    monkeypatch.setattr(vu.urllib.request, "urlopen", fake_urlopen)
    r = vu.check_url("https://bad-tls.test")
    assert r.category == "ssl"


# ---------------- verify_all end-to-end ----------------


def test_verify_all_with_mocked_check(tmp_path, monkeypatch):
    """End-to-end orchestration with check_fn injected and pubs/meta
    pointed at temp YAMLs."""
    pubs = tmp_path / "publications.yml"
    pubs.write_text(
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: A\n"
        "      year: 2024\n"
        "      doi: 10.1/ok\n"
        "      pmid: '1'\n"
        "    - title: B\n"
        "      year: 2023\n"
        "      doi: 10.1/bad\n"
    )
    meta = tmp_path / "meta.yml"
    meta.write_text("self_bold: Public JQ\n")

    # Mock check function: doi.org/10.1/bad → 404, everything else → 200.
    def fake_check(url):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if "10.1/bad" in url:
            return vu.CheckResult(
                url=url,
                status=404,
                final_url=url,
                error="HTTPError 404",
                category="4xx",
                checked_at=ts,
                method_used="HEAD",
            )
        return vu.CheckResult(
            url=url,
            status=200,
            final_url=url,
            error=None,
            category="ok",
            checked_at=ts,
            method_used="HEAD",
        )

    cache = vu.UrlCache(tmp_path / "cache", ttl_days=30)
    report = vu.verify_all(
        pubs_path=pubs,
        meta_path=meta,
        check_fn=fake_check,
        cache=cache,
        max_workers=2,
    )
    assert report.total_urls == 3  # 10.1/ok, 10.1/bad, pmid 1
    assert report.checked == 3
    assert report.cached_skips == 0
    assert len(report.by_category.get("ok", [])) == 2
    assert len(report.by_category.get("4xx", [])) == 1

    # Re-run: OK URLs cached, only the 404 re-checks (failures don't cache).
    report2 = vu.verify_all(
        pubs_path=pubs,
        meta_path=meta,
        check_fn=fake_check,
        cache=cache,
        max_workers=2,
    )
    assert report2.cached_skips == 2
    assert report2.checked == 1


def test_render_report_includes_failing_url(tmp_path):
    report = vu.Report(
        started_at="2026-05-15T00:00:00+00:00",
        finished_at="2026-05-15T00:01:00+00:00",
        total_urls=2,
        checked=2,
        cached_skips=0,
        by_category={
            "ok": [_make_result("https://doi.org/ok", 200)],
            "4xx": [_make_result("https://doi.org/bad", 404, "4xx", error="HTTPError 404")],
        },
        sources_by_url={
            "https://doi.org/ok": ["publications.yml#0:doi"],
            "https://doi.org/bad": ["publications.yml#1:doi"],
        },
    )
    text = vu.render_report(report)
    assert "URL Verification Report" in text
    assert "Failing: 1" in text
    assert "https://doi.org/bad" in text
    assert "404" in text
    assert "publications.yml#1:doi" in text


def test_render_report_clean_run():
    report = vu.Report(
        started_at="t1",
        finished_at="t2",
        total_urls=1,
        checked=1,
        cached_skips=0,
        by_category={"ok": [_make_result()]},
        sources_by_url={"https://doi.org/foo": ["publications.yml#0:doi"]},
    )
    text = vu.render_report(report)
    assert "All URLs healthy" in text


def test_render_report_flags_format_drift():
    report = vu.Report(
        started_at="t1",
        finished_at="t2",
        total_urls=1,
        checked=1,
        cached_skips=0,
        by_category={
            "ok": [
                _make_result(
                    url="https://doi.org/10.1/x",
                    status=200,
                    final_url="https://newpublisher.test/article/10.1/x",
                )
            ],
        },
        sources_by_url={"https://doi.org/10.1/x": ["publications.yml#0:doi"]},
    )
    text = vu.render_report(report)
    assert "format drift" in text.lower()


# ---------------- editor smoke tests ----------------


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_urls_verify_get_no_report(client, tmp_path, monkeypatch):
    # If the report doesn't exist, the page should still render.
    report_path = paths.qc_dir() / "urls_report.md"
    backup = None
    if report_path.exists():
        backup = report_path.read_bytes()
        report_path.unlink()
    try:
        resp = client.get("/urls/verify")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "URL Verification" in body
        assert "No report yet" in body
    finally:
        if backup is not None:
            report_path.write_bytes(backup)


def test_urls_verify_get_with_report(client, tmp_path):
    report_path = paths.qc_dir() / "urls_report.md"
    backup = report_path.read_bytes() if report_path.exists() else None
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# URL Verification Report\n\n"
        "- Total unique URLs: 271\n"
        "- OK: 270  ·  Failing: 1  ·  Checked this run: 271  ·  Skipped via cache: 0\n"
    )
    try:
        resp = client.get("/urls/verify")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "271" in body
        assert "1 failing" in body or "Failing: 1" in body
    finally:
        if backup is None:
            report_path.unlink(missing_ok=True)
        else:
            report_path.write_bytes(backup)


def test_urls_verify_post_kicks_runner(client, monkeypatch):
    """POST should call _kick_url_verify_if_idle. We patch the subprocess
    so no actual network call fires."""
    called = []

    def fake_thread_start(self):
        called.append("started")

    monkeypatch.setattr("threading.Thread.start", fake_thread_start)
    resp = client.post("/urls/verify", follow_redirects=False)
    assert resp.status_code == 302
    assert called == ["started"]


def test_urls_verify_post_force_passes_flag(client, monkeypatch):
    """POST with force=1 should append --force to the subprocess argv."""
    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.append(list(argv))

        class _Result:
            returncode = 0

        return _Result()

    # The subprocess.run is called inside the daemon thread's _run().
    # Intercept Thread.start to invoke the target synchronously so
    # captured_argv populates before we assert.
    import threading

    orig_init = threading.Thread.__init__
    captured_target = {}

    def fake_init(self, *a, target=None, **kw):
        captured_target["fn"] = target
        orig_init(self, *a, target=target, **kw)

    def fake_start(self):
        captured_target["fn"]()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("threading.Thread.__init__", fake_init)
    monkeypatch.setattr("threading.Thread.start", fake_start)

    resp = client.post("/urls/verify", data={"force": "1"}, follow_redirects=False)
    assert resp.status_code == 302
    assert captured_argv, "subprocess.run was not invoked"
    assert "--force" in captured_argv[0]
    assert "--quiet" in captured_argv[0]


def test_urls_verify_post_without_force_omits_flag(client, monkeypatch):
    """POST without the force field should NOT include --force."""
    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.append(list(argv))

        class _Result:
            returncode = 0

        return _Result()

    import threading

    orig_init = threading.Thread.__init__
    captured_target = {}

    def fake_init(self, *a, target=None, **kw):
        captured_target["fn"] = target
        orig_init(self, *a, target=target, **kw)

    def fake_start(self):
        captured_target["fn"]()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("threading.Thread.__init__", fake_init)
    monkeypatch.setattr("threading.Thread.start", fake_start)

    resp = client.post("/urls/verify", follow_redirects=False)
    assert resp.status_code == 302
    assert captured_argv, "subprocess.run was not invoked"
    assert "--force" not in captured_argv[0]


def test_urls_status_json_no_report(client):
    report_path = paths.qc_dir() / "urls_report.md"
    backup = report_path.read_bytes() if report_path.exists() else None
    if backup is not None:
        report_path.unlink()
    try:
        resp = client.get("/urls/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is False
        assert data["running"] is False
    finally:
        if backup is not None:
            report_path.write_bytes(backup)


def test_urls_report_text_404_when_absent(client):
    report_path = paths.qc_dir() / "urls_report.md"
    backup = report_path.read_bytes() if report_path.exists() else None
    if backup is not None:
        report_path.unlink()
    try:
        resp = client.get("/qc/urls_report")
        assert resp.status_code == 404
    finally:
        if backup is not None:
            report_path.write_bytes(backup)
