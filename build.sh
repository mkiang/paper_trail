#!/usr/bin/env bash
# Render every CV variant. Variants are data-driven from data/meta.yml's
# `build_variants:` — edit that to add, rename, or remove variants. Run from
# the repo root: ./build.sh
#
# The `modern` template uses Libertinus Serif, which is bundled in the Typst
# binary — no fonts need to be installed and no --font-path is required.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
OUT=output
mkdir -p "$OUT"

if ! command -v typst >/dev/null 2>&1; then
  echo "Error: 'typst' not found on PATH. Install Typst 0.15.x:" >&2
  echo "       https://github.com/typst/typst#installation" >&2
  exit 1
fi

# Friendly data check (non-blocking): surfaces located YAML/schema issues
# (e.g. an unescaped '$', an unquoted PMID) before the compile.
"$PY" scripts/validate_cv.py || true

# Regenerate publications.bib from data/publications.yml so it stays in sync.
"$PY" scripts/yaml_to_bibtex.py

# Emit + run one `typst compile` per variant (PyYAML only).
while IFS= read -r -d '' cmd; do
  eval "$cmd"
done < <("$PY" - <<'PY'
import shlex, sys, yaml
with open("data/meta.yml") as f:
    meta = yaml.safe_load(f)
for v in meta.get("build_variants", []):
    name = v["filename"]
    args = ["typst", "compile", "cv.typ", f"output/{name}.pdf"]
    for k, val in (v.get("inputs") or {}).items():
        args += ["--input", f"{k}={str(val).lower() if isinstance(val, bool) else val}"]
    sys.stdout.write(" ".join(shlex.quote(a) for a in args) + "\0")
PY
)

echo "Built PDFs in $OUT/"
