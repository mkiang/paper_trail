"""Shared skip-guard helpers for tests coupled to the private BESPOKE engine.

P5 (paper_trail inversion). Many render/freeze/flatten tests compile or query
the bespoke Typst template directly and need the curated ``fonts/`` masters.
In the private repo both are present, so these tests RUN + pass; in a public
``modern``-only extraction (bespoke + fonts absent) they must SKIP cleanly.

Re-key such a test's skip condition from typst-ONLY to ``typst AND bespoke AND
fonts`` by AND-ing ``HAS_BESPOKE`` into its existing ``skipif`` (or use
``bespoke_required`` when the only requirement is typst+bespoke+fonts).

``HAS_BESPOKE`` is evaluated against ``paths.project_root()`` (the ENGINE root,
which the test workspace-isolation fixture leaves pointed at the real repo), so
it stays honest even while the workspace ``data/`` is redirected to tmp.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cv_editor import capabilities, paths

HAS_TYPST = shutil.which("typst") is not None


def flags_typ_path(root: Path) -> Path:
    """The shared flags.typ under an engine root, across all three layouts:
    the vestigial root ``lib/`` (private, pre-cutover), ``src/lib/`` (the
    restructured public tree, P6b inc-4e), and — post-P7-cutover, when the
    private root ``lib/flags.typ`` is deleted and flags come from the vendored
    package — ``packages/local/paper-trail/<ver>/lib/flags.typ`` (CP4/H3).
    Returns the first that exists; falls back to the legacy root path so a
    genuinely missing copy fails loudly at the caller."""
    for cand in (root / "lib" / "flags.typ", root / "src" / "lib" / "flags.typ"):
        if cand.exists():
            return cand
    base = root / "packages" / "local" / "paper-trail"
    if base.is_dir():
        for verdir in sorted(p for p in base.iterdir() if p.is_dir()):
            cand = verdir / "lib" / "flags.typ"
            if cand.exists():
                return cand
    return root / "lib" / "flags.typ"


def local_package_dir(root: Path) -> Path:
    """The single repo-resident ``@local/paper-trail`` version dir. Fails
    loudly on zero or >1 (the CP4/B2 single-version invariant), so a test that
    hardcoded ``.../0.1.0`` can derive the version instead and survive the P7
    bump to 1.0.0."""
    base = root / "packages" / "local" / "paper-trail"
    dirs = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    assert len(dirs) == 1, (
        f"expected exactly one @local/paper-trail version dir, found {[d.name for d in dirs]}"
    )
    return dirs[0]


def local_package_version(root: Path) -> str:
    return local_package_dir(root).name


def _has_bespoke_engine() -> bool:
    root = paths.project_root()
    return (root / "templates" / "bespoke").is_dir() and (root / "fonts").is_dir()


HAS_BESPOKE = _has_bespoke_engine()

# Reusable marker for tests that READ the private bespoke template (or fonts/)
# from disk but do NOT invoke typst — skip when bespoke/fonts are absent (a
# public modern-only extraction) so a collection-time file read can't ERROR.
# For tests that additionally COMPILE, AND ``HAS_TYPST`` into their own skipif.
bespoke_required = pytest.mark.skipif(
    not HAS_BESPOKE,
    reason="needs the bespoke template + fonts/ (private engine)",
)

# P5.5: capability-keyed markers for tests that exercise a feature's ROUTES /
# nav / in-page UI. P5 gated freeze/typography/altmetric route registration on
# the active template's capabilities.toml; when the active template lacks the
# capability (e.g. the public `modern` default), those routes are NOT
# registered and the test would hit a 404. Evaluated ONCE at collection against
# the tree's default template — in the private repo (bespoke active) all three
# are True, so these tests RUN + the private suite is unchanged; in a public
# modern tree they SKIP cleanly. Distinct from ``bespoke_required`` (which
# guards file-reads of the bespoke source); a cap-route test that does NOT read
# bespoke files uses the precise cap marker instead.
_CAPS = capabilities.current()
freeze_required = pytest.mark.skipif(
    not _CAPS.freeze,
    reason="needs the freeze capability (active template's capabilities.toml)",
)
typography_required = pytest.mark.skipif(
    not _CAPS.typography,
    reason="needs the typography capability (active template's capabilities.toml)",
)
altmetric_required = pytest.mark.skipif(
    not _CAPS.altmetric,
    reason="needs the altmetric capability (active template's capabilities.toml)",
)


def _has_distinct_real_corpus() -> bool:
    """True when the shipped ``data/`` corpus is DISTINCT from the Jane Q Public
    sample (``data/example/``). A handful of tests assert an invariant that only
    holds for the private repo — that the real CV data and the example corpus
    differ in specific ways (e.g. the example's variant filenames are disjoint
    from the real ones, or the example headers are a genericized copy of the
    real ones). In a PUBLIC tree ``data/`` IS the Jane Q Public sample, so that
    comparison is vacuous and the tests must skip. Detected via ``self_bold``
    (robust to the sim injecting ``template: modern`` into ``data/meta.yml``)."""
    import yaml as _yaml

    root = paths.project_root()
    try:
        real = _yaml.safe_load((root / "data" / "meta.yml").read_text()) or {}
        example = _yaml.safe_load((root / "data" / "example" / "meta.yml").read_text()) or {}
    except Exception:
        return False
    return real.get("self_bold") != example.get("self_bold")


HAS_REAL_CORPUS = _has_distinct_real_corpus()
real_corpus_required = pytest.mark.skipif(
    not HAS_REAL_CORPUS,
    reason="compares real data/ against data/example/; vacuous when data/ IS the sample (public tree)",
)
