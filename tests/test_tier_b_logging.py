"""Tier B / B9 (2026-05-27) — file logging for background-daemon failures.

The cv_editor.log handler is attached to `app.logger` (NOT root via
basicConfig — that would pollute the user's real log on every pytest
create_app() call). Gated on `not app.testing`. Tests redirect via
`app.config["LOG_PATH"]` per the established sidecar idiom.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_with_logging(tmp_path):
    """Build an app whose FileHandler points at a tmp log file (NOT the
    user's real .cache/cv_editor.log). The default create_app() flow
    attaches a handler at the LOG_PATH default; this fixture detaches
    every FileHandler and replaces with one bound to tmp_path."""
    app = create_app()
    log_path = tmp_path / "cv_editor.log"
    # Detach the default-path handler create_app() attached.
    for h in list(app.logger.handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            app.logger.removeHandler(h)
    # Attach a fresh handler at tmp_path so test writes can't reach the
    # user's real log file.
    h = logging.FileHandler(str(log_path), encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    app.logger.addHandler(h)
    app.logger.setLevel(logging.INFO)
    app.config["LOG_PATH"] = str(log_path)
    yield app, log_path
    # Cleanup: detach again so no leak across tests.
    for h in list(app.logger.handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            app.logger.removeHandler(h)


def _flush(app):
    for h in app.logger.handlers:
        h.flush()


def test_app_logger_writes_to_log_path(app_with_logging):
    app, log_path = app_with_logging
    app.logger.warning("test-message-12345")
    _flush(app)
    assert log_path.exists()
    contents = log_path.read_text()
    assert "test-message-12345" in contents
    assert "WARNING" in contents


def test_app_logger_captures_exception_traceback(app_with_logging):
    """logger.exception() must capture the full traceback (the R4
    BLOCKER fix). Without this, background subprocess failures show
    only the exception's repr — losing the call chain that points to
    the actual bug."""
    app, log_path = app_with_logging
    try:
        raise ValueError("simulated-background-failure")
    except ValueError:
        app.logger.exception("kicker %r blew up", "qc_publications")
    _flush(app)
    contents = log_path.read_text()
    assert "simulated-background-failure" in contents
    assert "Traceback" in contents
    assert "kicker 'qc_publications' blew up" in contents


def test_default_log_path_is_in_cache_dir():
    """Default LOG_PATH lives under .cache/ (gitignored). The plan
    explicitly rejected ROOT/cv_editor.log because it'd show up in
    grep-in-project and git-status output."""
    fresh = create_app()
    default_path = Path(fresh.config["LOG_PATH"])
    assert ".cache" in default_path.parts
    assert default_path.name == "cv_editor.log"


def test_create_app_attaches_filehandler_outside_testing():
    """A fresh non-testing create_app() attaches a FileHandler to
    app.logger so background failures hit disk in production use."""
    app = create_app()
    file_handlers = [h for h in app.logger.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers, "create_app() should attach a FileHandler in non-testing mode"
    # Cleanup so this doesn't leak into other tests.
    for h in file_handlers:
        h.close()
        app.logger.removeHandler(h)
