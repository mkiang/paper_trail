#!/usr/bin/env python3
"""Launcher shim for the local CV editor.

Run from the project root:
    .venv/bin/python scripts/cv_editor.py

Re-execs under .venv/bin/python if invoked with the system Python, then
delegates to `cv_editor.cli.main()`. The real launcher logic now lives in
the package (`cv_editor/cli.py`) so it can also be exposed as the
`cv-editor` console script and via `python -m cv_editor`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # typst/
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _ensure_venv() -> None:
    """Re-exec under .venv/bin/python if we're not already inside a venv."""
    if VENV_PY.exists() and sys.prefix == sys.base_prefix:
        os.execv(str(VENV_PY), [str(VENV_PY), __file__, *sys.argv[1:]])


if __name__ == "__main__":
    _ensure_venv()
    # CP4/B1: pin the workspace + engine roots to this repo (mirrors
    # launch_editor.sh) so the documented `uv run scripts/cv_editor.py`
    # onboarding command stays correct post-inversion, when the vendored
    # engine is gone and an unset root would resolve into site-packages.
    # setdefault so an explicit env / launch_editor.sh export still wins;
    # pre-inversion ROOT == the legacy default, so behaviour is unchanged.
    os.environ.setdefault("CV_EDITOR_DATA_ROOT", str(ROOT))
    os.environ.setdefault("CV_EDITOR_PROJECT_ROOT", str(ROOT))
    os.environ.setdefault("TYPST_PACKAGE_PATH", str(ROOT / "packages"))
    sys.path.insert(0, str(ROOT / "scripts"))
    from cv_editor.cli import main

    sys.exit(main())
