"""M5 CP6/CP7: the /replace route (ask -> preview -> apply) + multi-file
all-or-nothing preflight + partial-failure manifest.

DATA-SAFE BY CONSTRUCTION: every apply test monkeypatches
`yaml_io.write_with_backup` (and `bulk_replace.apply_in_section`) so NO real
`data/*.yml` is ever written. Section loads + mtime reads are read-only. So this
module needs no snapshot/restore and is concurrency-safe (still: SEQUENTIAL pytest
per gotcha #70).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor import bulk_replace, schemas, yaml_io
from cv_editor.app import create_app
from cv_editor.bulk_replace import Hit
from cv_editor.yaml_io import StaleFileError

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    a = create_app()
    a.config["TESTING"] = True
    return a.test_client()


def _mtime(section: str) -> int:
    return yaml_io.mtime_ns(ROOT / schemas.get(section)["file"])


# ---------- ask + preview (read-only) ----------


def test_get_ask_form(client):
    body = client.get("/replace").get_data(as_text=True)
    assert "Find &amp; replace across all sections" in body
    assert 'name="needle"' in body and 'name="replacement"' in body


def test_preview_no_match_redirects(client):
    resp = client.post(
        "/replace",
        data={"action": "preview", "needle": "zzqqxx_no_such_token_zzqq", "replacement": "y"},
    )
    assert resp.status_code in (302, 303)  # flash + back to ask


def test_preview_renders_hits_with_risky_unchecked(client, monkeypatch):
    def fake_collect(data, key, needle, repl, cs):
        if key == "honors":
            return [
                Hit("honors", 0, "award", "Award One", "Old prize", "New prize", 1, False),
                Hit("honors", 1, "award", "Award Two", "*Bold* prize", "*Bold prize", 1, True),
            ]
        return []

    monkeypatch.setattr(bulk_replace, "collect_in_section", fake_collect)
    resp = client.post(
        "/replace", data={"action": "preview", "needle": "prize", "replacement": "X"}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "matches" in body and "Award One" in body and "Award Two" in body
    assert 'value="honors|0|award"' in body and 'value="honors|1|award"' in body
    # the markup-risky hit is surfaced + styled (default-unchecked is in the template)
    assert "replace-risky" in body and "WARNING" in body
    # carries per-file mtime for the all-or-nothing preflight
    assert 'name="mtime_honors"' in body


def test_preview_no_match_real_data_is_readonly(client):
    # Sanity: previewing against the real corpus does not write (mtimes unchanged).
    before = {s: _mtime(s) for s in bulk_replace.searchable_sections()}
    client.post("/replace", data={"action": "preview", "needle": "zzqq_absent", "replacement": "y"})
    after = {s: _mtime(s) for s in bulk_replace.searchable_sections()}
    assert before == after


# ---------- apply: guards + happy path + manifest (writes are FAKED) ----------


def test_apply_no_selection_redirects(client):
    resp = client.post("/replace", data={"action": "apply", "needle": "x", "replacement": "y"})
    assert resp.status_code in (302, 303)


def test_apply_stale_mtime_aborts_with_no_write(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        yaml_io, "write_with_backup", lambda *a, **k: called.append(1) or Path("x.bak")
    )
    resp = client.post(
        "/replace",
        data={
            "action": "apply",
            "needle": "x",
            "replacement": "y",
            "hit": "honors|0|award",
            "mtime_honors": "1",  # bogus -> stale
        },
    )
    assert resp.status_code in (302, 303)
    assert called == []  # all-or-nothing preflight wrote NOTHING


def test_apply_happy_path_writes_and_renders_manifest(client, monkeypatch):
    writes = []
    monkeypatch.setattr(bulk_replace, "apply_in_section", lambda *a, **k: 2)

    def fake_write(path, header, data, expected_mtime_ns=None):
        writes.append(path.name)
        return Path(f"{path.name}.123.bak")

    monkeypatch.setattr(yaml_io, "write_with_backup", fake_write)

    resp = client.post(
        "/replace",
        data={
            "action": "apply",
            "needle": "Old",
            "replacement": "New",
            "hit": "honors|0|award",
            "mtime_honors": str(_mtime("honors")),
        },
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Written" in body and "Honors" in body
    assert writes == ["honors.yml"]


def test_apply_no_match_writes_nothing(client):
    # REAL apply (no monkeypatch) with a needle absent in the selected field:
    # apply_in_section returns 0 -> the write is never reached -> the file is
    # untouched. Exercises the n==0 branch with a genuine no-write guarantee.
    before = _mtime("honors")
    resp = client.post(
        "/replace",
        data={
            "action": "apply",
            "needle": "zzqq_absent_token_zzqq",
            "replacement": "y",
            "hit": "honors|0|award",
            "mtime_honors": str(before),
        },
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "0 file" in body  # "replaced text in 0 file(s)"
    assert _mtime("honors") == before  # nothing written


def test_apply_partial_failure_renders_manifest(client, monkeypatch):
    monkeypatch.setattr(bulk_replace, "apply_in_section", lambda *a, **k: 2)
    seq = []

    def fake_write(path, header, data, expected_mtime_ns=None):
        seq.append(path.name)
        if path.name == "service.yml":
            raise StaleFileError("service changed under us")
        return Path(f"{path.name}.bak")

    monkeypatch.setattr(yaml_io, "write_with_backup", fake_write)

    resp = client.post(
        "/replace",
        data={
            "action": "apply",
            "needle": "X",
            "replacement": "Y",
            "hit": ["honors|0|award", "service|0|role"],
            "mtime_honors": str(_mtime("honors")),
            "mtime_service": str(_mtime("service")),
        },
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Partial apply" in body
    assert "Written" in body and "Honors" in body  # honors committed
    assert "Failed" in body and "Professional Service" in body  # service failed
    assert seq == ["honors.yml", "service.yml"]  # stopped after the failure
