"""Blank-CV / example-corpus scaffolding + guarded reset core (M5-5d CP4).

Flask-free. Consumed by three surfaces:
  * `scripts/init_cv.py` CLI (CP5),
  * the editor's `POST /reset` route (CP7),
  * the index onboarding predicate (CP8).

Every function takes explicit directory params (the `check_data(data_dir)`
precedent) so unit tests run on tmp trees. NOTE for tests: the per-section
writes delegate to `yaml_io.write_with_backup`, whose `.bak` side-writes go
to the module-global `yaml_io.BACKUP_DIR` — write-shaped tests MUST
`monkeypatch.setattr(yaml_io, "BACKUP_DIR", tmp_path / "backups")` or they
pollute (and eventually evict, keep=50) the user's real recovery backups.

Header single-sourcing: the blank scaffold derives every section header from
`data/example/*.yml` via `yaml_io.split_header` — the example corpus IS the
canonical schema documentation (drift-guarded against the real headers by
tests/test_m5_sample_data.py). Blank bodies are literally `[]`, never empty:
`split_header` on a comment-only file returns header="" on the NEXT save,
which would silently drop the schema docs.

The example corpus is also the LIST of what a corpus contains, not just the
headers. A host can carry data files this engine has no schema for — a
curation file read only by its own export script — and those files are part
of its corpus, so a reset has to handle them or it leaves the old corpus's
data sitting beside a fresh one while reporting a clean slate. Both tree
writers therefore iterate `example_dir/*.yml`; every schema section must
still appear there, or `_corpus_yml_names` raises.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from cv_editor import paths, schemas, sections, yaml_io
from cv_editor.atomic_json import atomic_write_json

# Real, readable module attributes (tests read scaffold.EXAMPLE_DATA) that
# track the active root via the refresh hook. The per-function default args
# below use None-sentinels resolving to these at CALL time — a `= DATA_DIR`
# default would freeze the import-time value and ignore a later configure().
ROOT = paths.data_root()
DATA_DIR = paths.data_dir()
QC_DIR = paths.qc_dir()
CACHE_DIR = paths.cache_dir()
EXAMPLE_DATA = paths.example_dir()


@paths.on_configure
def _refresh_paths() -> None:
    global ROOT, DATA_DIR, QC_DIR, CACHE_DIR, EXAMPLE_DATA
    ROOT = paths.data_root()
    DATA_DIR = paths.data_dir()
    QC_DIR = paths.qc_dir()
    CACHE_DIR = paths.cache_dir()
    EXAMPLE_DATA = paths.example_dir()


MODES = ("blank", "example")

# Corpus-derived qc/ artifacts a reset MOVES into the snapshot dir (one
# manifest constant so the list can't drift across call sites). Deliberately
# includes the user-managed pubmed_sync_decisions.yml + .template.yml — they
# describe the old corpus. qc/_archive/ + qc/visual_diff/ are dev artifacts
# and stay put.
CORPUS_QC_FILES = (
    "report.md",
    "report.json",
    "qc_decisions.json",
    "pubmed_sync_report.md",
    "pubmed_sync_decisions.yml",
    "pubmed_sync_decisions.gen.yml",
    "pubmed_sync_decisions.template.yml",
    "urls_report.md",
    "citations_report.md",
)

PUBMED_SIDECAR = "publications_pubmed_sync.json"
CITATION_SNAPSHOT = "citation_counts.json"
# Must byte-match citation_counts.load_snapshot's empty shape.
EMPTY_CITATION_SNAPSHOT = {"version": 1, "generated_at": None, "counts": {}}

# The blank meta body. Placeholders are load-bearing twice over: the
# renderer requires every one of these keys (render-header/setup have no
# defaults), and `meta_is_personalized` treats ANY edited placeholder (or a
# filled contacts/address list) as the signal that a human has started
# working — which blocks the reset route's no-confirm waiver.
BLANK_NAME_PLACEHOLDER = "Your Name"
# Shared with meta_is_personalized so the waiver check can't drift from
# what blank_tree actually writes.
BLANK_META_PLACEHOLDERS = {
    "name": BLANK_NAME_PLACEHOLDER,
    "position": "Your Title",
    "department": "Your Department",
    "institution": "Your Institution",
    "self_bold": BLANK_NAME_PLACEHOLDER,
}


def _blank_meta_body() -> dict:
    return {
        "name": BLANK_META_PLACEHOLDERS["name"],
        "position": BLANK_META_PLACEHOLDERS["position"],
        "department": BLANK_META_PLACEHOLDERS["department"],
        "institution": BLANK_META_PLACEHOLDERS["institution"],
        "address": [],
        "contacts": [],
        "footer": {
            "template": "Your Name (Curriculum Vitae --- {date})",
            "date_format": "[month repr:long] [year]",
            "show_on_first_page": False,
        },
        "self_bold": BLANK_META_PLACEHOLDERS["self_bold"],
        "sections": [
            "education",
            "appointments",
            "publications",
            "presentations",
            "research_support",
            "service",
            "teaching",
            "honors",
            "mentees",
        ],
        # No inputs: the default flags. build.sh tolerates an inputs-less
        # variant ((v.get("inputs") or {})).
        "build_variants": [{"filename": "cv"}],
    }


def _section_names() -> list[str]:
    return list(schemas.all_sections())


def _corpus_yml_names(example_dir: Path) -> list[str]:
    """Every `.yml` stem the example corpus declares, sorted.

    Wider than `_section_names()` on purpose: a host's non-schema corpus file
    (see the module docstring) is named nowhere else the engine can read. The
    schema sections are still required — an example file that went missing
    would otherwise drop its section from the reset silently, leaving the old
    corpus's entries in place under a header the page claims it rewrote.
    """
    example_dir = Path(example_dir)
    names = sorted(p.stem for p in example_dir.glob("*.yml"))
    missing = [n for n in _section_names() if n not in names]
    if missing:
        raise FileNotFoundError(
            f"example corpus {example_dir} has no example file for {missing} — "
            "every schema section needs one (headers are single-sourced from it)"
        )
    return names


def _example_header(name: str, example_dir: Path) -> str:
    header, _ = yaml_io.split_header((example_dir / f"{name}.yml").read_text(encoding="utf-8"))
    return header


def _blank_body(name: str, example_dir: Path):
    """The blank body for one corpus file: meta -> placeholders, otherwise an
    EMPTY container of the example file's own root type.

    All ten schema sections are sequences, so this is `[]` for every one of
    them and the blank tree is unchanged. A host's file can be a mapping, and
    `[]` there is not merely odd-looking — a loader that requires a mapping
    root raises on it, so the "clean slate" would break the very tooling it
    exists to reset. A header-only example file (root `None`) takes the
    documented `[]` default.
    """
    if name == "meta":
        return _blank_meta_body()
    _, data = yaml_io.load(Path(example_dir) / f"{name}.yml")
    return {} if isinstance(data, dict) else []


# ---------- emptiness (single source of truth for banner + route waiver) ----------


def corpus_entry_counts(data_dir: Path | None = None) -> dict[str, int | None]:
    """Per-section leaf-entry count. None = LOAD FAILURE (fail-closed).

    Missing file counts as 0 (nothing to lose); a file that exists but
    cannot be parsed/walked counts as None — the caller must treat that as
    NOT empty, because a corpus corrupted by e.g. a Dropbox sync conflict
    is indistinguishable from fresh under a fail-open predicate, and that
    is exactly when the reset guard matters most.
    """
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    counts: dict[str, int | None] = {}
    for name in _section_names():
        if name == "meta":
            continue
        path = Path(data_dir) / f"{name}.yml"
        if not path.exists():
            counts[name] = 0
            continue
        try:
            _, data = yaml_io.load(path)
            structure = schemas.get(name)["structure"]
            counts[name] = sum(1 for _ in sections.flatten(data, structure))
        except Exception:
            counts[name] = None
    return counts


def meta_is_personalized(data_dir: Path | None = None) -> bool:
    """True when meta.yml shows signs of a human's work.

    Checks every BLANK_META_PLACEHOLDERS field against its scaffold
    placeholder, plus non-empty contacts/address lists — so a fresh user
    who filled contacts but left `name: Your Name` still gets the confirm
    phrase (post-impl review: the plan's waiver decision names
    "name/contacts", not name alone). Whole-body comparison is deliberately
    avoided (brittle across quote normalization). Fail-closed: an
    unreadable meta counts as personalized.
    """
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    path = Path(data_dir) / "meta.yml"
    if not path.exists():
        return False
    try:
        _, meta = yaml_io.load(path)
    except Exception:
        return True
    if meta is None:
        return False
    for key, placeholder in BLANK_META_PLACEHOLDERS.items():
        val = str(meta.get(key) or "").strip()
        if val and val != placeholder:
            return True
    return bool(meta.get("contacts")) or bool(meta.get("address"))


def corpus_is_empty(data_dir: Path | None = None) -> bool:
    """True only when there is provably nothing to lose: every non-meta
    section has zero leaf entries (missing file == zero), NO section failed
    to load, and meta is missing-or-unpersonalized."""
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    counts = corpus_entry_counts(data_dir)
    if any(c is None for c in counts.values()):
        return False
    if any(c > 0 for c in counts.values()):
        return False
    return not meta_is_personalized(data_dir)


# ---------- snapshot ----------


def snapshot_tree(
    *,
    data_dir: Path | None = None,
    cache_dir: Path | None = None,
    backup_dir: Path | None = None,
    mode: str = "manual",
) -> Path:
    """Copy the current corpus + committed sidecars + the citation cache
    into `<backup_dir>/reset-<time_ns>/`, with a manifest.json.

    The `reset-*` SUBDIRECTORY is structurally immune to yaml_io's
    `_prune_backups` (root-level `name.<ns>.bak` glob only), so these
    snapshots survive indefinitely — unbounded growth is accepted and
    documented; each is small (the corpus is <1MB).

    Lock-free by design: a save racing the copy can produce a snapshot
    whose files are from slightly different instants (harmless single-user;
    the per-file .bak that write_with_backup makes during the reset loop
    still captures any late save exactly).
    """
    backup_dir = Path(backup_dir) if backup_dir is not None else yaml_io.BACKUP_DIR
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    cache_dir = CACHE_DIR if cache_dir is None else Path(cache_dir)
    snap = backup_dir / f"reset-{time.time_ns()}"
    while snap.exists():  # ns collision: mirror _make_backup's bump
        snap = backup_dir / f"reset-{time.time_ns()}"
    (snap / "data").mkdir(parents=True)

    copied: list[str] = []
    for p in sorted(data_dir.glob("*.yml")) + sorted(data_dir.glob("*.json")):
        if p.is_file():
            shutil.copy2(p, snap / "data" / p.name)
            copied.append(f"data/{p.name}")
    cache_snapshot = Path(cache_dir) / CITATION_SNAPSHOT
    if cache_snapshot.is_file():
        (snap / ".cache").mkdir()
        shutil.copy2(cache_snapshot, snap / ".cache" / CITATION_SNAPSHOT)
        copied.append(f".cache/{CITATION_SNAPSHOT}")

    _write_manifest(
        snap,
        {
            "version": 1,
            "mode": mode,
            "created_ns": int(snap.name.rsplit("-", 1)[1]),
            "phases": {"snapshot": copied},
            "completed": False,
        },
    )
    return snap


def _write_manifest(snap: Path, manifest: dict) -> None:
    # Atomic (post-impl review LOW): a crash mid-manifest-write must not
    # leave truncated JSON — the failure page reads this file.
    atomic_write_json(snap / "manifest.json", manifest)


def _read_manifest(snap: Path) -> dict:
    return json.loads((snap / "manifest.json").read_text(encoding="utf-8"))


# ---------- tree writers ----------


def _write_section(path: Path, header: str, data) -> str:
    """Exists -> write_with_backup(new_header=); missing -> write_new.

    The dispatch is load-bearing: write_with_backup raises FileNotFoundError
    on a missing target (backup + header re-read need the current file), and
    the first-run tree is exactly a missing-files tree.
    """
    if path.exists():
        yaml_io.write_with_backup(path, header, data, new_header=header)
        return "overwrote"
    yaml_io.write_new(path, header, data)
    return "created"


def blank_tree(data_dir: Path | None = None, *, example_dir: Path | None = None) -> dict[str, str]:
    """Write the blank scaffold: every example-declared `.yml` = its example
    header + an empty body of its own root type; meta = example header +
    placeholder body; empty citation snapshot.
    Returns {filename: created|overwrote}."""
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    example_dir = EXAMPLE_DATA if example_dir is None else Path(example_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name in _corpus_yml_names(example_dir):
        header = _example_header(name, example_dir)
        body = _blank_body(name, example_dir)
        written[f"{name}.yml"] = _write_section(data_dir / f"{name}.yml", header, body)
    atomic_write_json(data_dir / CITATION_SNAPSHOT, EMPTY_CITATION_SNAPSHOT)
    written[CITATION_SNAPSHOT] = "written"
    return written


def example_tree(
    data_dir: Path | None = None, *, example_dir: Path | None = None
) -> dict[str, str]:
    """Write the example corpus into data_dir (headers + bodies from
    data/example/, round-tripped through the sacred write pipeline so the
    publications shape-guard + normalizer run). Copies the example citation
    snapshot. Returns {filename: created|overwrote}."""
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    example_dir = EXAMPLE_DATA if example_dir is None else Path(example_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name in _corpus_yml_names(example_dir):
        src = Path(example_dir) / f"{name}.yml"
        header, data = yaml_io.load(src)
        written[f"{name}.yml"] = _write_section(data_dir / f"{name}.yml", header, data)
    snap = json.loads((Path(example_dir) / CITATION_SNAPSHOT).read_text(encoding="utf-8"))
    atomic_write_json(data_dir / CITATION_SNAPSHOT, snap)
    written[CITATION_SNAPSHOT] = "written"
    return written


# ---------- reset orchestrator ----------


def reset(
    mode: str,
    *,
    data_dir: Path | None = None,
    qc_dir: Path | None = None,
    cache_dir: Path | None = None,
    backup_dir: Path | None = None,
    example_dir: Path | None = None,
) -> dict:
    """Snapshot-first guarded reset. Returns the final manifest dict
    (including `snapshot_dir`).

    Phases (manifest.json inside the snapshot dir is REWRITTEN after each
    phase, so a mid-crash manifest reflects exactly what happened):
      1. snapshot     — copy data/* + .cache citation snapshot
      2. sections     — write blank/example tree (per-file .bak via
                        write_with_backup keeps each section individually
                        restorable through the existing /backups UI)
      3. sidecars     — every `data/*.json` phase 2 did not write is deleted
                        (all were snapshotted in phase 1 by the same glob).
                        That is the pubmed sidecar and any host sidecar
                        derived from the old corpus: leaving one behind lets a
                        single click repopulate the fresh corpus with the old
                        one's data, which is why the pubmed sidecar was
                        deleted by name here before this generalised;
                        .cache citation snapshot deleted (else one click of
                        POST /citations/snapshot resurrects the old corpus's
                        DOI counts into the fresh corpus);
                        data/ citation snapshot rewritten by phase 2
      4. qc artifacts — CORPUS_QC_FILES moved into the snapshot dir (their
                        finding IDs/decisions describe the old corpus; all
                        consumers 404/zero gracefully on absence)
      5. unmanaged    — a REPORT, not an action: any `data/*.yml` left that
                        phase 2 did not write, i.e. a corpus file with no
                        example counterpart. Empty in both known corpora;
                        it exists so the next such file surfaces on the page
                        instead of surviving a reset unmentioned.

    NOT cross-file atomic, deliberately: the source content is known-good,
    so a mid-loop failure is fixed by re-running reset; callers surface the
    manifest (wrote / not-attempted + snapshot path) per the /replace
    precedent.
    """
    if mode not in MODES:
        raise ValueError(f"unknown reset mode {mode!r} (expected one of {MODES})")
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    qc_dir = QC_DIR if qc_dir is None else Path(qc_dir)
    cache_dir = CACHE_DIR if cache_dir is None else Path(cache_dir)
    example_dir = EXAMPLE_DATA if example_dir is None else Path(example_dir)

    snap = snapshot_tree(data_dir=data_dir, cache_dir=cache_dir, backup_dir=backup_dir, mode=mode)
    manifest = _read_manifest(snap)
    manifest["snapshot_dir"] = str(snap)

    writer = blank_tree if mode == "blank" else example_tree
    try:
        manifest["phases"]["sections"] = writer(data_dir, example_dir=example_dir)
    except Exception:
        # Honesty over granularity (post-impl review): some sections may
        # already be rewritten. Record that in the on-disk manifest before
        # propagating so the failure page never under-reports.
        manifest["phases"]["sections"] = (
            "FAILED mid-write — some sections may have been rewritten; each "
            "rewrite left a per-file .bak, and this snapshot holds the full "
            "pre-reset tree"
        )
        _write_manifest(snap, manifest)
        raise
    _write_manifest(snap, manifest)

    written = manifest["phases"]["sections"]
    sidecars: dict[str, str] = {}
    # Phase 1 snapshots data/*.json with this same glob, so "was snapshotted"
    # holds by construction for everything deleted here — asserted by
    # test_reset_deletes_every_json_sidecar_it_did_not_write.
    for stale in sorted(data_dir.glob("*.json")):
        if stale.name in written:
            continue  # phase 2 owns it (citation_counts.json)
        stale.unlink()
        sidecars[stale.name] = "deleted (snapshotted)"
    cache_snapshot = cache_dir / CITATION_SNAPSHOT
    if cache_snapshot.exists():
        cache_snapshot.unlink()
        sidecars[f".cache/{CITATION_SNAPSHOT}"] = "deleted (snapshotted)"
    manifest["phases"]["sidecars"] = sidecars
    _write_manifest(snap, manifest)

    moved: list[str] = []
    if qc_dir.is_dir():
        (snap / "qc").mkdir(exist_ok=True)
        for name in CORPUS_QC_FILES:
            src = qc_dir / name
            if src.is_file():
                shutil.move(str(src), str(snap / "qc" / name))
                moved.append(f"qc/{name}")
    manifest["phases"]["qc_moved"] = moved

    # Phase 5 — report only. After phase 2 wrote every example-declared .yml
    # and phase 3 removed every unwritten .json, anything still here is a
    # corpus file with no example counterpart. The engine cannot guess its
    # blank shape, so it says so rather than leaving it unmentioned.
    manifest["phases"]["unmanaged"] = sorted(
        p.name
        for p in list(data_dir.glob("*.yml")) + list(data_dir.glob("*.json"))
        if p.name not in written
    )
    manifest["completed"] = True
    _write_manifest(snap, manifest)
    return manifest
