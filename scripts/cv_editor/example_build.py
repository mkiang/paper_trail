"""Stage + compile a CV tree whose data/ is NOT the real corpus (M5-5d CP2).

The renderer hardcodes relative data paths at module scope
(`templates/bespoke/render.typ` + `templates/bespoke/lib/typography.typ` load
`../data/meta.yml` / `../data/citation_counts.json`; every
`templates/bespoke/content/*.typ` loads `../data/<x>.yml`), so an
alternate corpus like `data/example/` cannot compile in place. This
module stages a throwaway tree — `cv.typ` + `lib/` + `templates/`
(which holds `templates/bespoke/content/`) copied verbatim, the
alternate corpus placed at `<stage>/data/` — and compiles it with the
REAL `fonts/` dir via an absolute `--font-path`. Zero Typst-source
changes; the real CV's bytes are untouched by construction.

Flask-free. CLI shim: `scripts/build_example.py`. Reuses
`freezer._copytree_filtered` (symlink-safe copy) and
`build_variant.compile_variant` (the canonical direct-compile seam).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cv_editor import paths
from cv_editor.freezer import _copytree_filtered

# Engine root: cv.typ, lib/, templates/, fonts/ + the example corpus. The
# `root=` default args below use None-sentinels resolving to these at call
# time (a `= ROOT` default would freeze the import-time value).
ROOT = paths.project_root()
EXAMPLE_DATA = paths.example_dir()


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, EXAMPLE_DATA
    ROOT = paths.project_root()
    EXAMPLE_DATA = paths.example_dir()


# Copied verbatim into the stage (data/ is supplied by the caller's corpus).
_STAGE_FILES = ("cv.typ",)
# Engine dirs staged if present (layout-dependent): the private repo ships a root
# `lib/` (flags) + resolves the shared engine via the @local package (env), while
# the restructured public tree ships the engine under `src/`. `templates/` (the
# registry + a template's glue) is always present. Missing dirs are skipped.
_STAGE_DIRS = ("lib", "src", "templates")


def stage_tree(src_data_dir: Path, stage_dir: Path, *, root: Path | None = None) -> Path:
    """Build a compilable throwaway tree at stage_dir; returns stage_dir.

    src_data_dir must hold the full corpus shape (10 section YAMLs +
    citation_counts.json — render.typ requires the snapshot to exist).
    """
    root = ROOT if root is None else Path(root)
    src_data_dir = Path(src_data_dir)
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    for name in _STAGE_FILES:
        shutil.copy2(root / name, stage_dir / name)
    for name in _STAGE_DIRS:
        src_dir = root / name
        if not src_dir.is_dir():
            continue  # layout-dependent (root lib/ privately, src/ publicly)
        out = stage_dir / name
        out.mkdir(parents=True, exist_ok=True)
        _copytree_filtered(src_dir, out)
    data_out = stage_dir / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    _copytree_filtered(src_data_dir, data_out)
    snapshot = data_out / "citation_counts.json"
    if not snapshot.exists():
        raise FileNotFoundError(
            f"{src_data_dir} has no citation_counts.json — render.typ loads it "
            "at module scope, so every variant would fail to compile"
        )
    return stage_dir


def compile_staged(
    stage_dir: Path, out_pdf: Path, *, variant_inputs: dict | None = None, root: Path | None = None
):
    """Compile the staged tree's cv.typ using the real fonts dir."""
    root = ROOT if root is None else Path(root)
    from build_variant import compile_variant  # scripts/ pythonpath, lazy

    return compile_variant(
        variant_inputs=variant_inputs or {},
        out_pdf=Path(out_pdf),
        typst_root=Path(stage_dir),
        font_path=str(root / "fonts"),
    )


def build_corpus(
    src_data_dir: Path, output_dir: Path, *, stage_dir: Path, root: Path | None = None
) -> list:
    """Stage src_data_dir and compile EVERY variant in its meta.yml.

    Mirrors build.sh's variant loop (one PDF per build_variants row) but
    against the staged tree. Returns the list of CompileResults.
    """
    root = ROOT if root is None else Path(root)
    import yaml as pyyaml

    stage_tree(src_data_dir, stage_dir, root=root)
    meta = pyyaml.safe_load((Path(stage_dir) / "data" / "meta.yml").read_text(encoding="utf-8"))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for variant in meta.get("build_variants") or []:
        inputs = {
            k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in (variant.get("inputs") or {}).items()
        }
        out_pdf = output_dir / f"{variant['filename']}.pdf"
        results.append(compile_staged(stage_dir, out_pdf, variant_inputs=inputs, root=root))
    return results
