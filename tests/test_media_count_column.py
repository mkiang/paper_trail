"""Media count column on /publications (2026-05-26).

Per-row count of media outlets: `shown / total`, where shown = outlets
without `highlighted: true`. Hidden by default; toggled by the "Show
Media (shown / total)" checkbox on the toolbar. Mirrors the V14 Cited-by
toggle pattern.

Sort key = total count (so sort-desc floats high-coverage papers).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cv_editor import paths
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent

# Synthetic publications corpus with KNOWN, controlled media-outlet counts, so
# the shown/total logic is asserted against deterministic values rather than
# the real corpus's scale. Written into the isolated tmp workspace (the autouse
# `_workspace_isolation` fixture points paths.data_dir() at a per-test copy), so
# it never touches real data. Outlet-count contract (shown = outlets without
# `highlighted: true`; total = every outlet across every media note):
#   Alpha  -> 1 media note, 3 outlets, 1 highlighted        => shown 2 / total 3
#   Beta   -> 1 media note, 2 outlets, 0 highlighted         => shown 2 / total 2
#   Gamma  -> no media notes                                 => total 0 (em-dash)
#   Delta  -> 2 media notes, 4 outlets total, 2 highlighted  => shown 2 / total 4
_SYNTHETIC_PUBLICATIONS = """\
# Synthetic publications corpus (fictional) for the media-count column test.
- subsection: Peer-Reviewed Original Research
  entries:
  - title: Alpha study with mixed outlets
    authors:
    - Public JQ
    - Doe AB
    year: 2025
    doi: 10.9999/alpha.2025
    notes:
    - type: media
      outlets:
      - name: Outlet One
        url: https://example.org/a1
      - Outlet Two
      - name: Outlet Three
        url: https://example.org/a3
        highlighted: true
  - title: Beta study fully shown
    authors:
    - Public JQ
    year: 2024
    doi: 10.9999/beta.2024
    notes:
    - type: media
      outlets:
      - Outlet One
      - name: Outlet Two
        url: https://example.org/b2
  - title: Gamma study with no media
    authors:
    - Public JQ
    year: 2023
    doi: 10.9999/gamma.2023
  - title: Delta study with two media notes
    authors:
    - Public JQ
    year: 2022
    doi: 10.9999/delta.2022
    notes:
    - type: media
      outlets:
      - name: Outlet One
        url: https://example.org/d1
        highlighted: true
      - Outlet Two
    - type: media
      outlets:
      - Outlet Three
      - name: Outlet Four
        url: https://example.org/d4
        highlighted: true
"""

# Expected (shown, total) per synthetic entry, independent of row order.
_EXPECTED_COUNTS = {(2, 3), (2, 2), (2, 4)}  # nonzero rows; Gamma is 0 (em-dash)


@pytest.fixture
def client():
    # Overwrite the isolated-workspace publications.yml with the synthetic
    # corpus BEFORE create_app so the /publications route loads known counts.
    (paths.data_dir() / "publications.yml").write_text(_SYNTHETIC_PUBLICATIONS, encoding="utf-8")
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_media_count_column_renders_on_publications(client):
    body = client.get("/publications").get_data(as_text=True)
    assert 'id="show-media-counts"' in body
    assert "col-media-count-head" in body
    assert "col-media-count" in body


def test_media_count_column_hidden_by_default(client):
    body = client.get("/publications").get_data(as_text=True)
    # Both header and cells carry `hidden` attribute server-side. JS toggle
    # flips this on user opt-in.
    # data-kind="text" matches the invariant from test_v17_polish.py
    # (numeric kind would parseFloat date strings and re-trigger the cross-year
    # bug; all sortable columns are text-kind with pre-normalized sort values).
    assert (
        'col-media-count-head" data-col="media_count" data-kind="text" aria-sort="none" hidden'
        in body
    )


def test_media_count_column_absent_on_non_publications(client):
    """The column header + cells + toolbar toggle are gated behind
    `section_key == 'publications'`. The JS handler text references the
    selector strings on every page (harmless no-op when querySelectorAll
    matches nothing), so check for the actual <th>/<td> markup."""
    body = client.get("/mentees").get_data(as_text=True)
    # The header is a `<th class="sortable-col col-media-count-head" ...>`;
    # the cell is `<td class="num col-media-count" ...>`. Both are gated.
    assert "<th class=\"sortable-col col-media-count-head\"" not in body
    assert "<td class=\"num col-media-count\"" not in body
    # Toolbar toggle (gated by section_key == 'publications').
    assert 'name="show-media-counts"' not in body
    assert '<input type="checkbox" id="show-media-counts"' not in body


def test_media_count_cells_show_shown_slash_total(client):
    body = client.get("/publications").get_data(as_text=True)
    cells = re.findall(
        r'col-media-count"\s+data-sort-value="(\d+)"\s+hidden>\s*(.*?)\s*</td>',
        body,
        re.DOTALL,
    )
    # Every synthetic publication row contributes exactly one cell.
    assert len(cells) == 4, f"expected 4 media-count cells, got {len(cells)}"
    nonzero = [(int(t), d) for t, d in cells if int(t) > 0]
    # Three of the four synthetic entries have media outlets.
    assert len(nonzero) == 3, f"expected 3 nonzero media cells, got {len(nonzero)}"
    # Display format: "<shown> / <total>"; sort-value == total; shown <= total.
    observed = set()
    for total, display in nonzero:
        match = re.match(r"^(\d+)\s*/\s*(\d+)$", display)
        assert match, f"expected 'shown / total', got {display!r}"
        shown, displayed_total = int(match.group(1)), int(match.group(2))
        assert displayed_total == total, "total in display must match sort-value"
        assert shown <= total, f"shown ({shown}) must be <= total ({total})"
        observed.add((shown, total))
    # The exact known (shown, total) pairs for the synthetic corpus.
    assert observed == _EXPECTED_COUNTS, observed


def test_media_count_zero_papers_render_emdash(client):
    body = client.get("/publications").get_data(as_text=True)
    # Some papers have no media notes at all (total=0). They render `—`.
    cells = re.findall(
        r'col-media-count"\s+data-sort-value="0"\s+hidden>\s*(.*?)\s*</td>',
        body,
        re.DOTALL,
    )
    assert cells, "expected some papers to have zero media outlets"
    for display in cells[:5]:
        assert "—" in display or "&#x2014;" in display or "&mdash;" in display, (
            f"expected em-dash for zero-media row, got {display!r}"
        )


def test_media_count_total_includes_highlighted(client):
    """Total should count ALL outlets including highlighted:true ones —
    that's the whole point of distinguishing shown vs total."""
    body = client.get("/publications").get_data(as_text=True)
    # Find any row where shown < total (proves highlighted outlets ARE counted).
    cells = re.findall(
        r'col-media-count"\s+data-sort-value="(\d+)"\s+hidden>\s*(\d+)\s*/\s*(\d+)\s*</td>',
        body,
        re.DOTALL,
    )
    nonzero = [(int(t1), int(s), int(t2)) for t1, s, t2 in cells if int(t1) > 0]
    # At least one synthetic paper has hidden outlets (shown < total): Alpha
    # (2/3) and Delta (2/4) both carry highlighted: true outlets, which prove
    # highlighted outlets ARE counted in total.
    has_hidden = [(s, t) for _, s, t in nonzero if s < t]
    assert has_hidden, (
        "expected at least one paper with highlighted outlets (shown < total). "
        "If this fires, the count predicate may be ignoring highlighted: true."
    )
