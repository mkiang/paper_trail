#!/usr/bin/env python3
"""Build the data/example/ sample corpus into output/*.pdf (M5-5d CP2).

Thin CLI shim over cv_editor.example_build (the check_data.py pattern):
stages a throwaway tree (cv.typ + lib/ + content/ + the corpus at data/)
in a temp dir and compiles every variant in its meta.yml. The real CV's
files and output PDFs are untouched — the example meta's variant
filenames (example-cv, example-public) are test-pinned disjoint from the
real ones.

Usage:
  .venv/bin/python scripts/build_example.py            # data/example -> output/
  .venv/bin/python scripts/build_example.py --data-dir <corpus> --output-dir <dir>
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cv_editor import example_build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=example_build.EXAMPLE_DATA,
        help="corpus to build (default: data/example)",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=example_build.ROOT / "output",
        help="where the PDFs land (default: output/)",
    )
    args = ap.parse_args(argv)

    if not args.data_dir.is_dir():
        print(f"error: {args.data_dir} is not a directory", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="cv_example_stage_") as stage:
        results = example_build.build_corpus(args.data_dir, args.output_dir, stage_dir=Path(stage))
    if not results:
        print("error: corpus meta.yml has no build_variants", file=sys.stderr)
        return 1
    failed = 0
    for res in results:
        status = "ok" if res.returncode == 0 else f"FAILED ({res.returncode})"
        print(f"{status} in {res.seconds}s -> {res.pdf_path}")
        if res.returncode != 0:
            print(res.stderr, file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
