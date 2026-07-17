#!/usr/bin/env python3
"""Render the CV to a standalone HTML document (M5 5b export).

    python scripts/export_html.py                 # -> stdout, public default view
    python scripts/export_html.py -o cv.html      # -> file
    python scripts/export_html.py --variant everything
    python scripts/export_html.py --data-dir /path/to/data

MIDDLE fidelity, default public view (the first build variant). Self-contained single file
(inline CSS, no external assets — freeze ethos). Read-only — never writes data/.
The editor exposes the same via a /export/html route (UI follows the CLI).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cv_editor import export_core, export_emit, paths  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the CV to standalone HTML (read-only).")
    ap.add_argument("--data-dir", default=None, help="data dir (default: ./data)")
    ap.add_argument(
        "--variant",
        default=None,
        help="build variant view (default: the first build variant in meta.yml)",
    )
    ap.add_argument("-o", "--output", default=None, help="write to FILE instead of stdout")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else paths.data_dir()
    doc = export_core.build_model(data_dir, target=export_core.HTML, variant=args.variant)
    out = export_emit.render_html(doc, variant=args.variant)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Wrote {args.output} ({len(out)} bytes).", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
