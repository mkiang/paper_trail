"""V16-A template dispatch.

Before V16-A the bespoke renderer lived at `lib/render.typ` + `lib/emit.typ`
+ root `content/*.typ`, and `cv.typ` dispatched sections directly. V16-A
relocated the whole bespoke implementation to `templates/bespoke/{render.typ,
emit.typ, content/, template.typ}` and introduced `templates/registry.typ` —
the single "publish seam" file that statically imports template modules and
exposes `default-template`, `templates`, `section-keys`, `resolve-name(meta)`,
`resolve(meta)`. Post-P2-bespoke, `cv.typ` is the CONSUMER: it owns every
`yaml()` load, validates `meta.sections` against `registry.section-keys`, and
injects `meta` + `section-data` into `tpl.render(meta:, section-data:)` (modern
renders from the injected data; bespoke ignores it via a `..args` sink and
self-loads). `flatten.typ` gained a bespoke-only guard (freeze/flatten still
only understands the bespoke render path) that panics via `resolve-name(meta)`
when a non-bespoke template is selected.

This file guards: (a) the file move happened and nothing was left behind at
the old locations; (b) `templates/registry.typ` really is the only place
that imports a template's `template.typ`; (c) the registry's default +
`cv.typ`'s thinness + the flatten guard's presence, all via pure source
inspection (no typst required); (d) actual dispatch BEHAVIOR (unknown-
template panic, explicit-bespoke == default byte-identity, the flatten
guard firing) via real `typst` compiles/queries, skipped if `typst` isn't
on PATH; (e) that an unknown `template:` input key survives the editor's
Style form round-trip like any other unknown input key (mirrors
tests/test_v5_review_gaps.py's `custom_renderer_flag` coverage).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from _engine_guards import (
    HAS_BESPOKE,
    bespoke_required,
    local_package_dir,
    local_package_version,
)
from cv_editor import build_variants as bv
from ruamel.yaml.comments import CommentedMap

ROOT = Path(__file__).resolve().parents[1]

_HAS_TYPST = shutil.which("typst") is not None
# Bespoke render tests additionally need the private template + curated fonts.
typst_required = pytest.mark.skipif(
    not (_HAS_TYPST and HAS_BESPOKE), reason="typst + bespoke template required"
)


def _registry_resolvable(root: Path) -> bool:
    """True when every `#import "X/template.typ"` in registry.typ resolves.

    The PRIVATE registry imports `bespoke/template.typ`; the PUBLIC (exported)
    registry imports `modern` relatively from `../src/lib.typ` (no such import).
    The `simulate_public_tree.sh` proxy keeps the PRIVATE registry but excludes
    `templates/bespoke/`, so cv.typ is UNCOMPILABLE there (registry.typ:24 ->
    missing bespoke/template.typ) — a known sim-hybrid limitation, NOT a defect
    (Typst-compile faithfulness is covered by the exported-tree suite + the
    modern-on-example golden). Gate the compile tests on this so they RUN in the
    private + real-exported trees but SKIP in the sim hybrid."""
    reg = root / "templates" / "registry.typ"
    if not reg.is_file():
        return False
    for m in re.finditer(r'#import "([^"]+)/template\.typ"', reg.read_text()):
        if not (root / "templates" / m.group(1) / "template.typ").is_file():
            return False
    return True


_REGISTRY_OK = _registry_resolvable(ROOT)
# Modern-path tests need typst + a resolvable registry (modern embeds Libertinus —
# no fonts dir, no bespoke). They MUST run in the public/bespoke-absent EXPORTED
# tree they exist to guard (its registry is modern-only), so NOT gated on
# HAS_BESPOKE — but they skip in the sim hybrid whose private registry can't load.
modern_typst_required = pytest.mark.skipif(
    not (_HAS_TYPST and _REGISTRY_OK),
    reason="typst + a resolvable registry required (sim keeps the private bespoke-importing registry but drops bespoke)",
)


# ---------------------------------------------------------------------------
# Pure-source guards (always run, no typst needed)
# ---------------------------------------------------------------------------


@bespoke_required
def test_moved_files_exist():
    """The V16-A move landed: bespoke lives under templates/bespoke/, and
    nothing was left behind at the pre-V16-A locations.

    Also guards the M5-5a2 privacy move: the six look-primitive files
    (styles/typography/entry/talk/grant/publication) live under
    templates/bespoke/lib/, and root lib/ holds ONLY flags.typ."""
    must_exist = [
        ROOT / "templates" / "bespoke" / "render.typ",
        ROOT / "templates" / "bespoke" / "emit.typ",
        ROOT / "templates" / "bespoke" / "template.typ",
        ROOT / "templates" / "bespoke" / "content" / "publications.typ",
        ROOT / "templates" / "bespoke" / "content" / "header.typ",
        ROOT / "templates" / "registry.typ",
        ROOT / "templates" / "bespoke" / "lib" / "styles.typ",
        ROOT / "templates" / "bespoke" / "lib" / "typography.typ",
        ROOT / "templates" / "bespoke" / "lib" / "entry.typ",
        ROOT / "templates" / "bespoke" / "lib" / "talk.typ",
        ROOT / "templates" / "bespoke" / "lib" / "grant.typ",
        ROOT / "templates" / "bespoke" / "lib" / "publication.typ",
    ]
    for p in must_exist:
        assert p.is_file(), f"expected {p.relative_to(ROOT)} to exist"

    must_not_exist = [
        ROOT / "lib" / "render.typ",
        ROOT / "lib" / "emit.typ",
        ROOT / "content",
        ROOT / "lib" / "styles.typ",
        ROOT / "lib" / "typography.typ",
        ROOT / "lib" / "entry.typ",
        ROOT / "lib" / "talk.typ",
        ROOT / "lib" / "grant.typ",
        ROOT / "lib" / "publication.typ",
    ]
    for p in must_not_exist:
        assert not p.exists(), (
            f"expected {p.relative_to(ROOT)} to NOT exist (pre-V16-A / pre-M5-5a2 location)"
        )

    # Root lib/ carries AT MOST flags.typ: exactly {flags.typ} pre-cutover; the
    # P7 cutover deletes root lib/flags.typ (flags then come from the vendored
    # @local package), so tolerate its absence while still catching a stray
    # look-primitive left behind in root lib/ (CP4/H3 — keeps P7 full-pytest
    # achievable without weakening the "no primitives in root lib/" guard).
    root_lib_typ_files = {f.name for f in (ROOT / "lib").glob("*.typ")}
    assert root_lib_typ_files <= {"flags.typ"}, (
        f"expected root lib/ to contain at most flags.typ, found {sorted(root_lib_typ_files)}"
    )


# Derive the single @local version dir (was hardcoded .../0.1.0) so these guards
# survive the P7 bump to 1.0.0 (CP4/B2 single-source; H3). Guarded at MODULE
# scope: the public tree has NO packages/ (modern is imported relatively from
# src/), and local_package_dir() asserts exactly one dir — so compute it only
# when the dir exists. The tests that dereference _PKG are all @bespoke_required
# (skip in the public tree), so None there is never touched.
_PKG = local_package_dir(ROOT) if (ROOT / "packages" / "local" / "paper-trail").is_dir() else None


@bespoke_required
def test_modern_lives_in_local_package_and_reads_no_files():
    """P6a mechanical extraction: the `modern` template's Typst CODE moved into
    the repo-resident `@local/paper-trail` package (`src/modern/`); the private
    `templates/modern/` keeps ONLY its editor `capabilities.toml` (the
    capabilities.py discovery contract; test_p5_capabilities reads it). And
    `modern` reads ZERO files at module scope (C3) so it compiles as a package."""
    pkg_modern = _PKG / "src" / "modern"
    for name in ("render.typ", "template.typ", "styles.typ"):
        assert (pkg_modern / name).is_file(), f"expected packaged modern/{name}"
        assert not (ROOT / "templates" / "modern" / name).exists(), (
            f"templates/modern/{name} should have moved into the @local package"
        )
    assert (ROOT / "templates" / "modern" / "capabilities.toml").is_file(), (
        "modern's editor capabilities descriptor stays under templates/modern/"
    )
    # C3: no file reads (module-scope OR function-body) in the packaged modern.
    file_read = re.compile(r"\b(yaml|read)\(")
    for f in pkg_modern.glob("*.typ"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("//", 1)[0]  # ignore comments
            assert not file_read.search(code), f"{f.name}:{i} reads a file: {line.strip()!r}"


@bespoke_required
def test_local_package_hub_reexports_modern_and_flags():
    """The `@local/paper-trail` entrypoint (src/lib.typ) re-exports the `modern`
    template module + the shared flags; the private registry imports `modern`
    from `@local` (not a relative path)."""
    hub = (_PKG / "src" / "lib.typ").read_text()
    assert 'import "modern/template.typ" as modern' in hub
    assert 'import "../lib/flags.typ": *' in hub
    registry = (ROOT / "templates" / "registry.typ").read_text()
    assert f'@local/paper-trail:{local_package_version(ROOT)}": modern' in registry
    # modern's OWN flags import must stay relative (never @local — circular).
    modern_render = (_PKG / "src" / "modern" / "render.typ").read_text()
    assert "../../lib/flags.typ" in modern_render
    assert "@local" not in modern_render


_TEMPLATE_IMPORT_RE = re.compile(r'"[^"]*template\.typ"')


def _candidate_typ_files() -> list[Path]:
    """.typ files directly in the repo root, lib/, and templates/ — NOT
    recursing into templates/*/ (i.e. not templates/bespoke/...)."""
    files: list[Path] = []
    files += sorted(ROOT.glob("*.typ"))
    files += sorted((ROOT / "lib").glob("*.typ"))
    files += sorted((ROOT / "templates").glob("*.typ"))
    return files


@bespoke_required
def test_registry_is_the_only_template_importer():
    """templates/registry.typ is the publish seam: the only file that
    statically imports a template's template.typ. cv.typ and flatten.typ
    go through the registry instead (flatten.typ additionally imports the
    bespoke render.typ/emit.typ mirror directly — that's allowed; it's
    NOT an import of template.typ)."""
    registry = ROOT / "templates" / "registry.typ"
    offenders = []
    for f in _candidate_typ_files():
        if f == registry:
            continue
        text = f.read_text()
        if _TEMPLATE_IMPORT_RE.search(text):
            offenders.append(str(f.relative_to(ROOT)))
    assert offenders == [], (
        f"only templates/registry.typ may import a template's template.typ; "
        f"found imports in: {offenders}"
    )

    cv_src = (ROOT / "cv.typ").read_text()
    flatten_src = (ROOT / "flatten.typ").read_text()
    assert '"templates/bespoke/template.typ"' not in cv_src
    assert '"templates/bespoke/template.typ"' not in flatten_src
    # flatten.typ IS allowed to import the bespoke render/emit mirror
    # directly (that's the freeze/flatten emit-mirror, gotcha #41), just
    # not template.typ itself.
    assert '"templates/bespoke/render.typ"' in flatten_src
    assert '"templates/bespoke/emit.typ"' in flatten_src


@bespoke_required
def test_registry_default_is_bespoke():
    src = (ROOT / "templates" / "registry.typ").read_text()
    assert '#let default-template = "bespoke"' in src


def test_cv_typ_is_consumer_dispatcher():
    # P2-bespoke: cv.typ is the CONSUMER — it owns the yaml() loads, resolves the
    # template with the loaded meta, and injects meta + section-data.
    src = (ROOT / "cv.typ").read_text()
    assert "templates/registry.typ" in src
    assert "resolve(meta)" in src
    assert 'yaml("data/meta.yml")' in src
    assert "section-data" in src
    assert "tpl.render(meta: meta, section-data: section-data)" in src
    # still a thin dispatcher — no template internals leak in
    assert "content/" not in src
    assert "dispatch" not in src
    assert "render-section" not in src


@bespoke_required
def test_flatten_guard_present():
    src = (ROOT / "flatten.typ").read_text()
    assert "resolve-name(meta)" in src
    assert "freeze/flatten supports only the bespoke template" in src


@bespoke_required
def test_template_contract_exports():
    src = (ROOT / "templates" / "bespoke" / "template.typ").read_text()
    assert "#let sections =" in src
    # bespoke swallows the injected args via a ..args sink and self-loads
    # (a named meta: param would shadow the module-scope meta it relies on).
    assert "#let render(..args)" in src
    assert '#import "render.typ": meta' in src
    assert '#import "lib/styles.typ": setup' in src


# ---------------------------------------------------------------------------
# Typst-gated behavior tests (skip without typst on PATH)
# ---------------------------------------------------------------------------


def _compile_cv(extra_inputs: list[str], out: Path) -> subprocess.CompletedProcess:
    argv = ["typst", "compile", "--root", str(ROOT)]
    # Only add the curated fonts dir when it exists (private tree). The public
    # tree ships no fonts/ and `modern` embeds Libertinus, so a modern compile
    # needs none — don't reference a nonexistent dir.
    if (ROOT / "fonts").is_dir():
        argv += ["--font-path", "fonts"]
    argv += ["--ignore-system-fonts", *extra_inputs, "cv.typ", str(out)]
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


@typst_required
def test_unknown_template_panics_with_names():
    out = Path(tempfile.gettempdir()) / f"_v16_unknown_{uuid.uuid4().hex}.pdf"
    try:
        proc = _compile_cv(["--input", "template=doesnotexist"], out)
        assert proc.returncode != 0
        # Token-level asserts (not the exact quoted phrase): Typst escapes the
        # quotes inside a panic repr (0.15.x emits `Unknown template
        # \"doesnotexist\"`), so a substring with literal quotes won't match.
        assert "Unknown template" in proc.stderr
        assert "doesnotexist" in proc.stderr
        assert "bespoke" in proc.stderr
        assert "modern" in proc.stderr
    finally:
        out.unlink(missing_ok=True)


@typst_required
def test_explicit_bespoke_matches_default():
    out_default = Path(tempfile.gettempdir()) / f"_v16_default_{uuid.uuid4().hex}.pdf"
    out_bespoke = Path(tempfile.gettempdir()) / f"_v16_bespoke_{uuid.uuid4().hex}.pdf"
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = "1700000000"
    try:
        proc_default = subprocess.run(
            [
                "typst",
                "compile",
                "--root",
                str(ROOT),
                "--font-path",
                "fonts",
                "--ignore-system-fonts",
                "cv.typ",
                str(out_default),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc_default.returncode == 0, proc_default.stderr
        proc_bespoke = subprocess.run(
            [
                "typst",
                "compile",
                "--root",
                str(ROOT),
                "--font-path",
                "fonts",
                "--ignore-system-fonts",
                "--input",
                "template=bespoke",
                "cv.typ",
                str(out_bespoke),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc_bespoke.returncode == 0, proc_bespoke.stderr
        assert out_default.read_bytes() == out_bespoke.read_bytes()
    finally:
        out_default.unlink(missing_ok=True)
        out_bespoke.unlink(missing_ok=True)


@typst_required
def test_flatten_guard_panics_on_non_bespoke():
    # A second template ("modern") is now registered, so this is a valid-
    # but-non-bespoke --input template= name: registry.resolve-name() no
    # longer panics on an unknown NAME, which means the live compile
    # actually reaches flatten.typ's own bespoke-only guard body — the
    # exact case the earlier comment said was unreachable until a second
    # template landed. Assert the guard's OWN message fires.
    argv = [
        "typst",
        "query",
        "--root",
        str(ROOT),
        "--font-path",
        "fonts",
        "--ignore-system-fonts",
        "--input",
        "template=modern",
        "flatten.typ",
        "<flat>",
        "--field",
        "value",
        "--one",
    ]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "freeze/flatten supports only the bespoke template" in proc.stderr


@modern_typst_required
def test_cv_injects_section_data_into_modern():
    # P2-bespoke regression guard: cv.typ (the consumer) must load + inject
    # section-data so `modern` (a pure data-injection function) renders end to
    # end. The delta-oracle only exercises this via the example corpus; this
    # pins it directly against the real data/.
    out = Path(tempfile.gettempdir()) / f"_v16_modern_{uuid.uuid4().hex}.pdf"
    try:
        proc = _compile_cv(["--input", "template=modern"], out)
        assert proc.returncode == 0, proc.stderr
        assert out.exists() and out.stat().st_size > 0
    finally:
        out.unlink(missing_ok=True)


@modern_typst_required
def test_modern_compiles_font_free():
    """P5 public-tree buildability guard: `modern` renders with binary-embedded
    Libertinus, so it must compile with NO `--font-path` (a public repo won't
    ship the curated fonts/ dir). Cheap standing regression so a future modern
    font change can't silently break font-free buildability. Uses
    `--ignore-system-fonts` to prove no system font is being borrowed either."""
    out = Path(tempfile.gettempdir()) / f"_v16_modern_fontfree_{uuid.uuid4().hex}.pdf"
    try:
        argv = [
            "typst",
            "compile",
            "--root",
            str(ROOT),
            "--ignore-system-fonts",
            "--input",
            "template=modern",
            "cv.typ",
            str(out),
        ]
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert out.exists() and out.stat().st_size > 0
    finally:
        out.unlink(missing_ok=True)


@modern_typst_required
def test_cv_unknown_section_key_gives_friendly_panic():
    # P2-bespoke regression guard (scripts/CLAUDE.md gotcha #71): a typo/unknown
    # key in meta.yml `sections:` must yield cv.typ's friendly "Unknown section
    # key ... Valid keys: [...]" panic BEFORE the eager per-section yaml() load
    # degrades it to a bare "file not found". Staged: copy the real engine + a
    # full copy of data/ (module scope loads more than meta.yml), then prepend a
    # bogus key to the copied `sections:` so cv.typ's validation fires FIRST.
    import shutil

    import yaml as _yaml

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        # Layout-tolerant: the private tree stages cv.typ + templates + root
        # lib/ (bespoke default); the public tree stages cv.typ + templates +
        # src/ (modern default). Copy whichever engine dirs exist, skip the rest.
        for item in ("cv.typ", "templates", "lib", "src", "data"):
            src = ROOT / item
            if not src.exists():
                continue
            dst = stage / item
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        meta_path = stage / "data" / "meta.yml"
        meta = _yaml.safe_load(meta_path.read_text())
        meta["sections"] = ["bogus_section"] + list(meta.get("sections") or [])
        meta_path.write_text(_yaml.safe_dump(meta), encoding="utf-8")
        argv = ["typst", "compile", "--root", str(stage)]
        if (ROOT / "fonts").is_dir():
            argv += ["--font-path", str(ROOT / "fonts")]
        argv += ["--ignore-system-fonts", str(stage / "cv.typ"), str(stage / "out.pdf")]
        proc = subprocess.run(argv, cwd=stage, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "Unknown section key" in proc.stderr
        assert "bogus_section" in proc.stderr
        assert "Valid keys" in proc.stderr
        # must NOT be the degraded bare-file-not-found path
        assert "file not found" not in proc.stderr


# ---------------------------------------------------------------------------
# Editor round-trip (pure Python)
# ---------------------------------------------------------------------------


def test_template_input_survives_style_roundtrip():
    """Mirrors test_v5_review_gaps.py::test_form_to_variant_preserves_
    unknown_existing_input_keys: an unknown `template:` input key (the
    Style form doesn't surface a template picker yet) must survive a
    round-trip through variant_to_form -> form_to_variant."""
    existing = CommentedMap(
        {
            "filename": "custom",
            "inputs": CommentedMap(
                {
                    "audience": "academic",
                    "template": "bespoke",  # unknown to the editor form
                }
            ),
        }
    )
    form = bv.variant_to_form(existing)
    out = bv.form_to_variant(form, existing=existing)
    assert out["inputs"]["template"] == "bespoke"
    assert out["inputs"]["audience"] == "academic"
