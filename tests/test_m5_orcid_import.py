"""M5 5b CP5a: ORCID importer (discovery-only). Pure functions + mocked fetch
seam — ZERO real network, ZERO data writes (the CLI is read-only; the corruption
canary backstops). Fixtures are built as dicts matching the live ORCID v3.0
/works shape (group-level external-ids, lowercase types, normalized value,
relationship). See gotcha #14 (no-PII), #33 (uppercase DOI dedup), #58 (authors).
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
from cv_editor import orcid_client as oc

ROOT = Path(__file__).resolve().parent.parent
EXPORT_FIXTURE = (
    ROOT / "tests" / "fixtures" / "export"
)  # has publications.yml (doi 10.1/abc, pmid 12345678)


# ---------- fixture builders (live ORCID /works shape) ----------


def _eid(t, value, *, rel="self", norm=None):
    d = {"external-id-type": t, "external-id-value": value, "external-id-relationship": rel}
    if norm is not None:
        d["external-id-normalized"] = {"value": norm, "transient": True}
    return d


def _group(eids, *, title="A work", summaries=1, put_start=100):
    ws = [
        {"put-code": put_start + i, "title": {"title": {"value": title}}, "type": "journal-article"}
        for i in range(summaries)
    ]
    return {"external-ids": {"external-id": eids}, "work-summary": ws}


def _works(*groups):
    return {"group": list(groups)}


# ---------- is_valid_orcid_id ----------


def test_is_valid_orcid_id_accepts_well_formed():
    assert oc.is_valid_orcid_id("0000-0002-1825-0097")
    assert oc.is_valid_orcid_id("0000-0002-0103-401X")  # trailing X checksum


def test_is_valid_orcid_id_rejects_bad():
    for bad in (
        "",
        None,
        "1234",
        "0000-0002-1825",
        "0000-0002-1825-009",
        "0000_0002_1825_0097",
        "0000-0002-1825-009x",
        "garbage",
    ):
        assert not oc.is_valid_orcid_id(bad), bad


# ---------- extract_external_ids ----------


def test_extract_reads_group_level_ids_not_summary():
    # The DOI lives ONLY in the group-level external-ids; summaries carry none.
    # Reading work-summary[0] would miss it — proves we read the merged group node.
    wj = _works(_group([_eid("doi", "10.1/grouplevel")], summaries=3))
    refs = oc.extract_external_ids(wj)
    assert len(refs) == 1 and refs[0].doi == "10.1/grouplevel"
    assert refs[0].put_codes == (100, 101, 102)  # all summaries' put-codes


def test_extract_prefers_doi_over_pmid():
    wj = _works(_group([_eid("pmid", "999"), _eid("doi", "10.1/Both")]))
    r = oc.extract_external_ids(wj)[0]
    assert r.doi == "10.1/both" and r.pmid == "999"  # DOI kept (lowercased) + pmid retained


def test_extract_pmid_only():
    r = oc.extract_external_ids(_works(_group([_eid("pmid", "12345")])))[0]
    assert r.doi is None and r.pmid == "12345" and r.has_id


def test_extract_no_usable_id_when_only_nonseed_types():
    r = oc.extract_external_ids(
        _works(_group([_eid("source-work-id", "abc"), _eid("other-id", "xyz")]))
    )[0]
    assert r.doi is None and r.pmid is None and not r.has_id


def test_extract_excludes_part_of_relationship():
    # A `part-of` DOI is the container's id (e.g. the book a chapter is in) — not
    # this work's. It must be ignored, leaving the work with no usable id.
    r = oc.extract_external_ids(_works(_group([_eid("doi", "10.1/container", rel="part-of")])))[0]
    assert r.doi is None and not r.has_id


def test_extract_dedups_duplicate_doi_across_groups():
    wj = _works(
        _group([_eid("doi", "10.1/dup")], title="first"),
        _group([_eid("doi", "10.1/DUP")], title="second"),
    )
    refs = oc.extract_external_ids(wj)
    assert len(refs) == 1 and refs[0].title == "first"


def test_extract_normalizes_and_lowercases_doi():
    # Prefer external-id-normalized; strip URL prefix; lowercase.
    wj = _works(
        _group([_eid("doi", "https://doi.org/10.2105/AJPH.2024.1", norm="10.2105/AJPH.2024.1")])
    )
    assert oc.extract_external_ids(wj)[0].doi == "10.2105/ajph.2024.1"


def test_extract_ignores_non_seed_types():
    r = oc.extract_external_ids(
        _works(_group([_eid("eid", "2-s2.0-x"), _eid("isbn", "978"), _eid("arxiv", "2401.0001")]))
    )[0]
    assert not r.has_id


def test_extract_empty_profile():
    assert oc.extract_external_ids({"group": []}) == []
    assert oc.extract_external_ids({}) == []


def test_extract_dedups_pmid_across_groups_even_with_new_doi():
    # Post-impl review fix: a bare-PMID group followed by a DOI+PMID group for the
    # SAME paper (same PMID) must NOT double-emit just because the second group's
    # DOI looks new. PMID uniquely identifies a paper; first occurrence wins.
    wj = _works(
        _group([_eid("pmid", "777")], title="first"),
        _group([_eid("pmid", "777"), _eid("doi", "10.1/late")], title="second"),
    )
    refs = oc.extract_external_ids(wj)
    assert len(refs) == 1 and refs[0].title == "first" and refs[0].doi is None


def test_extract_excludes_version_of_relationship():
    # `version-of` is the related version's id (e.g. the preprint a paper supersedes),
    # not THIS work's — like `part-of`, it must be ignored. The live API emits it.
    r = oc.extract_external_ids(
        _works(_group([_eid("doi", "10.1/other-version", rel="version-of")]))
    )[0]
    assert r.doi is None and not r.has_id


def test_extract_keeps_id_when_relationship_absent():
    # ORCID sometimes omits external-id-relationship; the code defaults it to
    # `self`, so the id is kept. Pin that default against a refactor.
    eid = {"external-id-type": "doi", "external-id-value": "10.1/norel"}
    r = oc.extract_external_ids(_works(_group([eid])))[0]
    assert r.doi == "10.1/norel" and r.has_id


def test_extract_strips_whitespace_in_id_values():
    r = oc.extract_external_ids(
        _works(_group([_eid("doi", "  10.1/SPACE  "), _eid("pmid", " 123 ")]))
    )[0]
    assert r.doi == "10.1/space" and r.pmid == "123"


def test_extract_put_codes_drop_none():
    # Provenance tuple skips summaries with no put-code (corrupt/partial records).
    g = {
        "external-ids": {"external-id": [_eid("doi", "10.1/pc")]},
        "work-summary": [
            {"put-code": 5, "title": {"title": {"value": "t"}}},
            {"title": {"title": {"value": "no put-code"}}},
        ],
    }
    assert oc.extract_external_ids(_works(g))[0].put_codes == (5,)


# ---------- partition_against_cv ----------


def test_partition_dedup_case_insensitive_doi():
    # gotcha #33: corpus stores uppercase DOI suffixes; ORCID gives lowercase.
    refs = [oc.WorkRef(doi="10.2105/ajph.2024.1", pmid=None, title="t")]
    cv = [{"doi": "10.2105/AJPH.2024.1"}]
    part = oc.partition_against_cv(refs, cv)
    assert part.in_cv and not part.new


def test_partition_pmid_as_string():
    refs = [oc.WorkRef(doi=None, pmid="90000044", title="t")]
    cv = [{"pmid": 90000044}]  # int-coerced in YAML
    assert oc.partition_against_cv(refs, cv).in_cv


def test_partition_three_buckets():
    refs = [
        oc.WorkRef(doi="10.1/new", pmid=None, title="new one"),
        oc.WorkRef(doi="10.1/old", pmid=None, title="have it"),
        oc.WorkRef(doi=None, pmid=None, title="no id work"),
    ]
    cv = [{"doi": "10.1/OLD"}]
    part = oc.partition_against_cv(refs, cv)
    assert [r.title for r in part.new] == ["new one"]
    assert [r.title for r in part.in_cv] == ["have it"]
    assert [r.title for r in part.no_id] == ["no id work"]


def test_partition_doi_match_wins_when_pmid_is_new():
    # A ref carrying BOTH ids where the DOI is in the CV but the PMID isn't must
    # land in in_cv — a DOI match alone is sufficient (don't re-import the paper).
    refs = [oc.WorkRef(doi="10.1/have", pmid="999999", title="dup by doi")]
    cv = [{"doi": "10.1/HAVE"}]  # pmid 999999 deliberately absent
    part = oc.partition_against_cv(refs, cv)
    assert [r.title for r in part.in_cv] == ["dup by doi"] and not part.new


# ---------- fetch_works (mocked seam — no real network) ----------


class _FakeResp:
    def __init__(self, body=b"{}"):
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_works_sends_clean_ua_no_auth_no_query():
    captured = {}

    def fake_open(req, *, timeout):
        captured["req"] = req
        return _FakeResp(b'{"group": []}')

    out = oc.fetch_works("0000-0002-1825-0097", urlopen=fake_open)
    assert out == {"group": []}
    req = captured["req"]
    assert req.get_header("User-agent") == "cv-editor/1.0"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("Authorization") is None  # NO auth, even though docs invite it
    assert req.get_header("From") is None  # NO From: header (no-PII rule names it)
    assert "?" not in req.full_url  # NO query string (no ?email=/?mailto=)
    assert req.full_url == "https://pub.orcid.org/v3.0/0000-0002-1825-0097/works"


def test_fetch_works_invalid_id_raises_before_network():
    calls = []
    with pytest.raises(ValueError):
        oc.fetch_works("not-an-orcid", urlopen=lambda *a, **k: calls.append(1))
    assert not calls  # never reached the opener


def test_fetch_works_returns_none_on_http_error():
    def boom(req, *, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    assert oc.fetch_works("0000-0002-1825-0097", urlopen=boom) is None


def test_fetch_works_returns_none_on_url_error():
    def boom(req, *, timeout):
        raise urllib.error.URLError("timed out")

    assert oc.fetch_works("0000-0002-1825-0097", urlopen=boom) is None


def test_fetch_works_returns_none_on_non_json_body():
    assert (
        oc.fetch_works(
            "0000-0002-1825-0097", urlopen=lambda req, *, timeout: _FakeResp(b"<html>nope")
        )
        is None
    )


def test_orcid_no_pii_in_user_agent():
    # gotcha #14 enforcement mirror (test_v17_polish::test_*_no_pii_in_user_agent).
    assert oc.UA == "cv-editor/1.0"
    low = oc.UA.lower()
    for token in ("@", "mailto:", "public", "stanford", "mathew", "gmail"):  # leak-allow
        assert token not in low


# ---------- CLI (read-only; --data-dir at the export fixture corpus) ----------


def _run_cli(argv, monkeypatch, capsys, works):
    monkeypatch.setattr(oc, "fetch_works", lambda *a, **k: works)
    import importlib.util

    spec = importlib.util.spec_from_file_location("orcid_cli", ROOT / "scripts" / "orcid_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(argv)
    return rc, capsys.readouterr()


def test_cli_dry_run_prints_partition(monkeypatch, capsys):
    # 10.1/abc IS in the export fixture publications.yml -> in_cv; the other two are new/no-id.
    works = _works(
        _group([_eid("doi", "10.1/abc")], title="already have"),
        _group([_eid("doi", "10.9/brand-new")], title="brand new paper"),
        _group([_eid("source-work-id", "x")], title="no id here"),
    )
    rc, out = _run_cli(
        ["0000-0002-1825-0097", "--data-dir", str(EXPORT_FIXTURE)], monkeypatch, capsys, works
    )
    assert rc == 0
    assert "NEW (1)" in out.out and "brand new paper" in out.out
    assert "ALREADY IN CV (1)" in out.out and "already have" in out.out
    assert "NO USABLE ID (1)" in out.out and "no id here" in out.out


def test_cli_invalid_id_errors_before_fetch(monkeypatch, capsys):
    def must_not_call(*a, **k):
        raise AssertionError("fetch_works called on an invalid iD")

    monkeypatch.setattr(oc, "fetch_works", must_not_call)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "orcid_cli2", ROOT / "scripts" / "orcid_import.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["bad-id"]) == 2


def test_cli_fetch_failure_returns_1(monkeypatch, capsys):
    rc, out = _run_cli(["0000-0002-1825-0097"], monkeypatch, capsys, None)
    assert rc == 1
    assert "Could not fetch" in out.err
