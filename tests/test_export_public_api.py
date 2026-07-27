"""Pin the public export-API surface (v1.1.0, A3).

An out-of-package exporter (a website JSON builder) reuses this engine's markup
+ leak-guard visibility helpers. Before v1.1.0 it had to import the private
`_mk` / `_visible` / `_entry_visible` (+ inline `_plain`/`_em`/... ) symbols
with a fragile import-guard. A3 promotes stable public aliases; this test pins
their presence + that they alias the internal implementation, so a refactor
of the private internals can't silently break a downstream exporter.
"""

from cv_editor import export_core

# (public name, private implementation) pairs the exporter depends on.
_PUBLIC_API = [
    ("mk", "_mk"),
    ("plain", "_plain"),
    ("emphasis", "_em"),
    ("strong", "_strong"),
    ("sup", "_sup"),
    ("link", "_link"),
    ("self_bold_terms", "_self_bold_terms"),
    ("visible", "_visible"),
    ("entry_visible", "_entry_visible"),
]


def test_public_names_exist_and_are_callable():
    for public, _ in _PUBLIC_API:
        fn = getattr(export_core, public, None)
        assert callable(fn), f"export_core.{public} missing or not callable"


def test_public_names_alias_the_internal_impl():
    for public, private in _PUBLIC_API:
        assert getattr(export_core, public) is getattr(export_core, private), (
            f"export_core.{public} must alias export_core.{private}"
        )


def test_public_helpers_behave():
    HTML = export_core.HTML
    assert export_core.mk("*bold* _it_", HTML, []) == "<strong>bold</strong> <em>it</em>"
    assert export_core.plain("a & b", HTML) == "a &amp; b"
    assert export_core.self_bold_terms("Public JQ") == ["Public JQ"]
    # visibility: full sees everything; hide-from wins.
    assert export_core.visible([], [], "full") is True
    assert export_core.visible(["academic"], [], "public-health") is False
    assert export_core.entry_visible({"highlighted": True}, "full", False) is False
