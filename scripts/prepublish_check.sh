#!/usr/bin/env bash
# Pre-push leak gate for paper_trail. Run from the repo root. Exit non-zero on
# any finding; review and clear before `gh repo create` / `git push`. This is
# self-contained (safe to run in CI); the digit cross-check against the private
# source is a separate export-time step.
#
# NOTE: this checks for the genuinely-private material — real grant/project IDs,
# coauthor names, the private tooling, local paths, licensed fonts, the private
# institution. The repo author's OWN name is intentional (LICENSE + commit
# author) and is deliberately NOT flagged here.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0

echo "[1/5] private-material scan (content + filenames + history)"
PAT='stanford|/users/mvk|dropbox/personal|garamond|bespoke|cv_editor|R00DA051534|349940|nasem|graff'
if git grep -IniE "$PAT" -- . ':!scripts/prepublish_check.sh' 2>/dev/null | grep .; then
  echo "  FAIL: private-material hit in tracked content"; fail=1
fi
if git ls-files | grep -iE "$PAT"; then echo "  FAIL: private material in a filename"; fail=1; fi
if git rev-parse HEAD >/dev/null 2>&1; then
  if git log -p --all -- . ':(exclude)scripts/prepublish_check.sh' | grep -iE "$PAT"; then
    echo "  FAIL: private material in git history"; fail=1
  fi
fi

echo "[2/5] no font files (Adobe/Garamond backstop)"
if git ls-files | grep -iE '\.(otf|ttf)$'; then echo "  FAIL: a font file is tracked"; fail=1; fi

echo "[3/5] no 'bespoke' token anywhere"
if git grep -Ini 'bespoke' -- . ':!scripts/prepublish_check.sh' 2>/dev/null | grep .; then
  echo "  FAIL: 'bespoke' token present"; fail=1
fi

echo "[4/5] no stray artifacts tracked"
if git ls-files | grep -iE '(^|/)(\.DS_Store|\.cache|\.cv_editor_backups)|(^|/)output/|\.pyc$'; then
  echo "  FAIL: stray artifact tracked"; fail=1
fi

echo "[5/5] sample PDF metadata (if present)"
for pdf in examples/*.pdf; do
  [ -e "$pdf" ] || continue
  if command -v pdffonts >/dev/null 2>&1 && pdffonts "$pdf" 2>/dev/null | grep -iE 'garamond|adobe'; then
    echo "  FAIL: $pdf embeds a non-Libertinus font"; fail=1
  fi
done

if [ "$fail" -eq 0 ]; then echo "PREPUBLISH CHECK: CLEAN"; else echo "PREPUBLISH CHECK: FINDINGS ABOVE"; fi
exit "$fail"
