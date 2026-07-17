#!/usr/bin/env python3
"""Environment doctor — check that the toolchain needed to build/edit the CV
is present, and report actionable hints when something is missing.

    python scripts/doctor.py        # or: make doctor

Exit code is 0 when everything required is present, 1 otherwise.

Scope (M0): typst binary, core Python deps, data files on disk. The M5
"friendly validation-on-load" feature extends this with per-file YAML/schema
linting (it will import and call `check_data()` here).
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # typst/

CORE_DEPS = ("flask", "ruamel.yaml", "yaml", "bibtexparser", "filelock")
REQUIRED_DATA = ("meta.yml", "publications.yml")

OK = "[ OK ]"
BAD = "[FAIL]"


def _check_typst() -> bool:
    path = shutil.which("typst")
    if not path:
        print(
            f"{BAD} typst binary: not on PATH.\n"
            "       Install from https://github.com/typst/typst/releases "
            "(or `brew install typst`)."
        )
        return False
    # Report the version and, if a `.typst-version` pin is present, warn (non-fatal)
    # on a mismatch. The pin matters for byte-reproducible builds — the paper_trail
    # inversion delta-oracle (scripts/repro_oracle.sh) hard-fails on a mismatch; here
    # we only surface it so a daily build isn't blocked by a version bump.
    import subprocess

    pin_file = ROOT / ".typst-version"
    have = ""
    try:
        out = subprocess.run(
            ["typst", "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        have = out.split()[1] if len(out.split()) > 1 else out.strip()
    except Exception:
        pass
    if pin_file.exists():
        pin = pin_file.read_text().strip()
        if have and have != pin:
            print(f"{OK} typst binary: {path} (version {have}; WARNING: .typst-version pins {pin})")
        else:
            print(f"{OK} typst binary: {path} (version {have or '?'}, matches pin {pin})")
    else:
        print(f"{OK} typst binary: {path}" + (f" (version {have})" if have else ""))
    return True


def _check_deps() -> bool:
    ok = True
    for mod in CORE_DEPS:
        try:
            importlib.import_module(mod)
            print(f"{OK} python dep: {mod}")
        except Exception as exc:  # ImportError + any import-time failure
            print(f"{BAD} python dep: {mod} ({exc}) -- run `make install`")
            ok = False
    return ok


def _check_data() -> bool:
    data = ROOT / "data"
    missing = [f for f in REQUIRED_DATA if not (data / f).exists()]
    if missing:
        print(f"{BAD} data/: missing {missing} (run `make init` to scaffold a blank CV)")
        return False
    print(f"{OK} data/: {', '.join(REQUIRED_DATA)} present")
    return True


def _check_data_lint() -> bool:
    """Whole-corpus YAML/schema lint (M5). Imported lazily so a missing dep
    surfaces in `_check_deps` first rather than crashing here. ERRORs fail the
    doctor (genuine build/save breakers); WARNINGs print but stay advisory —
    the corpus still builds, so they must not gate `make doctor`."""
    try:
        from cv_editor import data_check
    except Exception as exc:  # deps not yet installed, etc.
        print(f"{BAD} data lint: cannot import validator ({exc}) -- run `make install`")
        return False
    issues = data_check.check_data()
    errs = [i for i in issues if i.severity == data_check.ERROR]
    warns = [i for i in issues if i.severity == data_check.WARNING]
    if not issues:
        print(f"{OK} data lint: corpus clean (0 errors, 0 warnings)")
        return True
    for i in errs + warns:
        loc = f":{i.line}" if i.line else ""
        fld = f" [{i.field}]" if i.field else ""
        print(f"       {i.severity.upper()} {i.file}{loc}{fld}  {i.entry_label}: {i.message}")
    if errs:
        print(
            f"{BAD} data lint: {len(errs)} error(s), {len(warns)} warning(s) "
            "-- run `python scripts/check_data.py` for detail"
        )
        return False
    print(f"{OK} data lint: {len(warns)} warning(s) (advisory; build not blocked)")
    return True


def _norm_ver(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    return v[1:] if v[:1].lower() == "v" else v


def _typst_toml_version(path: Path) -> str | None:
    try:
        import tomllib

        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None
    pkg = data.get("package")
    if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
        return pkg["version"]
    v = data.get("version")
    return v if isinstance(v, str) else None


def _pyproject_paper_trail_pin(path: Path) -> str | None:
    try:
        import tomllib

        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None
    deps = (data.get("project") or {}).get("dependencies") or []
    for dep in deps:
        if isinstance(dep, str) and "paper-trail" in dep.lower():
            m = re.search(r"v?(\d+\.\d+\.\d+)", dep)
            return m.group(1) if m else dep
    return None


def _installed_paper_trail_version() -> str | None:
    """Version of a pip-installed `paper-trail`, or None if not installed.

    For a git-tag install (`pip install "paper-trail @ git+...@v1.0.0"`), the
    tag lives in `direct_url.json`'s `vcs_info.requested_revision`, NOT in
    `metadata.version` (which would be the setuptools-computed number). Prefer
    the requested revision; fall back to the metadata version for a plain
    PyPI/version install."""
    from importlib import metadata

    try:
        dist = metadata.distribution("paper-trail")
    except Exception:  # PackageNotFoundError + any metadata error
        return None
    try:
        durl = dist.read_text("direct_url.json")
        if durl:
            import json

            info = json.loads(durl)
            rev = (info.get("vcs_info") or {}).get("requested_revision")
            if rev:
                return rev
    except Exception:
        pass
    try:
        return dist.version
    except Exception:
        return None


_LOCAL_IMPORT_RE = re.compile(r"@local/paper-trail:(\d+\.\d+\.\d+)")


def scan_local_versions(root: Path) -> dict[str, str]:
    """Every place that PINS the ``@local/paper-trail`` Typst version: each
    package dir name, its ``typst.toml`` version, and every ``.typ`` import
    ``@local/paper-trail:<ver>``. CP4/B2: these must ALWAYS agree, so a P7
    partial bump (some of the ~10 sites left at the old version) is caught.
    Returns ``{location: version}`` (``output/``/``.venv``/``.git`` skipped).
    The exporter's OUTPUT ``typst.toml`` rewrite is a ``re.sub`` on content, not
    an import/dir literal, so it is naturally excluded."""
    out: dict[str, str] = {}
    base = root / "packages" / "local" / "paper-trail"
    if base.is_dir():
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            out[f"packages/local/paper-trail/{d.name}/ (dir)"] = d.name
            tv = _typst_toml_version(d / "typst.toml")
            if tv:
                out[f"packages/local/paper-trail/{d.name}/typst.toml"] = tv
    for f in root.rglob("*.typ"):
        if {"output", ".venv", ".git"} & set(f.parts):
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = _LOCAL_IMPORT_RE.search(line)
            if m:
                out[f"{f.relative_to(root)}:{i}"] = m.group(1)
    return out


def _check_versions() -> bool:
    """CP4/H1 three-version handshake: the `@local` Typst package version, the
    pip-installed `paper-trail` version, and the `pyproject` pin must agree
    post-inversion. Pre-inversion (paper-trail not yet published/installed) the
    pip + pyproject legs are ADVISORY — only a version SKEW across the @local
    sites (dir / typst.toml / the .typ imports; CP4/B2) fails — so `make doctor`
    is green in the private dev repo. Fully exercised at P7 once installed."""
    ok = True
    scanned = scan_local_versions(ROOT)
    if not scanned:
        print(f"{OK} @local package: none found (nothing to check)")
        return True
    dir_versions = {v for k, v in scanned.items() if k.endswith("(dir)")}
    if len(dir_versions) > 1:
        print(
            f"{BAD} @local package: {len(dir_versions)} version dirs {sorted(dir_versions)} -- expected one"
        )
        ok = False
    distinct = set(scanned.values())
    if len(distinct) > 1:
        canon = next(iter(dir_versions), None) or sorted(distinct)[0]
        print(f"{BAD} @local version SKEW across {len(scanned)} sites (expected {canon}):")
        for loc, v in sorted(scanned.items()):
            if v != canon:
                print(f"       {loc}: {v}")
        ok = False
    else:
        print(
            f"{OK} @local package: version {next(iter(distinct))} unanimous across {len(scanned)} site(s)"
        )
    local_ver = next(iter(dir_versions), None) or next(iter(distinct))
    pip_ver = _installed_paper_trail_version()
    if pip_ver is None:
        print(f"{OK} paper-trail dependency: not pip-installed (pre-P7 -- advisory)")
        return ok
    pin_ver = _pyproject_paper_trail_pin(ROOT / "pyproject.toml")
    present = {
        "@local": _norm_ver(local_ver),
        "pip": _norm_ver(pip_ver),
        "pyproject": _norm_ver(pin_ver),
    }
    distinct = {v for v in present.values() if v}
    if len(distinct) > 1:
        print(f"{BAD} version handshake mismatch: {present}")
        ok = False
    else:
        print(f"{OK} version handshake: agree ({sorted(distinct)})")
    return ok


def _check_env() -> bool:
    """CP4/H1 env check. Unset `CV_EDITOR_*` is ADVISORY (the legacy repo-root
    default is correct pre-inversion). A var that is SET but points at a
    missing dir, or a workspace resolving INSIDE the Python install tree (the
    H6 hazard), is a hard error."""
    ok = True
    from cv_editor import paths

    for var in (paths.ENV_DATA_ROOT, paths.ENV_PROJECT_ROOT):
        val = os.environ.get(var)
        if val is None:
            print(f"{OK} env: {var} unset (legacy repo-root default -- fine pre-P7)")
        elif not Path(val).is_dir():
            print(f"{BAD} env: {var}={val} points at a missing directory")
            ok = False
        else:
            print(f"{OK} env: {var}={val}")
    if paths.is_inside_install_tree(paths.data_root()):
        print(
            f"{BAD} env: workspace {paths.data_root()} resolves inside the Python "
            "install tree -- set CV_EDITOR_DATA_ROOT or use ./launch_editor.sh"
        )
        ok = False
    return ok


def main() -> int:
    print("CV environment check\n" + "-" * 20)
    results = [
        _check_typst(),
        _check_deps(),
        _check_data(),
        _check_data_lint(),
        _check_versions(),
        _check_env(),
    ]
    ok = all(results)
    print("-" * 20)
    print("All good." if ok else "Some checks failed — see hints above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
