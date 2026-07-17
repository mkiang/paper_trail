"""M5 CP3: the /validate editor surface + index banner. GET-only, write-free.

Synthetic issues are injected by monkeypatching data_check.check_data so the
tests never depend on (or mutate) the real corpus state. The real corpus is
currently clean, so the no-banner / all-clear assertions also hold against it.
"""

from __future__ import annotations

import pytest
from cv_editor import data_check
from cv_editor.app import create_app


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


def _issue(severity, section, file, line, label, field, msg, gidx):
    return data_check.Issue(severity, section, file, line, label, field, msg, gidx)


# ---------- clean corpus (real data is clean) ----------


def test_validate_page_renders_all_clear_when_clean(client):
    body = client.get("/validate").get_data(as_text=True)
    assert "Data validation" in body
    assert "All clear" in body


def test_index_has_no_data_banner_when_clean(client):
    body = client.get("/").get_data(as_text=True)
    assert "will break the build" not in body
    assert "data warning" not in body.lower()


def test_tools_nav_has_validate_link(client):
    body = client.get("/").get_data(as_text=True)
    assert "/validate" in body  # Tools-menu link to the page


# ---------- with injected issues (monkeypatched, no writes) ----------


def test_validate_page_lists_issues_with_jump_links(client, monkeypatch):
    fake = [
        _issue(
            data_check.WARNING,
            "research_support",
            "data/research_support.yml",
            7,
            "Old grant",
            "date",
            "Active grant end date is in the past",
            0,
        ),
        _issue(
            data_check.ERROR,
            "publications",
            "data/publications.yml",
            42,
            "A study",
            "title",
            r"unescaped '$' opens Typst math mode",
            3,
        ),
    ]
    monkeypatch.setattr(data_check, "check_data", lambda *a, **k: fake)
    body = client.get("/validate").get_data(as_text=True)
    assert "ERROR" in body and "WARNING" in body
    assert "A study" in body and "Old grant" in body
    # Errors sort first.
    assert body.index("A study") < body.index("Old grant")
    # Jump-to-edit links use global_idx.
    assert "/publications/3" in body
    assert "/research_support/0" in body


def test_index_banner_shows_error_count_and_links_to_validate(client, monkeypatch):
    fake = [
        _issue(
            data_check.ERROR, "publications", "data/publications.yml", 42, "T", "title", "boom", 3
        ),
        _issue(data_check.WARNING, "service", "data/service.yml", 9, "S", "date", "meh", 1),
    ]
    monkeypatch.setattr(data_check, "check_data", lambda *a, **k: fake)
    body = client.get("/").get_data(as_text=True)
    assert "will break the build" in body
    assert "/validate" in body


def test_index_banner_warning_only_uses_info_style(client, monkeypatch):
    fake = [_issue(data_check.WARNING, "service", "data/service.yml", 9, "S", "date", "meh", 1)]
    monkeypatch.setattr(data_check, "check_data", lambda *a, **k: fake)
    body = client.get("/").get_data(as_text=True)
    assert "will break the build" not in body  # no error banner
    assert "to review" in body  # warning banner text
    assert "/validate" in body


def test_meta_issue_links_to_meta_view(client, monkeypatch):
    fake = [
        _issue(
            data_check.WARNING,
            "meta",
            "data/meta.yml",
            3,
            "meta header",
            "self_bold",
            "required field is empty",
            0,
        )
    ]
    monkeypatch.setattr(data_check, "check_data", lambda *a, **k: fake)
    body = client.get("/validate").get_data(as_text=True)
    assert "/meta" in body  # meta jump goes to meta_view, not entry_view
