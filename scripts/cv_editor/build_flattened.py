#!/usr/bin/env python3
"""Build each variant's PDF AND write its flattened .typ into output/flattened_typs/.

For every target build variant this:
  1. compiles the CANONICAL PDF from cv.typ (exactly build.sh's path — the
     shipped PDF must stay byte-faithful to the render path, NOT the emit/flatten
     mirror), to <output-dir>/<name>.pdf; and
  2. if the variant uses the bespoke template, writes the flattened, self-
     contained literal-markup source to <output-dir>/flattened_typs/<name>.typ.

Flatten is bespoke-only (flatten.typ panics otherwise): a non-bespoke variant
still gets its PDF, with a printed skip line (NOT a failure). The `.typ`
half is the `freeze` CAPABILITY's implementation (P5, paper_trail inversion) —
only reached for the bespoke template; `resolve_template` already gates it.

Two flatten outputs coexist under output/ — keep them straight:
  - output/frozen-<variant>-<ns>/  archival, timestamped hand-edit workspace
    (fonts/ + render.sh, no PDF, pruned at 30d) — the editor's /freeze button.
  - output/flattened_typs/<name>.typ  disposable mirror of the LAST build,
    overwritten each run, paired with the canonical PDF — this tool.

Usage:
  .venv/bin/python scripts/build_flattened.py            # primary variant only
  .venv/bin/python scripts/build_flattened.py --all      # every variant
  .venv/bin/python scripts/build_flattened.py --verify   # also compile each .typ (self-containment)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import build_variant  # top-level py-module (viz cluster); on path via the editable install

from cv_editor import build_runner, freezer, paths, yaml_io
from cv_editor import build_variants as bv

# Two-field seam (P1): ROOT is WORKSPACE (output/ default + data/meta.yml);
# _ENGINE is the PROJECT root (build_lock_check.py + the build cwd).
ROOT = paths.data_root()
_ENGINE = paths.project_root()


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, _ENGINE
    ROOT = paths.data_root()
    _ENGINE = paths.project_root()


def resolve_template(variant: dict, meta: dict) -> str:
    """The template a variant resolves to, mirroring templates/registry.typ's
    precedence: `--input template=` (rides inputs:) → meta top-level `template:`
    → the disk-derived default (bespoke if present, else modern — see
    capabilities.default_template_name). The flatten.typ panic is the backstop."""
    from cv_editor import capabilities

    inputs = variant.get("inputs") or {}
    return str(
        inputs.get("template") or meta.get("template") or capabilities.default_template_name()
    ).strip()


def select_variants(meta: dict, *, all_variants: bool) -> list[dict]:
    """The variant rows to build. --all → every row; else the primary variant
    (build_runner.default_variant_name()). If no row matches the primary name
    (blank/unreadable meta), fall back to a synthetic no-inputs row so the
    primary path still produces a bare compile — mirrors
    build_runner._default_variant_argv's fallback."""
    variants = list(meta.get("build_variants") or [])
    if all_variants:
        return variants
    name = build_runner.default_variant_name()
    row = next((v for v in variants if (v.get("filename") or "").strip() == name), None)
    return [row] if row is not None else [{"filename": name}]


def _verify_self_contained(flat_path: Path) -> tuple[bool, str]:
    """Compile the emitted .typ to a throwaway PDF to prove it is self-
    contained (guards against an emit.typ drift on live data shipping a broken
    .typ next to a good PDF). Returns (ok, stderr)."""
    with tempfile.NamedTemporaryFile(prefix="cv_flat_verify_", suffix=".pdf") as tmp:
        res = build_variant.compile_variant(
            variant_inputs={},  # the flat file bakes every flag; needs no --input
            entry=str(flat_path),
            out_pdf=Path(tmp.name),
        )
    return res.returncode == 0, res.stderr


def build_one(variant: dict, meta: dict, output_dir: Path, *, verify: bool = False) -> dict:
    """Build one variant's PDF + (if bespoke) its flattened .typ. Pure of
    argparse/lock so it is unit-testable with compile_variant/flatten_source
    monkeypatched. Returns a result dict for the summary + exit code."""
    name = (variant.get("filename") or "").strip()
    inputs = bv.variant_inputs_map(variant)

    print(f"[build-flat] {name}: compiling PDF...", flush=True)
    res = build_variant.compile_variant(variant_inputs=inputs, out_pdf=output_dir / f"{name}.pdf")
    pdf_ok = res.returncode == 0
    if pdf_ok:
        print(f"[build-flat] {name}: PDF ok ({res.seconds}s) -> {res.pdf_path}", flush=True)
    else:
        print(f"[build-flat] {name}: PDF FAILED (exit {res.returncode})", flush=True)
        if res.stderr:
            print(res.stderr, flush=True)

    result = {
        "name": name,
        "pdf_ok": pdf_ok,
        "flat_written": False,
        "flat_failed": False,
        "verify_failed": False,
        "skipped": False,
    }

    tpl = resolve_template(variant, meta)
    if tpl != "bespoke":
        result["skipped"] = True
        print(f"[build-flat] {name}: flatten skipped (template={tpl}; bespoke-only)", flush=True)
        return result

    print(f"[build-flat] {name}: flattening...", flush=True)
    try:
        src = freezer.flatten_source(inputs, name)
        flat_dir = output_dir / "flattened_typs"
        flat_dir.mkdir(parents=True, exist_ok=True)
        flat_path = flat_dir / f"{name}.typ"
        flat_path.write_text(src, encoding="utf-8")
        result["flat_written"] = True
        print(
            f"[build-flat] {name}: flattened -> {flat_path} ({len(src) // 1024} KB)",
            flush=True,
        )
    except Exception as e:  # a real typst query failure on a bespoke variant
        result["flat_failed"] = True
        print(f"[build-flat] {name}: FLATTEN FAILED: {type(e).__name__}: {e}", flush=True)
        return result

    if verify:
        ok, stderr = _verify_self_contained(flat_path)
        if ok:
            print(f"[build-flat] {name}: flattened .typ compiles standalone.", flush=True)
        else:
            result["verify_failed"] = True
            print(f"[build-flat] {name}: SELF-CONTAINMENT CHECK FAILED", flush=True)
            if stderr:
                print(stderr, flush=True)
    return result


_README = """\
# Flattened CVs (`output/flattened_typs/`)

Each `<variant>.typ` here is a **disposable, self-contained** flattened build —
literal Typst markup, one explicit call per entry, every flag baked in (no
`#import`, no `data/`, no `--input`). Regenerated (overwritten) each time you run
`make build-flat` / `make build-flat-all` or the editor's "Build + flatten"
button, and paired with the canonical `output/<variant>.pdf`.

## Compile one

From the project root:

```bash
typst compile --font-path fonts --ignore-system-fonts \\
  output/flattened_typs/<variant>.typ output/<variant>.pdf
```

## Not the same as `output/frozen-*/`

`output/frozen-<variant>-<ns>/` (the editor's **Freeze & flatten**) is an
*archival* snapshot: timestamped, kept until pruned (30d), with its own `fonts/`
and `render.sh`, meant for hand-editing a one-off. The files here are a
*throwaway mirror* of the most recent build — edit them freely, but they are
overwritten on the next run.
"""


def _write_readme(flat_dir: Path) -> None:
    flat_dir.mkdir(parents=True, exist_ok=True)
    (flat_dir / "README.md").write_text(_README, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--all",
        action="store_true",
        dest="all_variants",
        help="build every variant (default: the primary/default variant only)",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output",
        help="where PDFs + flattened_typs/ land (default: output/)",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="also compile each emitted .typ to prove it is self-contained",
    )
    args = ap.parse_args(argv)

    # Cooperate with the editor's build lock, exactly like build.sh (line 47).
    # Skipped automatically when the editor launched us: stream_subprocess sets
    # CV_EDITOR_INTERNAL_BUILD=1, which build_lock_check honors (exit 0). Use
    # sys.executable, not bare python3 — build_lock_check imports filelock
    # before that env short-circuit, so it needs the venv interpreter.
    probe = subprocess.run(
        [sys.executable, str(_ENGINE / "scripts" / "build_lock_check.py")], cwd=str(_ENGINE)
    )
    if probe.returncode != 0:
        print(
            "[build-flat] another build holds the editor lock; aborting.",
            file=sys.stderr,
        )
        return 1

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "flattened_typs").mkdir(parents=True, exist_ok=True)

    try:
        _, meta = yaml_io.load(ROOT / "data" / "meta.yml")
    except Exception as e:
        print(f"[build-flat] could not read data/meta.yml: {e}", file=sys.stderr)
        meta = {}
    meta = meta or {}

    variants = select_variants(meta, all_variants=args.all_variants)
    if not variants:
        print("[build-flat] no build_variants to build.", file=sys.stderr)
        return 1

    results = [build_one(v, meta, output_dir, verify=args.verify) for v in variants]
    _write_readme(output_dir / "flattened_typs")

    n_pdf = sum(1 for r in results if r["pdf_ok"])
    n_flat = sum(1 for r in results if r["flat_written"])
    n_skip = sum(1 for r in results if r["skipped"])
    # A non-bespoke flatten SKIP is not a failure; a PDF-compile failure, a real
    # flatten error, or a --verify self-containment failure is.
    failed = sum(1 for r in results if not r["pdf_ok"] or r["flat_failed"] or r["verify_failed"])
    print(
        f"[build-flat] done: {len(results)} variant(s), {n_pdf} PDF(s), "
        f"{n_flat} flattened, {n_skip} skipped, {failed} failed.",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
