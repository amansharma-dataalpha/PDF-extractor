"""
# read.py - read the words on every PDF page

Opens a PDF and returns, for each page, every word with its position on the page (a box: left, top, right,
bottom), its font size and whether it is bold. Everything else in the pipeline is built from this list of words.

The reader is PyMuPDF (fast; AGPL licence - a closed-source product needs a commercial licence). For pages without
text, `ink` measures how much of the page is covered by pictures (a scan) and how many drawn shapes it holds (text
printed as outlines, as "Microsoft Print to PDF" does), so the pipeline can send such pages to OCR.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

SPACES = re.compile(r"\s+")  # any run of spaces, including non-breaking ones
SOFT_MINUS = re.compile(r"\u00ad(?=[\d$(.])|(?<=\d)\u00ad$|^\u00ad+$")  # a soft hyphen standing in for a minus sign


# One word: its box on the page, its text, font size and boldness.
@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 0.0
    bold: bool = False

    @property
    def yc(self) -> float:  # vertical centre of the word
        return (self.y0 + self.y1) / 2


# One page: its number (1 = first page), its size, and all its words.
@dataclass
class PageData:
    no: int
    width: float
    height: float
    words: list[Word] = field(default_factory=list)


# Tidy a word: remove broken-glyph marks, restore line-break hyphens, collapse odd spaces.
# Some PDFs print the minus sign as a soft hyphen: next to a number (or alone, as a dash) it is kept as "-".
def clean(text: str) -> str:
    text = SOFT_MINUS.sub("-", text.replace("�", "").replace("￾", "-"))
    return SPACES.sub(" ", text.replace("­", "")).strip()


# Read every page of a PDF. PyMuPDF gives words with boxes; size and bold come from the text run each word sits in.
# Also returns True when the file is damaged (PyMuPDF had to repair it to open it - a cut-off download, for example),
# so the caller can say that pages may be missing.
def load_pages(path: str) -> tuple[list[PageData], bool]:
    import pymupdf

    out = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("the PDF is password-protected; remove the password and try again")
        for n in range(1, len(doc) + 1):
            pg = doc[n - 1]
            spans = sorted((sp["bbox"][1], sp["bbox"][3], sp["bbox"][0], sp["bbox"][2], sp["size"],
                            bool(sp["flags"] & 16) or "bold" in sp["font"].lower())
                           for blk in pg.get_text("dict", flags=0)["blocks"] for ln in blk.get("lines", ())
                           for sp in ln["spans"] if sp["text"].strip())
            tops = [s[0] for s in spans]
            words = []
            for x0, y0, x1, y1, t, *_ in pg.get_text("words"):
                t = clean(t)
                if not t:
                    continue
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                i = bisect.bisect_right(tops, cy + 1)  # only text runs starting above the word's centre can contain it
                size, bold = next(((s[4], s[5]) for s in reversed(spans[max(0, i - 60):i])
                                   if s[0] - 1 <= cy <= s[1] + 1 and s[2] - 1 <= cx <= s[3] + 1), (y1 - y0, False))
                words.append(Word(x0, y0, x1, y1, t, size, bold))
            out.append(PageData(n, pg.rect.width, pg.rect.height, words))
        return out, doc.is_repaired


# For the given pages: (share of the page covered by pictures, number of drawn path pieces - lines and curves).
def ink(path: str, pages: list[int]) -> dict[int, tuple[float, int]]:
    import pymupdf

    with pymupdf.open(path) as doc:
        return {n: (sum(abs(pymupdf.Rect(im["bbox"]) & doc[n - 1].rect) for im in doc[n - 1].get_image_info()) / max(abs(doc[n - 1].rect), 1.0),
                    sum(len(d["items"]) for d in doc[n - 1].get_drawings())) for n in pages}

