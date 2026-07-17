"""Make `scripts/` importable so tests can `from cv_editor import ...`."""

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_ROOT / "scripts"))
# Also expose the project root so the few tests that import via the
# `scripts.` package prefix (e.g. `from scripts.yaml_to_bibtex import ...`)
# resolve the same modules the bare-name imports above reach.
sys.path.insert(0, str(PROJ_ROOT))


@pytest.fixture(autouse=True)
def _workspace_isolation(tmp_path_factory):
    """P1 seam (paper_trail inversion) — per-test WRITE-ISOLATION.

    Every test runs against a fresh tmp COPY of the real ``data/`` corpus,
    with the workspace root (``paths.data_root()``) pointed at it. Content is
    byte-identical to the real corpus, so every existing assertion is
    preserved; but all writes — including a kicked ``pubmed_sync.py`` /
    ``qc_publications.py`` SUBPROCESS, which inherits ``CV_EDITOR_DATA_ROOT``
    from ``os.environ`` — land in tmp and can never touch the real
    ``data/*``. This finally closes the long-deferred DATA_DIR isolation debt
    (gotcha #70) that the corruption canary below was papering over.

    The ENGINE root (templates/, fonts/, scripts/, data/example/) stays the
    real repo, so renders/compiles still find the real assets — only the
    workspace (data/qc/.cache/output/backups) is redirected.

    A separate tmp dir (NOT the shared ``tmp_path``) is used so it can't
    collide with a test that builds its own ``tmp_path/data`` tree. Tests
    that need an explicit root still call ``create_app(data_dir=...)`` /
    ``paths.configure(...)``, which override this per test; the finally
    restores the default afterward.
    """
    try:
        from cv_editor import paths
    except Exception:
        yield
        return
    ws = tmp_path_factory.mktemp("cv_ws")
    real_data = PROJ_ROOT / "data"
    if real_data.is_dir():
        shutil.copytree(real_data, ws / "data")
    try:
        paths.configure(data_dir=ws)
        yield ws
    finally:
        paths.reset()


@pytest.fixture(autouse=True)
def _reset_paths_seam():
    """Belt-and-suspenders: even if _workspace_isolation is bypassed (import
    failure), leave the seam at its default roots after every test so config
    can't leak across tests."""
    try:
        from cv_editor import paths
    except Exception:
        yield
        return
    yield
    paths.reset()


@pytest.fixture(autouse=True)
def _reset_unshorten_me_cooldown_between_tests():
    """The unshorten.me circuit breaker is module-level state in
    altmetric_client. Without a reset between tests, a test that legitimately
    trips the cooldown (e.g. a 429-handling test) leaves the breaker armed
    and any subsequent call to resolve_via_unshorten_me short-circuits to
    failed_rate_limit before it ever reaches its urlopen stub. Reset before
    AND after so test order is irrelevant."""
    try:
        from cv_editor import altmetric_client
    except Exception:
        yield
        return
    altmetric_client._reset_unshorten_me_cooldown()
    yield
    altmetric_client._reset_unshorten_me_cooldown()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Real data files the suite must NEVER mutate, mapped to the opt-out
# marker a test may set when it legitimately rewrites that file.
# M2 (2026-05-29): generalized from publications-only after the
# decomposition critique flagged DATA_DIR isolation as deferred — this
# canary is now the PRIMARY guard against real-data clobbering during
# the refactor, so it watches the files the generic CRUD path can write.
# M5-5d CP3 (2026-07-01): expanded to EVERY data/*.yml + both committed
# sidecars before the reset feature landed — scaffold.reset can rewrite
# all of them, so a route test whose scaffold monkeypatch silently fails
# must trip here, not corrupt the real CV (the pre-impl review's HIGH).
WATCHED_DATA_FILES = {
    "publications.yml": "mutates_publications_yml",
    "meta.yml": "mutates_meta_yml",
    "honors.yml": "mutates_honors_yml",
    "presentations.yml": "mutates_presentations_yml",
    "research_support.yml": "mutates_research_support_yml",
    "service.yml": "mutates_service_yml",
    "teaching.yml": "mutates_teaching_yml",
    "mentees.yml": "mutates_mentees_yml",
    "education.yml": "mutates_education_yml",
    "appointments.yml": "mutates_appointments_yml",
    "publications_pubmed_sync.json": "mutates_pubmed_sync_sidecar",
    "citation_counts.json": "mutates_citation_snapshot",
}


def _snapshot_watched(root: Path, markers_active) -> list:
    """Return [(path, bytes), ...] for every watched data file that exists
    and is NOT opted out via its marker. `markers_active` is a callable
    taking a marker name -> bool."""
    snaps = []
    for fname, marker in WATCHED_DATA_FILES.items():
        p = root / "data" / fname
        if p.exists() and not markers_active(marker):
            snaps.append((p, p.read_bytes()))
    return snaps


def _restore_and_collect_changes(snapshots) -> list:
    """Given [(path, before_bytes), ...], restore any file that changed and
    return the list of changed filenames. Pure + unit-tested (see
    tests/test_m2_canary.py) so the canary's core logic has a regression
    guard independent of the autouse fixture wiring.

    Deletion-tolerant (M5-5d post-impl review): scaffold.reset DELETES the
    pubmed sidecar, so a rogue reset in a test must not crash the restore
    loop mid-way (which would strand every later watched file unrestored) —
    a deleted file is restored from its snapshot bytes and reported."""
    changed = []
    for path, before in snapshots:
        try:
            after = path.read_bytes()
        except FileNotFoundError:
            path.write_bytes(before)  # restore the DELETED file
            changed.append(f"{path.name} (was deleted)")
            continue
        if before != after:
            path.write_bytes(before)  # restore before failing
            changed.append(path.name)
    return changed


@pytest.fixture(autouse=True)
def _real_data_corruption_canary(request):
    """Task #42 (2026-05-26): catch any test that accidentally mutates a
    real `data/*.yml`. The 2026-05-26 morning corruption (Long Arm authors
    -> `['a','b','c','d']`) was traced to a QC apply-route test that
    redirected only the SIDECAR + DECISIONS paths to tmp while the route
    still wrote the REAL publications.yml. Without a canary, future
    regressions of this class go unnoticed until the user sees broken data.

    Snapshots each watched file's bytes before the test; restores + fails
    if any changed. Opt out per file via its marker in WATCHED_DATA_FILES
    (e.g. `@pytest.mark.mutates_publications_yml`) — none today."""
    snapshots = _snapshot_watched(
        PROJ_ROOT, lambda m: request.node.get_closest_marker(m) is not None
    )
    yield
    changed = _restore_and_collect_changes(snapshots)
    if changed:
        pytest.fail(
            f"Test {request.node.nodeid} modified real data file(s): "
            f"{', '.join(changed)}. This is the task-#42 class of bug: a "
            f"fixture redirected sidecar/decisions paths to tmp but the "
            f"route still wrote a real data/*.yml. File(s) restored from "
            f"the pre-test snapshot; investigate the test's fixture isolation."
        )
