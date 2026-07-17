"""Typst-markup -> HTML / Markdown converter (M5 5b exports).

Converts the small Typst markup vocabulary used in CV data to HTML or Markdown.
NEVER evals (unlike `templates/bespoke/render.typ` `mk()`, which calls `eval(s, mode:"markup")`).

Vocabulary:
    *bold*              -> <strong>..</strong>   /  **..**
    _italic_            -> <em>..</em>            /  _.._
    ---                 -> em-dash (—)
    --                  -> en-dash (–)
    \\$ \\* \\_          -> literal $ * _ (the Typst escapes)
    #link("url")[label] -> <a href="safe(url)">label</a>  /  [label](url)

HTML target ESCAPES `& < >` in raw text BEFORE applying markup (this is a
PUBLISHED artifact — a title like `A < B` or `Smith & Jones` must not corrupt the
page) and routes link targets through a scheme allow-list (http/https/mailto) — an
unsafe target (e.g. `javascript:`) renders as plain label text, never a live link.

ORPHAN delimiters degrade to literal: an odd `*` stays a literal `*`, never an
unterminated `<strong>`. (data_check ERRORs on a bare `$`, but unbalanced `*`/`_`
can still sit in the data, so the converter must be robust.)

Self-bold (wrapping `meta.self_bold` occurrences) is NOT done here — it is
name-aware and handled by the export author formatter; this module is pure markup.
"""

from __future__ import annotations

import html
import re

from cv_editor import url_helpers

HTML = "html"
MD = "md"

_LINK_RE = re.compile(r'#link\("([^"]*)"\)\[([^\]]*)\]')
# Paired, non-greedy; a leading `\` (escaped delimiter) does not open a span.
_BOLD_RE = re.compile(r"(?<!\\)\*([^*]+?)\*")
_ITALIC_RE = re.compile(r"(?<!\\)_([^_]+?)_")
_PLACEHOLDER_RE = re.compile("\x00LINK(\\d+)\x00")


def _link_target(url: str) -> str | None:
    """Return the URL if it is a safe display scheme (http/https/mailto), else None."""
    u = (url or "").strip()
    if url_helpers.is_safe_fetch_url(u) or u.lower().startswith("mailto:"):
        return u
    return None


def _convert(s: str, target: str) -> str:
    if not isinstance(s, str):
        s = str(s)

    # 1. Stash #link(...)[...] BEFORE escaping so url + label are captured raw.
    links: list[tuple[str, str]] = []

    def _stash(m: re.Match) -> str:
        links.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    s = _LINK_RE.sub(_stash, s)

    # 2. Escape literal text for HTML (entities for & < >; keep * _ - $).
    if target == HTML:
        s = html.escape(s, quote=False)

    # 3. Dashes (--- before --) and the literal escapes.
    em, en = ("&mdash;", "&ndash;") if target == HTML else ("—", "–")
    s = s.replace("---", em).replace("--", en)

    # 4. Paired bold / italic; orphan delimiters left literal.
    if target == HTML:
        s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
        s = _ITALIC_RE.sub(r"<em>\1</em>", s)
    else:
        s = _BOLD_RE.sub(r"**\1**", s)
        s = _ITALIC_RE.sub(r"_\1_", s)

    # 4b. Resolve the Typst escapes to their literal characters.
    s = s.replace(r"\$", "$").replace(r"\*", "*").replace(r"\_", "_")

    # 5. Restore links (label NOT re-marked-up — middle-fidelity scope).
    def _restore(m: re.Match) -> str:
        url, label = links[int(m.group(1))]
        safe = _link_target(url)
        if target == HTML:
            label_e = html.escape(label, quote=False)
            if safe is None:
                return label_e
            return f'<a href="{html.escape(safe, quote=True)}">{label_e}</a>'
        if safe is None:
            return label
        return f"[{label}]({safe})"

    return _PLACEHOLDER_RE.sub(_restore, s)


def to_html(s: str) -> str:
    """Typst markup -> safe HTML inline string (escaped, scheme-guarded links)."""
    return _convert(s, HTML)


def to_markdown(s: str) -> str:
    """Typst markup -> Markdown inline string. (Literal `*`/`_` in source text are
    NOT MD-escaped in v1 — rare in CV prose; documented limitation.)"""
    return _convert(s, MD)
