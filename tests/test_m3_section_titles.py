"""M3.3 section-title drift guard (gotcha #41).

Section display titles are DUPLICATED: each ``content/<key>.typ`` glue file
renders ``section(...)[<Title>]``, and ``templates/bespoke/emit.typ`` emits the SAME title as
a literal source string for the freeze/flatten path. They are hand-kept in sync
and have drifted before. This guard pins both to ONE canonical map: rename a
header and you must update ``content/``, ``templates/bespoke/emit.typ``, AND this map, or a
test fails.

Pure Python, ZERO Typst change. M3 ships the CHECK only; render-side
single-sourcing (a shared title dict consumed by content/ + emit) is deferred
to M5a, where ``templates/bespoke/render.typ`` moves anyway. Do NOT implement it with
``eval(source-string, mode: "markup")``: there is no byte-identical proof for
the ``honors`` ``#emph[&]`` markup (the ``&`` is emphasised, not literal), and
the freeze byte-diff test only exercises emit.typ's literal strings, not a
render-side eval path — so an eval drift would go uncaught.
"""

from __future__ import annotations

import re
from pathlib import Path

from _engine_guards import HAS_BESPOKE, bespoke_required

# Both tests here read the private bespoke template (content/ + emit.typ);
# skip cleanly on a bespoke-absent tree instead of ERRORing at collection.
pytestmark = bespoke_required

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "templates" / "bespoke" / "content"
# Guarded so the module-scope read can't raise FileNotFoundError at COLLECTION
# time on a bespoke-absent tree (skipif can't rescue a module-level crash).
EMIT = (ROOT / "templates" / "bespoke" / "emit.typ").read_text() if HAS_BESPOKE else ""

# section key -> display title. The title carries Typst markup VERBATIM:
# `honors` uses `#emph[&]` (emphasised ampersand) — it must NOT be flattened to
# a plain "&" or the rendered output changes (byte-identical invariant).
SECTION_TITLES = {
    "education": "Education",
    "appointments": "Professional Appointments",
    "publications": "Scholarly Publications",
    "presentations": "Presentations",
    "research_support": "Research Support",
    "service": "Professional Service",
    "teaching": "Teaching Experience",
    "honors": "Honors #emph[&] Awards",
    "mentees": "Mentees",
}


def test_content_glue_files_use_canonical_titles():
    for key, title in SECTION_TITLES.items():
        src = (CONTENT / f"{key}.typ").read_text()
        assert f"[{title}]" in src, (
            f"content/{key}.typ does not render section(...)[{title}] — the "
            f"title drifted from the canonical map (update content/, emit.typ, "
            f"AND this test)."
        )


def test_emit_mirror_uses_canonical_titles():
    for key, title in SECTION_TITLES.items():
        assert f")[{title}]" in EMIT, (
            f"templates/bespoke/emit.typ does not emit )[{title}] for '{key}' — the freeze "
            f"mirror drifted from content/{key}.typ (gotcha #41)."
        )


def test_each_content_file_has_exactly_one_section_header():
    # A stray/renamed second header in a glue file could slip past the presence
    # checks above; pin each file to exactly one section(...) call.
    for key in SECTION_TITLES:
        src = (CONTENT / f"{key}.typ").read_text()
        headers = re.findall(r"section\([^)]*\)\[", src)
        assert len(headers) == 1, (
            f"content/{key}.typ has {len(headers)} section(...) headers; expected exactly 1."
        )
