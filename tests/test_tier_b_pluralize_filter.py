"""Tier B / B7 (2026-05-27) — `pluralize` Jinja filter.

Replaces the 12 inline `{{ '' if N == 1 else 's' }}` snippets in
_macros.html. Takes BOTH forms explicitly because naive `+s` corrupts
"match" → "matchs" (correct: "matches"). The critic R-B pre-impl
review flagged this with the real qc_findings_banner_entry example.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app():
    from cv_editor.app import create_app

    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---- pluralize filter unit tests ----


def test_pluralize_singular_at_count_1(app):
    with app.test_request_context():
        from flask import render_template_string

        out = render_template_string('{{ 1|pluralize("finding", "findings") }}')
    assert out == "finding"


def test_pluralize_plural_at_count_other(app):
    with app.test_request_context():
        from flask import render_template_string

        for n in (0, 2, 5, 100):
            out = render_template_string(f'{{{{ {n}|pluralize("finding", "findings") }}}}')
            assert out == "findings", f"count={n} returned {out!r}"


def test_pluralize_handles_irregular_es_pattern(app):
    """The matchs/matches case that motivated this filter (critic R-B)."""
    with app.test_request_context():
        from flask import render_template_string

        assert render_template_string('{{ 1|pluralize("match", "matches") }}') == "match"
        assert render_template_string('{{ 3|pluralize("match", "matches") }}') == "matches"
        assert render_template_string('{{ 1|pluralize("mismatch", "mismatches") }}') == "mismatch"
        assert render_template_string('{{ 5|pluralize("mismatch", "mismatches") }}') == "mismatches"


def test_pluralize_handles_verb_agreement(app):
    """has/have, is/are — the filter is noun-agnostic and works for
    verbs too. The critic R-A explicitly flagged that verb agreement
    couldn't be derived from a noun-pluralizer."""
    with app.test_request_context():
        from flask import render_template_string

        assert render_template_string('{{ 1|pluralize("has", "have") }}') == "has"
        assert render_template_string('{{ 2|pluralize("has", "have") }}') == "have"


def test_pluralize_handles_none_and_invalid_count(app):
    """Resilient to None/empty/string-coerced inputs — Jinja templates
    sometimes pass `None` for a missing optional count."""
    with app.test_request_context():
        from flask import render_template_string

        assert (
            render_template_string('{{ none|pluralize("x", "xs") }}') == "xs"
        )  # None → 0 → plural form
        assert (
            render_template_string('{{ "abc"|pluralize("x", "xs") }}') == "xs"
        )  # bad input → 0 → plural form


# ---- callsite integration tests ----


def test_search_page_uses_pluralize_for_matches(client):
    body = client.get("/search?q=test").get_data(as_text=True)
    # "match" or "matches" must appear, but NOT "matchs" (the bug the
    # filter was added to prevent).
    assert "matchs" not in body
    assert "match" in body  # some form of "match" should be present


def test_qc_triage_banner_uses_pluralize_in_entry_view(client):
    """The qc_findings_banner_entry macro had the matchs/matches case
    inline. After B7 it should use the filter. We can't assert the
    filter is used by inspecting rendered HTML directly, but we CAN
    assert the rendered output never contains "matchs"."""
    # entry_view renders qc_findings_banner_entry when the entry has
    # effective findings. We can't easily force the banner to appear
    # without a sidecar, so instead grep the source.
    macros = (ROOT / "scripts" / "cv_editor" / "templates" / "_macros.html").read_text()
    assert "matchs" not in macros  # never the naive +s pattern
    # The filter should be referenced (proves the migration landed).
    assert "|pluralize(" in macros
