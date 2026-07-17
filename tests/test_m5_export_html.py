"""M5 5b CP4: HTML emitter + CLI. Golden test vs the frozen fixture corpus +
well-formedness + escaping/leak structural asserts. Read-only (no real-data writes).
Regenerate the golden with:
    python scripts/export_html.py --data-dir tests/fixtures/export -o tests/fixtures/export/expected.html
"""

from __future__ import annotations

from pathlib import Path

import lxml.html as LH
from cv_editor import export_core, export_emit

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "export"
GOLDEN = FIXTURE / "expected.html"


def _render():
    return export_emit.render_html(export_core.build_model(FIXTURE, target=export_core.HTML))


def test_html_matches_golden():
    out = _render()
    assert out == GOLDEN.read_text(encoding="utf-8"), (
        "HTML export drifted from the golden. If intentional, regenerate:\n"
        "  python scripts/export_html.py --data-dir tests/fixtures/export "
        "-o tests/fixtures/export/expected.html"
    )


def test_html_is_well_formed():
    """lxml parses without error and the structure is what the model implies
    (all 9 section builders exercised, in meta.sections order)."""
    root = LH.fromstring(_render())
    assert [e.text_content() for e in root.xpath("//h2")] == [
        "Education",
        "Professional Appointments",
        "Scholarly Publications",
        "Presentations",
        "Research Support",
        "Professional Service",
        "Teaching Experience",
        "Honors & Awards",
        "Mentees",
    ]
    assert [e.text_content() for e in root.xpath("//h3")] == [
        "Academic Appointments",
        "Peer-Reviewed Original Research",
        "Invited Talks",
        "Active Support",
        "Previous Support",
        "Professional Service",
    ]
    assert len(root.xpath("//ul[@class='entries']/li")) >= 13  # exact count pinned by the golden


def test_html_education_clusters_sorted_newest_first():
    """Fix B: render-education reorders clusters newest-first; export must match
    even though the fixture YAML lists Older State College first."""
    out = _render()
    edu = out.split("<h2>Education</h2>", 1)[1].split("<h2>", 1)[0]
    assert edu.index("Newer A&amp;M University") < edu.index("Older State College")


def test_html_cluster_institution_is_literal_not_self_bolded():
    """Fix C: cluster institutions render via the literal `institution()` helper
    (text(weight:bold)), NOT mk() — so a self_bold term inside is NOT separately
    bolded, and a literal & is escaped (not markup)."""
    out = _render()
    assert "<strong>Public JQ University</strong>" in out  # whole name bold, no inner <strong>
    assert "<strong><strong>" not in out  # no nested self-bold
    assert "<strong>Newer A&amp;M University</strong>" in out


def test_html_escapes_literal_ampersand_in_section_title():
    """'Honors & Awards' must be &amp;-escaped (the model-layer _plain fix)."""
    out = _render()
    assert "<h2>Honors &amp; Awards</h2>" in out
    assert "<h2>Honors & Awards</h2>" not in out


def test_html_links_are_safe_schemes_only():
    root = LH.fromstring(_render())
    hrefs = [a.get("href") for a in root.xpath("//a")]
    assert hrefs, "expected linked ids/contacts"
    for h in hrefs:
        assert h.split(":", 1)[0] in ("http", "https", "mailto"), f"unsafe scheme: {h}"
    assert "mailto:jane@example.org" in hrefs  # email linkified
    assert "https://example.org/jane" in hrefs  # website linkified


def test_html_key_fidelity_markers():
    out = _render()
    assert "<h1>Jane Q Public</h1>" in out  # header
    assert "<strong>Public JQ</strong>" in out  # self-bold
    assert "<sup>‡</sup>" in out and "<sup>†</sup>" in out  # co-author glyphs
    assert "Senior authors contributed equally." in out  # footnote (author_flags reuse)
    assert "<strong>examples</strong>" in out  # *examples* markup converted
    assert "<em>Journal of Examples</em>" in out  # _italic_ markup
    assert "2024 Mar 5;9(2):100-12" in out  # pub date assembly
    assert "2023; e069008" in out  # electronic id, no volume
    assert "Active Support" in out and "Previous Support" in out
    assert "Pending Support" not in out  # show_pending False
    assert "($1,000,000)" in out  # grant amount keeps the $ (Fix A)
    assert "View variant: fullcv" in out  # variant surfaced (HTML comment)
    # New-section coverage (all 9 builders): presentations / service / teaching / mentees.
    assert "(June 2024)" in out  # presentation month-year
    assert "Alpha Review • Zeta Journal" in out  # ad-hoc reviewer journals sorted
    assert "<em>PhD</em>, Examples" in out  # education degree
    assert "Alex Example (Example University)" in out  # mentee


def test_html_leak_guard():
    """The public (fullcv) export must omit every hidden fixture item, across
    all 9 sections (entry-level hide-from in pubs/talks/service/appointments/
    mentees/honors + pending grant)."""
    out = _render()
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


def test_cli_runs_on_fixture(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_html_cli", ROOT / "scripts" / "export_html.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--data-dir", str(FIXTURE)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<!doctype html>" in out and "<h1>Jane Q Public</h1>" in out
