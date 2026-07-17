#!/usr/bin/env bash
# Structural leak gate for the public repo (runs in CI on every PR; no private
# oracle). It enforces the invariants a public paper_trail tree must always
# hold; it CANNOT semantically detect an unseen real identifier (that is done
# once, at export time, by the exporter's digit cross-check against the private
# source). Exit non-zero on any finding.
#
# The repo author's OWN name is intentional in LICENSE (copyright) + git commit
# author, so name checks are path-scoped to exclude LICENSE.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
note() { echo "  FAIL: $1"; fail=1; }

echo "[1/7] no font files (licensed-font backstop)"
git ls-files | grep -iE '\.(otf|ttf)$' && note "a font file is tracked"

echo "[2/7] no private template dir / private local paths"
git ls-files | grep -iE '(^|/)templates/bespoke/' && note "a private template path is tracked"
git grep -InE '/Users/|Dropbox/Personal' -- . ':!scripts/ci_leak_check.sh' 2>/dev/null | grep . \
  && note "a private local filesystem path is present"

# Privacy-GUARD constructs legitimately NAME a forbidden token to assert its
# ABSENCE — they enforce privacy, they do not leak. Allowlist `assert ... not in`
# lines, lines carrying an explicit `# leak-allow` marker (a denylist loop that
# asserts absence in its body), and `forbidden = [...]` denylist literals. A
# bare `for X in (...)` is NOT allowlisted (it excused ordinary tuple loops).
# CP4/B6: the `not in` alternative is ANCHORED with POSIX word boundaries
# (`(^|[^[:alnum:]_])not[[:space:]]+in([^[:alnum:]_]|$)`) — a bare `not in`
# substring also excused "not intended"/"not including". POSIX classes (not GNU
# `\b`/`\s`) so a contributor's macOS BSD `grep -E` matches identically to CI.
GUARD='(^|[^[:alnum:]_])not[[:space:]]+in([^[:alnum:]_]|$)|# leak-allow|forbidden[[:space:]]*='
echo "[3/7] no owner real name outside LICENSE (privacy-guard lines allowed)"
git grep -IniE 'kiang|mkiang|mathew' -- . ':!LICENSE' ':!scripts/ci_leak_check.sh' 2>/dev/null \
  | grep -vE "$GUARD" | grep . && note "owner real name present outside LICENSE"

echo "[4/7] no private institution / font family tokens (privacy-guard lines allowed)"
git grep -IniE 'stanford|garamond|nasem|graff' -- . ':!scripts/ci_leak_check.sh' 2>/dev/null \
  | grep -vE "$GUARD" | grep . && note "private institution/font token present"

# NOTE: this ongoing STRUCTURAL gate deliberately does NOT hardcode the owner's
# real grant/project numbers (naming them here would itself leak them). It flags
# only the personal NCBI-profile URL shape. Semantic detection of an unseen real
# numeric ID is done ONCE, at export time, by the exporter's digit cross-check
# against the private source (see the residual-risk model in plans/p6-design.md).
echo "[5/7] no personal NCBI profile URL"
git grep -InE 'ncbi\.nlm\.nih\.gov/myncbi/[A-Za-z0-9._-]+/' \
  -- . ':!scripts/ci_leak_check.sh' 2>/dev/null | grep . && note "a personal NCBI profile URL is present"

echo "[6/7] no stray private artifacts tracked"
git ls-files | grep -iE '(^|/)(\.DS_Store|\.cache|\.cv_editor_backups|qc/|publications\.bib$)|(^|/)output/|\.pyc$' \
  && note "a private/stray artifact is tracked"

# CP4/B5: the discrete-pattern layer. leak_scan.py (shipped) + leak_allow.txt are
# the SINGLE shared implementation the exporter's pre-ship gate also runs, so the
# two can't drift. It flags an ORCID/DOI/PMCID/PMID/phone/email/comma-$ in the
# shipped corpus/code that isn't a known fictional value — i.e. a seeded real
# identifier fails CI red (P8's acceptance criterion). Stdlib-only, no deps.
echo "[7/7] discrete-pattern scan (leak_scan.py vs scripts/leak_allow.txt)"
python3 scripts/leak_scan.py . || note "leak_scan.py flagged an unexpected identifier-shaped string"

if [ "$fail" -eq 0 ]; then echo "CI LEAK CHECK: CLEAN"; else echo "CI LEAK CHECK: FINDINGS ABOVE"; fi
exit "$fail"
