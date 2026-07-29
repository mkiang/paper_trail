"""The two version declarations in this repo must agree.

There are TWO, neither driving the other:

  * `pyproject.toml:version` — the pip/wheel version.
  * `typst.toml:version`     — the `@local/paper-trail` Typst package version.

Nothing in this repo compared them before, so a release could bump one and forget
the other, merge green, and get tagged. The failure then surfaces DOWNSTREAM, in a
consumer's vendoring step, AFTER the tag is published — at which point the only
honest fixes are cutting another patch release or moving a published tag (which
desynchronises a consumer's lockfile from its recorded commit sha, so: don't).

Found by review of the 1.2.0 release plan, where the bump was specified for
`pyproject.toml` alone.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _version(name: str) -> str:
    with (ROOT / name).open("rb") as fh:
        doc = tomllib.load(fh)
    return doc.get("project", doc.get("package", {})).get("version") or doc["package"]["version"]


def test_the_wheel_and_typst_package_versions_agree():
    py, ty = _version("pyproject.toml"), _version("typst.toml")
    assert py == ty, (
        f"pyproject.toml says {py} but typst.toml says {ty}. Both are part of one "
        "release artifact; bump them in the same commit."
    )


def test_the_changelog_documents_the_current_version():
    py = _version("pyproject.toml")
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(py)}$", text, re.M), (
        f"CHANGELOG.md has no '## {py}' section for the version in pyproject.toml"
    )


def test_a_tag_on_this_commit_matches_the_declared_version():
    """Close the gap between "the files say X" and "the tag says X".

    CI never sees this: `ci.yml` triggers on pushes to `main` and on pull requests,
    never on a tag push. And tagging is manual — no `make release` target. So a
    `git tag v1.2.2` cut against a tree whose files still say `1.2.1` would publish
    with every check green. This test is the only thing between those two states,
    and it only helps when run on the tagged commit; the release checklist in
    CONTRIBUTING.md carries the rest.
    """
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if tags.returncode != 0:
        pytest.skip("not a git checkout")
    version_tags = [t for t in tags.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if not version_tags:
        pytest.skip("HEAD carries no version tag (the normal case during development)")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "pyproject.toml", "typst.toml", "CHANGELOG.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        # A version bump in progress on top of the previous release's commit: the
        # files legitimately name the NEXT version while HEAD still carries the last
        # tag. Only a committed tree can be judged against its tag.
        pytest.skip("version files are modified relative to HEAD (bump in progress)")
    declared = _version("pyproject.toml")
    for tag in version_tags:
        assert tag == f"v{declared}", (
            f"tag {tag} is on this commit but pyproject.toml says {declared}"
        )
