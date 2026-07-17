"""M5 CP1: whole-corpus load-time validation (cv_editor.data_check).

All write-shaped tests run against TMP fixtures — they never touch the real
`data/*.yml`. One read-only sanity test asserts the real corpus is ERROR-clean
(it builds, so it must be). SEQUENTIAL pytest only (gotcha #70) — though these
tests perform NO writes, so they're concurrency-safe by construction.
"""

from __future__ import annotations

from pathlib import Path

from cv_editor import data_check
from cv_editor.data_check import ERROR, WARNING

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


# ---------- clean fixture ----------

CLEAN_PUBS = """\
# publications fixture
- subsection: Peer-Reviewed Original Research
  entries:
    - title: A study of things
      authors:
        - Public JQ
        - Smith J
      journal: JAMA
      year: 2024
"""


def test_clean_publications_no_issues(tmp_path):
    p = _write(tmp_path, "publications.yml", CLEAN_PUBS)
    issues = data_check.check_file("publications", p)
    assert issues == [], [(i.field, i.message) for i in issues]


# ---------- ERROR: YAML won't parse ----------


def test_yaml_parse_error_is_located_error(tmp_path):
    p = _write(tmp_path, "honors.yml", "# h\n- date: 2024\n  award: [unterminated\n")
    issues = data_check.check_file("honors", p)
    assert len(issues) == 1
    assert issues[0].severity == ERROR
    assert "will not parse" in issues[0].message
    assert issues[0].line is not None


# ---------- ERROR: authors-shape invariant (gotcha #58, delegated) ----------


def test_string_authors_is_error(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: Bad authors\n"
        "      authors: a; b; c; d\n"
        "      journal: JAMA\n"
        "      year: 2024\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    assert any(i.severity == ERROR and i.field == "authors" for i in issues)


# ---------- ERROR: unescaped $ opens Typst math ----------


def test_bare_dollar_in_title_is_error_and_located(tmp_path):
    body = (
        "# publications fixture\n"  # line 1
        "- subsection: Peer-Reviewed Original Research\n"  # line 2
        "  entries:\n"  # line 3
        "    - title: Cost was $5 million\n"  # line 4
        "      authors:\n"  # line 5
        "        - Public JQ\n"  # line 6
        "      journal: JAMA\n"  # line 7
        "      year: 2024\n"  # line 8
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    dollar = [i for i in issues if i.field == "title" and "math mode" in i.message]
    assert len(dollar) == 1
    assert dollar[0].severity == ERROR
    assert dollar[0].line == 4


def test_escaped_dollar_is_not_flagged(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: 'Cost was \\$5 million'\n"
        "      authors:\n"
        "        - Public JQ\n"
        "      journal: JAMA\n"
        "      year: 2024\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    assert not any("math mode" in i.message for i in issues)


# ---------- WARNING: quoted-numeric coercion ----------


def test_unquoted_pmid_is_coercion_warning(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: Numeric pmid\n"
        "      authors:\n"
        "        - Public JQ\n"
        "      journal: JAMA\n"
        "      year: 2024\n"
        "      pmid: 90000011\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    coerce = [i for i in issues if i.field == "pmid"]
    assert len(coerce) == 1
    assert coerce[0].severity == WARNING
    assert "quote it" in coerce[0].message


# ---------- WARNING: schema-rule violations (reused from field_handlers) ----------


def test_bad_doi_regex_is_warning(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: Bad doi\n"
        "      authors:\n"
        "        - Public JQ\n"
        "      journal: JAMA\n"
        "      year: 2024\n"
        "      doi: not-a-doi\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    assert any(i.field == "doi" and i.severity == WARNING for i in issues)


def test_month_out_of_range_is_warning(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: Bad month\n"
        "      authors:\n"
        "        - Public JQ\n"
        "      journal: JAMA\n"
        "      year: 2024\n"
        "      month: 13\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    assert any(i.field == "month" and i.severity == WARNING for i in issues)


def test_required_field_missing_is_warning(tmp_path):
    body = (
        "- subsection: Peer-Reviewed Original Research\n"
        "  entries:\n"
        "    - title: No journal\n"
        "      authors:\n"
        "        - Public JQ\n"
        "      year: 2024\n"
    )
    p = _write(tmp_path, "publications.yml", body)
    issues = data_check.check_file("publications", p)
    miss = [i for i in issues if i.field == "journal"]
    assert len(miss) == 1
    assert miss[0].severity == WARNING and "required" in miss[0].message


def test_bad_select_status_is_warning(tmp_path):
    body = (
        "- status: bogus\n  date: 01/2024 - 12/2026\n  agency: NIH\n  title: A grant\n  role: PI\n"
    )
    p = _write(tmp_path, "research_support.yml", body)
    issues = data_check.check_file("research_support", p)
    assert any(i.field == "status" and i.severity == WARNING for i in issues)


# ---------- WARNING: active-grant past-end (reused helper) ----------


def test_active_grant_past_end_is_warning(tmp_path):
    body = (
        "- status: active\n"
        "  date: 01/2020 - 12/2020\n"
        "  agency: NIH\n"
        "  title: Old grant\n"
        "  role: PI\n"
    )
    p = _write(tmp_path, "research_support.yml", body)
    issues = data_check.check_file("research_support", p)
    pe = [i for i in issues if i.field == "date" and "past" in i.message]
    assert len(pe) == 1 and pe[0].severity == WARNING


# ---------- check_data over a directory ----------


def test_check_data_missing_file_is_error(tmp_path):
    # Only drop in publications.yml; the other 9 section files are absent.
    _write(tmp_path, "publications.yml", CLEAN_PUBS)
    issues = data_check.check_data(tmp_path)
    missing = [i for i in issues if i.message == "data file not found"]
    assert len(missing) == 9  # all sections except publications
    assert all(i.severity == ERROR for i in missing)


def test_summarize_and_has_errors():
    issues = [
        data_check.Issue(ERROR, "s", "f", 1, "e", "x", "boom"),
        data_check.Issue(WARNING, "s", "f", 2, "e", "y", "meh"),
        data_check.Issue(WARNING, "s", "f", 3, "e", "z", "meh"),
    ]
    assert data_check.summarize(issues) == {ERROR: 1, WARNING: 2}
    assert data_check.has_errors(issues) is True
    assert data_check.has_errors(issues[1:]) is False


# ---------- read-only sanity: the real corpus must be ERROR-clean ----------


def test_real_corpus_has_no_errors():
    """The shipped data/ builds, so it must carry zero ERROR-tier issues.
    READ-ONLY — no writes, no concurrency hazard."""
    issues = data_check.check_data(ROOT / "data")
    errs = [i for i in issues if i.severity == ERROR]
    assert errs == [], [(i.file, i.line, i.field, i.message) for i in errs]


# ---------- CP2: scripts/check_data.py CLI shim exit codes ----------


def _import_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_data_cli", ROOT / "scripts" / "check_data.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_clean_corpus_exit_0(tmp_path):
    # A full clean corpus is hard to fixture; point at the real (clean) data/
    # via --data-dir and assert exit 0 (errors fail; this corpus has none).
    cli = _import_cli()
    assert cli.main(["--data-dir", str(ROOT / "data")]) == 0


def test_cli_error_fails_default(tmp_path):
    # A dir with only publications.yml -> the 9 missing files are ERRORs.
    _write(tmp_path, "publications.yml", CLEAN_PUBS)
    cli = _import_cli()
    assert cli.main(["--data-dir", str(tmp_path)]) == 1


PUBS_ONE_WARNING = CLEAN_PUBS.rstrip() + "\n      pmid: 12345678\n"  # unquoted -> 1 warning


def test_cli_warning_only_passes_default_but_fails_strict(tmp_path):
    # Build a corpus that is 0-error / 1-warning: copy the (clean) real corpus,
    # then overwrite ONLY publications.yml with a controlled one-warning fixture.
    # No string-matching the real file — fully robust.
    import shutil

    for key in data_check.schemas.all_sections():
        name = Path(data_check.schemas.get(key)["file"]).name
        shutil.copy(ROOT / "data" / name, tmp_path / name)
    (tmp_path / "publications.yml").write_text(PUBS_ONE_WARNING)
    issues = data_check.check_data(tmp_path)
    assert data_check.has_errors(issues) is False
    assert any(i.field == "pmid" for i in issues)
    cli = _import_cli()
    assert cli.main(["--data-dir", str(tmp_path)]) == 0  # warnings don't fail
    assert cli.main(["--data-dir", str(tmp_path), "--strict"]) == 1  # strict does
