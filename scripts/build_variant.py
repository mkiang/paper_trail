"""Compile a single Typst variant with arbitrary typography overrides.

This is the seam the visual-diff sweep drives. It calls `typst compile` directly
rather than going through `build.sh`, because build.sh regenerates the .bib, probes
the editor filelock, and builds every variant -- none of which a tuning iteration
wants. Output goes to a caller-chosen path (default a temp file) so the committed
PDFs in output/ are never touched.

  from build_variant import compile_variant
  res = compile_variant(variant_inputs={"audience": "industry", ...},
                        ty_overrides={"ty_body_leading": "0.34em"})
  # res.pdf_path -> the compiled PDF
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # allow `from cv_editor import`
from cv_editor import paths  # noqa: E402

# Engine root (typst project root: cv.typ, templates/, fonts/). The
# `typst_root=` default below uses a None-sentinel resolving here at call
# time (a `= TYPST_ROOT` default would freeze the import-time value).
TYPST_ROOT = paths.project_root()


@paths.on_configure
def _refresh_paths() -> None:
    global TYPST_ROOT
    TYPST_ROOT = paths.project_root()


# Inputs for the `everything` variant (most content-complete; the harness target).
# IMPORTANT: must mirror data/meta.yml's `everything` build_variant inputs --
# tests that compare against `output/everything.pdf` (built from meta.yml)
# will break if these drift. 2026-05-26 PM: aligned with user-intended meta.yml
# state -- show_dollars stays default (true; the user explicitly wants dollars
# in the everything PDF), show_pending added, show_hidden_media added.
EVERYTHING_INPUTS = {
    "audience": "industry",
    "show_pending": "true",
    "show_highlighted": "true",
    "show_oa": "true",
    "show_citations": "true",
    "show_notes": "true",
    "show_media": "true",
    "show_hidden_media": "true",
    "show_future": "true",
}


@dataclass
class CompileResult:
    pdf_path: Path
    argv: list[str]
    returncode: int
    stderr: str
    seconds: float
    ty_overrides: dict = field(default_factory=dict)


def compile_variant(
    *,
    variant_inputs: dict | None = None,
    ty_overrides: dict | None = None,
    out_pdf: Path | None = None,
    typst_root: Path | None = None,
    entry: str = "cv.typ",
    font_path: str = "fonts",
) -> CompileResult:
    typst_root = TYPST_ROOT if typst_root is None else Path(typst_root)
    # font_path defaults to the cwd-relative "fonts" (byte-identical to the
    # pre-M5-5d behavior). Staged-tree callers (cv_editor.example_build) pass
    # an ABSOLUTE path to the real fonts/ so the stage need not copy ~4MB of
    # font masters.
    # Distinguish "omitted" (use the harness default) from "explicitly empty"
    # (a no-flags variant). `dict(x or EVERYTHING_INPUTS)` would treat an empty
    # dict as omitted and silently apply the full `everything` flag set — wrong
    # for a variant with an empty/absent `inputs:` block (build.sh emits zero
    # --input for it), which would also diverge from the flattened .typ.
    variant_inputs = EVERYTHING_INPUTS if variant_inputs is None else dict(variant_inputs)
    ty_overrides = dict(ty_overrides or {})
    if out_pdf is None:
        fd = tempfile.NamedTemporaryFile(prefix="cv_variant_", suffix=".pdf", delete=False)
        fd.close()
        out_pdf = Path(fd.name)

    argv = [
        "typst",
        "compile",
        "--font-path",
        str(font_path),
        "--ignore-system-fonts",
        entry,
        str(out_pdf),
    ]
    for k, v in {**variant_inputs, **ty_overrides}.items():
        argv += ["--input", f"{k}={v}"]

    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=str(typst_root), capture_output=True, text=True)
    secs = time.monotonic() - t0
    return CompileResult(
        pdf_path=out_pdf,
        argv=argv,
        returncode=proc.returncode,
        stderr=proc.stderr,
        seconds=round(secs, 2),
        ty_overrides=ty_overrides,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--ty",
        action="append",
        default=[],
        help="ty override as key=value, e.g. ty_body_leading=0.34em",
    )
    args = ap.parse_args()
    overrides = dict(kv.split("=", 1) for kv in args.ty)
    res = compile_variant(ty_overrides=overrides, out_pdf=args.out)
    status = "ok" if res.returncode == 0 else f"FAILED ({res.returncode})"
    print(f"{status} in {res.seconds}s -> {res.pdf_path}")
    if res.returncode != 0:
        print(res.stderr)
