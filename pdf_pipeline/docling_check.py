"""
# docling_check.py - Docling as a second reader, only for the pages that need it

Docling is an AI document reader (layout model + TableFormer table model, run locally): accurate but slow (about 5 s
per page in fast mode on a laptop CPU, plus about 20 s to load its models once per process), so it only reads:
- **recheck pages**: statement pages where our own reading failed - a row of numbers without a label, or a field row
  whose numbers do not add up. On those pages (see `confirm`):
  - a value Docling reads identically becomes "confirmed"; a similar row with different numbers (at least 60% of the
    label's words shared) becomes "conflict" (a person should look). Our numbers are never overwritten;
  - a row of numbers we read without a label gets Docling's label when exactly one Docling row has those numbers
    (`label_source: docling`) - the only thing Docling fills in.
- **ocr_pages**: pages with no text layer (a scan, or text drawn as shapes), read with OCR.

The pipeline works without Docling installed; these functions are only called when needed.
"""
from __future__ import annotations

import re

from .layout import parse_number

_converters: dict = {}  # one Docling converter with and one without OCR - loading the models is slow, so keep them


# Docling set up once (with or without OCR) and then reused; its table model runs in fast mode.
def _converter(ocr: bool):
    if ocr not in _converters:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = ocr
        opts.do_table_structure = True
        opts.table_structure_options.mode = TableFormerMode.FAST
        opts.generate_page_images = False
        _converters[ocr] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _converters[ocr]


def _runs(pages: list[int]) -> list[list[int]]:  # [3, 4, 9] -> [[3, 4], [9]]: Docling reads one block of pages at a time
    runs: list[list[int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    return runs


# Run Docling on one block of consecutive pages.
def convert(path: str, run: list[int], ocr: bool = False):
    return _converter(ocr).convert(path, page_range=(run[0], run[-1])).document


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


# Docling's tables on the given pages only.
def page_tables(path: str, pages: list[int]) -> list[dict]:
    return [t for run in _runs(pages) for t in docling_tables(convert(path, run))]


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


# Give every statement row Docling looked at a status: verified (arithmetic), confirmed (Docling read the same numbers,
# in order, on the same page), conflict (Docling read a similar row with different numbers) or unverified. A row of
# numbers without a label takes the label of the one Docling row with the same numbers.
def confirm(statements: dict, tables: list[dict]) -> dict:
    by_page: dict = {}
    for t in tables:
        for row in t["cells"]:
            by_page.setdefault(t["page"], []).append((row_numbers(row), " ".join(str(c) for c in row if not row_numbers([c])).strip()))
    stats = {"confirmed": 0, "conflict": 0, "labels_filled": 0}
    for st in statements.values():
        if not st.get("verifiable", True):
            continue
        for r in st["rows"]:
            vals = [v for v in r["values"] if v is not None]
            cands = by_page.get(r["page"], [])
            if not r["label"].strip() and any(vals):
                labels = {lab for nums, lab in cands if lab and _contains(nums, vals)}
                if len(labels) == 1:
                    r["label"], r["label_source"] = labels.pop(), "docling"
                    stats["labels_filled"] += 1
            ver = r.get("verified") or [False] * len(r["values"])
            if not vals or all(ver[i] for i, v in enumerate(r["values"]) if v is not None):
                r["status"] = "verified" if vals else "empty"
                continue
            if any(_contains(nums, vals) for nums, _ in cands):
                r["status"] = "confirmed"
                stats["confirmed"] += 1
                continue
            mine = _words(r["label"])
            conflict = [nums for nums, lab in cands if mine and len(mine & _words(lab)) / max(len(mine), 1) >= 0.6 and nums and nums != vals]
            if conflict:
                r["status"], r["docling_values"] = "conflict", conflict[0]
                stats["conflict"] += 1
            else:
                r["status"] = "unverified"
    return stats


# OCR the given pages; returns {page: {"text": ..., "tables": [...]}}.
def ocr_pages(path: str, pages: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for run in _runs(pages):
        doc = convert(path, run, ocr=True)
        tables = docling_tables(doc)
        for p in run:
            try:
                text = doc.export_to_markdown(page_no=p).replace("<!-- image -->", "")  # Docling's picture marker
            except Exception:
                text = ""
            out[p] = {"text": text, "tables": [t for t in tables if t["page"] == p]}
    return out
