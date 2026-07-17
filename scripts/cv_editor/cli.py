"""Console entry point for the local CV editor.

Exposed two ways:
    cv-editor                      # console script (pyproject [project.scripts])
    python -m cv_editor            # module form (see __main__.py)

The legacy `python scripts/cv_editor.py` invocation still works — that file
is now a thin shim that calls `main()` here.

Picks a free port, opens the browser at the app, and runs Flask's dev server
with the reloader off (one process owns the port; no duplicate browser tab).
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # typst/


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    # Defensive: make `scripts/` importable even if launched oddly (the
    # console-script + `-m` paths already have cv_editor on sys.path).
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from cv_editor.app import create_app

    # Mint a per-launch /quit token. Any process on the same box that can
    # POST /quit without this token gets a 403 (see app.py gotcha #68).
    os.environ.setdefault("CV_EDITOR_QUIT_TOKEN", secrets.token_hex(16))
    # Mint a per-launch Flask secret key (signs the session/flash cookie).
    os.environ.setdefault("CV_EDITOR_SECRET_KEY", secrets.token_hex(32))

    app = create_app()
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"CV Editor: {url}\nPress Ctrl-C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
