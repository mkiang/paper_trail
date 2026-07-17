"""M5 5b CP3: Markdown emitter + CLI. Golden test vs a frozen fixture corpus.
Read-only (no real-data writes). Regenerate the golden with:
    python scripts/export_markdown.py --data-dir tests/fixtures/export -o tests/fixtures/export/expected.md
"""

from __future__ import annotations

from pathlib import Path

from cv_editor import export_core, export_emit

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "export"
GOLDEN = FIXTURE / "expected.md"


def test_markdown_matches_golden():
    doc = export_core.build_model(FIXTURE, target=export_core.MD)
    out = export_emit.render_markdown(doc)
    assert out == GOLDEN.read_text(), (
        "Markdown export drifted from the golden. If intentional, regenerate:\n"
        "  python scripts/export_markdown.py --data-dir tests/fixtures/export "
        "-o tests/fixtures/export/expected.md"
    )


def test_markdown_leak_guard():
    """The public (fullcv) export must omit every hidden fixture item, across
    all 9 sections."""
    out = export_emit.render_markdown(export_core.build_model(FIXTURE, target=export_core.MD))
    for leaked in (
        "hidden from public",
        "Secret J",
        "Hidden honor",
        "Secret Org",
        "A pending grant",
        "Future Agency",
        "A hidden talk",
        "Secret Venue",
        "Hidden Service",
        "Hidden Appointment",
        "Hidden Mentee",
        "Secret Person",
    ):
        assert leaked not in out, f"LEAK: {leaked!r} appeared in the public export"


def test_markdown_key_fidelity_markers():
    out = export_emit.render_markdown(export_core.build_model(FIXTURE, target=export_core.MD))
    assert "# Jane Q Public" in out  # header
    assert "## Scholarly Publications" in out  # canonical section title
    assert "**Public JQ**" in out  # self-bold
    assert "Doe A‡" in out and "Roe B†" in out  # co-author glyphs
    assert "Senior authors contributed equally." in out  # footnote (author_flags reuse)
    assert "**examples**" in out  # *examples* markup converted
    assert "2024 Mar 5;9(2):100-12" in out  # pub date assembly
    assert "2023; e069008" in out  # electronic id, no volume
    assert "Active Support" in out and "Previous Support" in out
    assert "Pending Support" not in out  # show_pending False
    assert "($1,000,000)" in out  # grant amount keeps the $ (Fix A)
    assert "12/2026" in out and "–" in out  # en-dash date range


def test_markdown_all_nine_sections_and_fixes():
    """Coverage for the 6 section builders the original 3-file corpus missed,
    plus the education-sort (Fix B) and literal-cluster-institution (Fix C)."""
    out = export_emit.render_markdown(export_core.build_model(FIXTURE, target=export_core.MD))
    for header in (
        "## Education",
        "## Professional Appointments",
        "## Presentations",
        "## Professional Service",
        "## Teaching Experience",
        "## Mentees",
    ):
        assert header in out, f"missing section header {header!r}"
    # Fix B: education clusters newest-first despite oldest-first YAML order.
    edu = out.split("## Education", 1)[1].split("## Professional Appointments", 1)[0]
    assert edu.index("Newer A&M University") < edu.index("Older State College")
    # Fix C: literal cluster institution — "Public JQ" not separately bolded, whole bold.
    assert "**Public JQ University**" in out
    assert "***Public JQ**" not in out  # no markup/self-bold leak inside
    # Teaching clusters PRESERVE YAML order (NOT sorted) — Beta (2021) before PublicJQ (2024).
    teach = out.split("## Teaching Experience", 1)[1].split("## Honors", 1)[0]
    assert teach.index("Beta College") < teach.index("Public JQ University")
    # presentation month-year + ad-hoc journals sorted + mentee.
    assert "(June 2024)" in out
    assert "Alpha Review • Zeta Journal" in out
    assert "Alex Example (Example University)" in out


def test_cli_runs_on_fixture(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_md_cli", ROOT / "scripts" / "export_markdown.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--data-dir", str(FIXTURE)])
    assert rc == 0
    assert "# Jane Q Public" in capsys.readouterr().out
