"""The `web:` column and its filter on the section list (2026-08-15).

THE BUG THIS FIXES, stated as the reporter saw it: "the Show hidden filter on the
publications page doesn't work." It did not. `Show hidden` filters
`highlighted: true`, and in the reference corpus **no publication carries that
field** — so the control could never reveal a row, while a real and invisible
58-of-104 `web: hide` split sat on the same page with no column and no filter.

The lesson, recorded because it shaped the fix: a control that can never do
anything IS broken, even when the code behind it is sound. The first response to
the report was "correctly wired, nothing to reveal", which measured the right
thing and concluded the wrong thing.

Design points these tests pin:

* `web_hidden` counts ONLY an explicit `hide`. Blank means "automatic" and what it
  resolves to is the site exporter's business (it may default off a sibling field
  such as `slides`), so the engine must not guess. Under-reporting a blank is
  honest; inventing a default puts a number on the page no exporter agrees with.
* The toggle DEFAULTS ON, so the page opens on the full list exactly as it did
  before the filter existed. Chosen by the owner over defaulting to the
  web-visible subset.
* Column and filter travel together — a filter for a value you cannot see is the
  original defect one step removed.
* No row is hidden server-side for `web`, because the default is "show".
"""

from __future__ import annotations

import re

import pytest
from cv_editor import schemas
from cv_editor.app import create_app

#: Every section carrying the `web` field, and therefore the column + filter.
WEB_SECTIONS = ("publications", "presentations", "teaching")


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _tag(body: str, elem_id: str) -> str:
    m = re.search(rf'<input[^>]*id="{re.escape(elem_id)}"[^>]*>', body)
    assert m, f"input#{elem_id} not found in rendered page"
    return m.group(0)


# --------------------------------------------------------------------------- #
# schema: the column exists exactly where the field does
# --------------------------------------------------------------------------- #


def test_the_web_column_is_declared_for_every_section_carrying_the_field():
    """Both directions. A section with a `web` field but no column would have an
    invisible gate; a column without the field would render an empty stripe."""
    for key, sch in schemas.SCHEMAS.items():
        has_field = any(f.get("name") == "web" for f in sch.get("fields", []))
        has_column = "web" in (sch.get("list_columns") or [])
        assert has_field == has_column, (
            f"section {key!r}: web field={has_field} but web column={has_column} — "
            "the column and the field must travel together"
        )


def test_the_web_bearing_sections_are_the_expected_three():
    """Guards against silently widening the surface. If a fourth section gains
    `web`, that is a decision worth making on purpose."""
    found = tuple(
        k for k, s in schemas.SCHEMAS.items() if any(f.get("name") == "web" for f in s["fields"])
    )
    assert set(found) == set(WEB_SECTIONS), f"web-bearing sections changed: {found}"


# --------------------------------------------------------------------------- #
# the row flag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hide", True),
        ("HIDE", True),  # case-folded
        ("  hide  ", True),  # whitespace-tolerant
        ("show", False),
        ("", False),  # blank = automatic, NOT hidden
        (None, False),  # absent = automatic, NOT hidden
    ],
)
def test_web_hidden_counts_only_an_explicit_hide(client, raw, expected):
    """Pinned through the RENDERED page rather than by calling the private row
    builder, so the template's data attribute is covered too."""
    from cv_editor import yaml_io
    from cv_editor.paths import data_dir

    path = data_dir() / "publications.yml"
    header, data = yaml_io.load(path)
    entry = next(e for sub in data for e in (sub.get("entries") or []))
    original = entry.get("web", "__absent__")
    try:
        if raw is None:
            entry.pop("web", None)
        else:
            entry["web"] = raw
        yaml_io.write_with_backup(path, header, data)
        body = client.get("/publications").data.decode("utf-8")
        first = re.search(r'<tr class="entry-row.*?>', body, re.S).group(0)
        assert f'data-web-hidden="{"1" if expected else "0"}"' in first, (
            f"web={raw!r} should render web_hidden={expected}: {first[:200]}"
        )
    finally:
        header2, data2 = yaml_io.load(path)
        e2 = next(e for sub in data2 for e in (sub.get("entries") or []))
        if original == "__absent__":
            e2.pop("web", None)
        else:
            e2["web"] = original
        yaml_io.write_with_backup(path, header2, data2)


# --------------------------------------------------------------------------- #
# the control
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("section", WEB_SECTIONS)
def test_the_web_toggle_renders_checked_by_default(client, section):
    """DEFAULT ON is the owner's call: the page must open on the full list, the
    same as before this filter existed. An unchecked default would silently drop
    over half the publications on first paint."""
    body = client.get(f"/{section}").data.decode("utf-8")
    assert "checked" in _tag(body, "show-web-hidden")


def test_a_section_without_the_web_field_has_no_web_toggle(client):
    """`service` carries no `web` field, so offering the filter would imply a
    gate that section does not have."""
    body = client.get("/service").data.decode("utf-8")
    assert 'id="show-web-hidden"' not in body
    assert 'id="show-hidden"' in body, "the highlighted filter should still be offered"


@pytest.mark.parametrize("section", WEB_SECTIONS)
def test_no_row_is_hidden_server_side_for_web(client, section):
    """The default is "show", so the initial paint must not carry `hidden` on a
    web-hidden row. This is the same class as the 2026-05-25 on-load
    `applyFilters()` regression: a server-side `hidden` plus a checked box means
    rows that never come back until the user toggles twice."""
    body = client.get(f"/{section}").data.decode("utf-8")
    rows = re.findall(r'<tr class="entry-row.*?>', body, re.S)
    assert rows, f"/{section} rendered no rows"
    for r in rows:
        if 'data-web-hidden="1"' in r:
            assert "hidden" not in r.replace("data-web-hidden", ""), (
                f"a web-hidden row is hidden server-side: {r[:200]}"
            )


def test_the_filter_script_consults_the_web_flag(client):
    """The control and the data attribute are both useless without the join.

    PINS THE EXPRESSION LITERALLY, and that is deliberate. The first version of
    this test asserted that the identifiers `webHidden` and `showWebH` appeared
    somewhere in the page — and a mutant that replaced the whole filter clause
    with `(false && isWebHidden)` PASSED, because both identifiers survive in
    their declarations. Page furniture, exactly the failure this suite exists to
    catch. The filter runs in the browser, so nothing here can execute it; the
    honest substitute is to pin the one line that does the work.
    """
    body = client.get("/publications").data.decode("utf-8")
    assert "show-web-hidden" in body
    assert "r.dataset.webHidden === '1'" in body, "the row flag must be read from the DOM"
    assert "!showWebHidden || showWebHidden.checked" in body, (
        "an absent control must mean 'show everything', not 'hide everything'"
    )
    assert "(!showWebH && isWebHidden)" in body, (
        "the filter expression must actually gate on the web flag"
    )


def test_blank_web_renders_as_auto_not_as_empty(client):
    """Blank is a real third state. Rendering it as an empty cell reads as
    `show`, which is exactly the wrong inference — the exporter decides."""
    body = client.get("/publications").data.decode("utf-8")
    assert ">auto<" in body, "an unset `web` should render the muted 'auto' marker"


def test_the_hint_names_both_gates_and_says_they_are_independent(client):
    """The original report came from one control's label implying it covered
    "hidden" in general. The page must state that these are two gates."""
    body = client.get("/publications").data.decode("utf-8")
    assert "Two independent gates" in body
    assert "highlighted: true" in body
    assert "web: hide" in body
    assert "affects no PDF" in body
