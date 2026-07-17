"""Shared per-word PDF text extractor for the visual-diff harness.

Wraps `pdftotext -bbox` (poppler) and parses its XHTML into structured pages.
Coordinates are in PDF points, top-origin (yMin = top edge), identical schema
regardless of which renderer produced the PDF. No font/size/colour here -- that
needs PyMuPDF (see visual_diff.FontIntrospector). This layer is dependency-light
(lxml only) so page alignment and glyph pairing share one extraction path.
"""

from __future__ import annotations

import html
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Page:
    index: int  # 0-based
    width: float
    height: float
    words: list[Word] = field(default_factory=list)


# Some serif fonts render numbers as old-style figures (OSF glyphs). Certain PDF
# exporters encode those at Adobe's private-use block for old-style digits
# (U+F730..F739 == 0..9) with no ToUnicode mapping back to ASCII, so neither
# pdftotext nor fitz recovers them as digits. Map them back so such numerals pair
# with plain-digit output -- this is purely an encoding artifact of old-style
# figures, not a different typeface.
_PUA_DIGITS = {0xF730 + i: str(i) for i in range(10)}


def deglyph_digits(s: str) -> str:
    return s.translate(_PUA_DIGITS)


def normalize_text(s: str, *, casefold: bool = False) -> str:
    """NFC-normalise, unescape XML entities, de-glyph Word PUA digits, trim."""
    s = html.unescape(s)
    s = deglyph_digits(s)
    s = unicodedata.normalize("NFC", s)
    s = s.strip()
    if casefold:
        s = s.casefold()
    return s


def _run_pdftotext_bbox(pdf_path: Path, first: int | None, last: int | None) -> bytes:
    cmd = ["pdftotext", "-bbox"]
    if first is not None:
        cmd += ["-f", str(first)]
    if last is not None:
        cmd += ["-l", str(last)]
    cmd += [str(pdf_path), "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return proc.stdout


def extract_words(pdf_path: Path, pages: range | None = None) -> list[Page]:
    """Return a list of Page objects with per-word boxes (PDF points, top-origin).

    `pages` is a 0-based range; None means all pages. pdftotext -f/-l are 1-based.
    """
    pdf_path = Path(pdf_path)
    first = last = None
    if pages is not None:
        if len(pages) == 0:
            return []
        first = pages.start + 1
        last = pages.stop  # range is exclusive; stop == last 1-based page
    raw = _run_pdftotext_bbox(pdf_path, first, last)

    # Parser is namespace-aware; use {*} wildcard so we don't hardcode xhtml ns.
    parser = etree.XMLParser(recover=True, resolve_entities=False)
    root = etree.fromstring(raw, parser=parser)

    out: list[Page] = []
    page_offset = pages.start if pages is not None else 0
    for p_i, page_el in enumerate(root.iter("{*}page")):
        pw = float(page_el.get("width"))
        ph = float(page_el.get("height"))
        page = Page(index=page_offset + p_i, width=pw, height=ph)
        for w_el in page_el.iter("{*}word"):
            text = normalize_text(w_el.text or "")
            if not text:
                continue
            page.words.append(
                Word(
                    text=text,
                    x0=float(w_el.get("xMin")),
                    y0=float(w_el.get("yMin")),
                    x1=float(w_el.get("xMax")),
                    y1=float(w_el.get("yMax")),
                )
            )
        out.append(page)
    return out


if __name__ == "__main__":
    import sys

    pages = extract_words(Path(sys.argv[1]))
    print(f"{len(pages)} pages")
    for pg in pages[:1]:
        print(f"  page {pg.index}: {pg.width}x{pg.height}, {len(pg.words)} words")
        for w in pg.words[:5]:
            print(f"    {w.text!r} @ ({w.x0:.1f},{w.y0:.1f})-({w.x1:.1f},{w.y1:.1f})")
