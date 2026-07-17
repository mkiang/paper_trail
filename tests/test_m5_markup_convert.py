"""M5 5b CP1: Typst-markup -> HTML / Markdown converter. Pure, no I/O."""

from __future__ import annotations

from cv_editor import markup_convert as mc

# ---------- bold / italic ----------


def test_bold_italic_html():
    assert mc.to_html("a *bold* and _italic_ b") == "a <strong>bold</strong> and <em>italic</em> b"


def test_bold_italic_markdown():
    assert mc.to_markdown("a *bold* and _italic_ b") == "a **bold** and _italic_ b"


def test_orphan_delimiter_left_literal():
    # odd `*` must NOT produce an unterminated <strong>
    assert mc.to_html("a *lonely star") == "a *lonely star"
    assert mc.to_markdown("a _lonely under") == "a _lonely under"


# ---------- dashes + escapes ----------


def test_dashes():
    assert mc.to_html("2020--2024 yes---no") == "2020&ndash;2024 yes&mdash;no"
    assert mc.to_markdown("2020--2024 yes---no") == "2020–2024 yes—no"


def test_typst_escapes_resolve_to_literals():
    assert mc.to_html(r"\$75,000 and \*star\* and \_under\_") == "$75,000 and *star* and _under_"
    assert mc.to_markdown(r"\$75,000") == "$75,000"


def test_escaped_delimiter_does_not_open_span():
    # `\*x\*` -> literal *x* , not <strong>
    assert mc.to_html(r"\*x\*") == "*x*"


# ---------- HTML escaping (published artifact) ----------


def test_html_escapes_raw_text_first():
    assert mc.to_html("Smith & Jones < Co > end") == "Smith &amp; Jones &lt; Co &gt; end"
    # escaping happens before markup so a `<` in a bold span is still escaped
    assert mc.to_html("*a < b*") == "<strong>a &lt; b</strong>"


def test_markdown_does_not_html_escape():
    assert mc.to_markdown("Smith & Jones < Co") == "Smith & Jones < Co"


# ---------- links + scheme safety ----------


def test_link_html_safe():
    assert (
        mc.to_html('see #link("https://x.org/a")[here]') == 'see <a href="https://x.org/a">here</a>'
    )


def test_link_markdown_safe():
    assert mc.to_markdown('see #link("https://x.org/a")[here]') == "see [here](https://x.org/a)"


def test_link_unsafe_scheme_renders_label_only():
    # javascript:/data: must NOT become a live link in a published file
    assert mc.to_html('#link("javascript:alert(1)")[click]') == "click"
    assert mc.to_markdown('#link("data:text/html,x")[click]') == "click"


def test_link_mailto_allowed():
    assert mc.to_html('#link("mailto:a@b.org")[mail]') == '<a href="mailto:a@b.org">mail</a>'


def test_link_url_attribute_escaped():
    # an ampersand in the query string is attribute-escaped in HREF
    out = mc.to_html('#link("https://x.org/a?b=1&c=2")[q]')
    assert 'href="https://x.org/a?b=1&amp;c=2"' in out


# ---------- plain passthrough ----------


def test_plain_text_unchanged():
    assert mc.to_html("Just plain text.") == "Just plain text."
    assert mc.to_markdown("Just plain text.") == "Just plain text."
