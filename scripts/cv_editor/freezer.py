"""
P5 (paper_trail inversion): this module is the `freeze` CAPABILITY's
implementation. Freeze/flatten is bespoke-only (flatten.typ panics on any other
template), so it is only reached when the freeze capability is present — see
cv_editor/capabilities.py + the freeze-route gating in style_routes.py. A public
modern-only tree never invokes it.

Freeze the canonical CV into a self-contained `output/frozen-<ts>/cv.typ` whose
body is LITERAL Typst markup — one explicit `#pub-entry(...)` / `#entry(...)` /
`#talk(...)` / `#grant(...)` call per entry — so the author can hand-edit any
entry directly (fix a title, bold a word, nudge layout) and re-render, without
touching the canonical YAML / lib / content.

What's produced:
    cv.typ      single self-contained file: baked typography (`ty`) + meta
                dicts + the inlined layout helpers + the emitted literal-markup
                body. No `#import`, no `data/`, no `--input`.
    fonts/      the curated fonts the active template needs, if any
                (template-dependent; templates that use only Typst's bundled
                fonts produce no fonts/ dir).
    render.sh   `typst compile --font-path fonts --ignore-system-fonts cv.typ`.

The freeze bakes ONE build variant (literal content can't respond to --input),
chosen by the caller (default `cv`). It is non-destructive: the canonical
source tree is unchanged.

Mechanics: `flatten.typ` (compiled via `typst query` with the variant's flags)
yields the emitted body + the repr'd ty/meta dicts; this module inlines the
layout helpers (styles + publication/entry/talk/grant + the FLATTEN_INLINE-
anchored helpers in render.typ) and assembles the file. See
plans/freeze-flatten.md and templates/bespoke/emit.typ.

Concurrency: timestamp uses ns granularity (`time.time_ns()`) so two
near-simultaneous `/freeze` calls produce distinct directories.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cv_editor import paths

# Two-field seam (P1): ROOT is the WORKSPACE root (the output/frozen-* target
# + its relpath for the frozen-list UI); _ENGINE is the PROJECT root, where
# templates/bespoke/*, fonts/, and the typst `--root`/cwd resolve. The freeze
# path reads engine assets IN-PROCESS (not via build.sh), so this split is
# needed for the consumer even before the build.sh rewire (P7 gate #4).
ROOT = paths.data_root()
_ENGINE = paths.project_root()


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, _ENGINE
    ROOT = paths.data_root()
    _ENGINE = paths.project_root()


# Files within copied trees that should be skipped.
SKIP_FILES = {".DS_Store"}

# Lib files inlined VERBATIM (minus their `#import` lines), in dependency order:
# styles defines the consts + setup/section/subsection/institution the rest use;
# the layout helpers follow. typography.typ + flags.typ are NOT inlined — `ty`
# is baked as a dict and `visible`/flags are baked below.
_VERBATIM_STYLES = "templates/bespoke/lib/styles.typ"
_VERBATIM_HELPERS = (
    "templates/bespoke/lib/publication.typ",
    "templates/bespoke/lib/entry.typ",
    "templates/bespoke/lib/talk.typ",
    "templates/bespoke/lib/grant.typ",
)

# `visible()` mirrored from lib/flags.typ. The emitted body omits audiences/
# hide-from (entries are pre-filtered), so this returns true for them; it is
# present only because the inlined layout helpers call it.
_VISIBLE_DEF = """\
#let visible(audiences: (), hide-from: ()) = {
  if hide-from.contains(audience) { return false }
  audience == "full" or audiences.len() == 0 or audiences.contains(audience)
}
"""


_FROZEN_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _sanitize_variant_for_dir(variant_name: str) -> str:
    """Coerce a variant name into a filesystem-safe slug. Defense in
    depth — the form route already validates against the meta.yml
    allow-list, but freeze_workspace is also called directly from
    tests and could receive arbitrary input."""
    safe = (variant_name or "").strip().lower()
    if _FROZEN_VARIANT_RE.match(safe):
        return safe
    return "unknown"


@dataclass
class FreezeResult:
    """Returned by `freeze_workspace`."""

    path: Path  # absolute path to the new directory
    relpath: str  # relative to ROOT (for display + linking)
    files_copied: int
    bytes_copied: int
    variant: str = ""  # extracted from the dir name; populated by list_frozen


def _strip_imports(src: str) -> str:
    """Drop `#import` lines (the frozen file is self-contained)."""
    return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#import"))


def _extract_flatten_blocks(render_src: str) -> str:
    """Pull the `// FLATTEN_INLINE_BEGIN <name> ... // FLATTEN_INLINE_END`
    blocks out of templates/bespoke/render.typ, in file order (which is also
    dependency order: _self-bold-terms → mk → header helpers → linkify-dois)."""
    blocks = re.findall(
        r"^// FLATTEN_INLINE_BEGIN[^\n]*\n(.*?)\n// FLATTEN_INLINE_END\s*$",
        render_src,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not blocks:
        raise RuntimeError("no FLATTEN_INLINE blocks found in templates/bespoke/render.typ")
    return "\n\n".join(b.rstrip() for b in blocks)


def _typst_str(s: str) -> str:
    """Render a Python string as a Typst double-quoted string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _query_flatten(variant_inputs: dict | None) -> dict:
    """Run `typst query` on flatten.typ with the variant's flags; return the
    decoded `<flat>` metadata dict {body, ty, meta, audience, show_dollars}."""
    argv = [
        "typst",
        "query",
        "--root",
        str(_ENGINE),
        "--font-path",
        "fonts",
        "--ignore-system-fonts",
    ]
    for k, v in (variant_inputs or {}).items():
        argv += ["--input", f"{k}={v}"]
    argv += ["flatten.typ", "<flat>", "--field", "value", "--one"]
    proc = subprocess.run(argv, cwd=_ENGINE, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"typst query (flatten.typ) failed:\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"typst query returned non-JSON output: {e}\n{proc.stdout[:400]}")


def _assemble_cv_typ(flat: dict, variant_name: str) -> str:
    """Build the self-contained cv.typ source from the queried flatten data
    plus the inlined layout helpers."""
    styles = _strip_imports((_ENGINE / _VERBATIM_STYLES).read_text())
    render_blocks = _extract_flatten_blocks(
        (_ENGINE / "templates" / "bespoke" / "render.typ").read_text()
    )
    helpers = "\n".join(_strip_imports((_ENGINE / f).read_text()) for f in _VERBATIM_HELPERS)

    banner = (
        "// FROZEN, FLATTENED CV — generated by the cv_editor freeze tool.\n"
        f"// Variant: {variant_name}.  Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}.\n"
        "// Single self-contained file: baked `ty`/`meta` + inlined layout\n"
        "// helpers + literal-markup body. Edit any entry below directly, then\n"
        "// run ./render.sh. This is a one-off copy — changes do NOT propagate\n"
        "// back to the canonical typst/ source.\n"
    )
    parts = [
        banner,
        f"#let ty = {flat['ty']}",
        f"#let meta = {flat['meta']}",
        f"#let audience = {_typst_str(flat['audience'])}",
        f"#let show-dollars = {flat['show_dollars']}",
        _VISIBLE_DEF,
        "// ---- inlined layout primitives (templates/bespoke/lib/styles.typ) ----",
        styles,
        "// ---- inlined header + markup helpers (templates/bespoke/render.typ) ----",
        render_blocks,
        "// ---- inlined entry helpers (templates/bespoke/lib/{publication,entry,talk,grant}.typ) ----",
        helpers,
        "// ---- document ----",
        "#show: setup.with(meta: meta)",
        "#render-header(meta)",
        "// ---- body (literal markup; hand-edit freely) ----",
        flat["body"],
    ]
    return "\n\n".join(parts) + "\n"


def _resolve_variant_name(variant_name: str | None) -> str:
    """Resolve a caller-supplied variant name, or the default (first build
    variant in meta.yml) when None. Used only as a cosmetic label (the
    `// Variant:` comment / dir name / README)."""
    if variant_name:
        return variant_name
    from cv_editor import build_variants as bv
    from cv_editor import yaml_io

    try:
        _, meta = yaml_io.load(ROOT / "data" / "meta.yml")
        return bv.default_variant_name(meta or {})
    except Exception:
        return bv.DEFAULT_VARIANT_FALLBACK


def flatten_source(variant_inputs: dict | None = None, variant_name: str | None = None) -> str:
    """Assembled self-contained flattened cv.typ SOURCE for one build variant
    (no dir, no fonts copy). The single-file core of freeze_workspace() — used
    directly by scripts/build_flattened.py to drop one .typ into a shared dir.
    `variant_name=None` resolves to the default variant (first in meta.yml).

    May raise (via _query_flatten) on a typst query failure or a non-bespoke
    template (flatten.typ panics); callers decide how to surface it."""
    return _assemble_cv_typ(_query_flatten(variant_inputs), _resolve_variant_name(variant_name))


def freeze_workspace(
    variant_inputs: dict | None = None, variant_name: str | None = None
) -> FreezeResult:
    """Create a timestamped frozen, flattened workspace for one build variant.
    `variant_name=None` resolves to the default variant (first in meta.yml).

    The flatten query runs BEFORE the target dir is created, so a query/compile
    failure leaves nothing to clean up. Once the dir exists, any later failure
    triggers cleanup so no orphan directory is left behind.
    """
    variant_name = _resolve_variant_name(variant_name)
    cv_typ = flatten_source(variant_inputs, variant_name)  # may raise; no dir yet

    # 2026-05-28: dir name now carries the variant so the user can tell
    # at a glance which variant a frozen workspace holds. Without this,
    # every dir was `frozen-<ts>/` and the variant was only visible by
    # opening cv.typ and reading the // Variant: comment.
    safe_variant = _sanitize_variant_for_dir(variant_name)
    target = ROOT / "output" / f"frozen-{safe_variant}-{time.time_ns()}"
    if target.exists():
        raise FileExistsError(f"freeze target already exists: {target}")
    target.mkdir(parents=True)
    try:
        files_copied = 0
        bytes_copied = 0

        fonts_src = _ENGINE / "fonts"
        if fonts_src.exists() and not fonts_src.is_symlink():
            n, b = _copytree_filtered(fonts_src, target / "fonts")
            files_copied += n
            bytes_copied += b

        cv_path = target / "cv.typ"
        cv_path.write_text(cv_typ)
        files_copied += 1
        bytes_copied += cv_path.stat().st_size

        (target / "README.md").write_text(_render_readme(variant_name))
        sh = target / "render.sh"
        sh.write_text(_render_shell())
        sh.chmod(0o755)
        files_copied += 2

        return FreezeResult(
            path=target,
            relpath=str(target.relative_to(ROOT)),
            files_copied=files_copied,
            bytes_copied=bytes_copied,
        )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _copytree_filtered(src: Path, dst: Path) -> tuple[int, int]:
    """shutil.copytree variant that skips SKIP_FILES and symlinks.
    Reviewer-1 MEDIUM V5-D: don't follow symlinks (avoids amplifying
    storage when fonts/ etc. is a symlink, and avoids broken-link
    crashes mid-copy). Returns (count, bytes)."""
    n = 0
    b = 0
    for entry in src.iterdir():
        if entry.name in SKIP_FILES:
            continue
        if entry.is_symlink():
            continue  # skip symlinks rather than dereference them
        out = dst / entry.name
        if entry.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            sub_n, sub_b = _copytree_filtered(entry, out)
            n += sub_n
            b += sub_b
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, out)
            n += 1
            b += out.stat().st_size
    return n, b


def _render_readme(variant_name: str) -> str:
    return f"""# Frozen, flattened CV ({variant_name})

A single self-contained `cv.typ` for the **{variant_name}** build variant.
Its body is literal Typst markup — one explicit call per entry — so you can
hand-edit any entry directly (fix a title, bold a word, tweak spacing) and
re-render. The layout helpers and typography are inlined at the top; `fonts/`
is the only other file needed.

## Render

```bash
./render.sh
```

…or run typst directly:

```bash
typst compile --font-path fonts --ignore-system-fonts cv.typ cv.pdf
```

## Notes

- This is **flattened**: every flag (audience, show_oa, show_media, dollar
  amounts, etc.) is already baked into the literal content for the
  **{variant_name}** variant. There is no `--input`; to change a flag, edit the
  markup directly or re-freeze a different variant from the editor.
- The author's name renders as `#text(fill: rgb("#000000"))[*Your Name*]` — that
  verbose form is the faithful self-bold; edit the text inside the brackets.
- This directory is a one-off copy. Changes here do NOT propagate back to the
  canonical typst/ source. Delete the directory when you're done.
"""


def _render_shell() -> str:
    return """#!/usr/bin/env bash
# Render the frozen, flattened CV to cv.pdf. Every build flag is already baked
# into the literal content, so no flag overrides are needed.
set -euo pipefail
cd "$(dirname "$0")"
typst compile \\
  --font-path fonts \\
  --ignore-system-fonts \\
  cv.typ cv.pdf
echo "Built: $(pwd)/cv.pdf"
"""


_FROZEN_DIR_RE = re.compile(r"^frozen-(?P<variant>[a-z0-9][a-z0-9_-]*?)-(?P<ts>\d+)$")


def _extract_variant_from_dir(name: str) -> str:
    """Extract the variant slug from a `frozen-<variant>-<ts>` dirname.

    Returns "" for legacy `frozen-<ts>` dirs (created before the
    2026-05-28 rename); the freeze UI falls back to reading the cv.typ
    banner in that case."""
    m = _FROZEN_DIR_RE.match(name)
    return m.group("variant") if m else ""


def _read_variant_from_banner(d: Path) -> str:
    """Parse the variant name out of cv.typ's `// Variant: <name>.`
    banner. Used as a fallback for legacy frozen dirs that don't carry
    the variant in their name."""
    cv = d / "cv.typ"
    if not cv.exists():
        return ""
    try:
        with cv.open(encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"// Variant:\s+([A-Za-z0-9_-]+)", line.strip())
                if m:
                    return m.group(1).lower()
                if not line.startswith("//"):
                    break  # left the comment header; stop scanning
    except OSError:
        pass
    return ""


def list_frozen() -> list[FreezeResult]:
    """Enumerate existing frozen workspaces under output/. Newest first."""
    out_dir = ROOT / "output"
    if not out_dir.exists():
        return []
    results: list[FreezeResult] = []
    for d in sorted(out_dir.glob("frozen-*"), reverse=True):
        if not d.is_dir():
            continue
        total_n = 0
        total_b = 0
        for p in d.rglob("*"):
            if p.is_file():
                total_n += 1
                total_b += p.stat().st_size
        # Variant: prefer the dir-name encoding; fall back to the cv.typ
        # banner for legacy `frozen-<ts>` dirs.
        variant = _extract_variant_from_dir(d.name) or _read_variant_from_banner(d)
        results.append(
            FreezeResult(
                path=d,
                relpath=str(d.relative_to(ROOT)),
                files_copied=total_n,
                bytes_copied=total_b,
                variant=variant,
            )
        )
    return results


def prune_frozen(*, days_old: int = 30) -> list[str]:
    """Delete frozen workspaces whose directory mtime is older than
    `days_old` days. Returns the list of deleted directory names.

    `days_old` must be a positive integer; passing 0 or negative is a
    programming error and raises ValueError rather than nuking
    everything. The mtime check uses the directory's mtime (which
    reflects file additions/changes inside it) so a workspace the user
    recently re-rendered won't be pruned.
    """
    if not isinstance(days_old, int) or days_old <= 0:
        raise ValueError(f"days_old must be a positive int; got {days_old!r}")
    cutoff = time.time() - days_old * 86400
    deleted: list[str] = []
    for fr in list_frozen():
        try:
            mtime = fr.path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        # V17-D fix (C-M5): a manually-created frozen-foo dir survives
        # list_frozen()'s glob but fails delete_frozen()'s regex with
        # ValueError. Without this guard, one bad name would abort the
        # whole prune loop. Skip the bad name and continue.
        try:
            delete_frozen(fr.path.name)
            deleted.append(fr.path.name)
        except ValueError:
            continue
    return deleted


def delete_frozen(name: str) -> None:
    """Delete one frozen workspace by directory name. Validates the name
    so a crafted name can't escape output/.

    Accepted patterns (legacy + 2026-05-28):
      - `frozen-<digits>`              (pre-variant-in-dirname)
      - `frozen-<variant>-<digits>`    (current; variant is a-z0-9_-)
    """
    if not (re.fullmatch(r"frozen-\d+", name) or _FROZEN_DIR_RE.match(name)):
        raise ValueError(f"invalid frozen workspace name: {name!r}")
    target = ROOT / "output" / name
    if not target.exists() or not target.is_dir():
        raise FileNotFoundError(target)
    # Final safety: resolve both sides so symlink/Dropbox path differences
    # don't produce a false negative (Reviewer-1 MEDIUM V5-D).
    out_dir = (ROOT / "output").resolve()
    if out_dir not in target.resolve().parents:
        raise ValueError(f"resolved path escapes output/: {target}")
    shutil.rmtree(target)
