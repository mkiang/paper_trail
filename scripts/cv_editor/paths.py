"""Data-root / engine-root seam (P1, paper_trail inversion).

Splits the editor's two implicit roots so the package can be installed as
a dependency and driven against an EXTERNAL workspace (P7 consumer model):

  * DATA_ROOT (workspace): holds ``data/``, ``qc/``, ``.cache/``,
    ``output/``, ``.cv_editor_backups/`` — everything the editor WRITES.
  * PROJECT_ROOT (engine): holds ``templates/``, ``fonts/``, ``build.sh``,
    ``scripts/`` (the normalizer), and the ``data/example/`` corpus —
    everything the editor READS as immutable engine assets.

Both default to the legacy ``Path(__file__).resolve().parents[2]`` (the
``typst/`` repo root), so an un-configured private-repo build resolves
BYTE-IDENTICALLY to the pre-seam behaviour. ``parents[2]`` from
``scripts/cv_editor/paths.py`` is ``typst/`` — the same value every
module used to capture at import.

Precedence for each root: explicit ``configure()`` > environment variable
> legacy default.

``configure()`` ALSO writes ``CV_EDITOR_DATA_ROOT`` /
``CV_EDITOR_PROJECT_ROOT`` into ``os.environ`` so spawned ``-m``
subprocesses (fresh interpreters) inherit the redirect — ``cwd`` does NOT
affect ``Path(__file__)``-based resolution, so the env is the only channel
that reaches a child interpreter.

Two consumption patterns:

  1. **Call-time accessors** (``data_root()``, ``backup_dir()``, …) — the
     default. Never capture these at module scope; call them each time so a
     test-time ``configure()`` is observed everywhere.
  2. **``on_configure`` refresh hook** — for the few modules that must keep
     a REAL module-level attribute because tests monkeypatch it (e.g.
     ``yaml_io.BACKUP_DIR``; ~18 sites). Such a module registers a callback
     that recomputes its cached globals; ``configure()``/``reset()`` fire
     every callback so the globals track the active root while staying
     ordinary, monkeypatch-able attributes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LEGACY_ROOT = Path(__file__).resolve().parents[2]  # typst/

ENV_DATA_ROOT = "CV_EDITOR_DATA_ROOT"
ENV_PROJECT_ROOT = "CV_EDITOR_PROJECT_ROOT"

# P6a flags-decoupling: bespoke/consumer Typst files import the shared flags
# via `@local/paper-trail`, resolved from the repo-resident package dir
# `<project_root>/packages` (A1 item 4 — rides Dropbox with the repo, NOT a
# per-machine ~/Library symlink). Every editor/CLI process that spawns `typst`
# inherits TYPST_PACKAGE_PATH from os.environ, so setting it here (setdefault,
# so an explicit shell export / test fixture wins) covers build_variant /
# build_runner / freezer with no per-argv threading. build.sh + repro_oracle.sh
# export it themselves (shell), and a conftest fixture sets it for raw-typst
# tests. configure() re-points it when project_root is redirected (P7).
#
# CP4/B1 env-aware default: when CV_EDITOR_PROJECT_ROOT is set in the
# environment (the launcher / build.sh / a configured consumer export it),
# derive the package dir from THAT root so a fresh `-m` subprocess or a bare
# create_app() boot resolves @local to the same engine root as data/qc — not
# _LEGACY_ROOT, which in an installed wheel is `<venv>/lib/pythonX.Y`. Still a
# setdefault, so an explicit TYPST_PACKAGE_PATH / CV_PACKAGE_PATH export (build.sh,
# launcher) or a test fixture wins. project_root()/_from_env() aren't defined
# until below, so the env is read inline here (a call at import = NameError).
_env_proj_root = os.environ.get(ENV_PROJECT_ROOT)
_pkg_base = Path(_env_proj_root).resolve() if _env_proj_root else _LEGACY_ROOT
os.environ.setdefault("TYPST_PACKAGE_PATH", str(_pkg_base / "packages"))

# Explicit configure() overrides. None => fall back to env, then legacy.
_data_root: Path | None = None
_project_root: Path | None = None

# Refresh hooks for modules that cache derived Path globals (see docstring).
_on_configure_hooks: list = []


def _from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).resolve() if value else None


def data_root() -> Path:
    """Workspace root (parent of ``data/``, ``qc/``, ``.cache/``, …)."""
    if _data_root is not None:
        return _data_root
    env = _from_env(ENV_DATA_ROOT)
    return env if env is not None else _LEGACY_ROOT


def project_root() -> Path:
    """Engine root (parent of ``templates/``, ``fonts/``, ``build.sh``, …)."""
    if _project_root is not None:
        return _project_root
    env = _from_env(ENV_PROJECT_ROOT)
    return env if env is not None else _LEGACY_ROOT


def on_configure(fn):
    """Register a zero-arg callback re-run on every configure()/reset().

    For modules that must expose a real, monkeypatch-able Path attribute
    (rather than a call-time accessor) because tests set it via
    ``monkeypatch.setattr(mod, "NAME", ...)``. The callback recomputes those
    attributes from the current roots. Also fires the hook once immediately
    so registration establishes the initial value.
    """
    _on_configure_hooks.append(fn)
    fn()
    return fn


def _fire_hooks() -> None:
    for fn in _on_configure_hooks:
        fn()


def configure(data_dir: Path | str | None = None, project_root: Path | str | None = None) -> None:
    """Point the seam at a workspace and/or engine root.

    ``data_dir`` is the WORKSPACE root (the parent of ``data/``), NOT
    ``data/`` itself — passing ``tmp/data`` would create a ``tmp/data/data``
    off-by-one. Re-callable per test (no once-per-process guard); the
    autouse write-isolation fixture push/pops around it. Writes the matching
    ``CV_EDITOR_*`` env vars so ``-m`` subprocesses inherit the redirect.
    """
    global _data_root, _project_root
    if data_dir is not None:
        _data_root = Path(data_dir).resolve()
        os.environ[ENV_DATA_ROOT] = str(_data_root)
    if project_root is not None:
        _project_root = Path(project_root).resolve()
        os.environ[ENV_PROJECT_ROOT] = str(_project_root)
        os.environ["TYPST_PACKAGE_PATH"] = str(_project_root / "packages")
    _fire_hooks()


def reset() -> None:
    """Forget in-process config + env, restoring legacy defaults.

    Test teardown helper. Fires the refresh hooks so cached globals revert.
    """
    global _data_root, _project_root
    _data_root = None
    _project_root = None
    os.environ.pop(ENV_DATA_ROOT, None)
    os.environ.pop(ENV_PROJECT_ROOT, None)
    # Symmetric with configure(): restore TYPST_PACKAGE_PATH to the legacy
    # default so a test that configure()'d an alternate project_root does not
    # strand @local resolution at a dead path for every SUBSEQUENT test (which
    # would break freeze/`typst query`). project_root() is _LEGACY_ROOT again
    # here, so the package dir is _LEGACY_ROOT/packages.
    os.environ["TYPST_PACKAGE_PATH"] = str(_LEGACY_ROOT / "packages")
    _fire_hooks()


# ---- workspace-derived accessors (call-time; never capture at module scope) ----


def data_dir() -> Path:
    return data_root() / "data"


def qc_dir() -> Path:
    return data_root() / "qc"


def cache_dir() -> Path:
    return data_root() / ".cache"


def output_dir() -> Path:
    return data_root() / "output"


def backup_dir() -> Path:
    return data_root() / ".cv_editor_backups"


# ---- engine-derived accessors ----


def templates_dir() -> Path:
    return project_root() / "templates"


def fonts_dir() -> Path:
    return project_root() / "fonts"


def scripts_dir() -> Path:
    return project_root() / "scripts"


def build_script() -> Path:
    return project_root() / "build.sh"


def normalizer_path() -> Path:
    """Path to the YAML normalizer.

    P1-a: a script under ``scripts/``. P1-b converts yaml_io's invocation to
    ``python -m cv_editor.normalize_yaml_quotes`` (module form), after which
    this path accessor is no longer on the write hot path.
    """
    return scripts_dir() / "normalize_yaml_quotes.py"


def example_dir() -> Path:
    """The fictional example corpus (reset-to-example / blank-CV header seed).

    Resolution order:
      1. ``project_root()/data/example`` when it exists — the private repo's
         engine-root copy (byte-identical to the pre-P6b behaviour) and any
         configured engine root that carries one.
      2. else the copy bundled as ``cv_editor`` package data
         (``cv_editor/example_data/``) — the installed-wheel / public-tree case,
         where the working ``data/`` IS the corpus and no separate engine-root
         ``data/example`` exists. Located via ``importlib.resources`` so it
         resolves inside an installed wheel (P6b §4).
    Falls back to the (possibly absent) engine-root path so a genuinely missing
    corpus fails loudly downstream rather than silently here.
    """
    candidate = project_root() / "data" / "example"
    if candidate.is_dir():
        return candidate
    try:
        from importlib import resources

        bundled = resources.files("cv_editor") / "example_data"
        if bundled.is_dir():
            return Path(str(bundled))
    except (ModuleNotFoundError, TypeError, AttributeError):
        pass
    return candidate


def packages_dir() -> Path:
    """Repo-resident Typst `@local` package root (contains ``local/…``).

    Fed to ``typst --package-path`` via the ``TYPST_PACKAGE_PATH`` env var
    (set at import + on ``configure()``). P6a flags-decoupling (A1 item 4)."""
    return project_root() / "packages"


def is_inside_install_tree(p: Path | str) -> bool:
    """True when ``p`` resolves inside the Python install tree.

    CP4/H6 detector. When a bare ``cv-editor`` runs from an installed wheel
    with ``CV_EDITOR_*`` unset, ``data_root()`` falls back to ``_LEGACY_ROOT``
    = ``Path(__file__).parents[2]`` = ``<venv>/lib/pythonX.Y`` — the dir ABOVE
    ``site-packages``. So checking for a literal ``"site-packages"`` path
    component MISSES the real hazard; we compare against ``sys.prefix`` /
    ``sys.base_prefix`` (which contain that lib dir) via prefix containment.

    Used to (a) refuse to boot the editor when the WRITE workspace would land
    in site-packages (``create_app``), and (b) surface the same condition in
    ``make doctor``. Returns False for a normal repo checkout or a tmp
    workspace (neither is under the interpreter prefix).
    """
    try:
        rp = Path(p).resolve()
    except OSError:
        rp = Path(p)
    for base in {sys.prefix, sys.base_prefix}:
        try:
            if rp.is_relative_to(Path(base).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False
