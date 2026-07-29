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
import tomllib
from pathlib import Path

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
