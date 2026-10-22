"""
# read_pdf.py - read the words on every PDF page

Opens a PDF and returns, for each page, every word with its position on the page (a box: left, top, right,
bottom), its font size and whether it is bold. Everything else in finx is built from this list of words.

Three readers give the same kind of result; choose one with `backend=`:
- `pymupdf` (default): fastest. AGPL licence - a closed-source product needs a commercial licence.
- `pypdfium2`: permissive licence, about twice as slow.
- `pdfplumber`: permissive licence, much slower.
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


# Reader 1 (default), PyMuPDF: words come with boxes; size and bold come from the text run each word sits in.
def _pymupdf(path: str, pages: list[int] | None) -> list[PageData]:
    import pymupdf

    out = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("the PDF is password-protected; remove the password and try again")
        for n in pages or range(1, len(doc) + 1):
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
    return out


# Reader 2, pdfplumber: pure Python, slow but permissive licence.
def _pdfplumber(path: str, pages: list[int] | None) -> list[PageData]:
    import pdfplumber

    out = []
    with pdfplumber.open(path) as pdf:
        for n in pages or range(1, len(pdf.pages) + 1):
            pg = pdf.pages[n - 1]
            words = [Word(w["x0"], w["top"], w["x1"], w["bottom"], clean(w["text"]), w["size"], "bold" in w["fontname"].lower())
                     for w in pg.extract_words(extra_attrs=["size", "fontname"], x_tolerance=1.5, y_tolerance=2)]
            out.append(PageData(n, float(pg.width), float(pg.height), [w for w in words if w.text]))
            pg.flush_cache()  # otherwise pdfplumber keeps every page in memory
    return out


# Reader 3, pypdfium2: reads single characters and joins them into words where PDFium marks a gap.
def _pypdfium2(path: str, pages: list[int] | None) -> list[PageData]:
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    out = []
    pdf = pdfium.PdfDocument(path)
    try:
        for n in pages or range(1, len(pdf) + 1):
            page = pdf[n - 1]
            w, h = page.get_size()
            tp = page.get_textpage()
            left, bottom, right, top = (ctypes.c_double() for _ in range(4))
            words: list[Word] = []
            cur = None  # word being built: [x0, y0, x1, y1, text, size_sum, bold_count, char_count]

            def flush():
                nonlocal cur
                if cur and clean(cur[4]):
                    words.append(Word(cur[0], cur[1], cur[2], cur[3], clean(cur[4]), cur[5] / cur[7], cur[6] * 2 >= cur[7]))
                cur = None

            for i in range(tp.count_chars()):
                code = raw.FPDFText_GetUnicode(tp, i)
                ch = chr(code) if code else " "
                if code == 0xFFFE:
                    continue  # hyphen at a line break: the word continues on the next line
                if raw.FPDFText_IsGenerated(tp, i) or ch.isspace() or code == 0xFFFD:
                    flush()  # PDFium inserts a generated space wherever the layout shows a word gap
                    continue
                raw.FPDFText_GetCharBox(tp, i, left, right, bottom, top)
                x0, x1, y0, y1 = left.value, right.value, h - top.value, h - bottom.value
                size, bold = raw.FPDFText_GetFontSize(tp, i), raw.FPDFText_GetFontWeight(tp, i) >= 600
                if cur is not None and abs(y1 - cur[3]) > 0.6 * (cur[3] - cur[1]):
                    flush()  # the character is on another line
                if cur is None:
                    cur = [x0, y0, x1, y1, ch, size, int(bold), 1]
                else:
                    cur[:4] = min(cur[0], x0), min(cur[1], y0), max(cur[2], x1), max(cur[3], y1)
                    cur[4] += ch
                    cur[5] += size
                    cur[6] += int(bold)
                    cur[7] += 1
            flush()
            out.append(PageData(n, float(w), float(h), words))
            tp.close()
            page.close()
    finally:
        pdf.close()
    return out


BACKENDS = {"pymupdf": _pymupdf, "pdfplumber": _pdfplumber, "pypdfium2": _pypdfium2}


# Read the pages of a PDF (all pages, or only the listed page numbers) with the chosen reader.
def load_pages(path: str, backend: str = "pymupdf", pages: list[int] | None = None) -> list[PageData]:
    return BACKENDS[backend](path, pages)
