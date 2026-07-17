#!/usr/bin/env bash
# Render every CV variant. Variants are data-driven from data/meta.yml's
# `build_variants:` — edit that to add, rename, or remove variants. Run from the
# repo root: ./build.sh
#
# The `modern` template uses Libertinus Serif, which is bundled inside the Typst
# binary — no fonts need to be installed and no --font-path is required.
# --ignore-system-fonts keeps builds reproducible (only the bundled faces).
set -euo pipefail
cd "$(dirname "$0")"

# Make `python -m cv_editor.<name>` importable from a fresh clone without an
# install (the editor package lives under scripts/).
export PYTHONPATH="$PWD/scripts:${PYTHONPATH:-}"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python

OUT=output
mkdir -p "$OUT"

if ! command -v typst >/dev/null 2>&1; then
  echo "Error: 'typst' not found on PATH. Install Typst 0.15.x:" >&2
  echo "       https://github.com/typst/typst#installation" >&2
  exit 1
fi

# Friendly data preflight (non-blocking): surfaces located YAML/schema issues
# (an unescaped '$', an unquoted PMID) before the compile loop.
"$PY" scripts/check_data.py || true

# Regenerate publications.bib from data/publications.yml so it stays in sync.
"$PY" -m cv_editor.yaml_to_bibtex

# Emit one `typst compile …` line per variant (NUL-delimited so quoted args
# survive). Python is used instead of yq because it ships everywhere.
NAMES=""
while IFS= read -r -d '' cmd; do
  eval "$cmd"
  name=$(echo "$cmd" | sed -E 's#.*output/([^ ]+)\.pdf.*#\1#')
  NAMES="$NAMES,$name"
done < <("$PY" - <<'PY'
import shlex, sys, yaml
with open("data/meta.yml") as f:
    meta = yaml.safe_load(f)
for v in meta.get("build_variants", []):
    name = v["filename"]
    args = ["typst", "compile", "--ignore-system-fonts", "cv.typ", f"output/{name}.pdf"]
    for k, val in (v.get("inputs") or {}).items():
        args += ["--input", f"{k}={str(val).lower() if isinstance(val, bool) else val}"]
    sys.stdout.write(" ".join(shlex.quote(a) for a in args) + "\0")
PY
)

echo "Built: $OUT/{${NAMES#,}}.pdf"
