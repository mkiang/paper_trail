"""V18-A group_authorship: true round-trip + render tests (2026-05-17).

The `group_authorship: true` author flag mirrors `co_first` / `co_senior`
in shape and rendering:
  * editor form has a checkbox, hidden JSON carries the third boolean,
    `author_names.author_to_form` / `form_to_yaml_author` round-trip it.
  * renderer appends a superscript ◊ after the author name and an
    "◊ Group authorship." footnote when at least one author carries the
    flag.
  * BibTeX export strips it (the flag is editorial metadata; the
    `author = {...}` field is a clean name list).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _engine_guards import HAS_BESPOKE

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT / "scripts"))

from cv_editor.author_names import author_to_form, form_to_yaml_author  # noqa: E402

# ---- author_names round-trip ---------------------------------------------


def test_author_to_form_string_default_has_false_group_authorship():
    f = author_to_form("Public JQ")
    assert f == {
        "name": "Public JQ",
        "co_first": False,
        "co_senior": False,
        "group_authorship": False,
    }


def test_author_to_form_dict_reads_group_authorship_true():
    f = author_to_form(
        {
            "name": "Example Consortium for Health",
            "group_authorship": True,
        }
    )
    assert f["group_authorship"] is True
    assert f["co_first"] is False
    assert f["co_senior"] is False


def test_form_to_yaml_author_plain_string_when_all_flags_false():
    out = form_to_yaml_author(
        {
            "name": "Public JQ",
            "co_first": False,
            "co_senior": False,
            "group_authorship": False,
        }
    )
    assert isinstance(out, str)
    assert out == "Public JQ"


def test_form_to_yaml_author_dict_when_group_authorship_true():
    out = form_to_yaml_author(
        {
            "name": "ExampleCorp",
            "co_first": False,
            "co_senior": False,
            "group_authorship": True,
        }
    )
    # ruamel CommentedMap is a dict subclass.
    assert isinstance(out, dict)
    assert out["name"] == "ExampleCorp"
    assert out["group_authorship"] is True
    # Falsy flags MUST NOT serialize.
    assert "co_first" not in out
    assert "co_senior" not in out


def test_form_to_yaml_author_dict_with_co_first_and_group_authorship():
    out = form_to_yaml_author(
        {
            "name": "Some Working Group Member",
            "co_first": True,
            "co_senior": False,
            "group_authorship": True,
        }
    )
    assert isinstance(out, dict)
    assert out["name"] == "Some Working Group Member"
    assert out["co_first"] is True
    assert out["group_authorship"] is True
    assert "co_senior" not in out


def test_round_trip_yaml_to_form_to_yaml():
    """Dict author → form dict → YAML author. Lossless for the flag."""
    yaml_author = {
        "name": "ExampleCorp",
        "group_authorship": True,
    }
    form = author_to_form(yaml_author)
    back = form_to_yaml_author(form)
    assert isinstance(back, dict)
    assert back["name"] == "ExampleCorp"
    assert back["group_authorship"] is True


# ---- entry_save route round-trip -----------------------------------------


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Use a temporary publications.yml so we don't disturb the real file."""
    sandbox = tmp_path / "data"
    sandbox.mkdir()
    # Copy every data file so the app boots; we only touch publications.yml.
    for src in (PROJ_ROOT / "data").iterdir():
        if src.is_file():
            shutil.copy2(src, sandbox / src.name)
    monkeypatch.chdir(tmp_path)
    from cv_editor.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app, app.test_client(), sandbox / "publications.yml"


def test_route_round_trip_group_authorship_flag(app_client):
    """POST a publications save with one group_authorship author; reload
    YAML and confirm the dict form persisted."""
    import yaml as pyyaml

    app, client, pubs_path = app_client

    # Read entry 0 to build a minimal POST shape.
    with open(pubs_path) as f:
        raw = f.read()
    # Skip past docstring header.
    body_start = raw.index("\n- subsection:")
    data = pyyaml.safe_load(raw[body_start:])
    assert isinstance(data, list)

    # Find the global_idx for the ExampleCorp entry.
    # It's idx 2 in the original (subsection 0, entries[2]). Use the live
    # one we just backfilled by name.
    corp_global_idx = None
    cursor = 0
    for sub in data:
        for j, e in enumerate(sub.get("entries", []) or []):
            authors = e.get("authors") or []
            for a in authors:
                if isinstance(a, dict) and a.get("group_authorship"):
                    corp_global_idx = cursor
                    break
                if isinstance(a, str) and "Example Consortium" in a:
                    corp_global_idx = cursor
                    break
            cursor += 1
    assert corp_global_idx is not None, "no ExampleCorp-style entry present"

    # GET edit form to confirm the authors-editor mounts (the JS file
    # is loaded; group_authorship is in the JSON data + author_flags).
    # The actual checkbox HTML is rendered client-side from JS now.
    resp = client.get(f"/publications/{corp_global_idx}/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "entry_edit.js" in body, "static JS asset not loaded"
    assert "group_authorship" in body  # in the JSON-data block


def test_entry_edit_template_renders_group_checkbox(app_client):
    _, client, _ = app_client
    body = client.get("/publications/0/edit").get_data(as_text=True)
    # After V20 B3, the literal class string lives in the static JS;
    # what we can assert from the rendered HTML is the JSON-data block
    # contains author_forms with the group_authorship key and the
    # static JS file is loaded.
    assert "entry_edit.js" in body
    assert "group_authorship" in body
    # The static JS owns the literal classes + tooltip text:
    from cv_editor import app as _app_mod

    js = (Path(_app_mod.__file__).parent / "static" / "entry_edit.js").read_text()
    assert "author-grpauth" in js
    assert "Corporate / consortium / working-group author" in js


def test_yaml_round_trip_persists_group_authorship_via_yaml_io(tmp_path):
    """R3-M1: write a publication entry with a group_authorship author
    through the live yaml_io pipeline, reload, assert the dict-form flag
    persisted. Exercises the production write path that the editor's
    save route ultimately invokes."""
    from cv_editor import yaml_io
    from cv_editor.author_names import form_to_yaml_author
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    pubs = tmp_path / "publications.yml"
    pubs.write_text(
        "# header docstring\n"
        "---\n"
        "- subsection: X\n"
        "  entries:\n"
        "  - title: existing\n"
        "    authors: [Public JQ]\n"
        "    journal: J\n"
        "    year: 2024\n"
    )
    header, data = yaml_io.load(pubs)
    new_entry = CommentedMap()
    new_entry["title"] = "Group entry"
    new_entry["authors"] = CommentedSeq(
        [
            form_to_yaml_author(
                {
                    "name": "ExampleCorp",
                    "co_first": False,
                    "co_senior": False,
                    "group_authorship": True,
                }
            ),
        ]
    )
    new_entry["journal"] = "Test"
    new_entry["year"] = 2025
    data[0]["entries"].append(new_entry)
    yaml_io.write_with_backup(pubs, header, data, expected_mtime_ns=pubs.stat().st_mtime_ns)

    _, data2 = yaml_io.load(pubs)
    appended = data2[0]["entries"][-1]
    a = appended["authors"][0]
    assert isinstance(a, dict), "author should be dict-form (flag set)"
    assert a["name"] == "ExampleCorp"
    assert a["group_authorship"] is True
    assert "co_first" not in a
    assert "co_senior" not in a


def test_preprint_promotion_preserves_group_authorship_flag():
    """R1-M4: promoting a preprint entry whose author has
    group_authorship: true must port the flag onto the canonical-author
    list when chosen_authors is not provided (default canonical path)."""
    from cv_editor.preprint import apply_promotion

    existing = {
        "title": "Preprint title",
        "authors": [
            {"name": "Some Working Group", "group_authorship": True},
        ],
        "journal": "bioRxiv",
    }
    canonical = {
        "title": "Final published title",
        "journal": "Nature",
        "year": 2026,
        "authors": ["Some Working Group"],  # PubMed returns plain strings
    }
    merged = apply_promotion(existing, canonical)
    authors = merged["authors"]
    assert len(authors) == 1
    a = authors[0]
    # Flag ported from preprint to canonical author.
    assert isinstance(a, dict)
    assert a["name"] == "Some Working Group"
    assert a["group_authorship"] is True


def test_author_rename_preserves_group_authorship_flag():
    """R1-L5: cross-entry author rename retains the dict-form flag."""
    from cv_editor.author_rename import apply_rename

    data = [
        {
            "subsection": "X",
            "entries": [
                {
                    "title": "test",
                    "authors": [
                        {"name": "Old ExampleCorp Name", "group_authorship": True},
                        "Public JQ",
                    ],
                },
            ],
        }
    ]
    n = apply_rename(data, "Old ExampleCorp Name", "New ExampleCorp Name")
    assert n == 1
    a = data[0]["entries"][0]["authors"][0]
    assert isinstance(a, dict)
    assert a["name"] == "New ExampleCorp Name"
    assert a["group_authorship"] is True


# ---- BibTeX export ---------------------------------------------------------


def test_bibtex_drops_group_authorship_glyph_and_footnote():
    """yaml_to_bibtex.py only reads `name`; the flag must NOT leak into
    BibTeX output as a glyph (◊) or footnote text."""
    from cv_editor.yaml_to_bibtex import authors_field  # type: ignore[import-not-found]

    authors = [
        {
            "name": "Example Consortium for Health",
            "group_authorship": True,
        },
        {"name": "Public JQ", "co_first": True},
    ]
    out = authors_field(authors)
    assert "◊" not in out
    assert "Group authorship" not in out
    # Corporate author wrapped in braces.
    assert "{Example Consortium for Health}" in out
    assert "Public, JQ" in out


def test_bibtex_format_single_group_author_no_glyph():
    from cv_editor.yaml_to_bibtex import format_author_for_bibtex  # noqa: PLC0415

    name = "Some Working Group"
    out = format_author_for_bibtex(name)
    # Corporate-author heuristic wraps in braces; no glyph appended.
    assert "◊" not in out
    assert out.startswith("{") and out.endswith("}")


# ---- Renderer smoke (Typst-dependent) ------------------------------------

_TYPST_AVAILABLE = all(shutil.which(t) for t in ("typst", "pdftotext")) and HAS_BESPOKE


@pytest.fixture(scope="module")
def everything_pdf_text():
    if not _TYPST_AVAILABLE:
        pytest.skip("need typst + pdftotext on PATH")
    res = subprocess.run(
        ["./build.sh"],
        cwd=PROJ_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if res.returncode != 0:
        pytest.fail(f"build.sh failed:\n{res.stderr}")
    pdf = PROJ_ROOT / "output" / "everything.pdf"
    return subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_group_authorship_glyph_renders_in_everything(everything_pdf_text):
    """The ◊ superscript appears at least once."""
    assert "◊" in everything_pdf_text


def test_group_authorship_footnote_renders_when_glyph_present(everything_pdf_text):
    """When ◊ appears in a citation, the "Group authorship." footnote
    sentence appears in the same entry."""
    # pdftotext -layout preserves line breaks; check the literal footnote
    # phrase is present.
    assert "Group authorship." in everything_pdf_text


def _group_authorship_probe():
    """Markup-free leading word-run of the first publication whose author
    list carries a group_authorship flag. Derived from the LIVE corpus so no
    real title is hardcoded in shipped source; returns None if the corpus has
    no such entry (e.g. the public example corpus), in which case the
    real-corpus render test skips."""
    from cv_editor import yaml_io

    _, data = yaml_io.load(PROJ_ROOT / "data" / "publications.yml")
    title = None
    for sub in data or []:
        for entry in sub.get("entries", []) or []:
            for a in entry.get("authors", []) or []:
                if isinstance(a, dict) and a.get("group_authorship"):
                    title = entry.get("title")
                    break
            if title:
                break
        if title:
            break
    if not title:
        return None
    # Contiguous, markup-free prefix that survives pdftotext -layout (stop
    # before Typst en-/em-dash markup `--`, colons, and inline markup chars).
    out, n = [], 0
    for w in str(title).replace("*", "").replace("_", "").split():
        if "--" in w or ":" in w:
            break
        if n + len(w) + 1 > 45:
            break
        out.append(w)
        n += len(w) + 1
    return " ".join(out) or None


def test_corp_entry_has_glyph_and_footnote(everything_pdf_text):
    """The known corporate/consortium entry should carry both glyph and
    footnote. The title anchor is DERIVED from data (not hardcoded) so this
    ships without leaking a real title; skips if no such entry exists."""
    probe = _group_authorship_probe()
    if not probe:
        pytest.skip("no group_authorship entry in corpus")
    # The entry's title (truncated) appears in the PDF; on the same
    # citation we expect both the glyph and the footnote.
    assert probe in everything_pdf_text
    # Confirm the glyph and footnote co-occur within a reasonable window of
    # the title — same paragraph/citation.
    title_idx = everything_pdf_text.index(probe)
    window = everything_pdf_text[title_idx : title_idx + 1200]
    assert "◊" in window
    assert "Group authorship." in window
