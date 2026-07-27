"""A6: the global "Commit pending edits" button (POST /commit).

The route stages the editor-managed data files + a freshly-regenerated
publications.bib and makes ONE local git commit in the workspace repo. It never
pushes and never `git add -A`. Every git failure flashes rather than 500s.

Hermetic: git happy-path tests `git init` a throwaway repo INSIDE the
conftest-isolated tmp workspace (paths.data_root()), set a LOCAL git identity,
and never touch the real repo or the network. The `_commit_add_set` unit test
builds its own tmp tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from cv_editor import core_routes, paths
from cv_editor.app import create_app


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def git_ws(_workspace_isolation):
    """Turn the isolated tmp workspace into a real git repo with an initial
    commit + a LOCAL identity (so committing works regardless of ~/.gitconfig)."""
    ws = paths.data_root()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "test@example.invalid")
    _git(ws, "config", "user.name", "Test Runner")
    _git(ws, "config", "commit.gpgsign", "false")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    return ws


def _head(ws: Path) -> str:
    return _git(ws, "rev-parse", "HEAD").stdout.strip()


# ----- _commit_add_set (pure) -----------------------------------------------


def test_add_set_globs_data_yml_and_lists_sidecars_but_not_reports(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "qc").mkdir()
    (tmp_path / "data" / "publications.yml").write_text("[]\n")
    (tmp_path / "data" / "meta.yml").write_text("{}\n")
    (tmp_path / "data" / "citation_counts.json").write_text("{}\n")
    (tmp_path / "data" / "publications_pubmed_sync.json").write_text("{}\n")
    (tmp_path / "publications.bib").write_text("\n")
    (tmp_path / "qc" / "qc_decisions.json").write_text("{}\n")
    (tmp_path / "qc" / "pubmed_sync_decisions.gen.yml").write_text("{}\n")
    # Report churn — must NEVER be staged.
    (tmp_path / "qc" / "report.json").write_text("{}\n")
    (tmp_path / "qc" / "report.md").write_text("x\n")
    (tmp_path / "qc" / "citations_report.md").write_text("x\n")
    # Engine asset — the example corpus must stay out (glob is top-level only).
    (tmp_path / "data" / "example").mkdir()
    (tmp_path / "data" / "example" / "meta.yml").write_text("{}\n")

    got = set(core_routes._commit_add_set(tmp_path))
    assert "data/publications.yml" in got
    assert "data/meta.yml" in got
    assert "data/citation_counts.json" in got
    assert "data/publications_pubmed_sync.json" in got
    assert "publications.bib" in got
    assert "qc/qc_decisions.json" in got
    assert "qc/pubmed_sync_decisions.gen.yml" in got
    # Reports + example corpus excluded.
    assert not any("report" in p for p in got)
    assert "data/example/meta.yml" not in got


def test_add_set_skips_missing_files(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "meta.yml").write_text("{}\n")
    got = core_routes._commit_add_set(tmp_path)
    assert got == ["data/meta.yml"]  # no bib / sidecars exist -> not listed


# ----- route behaviour -------------------------------------------------------


def test_commit_not_a_repo_flashes(client):
    # The isolated tmp workspace is NOT a git repo.
    resp = client.post("/commit", follow_redirects=True)
    assert resp.status_code == 200
    assert "not a git repository" in resp.get_data(as_text=True)


def test_commit_happy_path_makes_one_commit(client, git_ws):
    before = _head(git_ws)
    honors = git_ws / "data" / "honors.yml"
    honors.write_text(honors.read_text() + "\n# editor touch\n")

    resp = client.post("/commit", data={"message": "Add a talk"}, follow_redirects=False)
    assert resp.status_code == 302

    after = _head(git_ws)
    assert after != before, "expected a new commit"
    # The touched YAML + the regenerated bib are in the commit.
    stat = _git(git_ws, "show", "--stat", "--name-only", "HEAD").stdout
    assert "data/honors.yml" in stat
    assert "publications.bib" in stat
    # Commit message honored.
    assert _git(git_ws, "log", "-1", "--pretty=%s").stdout.strip() == "Add a talk"
    # honors.yml is now clean.
    assert _git(git_ws, "status", "--porcelain", "--", "data/honors.yml").stdout.strip() == ""


def test_commit_default_message_when_blank(client, git_ws):
    honors = git_ws / "data" / "honors.yml"
    honors.write_text(honors.read_text() + "\n# touch\n")
    client.post("/commit", data={"message": "   "}, follow_redirects=False)
    assert _git(git_ws, "log", "-1", "--pretty=%s").stdout.strip() == "Update CV data via editor"


def test_commit_nothing_to_commit_second_run(client, git_ws):
    # First commit picks up the new publications.bib.
    client.post("/commit", follow_redirects=False)
    head1 = _head(git_ws)
    # Second run: bib regenerates identical, no YAML change -> nothing staged.
    resp = client.post("/commit", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "already committed" in body
    assert _head(git_ws) == head1, "no new commit expected"


def test_commit_refuses_detached_head(client, git_ws):
    _git(git_ws, "checkout", "--detach", "-q")
    before = _head(git_ws)
    honors = git_ws / "data" / "honors.yml"
    honors.write_text(honors.read_text() + "\n# touch\n")
    resp = client.post("/commit", follow_redirects=True)
    assert "detached HEAD" in resp.get_data(as_text=True)
    assert _head(git_ws) == before, "must not commit on detached HEAD"


def test_commit_never_stages_report_churn(client, git_ws):
    # A dirty qc report must be left untouched by the commit.
    (git_ws / "qc").mkdir(exist_ok=True)
    report = git_ws / "qc" / "report.md"
    report.write_text("stale report\n")
    honors = git_ws / "data" / "honors.yml"
    honors.write_text(honors.read_text() + "\n# touch\n")
    client.post("/commit", follow_redirects=False)
    # report.md is untracked+uncommitted after the commit.
    st = _git(git_ws, "status", "--porcelain", "--", "qc/report.md").stdout
    assert st.strip().startswith("??"), "report.md must remain unstaged"


# ----- index button discoverability -----------------------------------------


def test_index_shows_commit_bar_in_repo(client, git_ws):
    body = client.get("/").get_data(as_text=True)
    assert "Commit pending edits" in body


def test_index_hides_commit_bar_outside_repo(client):
    body = client.get("/").get_data(as_text=True)
    assert "Commit pending edits" not in body
