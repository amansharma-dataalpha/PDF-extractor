"""
# pipeline.py - PDF in, one JSON file out

    python -m finx.pipeline <pdf or folder> [-o out_dir] [--fast] [--backend pymupdf|pypdfium2|pdfplumber]
                            [--docling-mode fast|accurate] [--no-ocr] [-j N]      (run it from the backend folder)

Steps (no LLM anywhere):
1. **extract**: read words -> rows and tables -> page index -> find the statements -> check the numbers (seconds).
2. **scanned pages**: a page with almost no text, no table and pictures covering at least 10% of it is read with
   Docling OCR (only those pages, at most 40 per document; needs Docling installed; `--no-ocr` skips it).
3. **second opinion** (default; `--fast` skips it): Docling re-reads the statement pages; matching values become
   "confirmed", differing ones "conflict". Nothing is overwritten.

Every statement row ends with a status: verified | confirmed | unverified | conflict (or empty).
Note: called from Python, `run()` does NOT do the second opinion unless second_opinion=True (the command line does).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .check_numbers import verify
from .page_index import build_index
from .read_pdf import load_pages
from .statements import find_statements, merge
from .tables import group_lines, page_tables, split_row, strip_furniture

SCHEMA_VERSION = "0.1"
MAX_OCR_PAGES = 40  # OCR takes about 17 s per page, so at most this many pages per document
NO_TEXT_CHARS = 25  # a page with less text than this (and no table) may be a scan


# True when the text looks like nonsense (broken font encoding): mostly one-letter words or words without vowels.
def _looks_garbled(pages: list[dict]) -> bool:
    words = re.findall(r"[A-Za-z]+", " ".join(p["text"] for p in pages))
    if sum(map(len, words)) < 200:
        return False
    longer = [w for w in words if len(w) >= 3]
    no_vowel = sum(1 for w in longer if not re.search(r"[aeiouyAEIOUY]", w)) / max(len(longer), 1)
    return sum(1 for w in words if len(w) == 1) / len(words) > 0.5 or no_vowel > 0.35


# True when the PDF file was damaged and had to be repaired while opening (its text may be wrong).
def _was_repaired(path: str) -> bool:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return bool(doc.is_repaired)
    except Exception:
        return False


# The PDF's bookmarks (table of contents). Returns (bookmarks, error message or None).
def _outline(path: str) -> tuple[list[dict], str | None]:
    errors = []
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(path)
        try:
            return [{"title": str(b.title), "level": b.level + 1, "page": (b.page_index + 1) if b.page_index is not None else None}
                    for b in pdf.get_toc()], None
        finally:
            pdf.close()
    except Exception as e:  # reported in the JSON warnings, never hidden
        errors.append(f"pypdfium2: {type(e).__name__}: {e}")
    try:
        import pypdf

        reader = pypdf.PdfReader(path)
        out = []

        def walk(items, level):
            for it in items:
                if isinstance(it, list):
                    walk(it, level + 1)
                else:
                    out.append({"title": str(it.title), "level": level, "page": reader.get_destination_page_number(it) + 1})

        walk(reader.outline, 1)
        return out, None
    except Exception as e:
        errors.append(f"pypdf: {type(e).__name__}: {e}")
    return [], "; ".join(errors)


# Step 1: the fast, deterministic extraction of one PDF into a dict (page index, tables, statements, checks).
def extract(path: str, backend: str = "pymupdf") -> dict:
    t0 = time.perf_counter()
    pages = load_pages(path, backend)
    t_read = time.perf_counter()
    pages_lines, pages_kinds, all_tables = [], [], []
    for pg in pages:
        lines = group_lines(pg)
        rows = [split_row(l) for l in lines]
        pages_lines.append(lines)
        pages_kinds.append([r.kind for r in rows])
        all_tables.extend(page_tables(strip_furniture(rows, pg.width, pg.height)))
    t_layout = time.perf_counter()
    index = build_index(pages_lines, pages_kinds, [p.width for p in pages], [p.height for p in pages])
    statements = {k: merge(g) for k, g in find_statements(all_tables, {p["page"]: p for p in index["pages"]}).items()}
    ver = verify(statements)
    outline, outline_err = _outline(path)
    t_end = time.perf_counter()

    tables_json = [{**t.to_dict(), "id": f"t{i + 1}"} for i, t in enumerate(all_tables)]
    for p in index["pages"]:
        p["tables"] = [f"t{i + 1}" for i, t in enumerate(all_tables) if t.page == p["page"]]
    stmts_json = {}
    for k, t in statements.items():
        d = t.to_dict()
        mask = ver["masks"][k]
        for r, m in zip(d["rows"], mask):
            r["verified"] = m
        d["coverage"] = {"values": sum(1 for r in d["rows"] for v in r["values"] if v is not None),
                         "verified": sum(1 for r, m in zip(d["rows"], mask) for v, ok in zip(r["values"], m) if v is not None and ok)}
        d["verifiable"] = k != "equity"
        same = next((j for j, u in statements.items() if u is t and j != k and j in stmts_json), None)
        if same:  # e.g. one "operations and comprehensive income" statement: rows shown twice, counted once
            d["verifiable"], d["same_table_as"] = False, same
        stmts_json[k] = d
    checks = [{k: v for k, v in c.items() if k not in ("refs", "rows")} for c in ver["checks"]]
    counted = [s for s in stmts_json.values() if s["verifiable"]]
    return {
        "schema_version": SCHEMA_VERSION, "source_file": Path(path).name, "page_count": len(pages), "doc_type": index["doc_type"],
        "engine": f"finx/{backend}",
        "timings_s": {"read": round(t_read - t0, 3), "layout_tables": round(t_layout - t_read, 3),
                      "index_statements_verify": round(t_end - t_layout, 3), "total": round(t_end - t0, 3)},
        "outline": outline, "warnings": [f"outline unavailable: {outline_err}"] if outline_err else [],
        "pages": index["pages"], "tables": tables_json, "statements": stmts_json, "checks": checks,
        "summary": {"tables": len(all_tables), "statements_found": sorted(stmts_json), "checks_passed": sum(c["passed"] for c in checks),
                    "checks_total": len(checks), "statement_values": sum(s["coverage"]["values"] for s in counted),
                    "statement_values_verified": sum(s["coverage"]["verified"] for s in counted)},
    }


# Of the pages without text, those carrying a large image (a scan). A page with neither text nor image is blank.
def _image_pages(path: str, pages: list[int], min_area: float = 0.10) -> list[int]:
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        pdf = pdfium.PdfDocument(path)
        keep = []
        try:
            for n in pages:
                page = pdf[n - 1]
                w, h = page.get_size()
                area = 0.0
                for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
                    l, b, r, t = obj.get_pos()
                    area += max(r - l, 0) * max(t - b, 0)
                if area >= min_area * w * h:
                    keep.append(n)
                page.close()
        finally:
            pdf.close()
        return keep
    except Exception:
        return pages  # cannot tell: better to OCR a blank page than to skip a scan


# Count statement values by status (equity statements are not counted).
def _status_counts(statements: dict) -> dict:
    c = {"verified": 0, "confirmed": 0, "unverified": 0, "conflict": 0}
    for st in statements.values():
        if not st.get("verifiable", True):
            continue
        for r in st["rows"]:
            ver = r.get("verified") or [False] * len(r["values"])
            for i, v in enumerate(r["values"]):
                if v is not None:
                    c["verified" if ver[i] else r.get("status") if r.get("status") in ("confirmed", "conflict") else "unverified"] += 1
    return c


# The whole pipeline for one PDF: extract, OCR scanned pages, optional Docling second opinion, statuses and warnings.
def run(path: str, backend: str = "pymupdf", second_opinion: bool = False, docling_mode: str = "fast", ocr: bool = True) -> dict:
    t0 = time.perf_counter()
    res = extract(path, backend)
    res["pipeline"] = {"stages": ["finx"], "docling_mode": None, "ocr_pages": [], "second_opinion": False}
    warnings = res.setdefault("warnings", [])

    textless = [p["page"] for p in res["pages"] if len(p["text"].strip()) < NO_TEXT_CHARS and not p["tables"]]
    scans = _image_pages(path, textless) if textless else []
    res["quality"] = {"pages_without_text": textless, "scanned_pages": scans, "blank_pages": sorted(set(textless) - set(scans)),
                      "repaired_file": _was_repaired(path), "garbled_text": _looks_garbled(res["pages"])}
    if res["quality"]["garbled_text"]:
        warnings.append("the text looks garbled (broken font encoding): do not trust the extracted text or numbers")
    if not res["pages"]:
        warnings.append("the PDF has no pages")
    if scans and ocr:
        if len(scans) > MAX_OCR_PAGES:
            warnings.append(f"{len(scans)} pages have no text layer; OCR capped at {MAX_OCR_PAGES} pages (Docling is ~17 s/page)")
            scans = scans[:MAX_OCR_PAGES]
        try:
            from . import docling_check

            t = time.perf_counter()
            got = docling_check.ocr_pages(path, scans, docling_mode)
            for p in scans:
                page = res["pages"][p - 1]
                page["text"] = got[p]["text"]
                page["ocr"] = True
                for k, tb in enumerate(got[p]["tables"]):
                    tid = f"o{p}_{k + 1}"
                    res["tables"].append({"id": tid, "engine": "docling", "pages": [p], "kind": None, "title": "", "unit": None,
                                          "scale": None, "columns": [], "rows": [], "cells": tb["cells"], "flags": ["docling table (OCR page)"]})
                    page["tables"].append(tid)
            res["pipeline"]["stages"].append("docling_ocr")
            res["pipeline"]["ocr_pages"] = scans
            res["pipeline"]["ocr_seconds"] = round(time.perf_counter() - t, 1)
            res["pipeline"]["docling_mode"] = docling_mode
            empty = [p for p in scans if not got[p]["text"].strip()]
            if empty:
                warnings.append(f"OCR found no text on page(s) {empty}")
        except ImportError:
            warnings.append(f"{len(scans)} pages have no text layer but Docling is not installed: they were not read")
        except Exception as e:  # keep the finx result; report the failure
            warnings.append(f"OCR failed ({type(e).__name__}: {e}); {len(scans)} scanned pages were not read")
    elif scans:
        warnings.append(f"{len(scans)} pages have no text layer (OCR disabled)")

    if second_opinion and res["statements"]:
        try:
            from . import docling_check

            pages = sorted({p for s in res["statements"].values() for p in s["pages"]})
            t = time.perf_counter()
            stats = docling_check.confirm(res["statements"], docling_check.docling_tables(docling_check.convert(path, pages, docling_mode)))
            res["pipeline"].update(second_opinion=True, docling_mode=docling_mode, second_opinion_pages=pages,
                                   second_opinion_seconds=round(time.perf_counter() - t, 1), second_opinion_stats=stats)
            res["pipeline"]["stages"].append("docling_second_opinion")
        except ImportError:
            warnings.append("second opinion requested but Docling is not installed")
        except Exception as e:  # keep the finx result; values keep their arithmetic status
            warnings.append(f"second opinion failed ({type(e).__name__}: {e}); values keep their arithmetic status")
    for st in res["statements"].values():  # rows Docling did not look at: verified or unverified
        src = res["statements"].get(st.get("same_table_as") or "")
        for i, r in enumerate(st["rows"]):
            for key in ("confirmed", "status", "docling_values"):
                if src and key in src["rows"][i]:
                    r[key] = src["rows"][i][key]
            all_ok = r.get("verified") and all(r["verified"][i] for i, v in enumerate(r["values"]) if v is not None)
            r.setdefault("status", "verified" if all_ok and any(v is not None for v in r["values"]) else "unverified")

    c = _status_counts(res["statements"])
    total = sum(c.values())
    res["assurance"] = {**c, "total": total, "certified_pct": round(100 * (c["verified"] + c["confirmed"]) / total, 1) if total else None}
    if res["doc_type"] in ("10-K", "10-Q") and len(res["statements"]) < 3:
        warnings.append(f"{res['doc_type']} but only {len(res['statements'])} primary statements were found")
    failed = [x for x in res["checks"] if not x["passed"]]
    if failed:
        warnings.append(f"{len(failed)} arithmetic checks failed: " + "; ".join(f"{x['check']} (col {x['column']})" for x in failed[:5]))
    res["timings_s"]["pipeline_total"] = round(time.perf_counter() - t0, 3)
    return res


# Process one file for the command line; a bad PDF is reported, it never stops a folder run.
def _job(args: tuple) -> tuple[str, dict]:
    path, out_dir, kw = args
    try:
        res = run(path, **kw)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        out = Path(out_dir) / f"{Path(path).stem}.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return path, {"ok": True, "out": str(out), "pages": res["page_count"], "type": res["doc_type"], "seconds": res["timings_s"]["pipeline_total"],
                      "tables": res["summary"]["tables"], "statements": len(res["statements"]), "assurance": res["assurance"],
                      "warnings": res["warnings"], "ocr_pages": res["pipeline"]["ocr_pages"]}
    except Exception as e:
        return path, {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Command line: run every PDF given (files or folders), write one JSON each, print a one-line summary per file.
def main() -> int:
    ap = argparse.ArgumentParser(description="PDF -> JSON with deterministic extraction (no LLM)")
    ap.add_argument("inputs", nargs="+", help="PDF files or folders")
    ap.add_argument("-o", "--out-dir", default="output/finx")
    ap.add_argument("--backend", default="pymupdf", choices=["pymupdf", "pdfplumber", "pypdfium2"])
    ap.add_argument("--fast", action="store_true", help="skip the Docling second opinion (seconds per filing, but ~8%% of statement values stay unverified)")
    ap.add_argument("--docling-mode", default="fast", choices=["fast", "accurate"])
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=1)
    a = ap.parse_args()
    files: list[str] = []
    for i in a.inputs:
        p = Path(i)
        files += [str(x) for x in sorted(p.glob("*.pdf"))] if p.is_dir() else [str(p)]
    sys.stdout.reconfigure(errors="replace")  # file names in any language print without crashing
    if not files:
        print(f"no PDF files found in: {', '.join(a.inputs)}", file=sys.stderr)
        return 2
    kw = {"backend": a.backend, "second_opinion": not a.fast, "docling_mode": a.docling_mode, "ocr": not a.no_ocr}
    jobs = [(f, a.out_dir, kw) for f in files]
    if a.jobs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            results = list(ex.map(_job, jobs))
    else:
        results = [_job(j) for j in jobs]
    for path, r in results:
        name = Path(path).name
        if not r["ok"]:
            print(f"{name:34} FAILED  {r['error']}")
            continue
        asr = r["assurance"]
        print(f"{name:34} {r['type']:13} {r['pages']:>4}p {r['seconds']:>7.1f}s  tables {r['tables']:>3}  stmts {r['statements']}  "
              f"certified {asr['certified_pct']}%  (ver {asr['verified']} conf {asr['confirmed']} unver {asr['unverified']} conflict {asr['conflict']})"
              + (f"  OCR p{r['ocr_pages']}" if r["ocr_pages"] else "") + (f"  WARN {len(r['warnings'])}" if r["warnings"] else ""))
    return 1 if any(not r["ok"] for _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
