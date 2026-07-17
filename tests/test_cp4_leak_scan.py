r"""CP4/B5 + B6 — the shared discrete-pattern leak scanner + anchored `not in`.

leak_scan.py is the SINGLE implementation both the exporter's pre-ship
leak_gate() and the public ci_leak_check.sh run (no bash/python drift). These
pin: the real allowlist keeps the shipped corpus clean; a seeded fake identifier
in data/ is flagged (P8); tests/ + bare code digit-runs are out of scope; the
`# leak-allow` marker exempts a line in the pattern layer (which — CP4 re-confirm
— has NO `not in` exemption; that anchored guard is TOKEN-scan-only, pinned via
the exporter regex + the CI GUARD); and the exporter wires leak_scan in.
"""

from __future__ import annotations

import re
from pathlib import Path

import leak_scan
import pytest
from cv_editor import paths

_ROOT = paths.project_root()
ALLOW = _ROOT / "scripts" / "leak_allow.txt"
# The exporter + overlay are PRIVATE (denylisted) — absent in the shipped public
# tree, where leak_scan itself still runs in CI. Guard the tests that read them.
_HAS_EXPORTER = (_ROOT / "scripts" / "export_paper_trail.py").is_file()
_HAS_OVERLAY = (_ROOT / "plans" / "public_overlay").is_dir()


def _tree(tmp: Path, files: dict[str, str]) -> Path:
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp / "scripts" / "leak_allow.txt").write_text(ALLOW.read_text(encoding="utf-8"))
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp


def _scan(tmp: Path) -> list[str]:
    return leak_scan.scan_tree(tmp, tmp / "scripts" / "leak_allow.txt")


def test_real_export_tree_is_clean():
    # The actual shipped tree the exporter produced must pass the scanner.
    export = paths.project_root() / "output" / "paper_trail_export"
    if not (export / "scripts" / "leak_allow.txt").is_file():
        import pytest

        pytest.skip("no export tree present (run scripts/export_paper_trail.py)")
    assert leak_scan.scan_tree(export, export / "scripts" / "leak_allow.txt") == []


def test_allowlisted_corpus_ids_pass(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "data/pubs.yml": (
                "doi: 10.9999/jse.2025.0001\n"
                "orcid: 0000-0002-1825-0097\n"
                "pmid: '90000001'\n"
                "pmcid: PMC90000001\n"
            ),
            "data/meta.yml": "email: jane@example.org\nphone: 555-867-5309\n",
            "data/grants.yml": "amount: $425,000\n",
        },
    )
    assert _scan(tree) == []


def test_seeded_fake_ids_in_corpus_are_flagged(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "data/pubs.yml": (
                "orcid: 0000-0001-2222-3333\n"  # fake, not allowlisted
                "pmid: '38123456'\n"  # fake PMID (8-digit)
                "doi: 10.1001/jama.2099.5\n"  # real JAMA prefix, NOT the allowlisted example
                "email: real.person@metro.edu\n"  # non-@example domain -> flagged; not a forbidden token
                "phone: 617-555-0143\n"
            )
        },
    )
    kinds = {f.split(": ", 1)[1].split(" ")[0] for f in _scan(tree)}
    assert {"orcid", "pmid", "doi", "email", "phone"} <= kinds


def test_tests_dir_excluded_from_pattern_layer(tmp_path):
    tree = _tree(tmp_path, {"tests/test_x.py": "orcid = '0000-0001-9999-8888'\n"})
    assert _scan(tree) == []


def test_code_scope_ignores_bare_digit_runs_but_flags_structured(tmp_path):
    tree = _tree(
        tmp_path,
        {
            # a bare 8-digit run in code (line number / constant) is NOT a PMID hit
            "scripts/cv_editor/x.py": "MAGIC = 38123456  # not a PMID in code scope\n",
            # but a structured ORCID in code IS flagged
            "scripts/cv_editor/y.py": "ORCID = '0000-0001-2222-3333'\n",
        },
    )
    findings = _scan(tree)
    assert any("y.py" in f and "orcid" in f for f in findings)
    assert not any("x.py" in f for f in findings)


def test_leak_allow_marker_exempts_a_line(tmp_path):
    tree = _tree(
        tmp_path, {"data/x.yml": "orcid: 0000-0001-2222-3333  # leak-allow test fixture\n"}
    )
    assert _scan(tree) == []


def test_pattern_layer_has_no_not_in_exemption(tmp_path):
    # CP4 re-confirm (leak-gate MED): the discrete-pattern layer does NOT exempt
    # an `x not in y` line — only `# leak-allow` exempts here. A raw identifier on
    # a line that happens to contain "not in" is STILL flagged (there is no
    # assert-absence idiom for a raw DOI/PMID literal in a shipped non-test file,
    # so a `not in` exemption would only open a bypass). The anchored `not in`
    # guard lives ONLY in the exporter/CI TOKEN scan
    # (test_exporter_not_in_regex_is_anchored + test_ci_leak_check_invokes_scanner).
    tree = _tree(
        tmp_path,
        {
            # "not intended" was never exempt (no `not in` here) -> phone flagged
            "data/a.yml": "note: this is not intended  # phone 617-555-0143\n",
            # a literal `x not in y` line is NOW flagged in the pattern layer
            "data/b.yml": "assert '0000-0001-2222-3333' not in raw\n",
        },
    )
    findings = _scan(tree)
    assert any("a.yml" in f and "phone" in f for f in findings)
    assert any("b.yml" in f and "orcid" in f for f in findings)


@pytest.mark.skipif(not _HAS_EXPORTER, reason="private exporter not shipped to the public tree")
def test_exporter_not_in_regex_is_anchored():
    from export_paper_trail import _NOT_IN_RE

    assert _NOT_IN_RE.search("assert x not in y")
    assert not _NOT_IN_RE.search("this is not intended")
    assert not _NOT_IN_RE.search("features not including foo")


@pytest.mark.skipif(not _HAS_EXPORTER, reason="private exporter not shipped to the public tree")
def test_exporter_leak_gate_wires_in_leak_scan():
    # Cheap wiring guard: the exporter's leak_gate delegates to the shared scanner.
    src = (paths.project_root() / "scripts" / "export_paper_trail.py").read_text()
    assert "import leak_scan" in src
    assert "leak_scan.scan_tree(" in src


@pytest.mark.skipif(not _HAS_OVERLAY, reason="private overlay not shipped to the public tree")
def test_ci_leak_check_invokes_scanner():
    ci = (
        paths.project_root() / "plans" / "public_overlay" / "scripts" / "ci_leak_check.sh"
    ).read_text()
    assert "leak_scan.py" in ci
    # B6: the GUARD anchors `not in` with POSIX word boundaries (not bare substring).
    assert re.search(r"not\[\[:space:\]\]\+in", ci)
