"""`#link("url")[label]` URLs in any data/*.yml are collected for health-checking.

mk() evals Typst markup, so a link is legal in every field routed through it.
Before this collector existed, `verify_urls` walked publications.yml IDs/OA/
outlets and meta.yml contacts only, so a link written into a service entry,
an appointment, or an honor was published unchecked and could rot silently.
"""

from __future__ import annotations

from cv_editor.verify_urls import collect_markup_link_urls


def _urls(d):
    return sorted(e.url for e in collect_markup_link_urls(d))


def _sources(d):
    return sorted(e.source for e in collect_markup_link_urls(d))


def test_collects_links_from_any_file_and_any_field(tmp_path):
    """One link per file, each in a different field, none of them a schema
    the collector knows about — that is the whole point."""
    (tmp_path / "service.yml").write_text(
        "- subsection: S\n"
        "  entries:\n"
        "  - date: 01/2020\n"
        "    role: Member\n"
        "    venue: '#link(\"https://example.org/venue\")[Org]'\n"
        "    extras:\n"
        "    - '#link(\"https://example.org/extras\")[Committee]'\n",
        encoding="utf-8",
    )
    (tmp_path / "appointments.yml").write_text(
        "- subsection: A\n"
        "  clusters:\n"
        "  - institution: I\n"
        "    entries:\n"
        "    - date: 01/2016\n"
        "      role: Fellow\n"
        "      program: '#link(\"https://example.org/program\")[Prog]'\n",
        encoding="utf-8",
    )
    assert _urls(tmp_path) == [
        "https://example.org/extras",
        "https://example.org/program",
        "https://example.org/venue",
    ]


def test_skips_comment_lines(tmp_path):
    """Schema docstrings demonstrate the form with a placeholder. A collector
    that read comments would report a dead link on every single run."""
    (tmp_path / "service.yml").write_text(
        '# Linking: write it as `#link("https://docs.example/EXAMPLE")[label]`.\n'
        "#   indented comments count too:\n"
        '#     - \'#link("https://docs.example/ALSO-A-COMMENT")[x]\'\n'
        "- subsection: S\n"
        "  entries:\n"
        "  - date: 01/2020\n"
        "    role: '#link(\"https://example.org/real\")[R]'\n",
        encoding="utf-8",
    )
    assert _urls(tmp_path) == ["https://example.org/real"]


def test_drops_non_http_values(tmp_path):
    """`#link("url")[label]` written literally in prose, and mailto/relative
    targets, are not health-checkable and must not be reported as failures."""
    (tmp_path / "meta.yml").write_text(
        "a: '#link(\"url\")[placeholder]'\n"
        "b: '#link(\"mailto:someone@example.org\")[mail]'\n"
        "c: '#link(\"/relative/path\")[rel]'\n"
        "d: '#link(\"https://example.org/keep\")[keep]'\n",
        encoding="utf-8",
    )
    assert _urls(tmp_path) == ["https://example.org/keep"]


def test_source_names_the_file_and_line(tmp_path):
    """The report has to say WHERE, or a dead link is unfixable."""
    (tmp_path / "honors.yml").write_text(
        "- date: '2026'\n  award: '#link(\"https://example.org/award\")[A]'\n  institution: I\n",
        encoding="utf-8",
    )
    assert _sources(tmp_path) == ["honors.yml:2:#link"]


def test_two_links_on_one_line_both_collected(tmp_path):
    (tmp_path / "service.yml").write_text(
        "a: '#link(\"https://example.org/one\")[1] and #link(\"https://example.org/two\")[2]'\n",
        encoding="utf-8",
    )
    assert _urls(tmp_path) == ["https://example.org/one", "https://example.org/two"]


def test_missing_data_dir_is_not_an_error(tmp_path):
    assert _urls(tmp_path / "nope") == []


def test_example_subdirectory_is_not_walked(tmp_path):
    """data/example/ ships fictional URLs; checking them would add network
    calls and failures for links the owner does not publish."""
    (tmp_path / "example").mkdir()
    (tmp_path / "example" / "service.yml").write_text(
        "a: '#link(\"https://example.org/fictional\")[x]'\n", encoding="utf-8"
    )
    (tmp_path / "service.yml").write_text(
        "a: '#link(\"https://example.org/real\")[x]'\n", encoding="utf-8"
    )
    assert _urls(tmp_path) == ["https://example.org/real"]


def test_collector_is_wired_into_collect_all_urls(tmp_path):
    """The collector existing is worth nothing if the aggregator never calls
    it. Without this, deleting the one `entries.extend(...)` line leaves all
    the tests above green while the verifier goes back to its blind spot."""
    from cv_editor import schemas
    from cv_editor.verify_urls import collect_all_urls

    data = tmp_path / "data"
    data.mkdir()
    pubs = data / "publications.yml"
    pubs.write_text("- title: T\n  authors: [A]\n  year: 2020\n", encoding="utf-8")
    meta = data / "meta.yml"
    meta.write_text("contacts: []\n", encoding="utf-8")
    (data / "service.yml").write_text(
        "a: '#link(\"https://example.org/wired\")[x]'\n", encoding="utf-8"
    )
    assert schemas.get("publications")  # guard: the aggregator needs this schema

    urls = [e.url for e in collect_all_urls(pubs_path=pubs, meta_path=meta, data_dir=data)]
    assert "https://example.org/wired" in urls


def test_verify_all_isolation_does_not_leak_to_the_live_data_dir(tmp_path, monkeypatch):
    """Regression: the first cut of this collector ignored `pubs_path` and read
    the real data/ instead, so an isolated run silently pulled in the repo's own
    corpus. Caught by an EXISTING test going red, not by a new one."""
    from datetime import datetime, timezone

    from cv_editor import verify_urls as vu

    # ROOT/"data" is what the collector falls back to, so the sentinel has to
    # live at exactly that path or the test passes for the wrong reason.
    live_root = tmp_path / "live"
    (live_root / "data").mkdir(parents=True)
    (live_root / "data" / "service.yml").write_text(
        "a: '#link(\"https://example.org/LIVE-CORPUS\")[x]'\n", encoding="utf-8"
    )
    monkeypatch.setattr(vu, "ROOT", live_root)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    pubs = corpus / "publications.yml"
    pubs.write_text(
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: A\n      year: 2024\n      doi: 10.1/ok\n",
        encoding="utf-8",
    )
    meta = corpus / "meta.yml"
    meta.write_text("self_bold: Public JQ\n", encoding="utf-8")

    def fake_check(url):
        return vu.CheckResult(
            url=url,
            status=200,
            final_url=url,
            error=None,
            category="ok",
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            method_used="HEAD",
        )

    report = vu.verify_all(
        pubs_path=pubs,
        meta_path=meta,
        check_fn=fake_check,
        cache=vu.UrlCache(tmp_path / "cache", ttl_days=30),
        max_workers=1,
    )
    assert not any("LIVE-CORPUS" in u for u in report.sources_by_url), (
        "verify_all pulled a URL from the live data dir despite pubs_path isolation"
    )
