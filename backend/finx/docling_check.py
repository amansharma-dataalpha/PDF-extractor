"""
# docling_check.py - Docling as a second reader (optional)

Docling is an AI document reader: accurate but slow (about 15-20 s per page in fast mode, 30-60 s in accurate mode,
on a laptop CPU), so it only reads the few pages that need it:
- **confirm**: re-reads the statement pages. A value Docling reads identically becomes "confirmed"; a similar row
  with different numbers (at least 60% of the label's words shared) becomes "conflict" (a person should look).
  Rows already verified by arithmetic, and equity statements, are skipped. finx's numbers are never overwritten.
- **ocr_pages**: reads scanned pages (pages that are only a picture) with OCR - reading text from a picture of a page.

finx works without Docling installed; these functions are only called when needed.
"""
from __future__ import annotations

import re

from .tables import parse_number

_converters: dict = {}  # one Docling converter per (mode, ocr) - loading the models is slow, so keep them


# Docling set up once per mode (fast/accurate, with or without OCR) and then reused.
def _converter(mode: str, ocr: bool):
    if (mode, ocr) not in _converters:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = ocr
        opts.do_table_structure = True
        opts.table_structure_options.mode = TableFormerMode.FAST if mode == "fast" else TableFormerMode.ACCURATE
        opts.generate_page_images = False
        _converters[(mode, ocr)] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _converters[(mode, ocr)]


# Run Docling on the smallest block of consecutive pages that covers the given pages.
def convert(path: str, pages: list[int], mode: str = "fast", ocr: bool = False):
    return _converter(mode, ocr).convert(path, **({"page_range": (min(pages), max(pages))} if pages else {})).document


# Docling's tables as plain lists of cell texts, with their page numbers.
def docling_tables(doc) -> list[dict]:
    out = []
    for t in doc.tables:
        try:
            df = t.export_to_dataframe(doc)
            cells = [list(map(str, df.columns))] + df.astype(str).values.tolist()
        except Exception:
            cells = []  # a table Docling cannot export confirms nothing (its values simply stay unconfirmed)
        out.append({"page": t.prov[0].page_no if t.prov else None, "cells": cells})
    return out


# All numbers in a Docling table row, in order ("(56)" and "56)" -> -56, a dash -> 0).
def row_numbers(cells: list[str]) -> list[float]:
    nums: list[float] = []
    for cell in cells:
        for tok in str(cell).replace("|", " ").split():
            tok = tok.strip().replace("$", "").replace("−", "-")
            if tok in ("—", "–", "-"):
                nums.append(0.0)
                continue
            if tok.endswith(")") and "(" not in tok and tok[:-1].replace(",", "").replace(".", "").isdigit():
                tok = "(" + tok
            v = parse_number(tok)
            if v is not None:
                nums.append(v)
    return nums


def _words(label: str) -> set[str]:  # words of 3+ letters, for comparing labels
    return set(re.findall(r"[a-z]{3,}", label.lower()))


def _contains(seq: list[float], sub: list[float]) -> bool:  # sub appears in seq as an unbroken run
    return len(sub) > 0 and any(seq[i:i + len(sub)] == sub for i in range(len(seq) - len(sub) + 1))


# Give every statement row a status: verified (arithmetic), confirmed (Docling read the same numbers, in order, on the
# same page), conflict (Docling read a similar row with different numbers) or unverified.
def confirm(statements: dict, tables: list[dict]) -> dict:
    by_page: dict = {}
    for t in tables:
        for row in t["cells"]:
            by_page.setdefault(t["page"], []).append((row_numbers(row), " ".join(str(c) for c in row if not row_numbers([c]))))
    stats = {"confirmed": 0, "conflict": 0}
    for st in statements.values():
        if not st.get("verifiable", True):
            continue
        for r in st["rows"]:
            vals = [v for v in r["values"] if v is not None]
            ver = r.get("verified") or [False] * len(r["values"])
            if not vals or all(ver[i] for i, v in enumerate(r["values"]) if v is not None):
                r["status"] = "verified" if vals else "empty"
                r["confirmed"] = [False] * len(r["values"])
                continue
            cands = by_page.get(r["page"], [])
            if any(_contains(nums, vals) for nums, _ in cands):
                r["confirmed"] = [v is not None for v in r["values"]]
                r["status"] = "confirmed"
                stats["confirmed"] += 1
                continue
            mine = _words(r["label"])
            conflict = [nums for nums, lab in cands if mine and len(mine & _words(lab)) / max(len(mine), 1) >= 0.6 and nums and nums != vals]
            r["confirmed"] = [False] * len(r["values"])
            if conflict:
                r["status"], r["docling_values"] = "conflict", conflict[0]
                stats["conflict"] += 1
            else:
                r["status"] = "unverified"
    return stats


# OCR the given scanned pages; returns {page: {"text": ..., "tables": [...]}}.
def ocr_pages(path: str, pages: list[int], mode: str = "fast") -> dict[int, dict]:
    out: dict[int, dict] = {}
    runs: list[list[int]] = []  # consecutive pages are read together
    for p in sorted(pages):
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    for run in runs:
        doc = convert(path, run, mode, ocr=True)
        tables = docling_tables(doc)
        for p in run:
            try:
                text = doc.export_to_markdown(page_no=p)
            except Exception:
                text = ""
            out[p] = {"text": text, "tables": [t for t in tables if t["page"] == p]}
    return out
