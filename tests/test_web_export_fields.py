"""Pin the website-export schema fields (v1.1.0).

`web` / `slides` / `paper_pdf` are read only by an external site exporter and are
inert in the Typst renderer, so no drift-guard would otherwise catch their
removal. This test pins their presence + shape so a future schema refactor can't
silently drop them.
"""

from cv_editor import schemas
from cv_editor.field_handlers import FIELD_HANDLERS


def _fields(key):
    return {f["name"]: f for f in schemas.SCHEMAS[key]["fields"]}


def test_publications_has_web_and_hosted_pdf():
    f = _fields("publications")
    assert f["web"]["type"] == "select"
    assert f["web"]["choices"] == ["", "show", "hide"]
    assert f["paper_pdf"]["type"] == "text"


def test_presentations_has_web_and_slides():
    f = _fields("presentations")
    assert f["web"]["type"] == "select"
    assert f["web"]["choices"] == ["", "show", "hide"]
    assert f["slides"]["type"] == "text"


def test_teaching_has_web_only():
    f = _fields("teaching")
    assert f["web"]["type"] == "select"
    assert "slides" not in f and "paper_pdf" not in f


def test_web_is_select_not_bool():
    """A bool field deletes the key when unchecked, so it can't express
    'force hide'. `web` must stay a select."""
    for key in ("publications", "presentations", "teaching"):
        assert _fields(key)["web"]["type"] == "select"


def test_web_field_types_are_registered_and_blank_validates():
    assert "select" in FIELD_HANDLERS and "text" in FIELD_HANDLERS
    web = _fields("publications")["web"]
    # "" is a member of choices, so a blank/absent web passes validation.
    assert not FIELD_HANDLERS["select"].validate("", web)
    assert not FIELD_HANDLERS["select"].validate("show", web)
    assert FIELD_HANDLERS["select"].validate("bogus", web)  # not in choices -> error
