"""
# pipeline.py - one PDF in, its required fields and metadata out

`process(path)` is the one function the rest of the world calls (`extract.py`, or the agent directly): it runs the
steps below, then `fields.py` keeps only the required fields, and it returns `(fields JSON or None, metadata)`. It never
raises for a bad PDF: the metadata then says `"status": "failed: ..."`.

Steps (no LLM anywhere):
1. **extract**: read words -> rows and tables -> page index -> find the statements -> check that our reading adds up
   (a filing always adds up, so a failed check means we misread a row). Seconds per filing.
2. **OCR**: a page with almost no text and no table, carrying pictures over at least 10% of it (a scan) or many drawn
   shapes (text printed as outlines), is read with Docling OCR - only inside the filing body (first Item heading to the
   SIGNATURES heading, plus "F-" pages) and at most MAX_OCR_PAGES pages.
3. **recheck** (`second_opinion=True`): Docling re-reads only the statement pages where our reading failed - a row of
   numbers without a label, or a field row whose numbers do not add up. Matching values become "confirmed", differing
   ones "conflict"; a row without a label gets Docling's label. Our numbers are never overwritten.

OCR and recheck are skipped for a document whose type has no field list (e.g. a 20-F, or "other"); a document whose
first pages have no text is OCR'd first and its type is decided from the OCR text. Document types and their field lists
come from config (`document_types` and `fields` in config/extraction.json).

Every statement row ends with a status: verified | confirmed | unverified | conflict (or empty).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .fields import build
from .layout import build_index, doc_type, group_lines, page_tables, split_row, strip_furniture
from .read import ink, load_pages
from .statements import FIELDS, find_statements, merge, norm, verify

CONFIG = Path(__file__).resolve().parent.parent / "config" / "extraction.json"  # the one config file
MAX_OCR_PAGES = 40  # OCR takes 5-30 s per page, so at most this many pages per document
MIN_PICTURE = 0.10  # a text-less page whose pictures cover at least this share of it is a scan
MIN_SHAPES = 2000  # a text-less page with at least this many drawn path pieces is text printed as outlines
NO_TEXT_CHARS = 25  # a page with less text than this (and no table) may be a scan or drawn text
SIGNATURES = re.compile(r"^signatures?$", re.I)


# Step 1: the fast, deterministic extraction of one PDF into a dict (page index, tables, statements, checks).
# `types`: the document types of config/extraction.json, to decide what the document is.
def extract(path: str, types: dict) -> dict:
    pages, repaired = load_pages(path)
    pages_lines, pages_kinds, all_tables = [], [], []
    for pg in pages:
        lines = group_lines(pg)
        rows = [split_row(l) for l in lines]
        pages_lines.append(lines)
        pages_kinds.append([r.kind for r in rows])
        all_tables.extend(page_tables(strip_furniture(rows, pg.width, pg.height)))
    index = build_index(pages_lines, pages_kinds, [p.width for p in pages], [p.height for p in pages], types)
    statements = {k: merge(g) for k, g in find_statements(all_tables, {p["page"]: p for p in index["pages"]}).items()}
    ver = verify(statements)

    for p in index["pages"]:
        p["tables"] = [f"t{i + 1}" for i, t in enumerate(all_tables) if t.page == p["page"]]
    stmts_json = {}
    for k, t in statements.items():
        d = t.to_dict()
        for r, m in zip(d["rows"], ver["masks"][k]):
            r["verified"] = m
        d["verifiable"] = k != "equity"
        same = next((j for j, u in statements.items() if u is t and j != k and j in stmts_json), None)
        if same:  # e.g. one "operations and comprehensive income" statement: rows shown twice, checked once
            d["verifiable"], d["same_table_as"] = False, same
        stmts_json[k] = d
    textless = ink(path, [p["page"] for p in index["pages"] if len(p["text"].strip()) < NO_TEXT_CHARS and not p["tables"]])
    scanned = [n for n, (pic, _) in textless.items() if pic >= MIN_PICTURE]
    drawn = [n for n, (pic, shapes) in textless.items() if pic < MIN_PICTURE and shapes >= MIN_SHAPES]
    return {
        "source_file": Path(path).name, "page_count": len(pages), "doc_type": index["doc_type"],
        "warnings": [], "quality": {"damaged": repaired, "scanned_pages": scanned, "drawn_text_pages": drawn,
                                    "blank_pages": sorted(set(textless) - set(scanned) - set(drawn))},
        "pages": index["pages"], "tables": [{**t.to_dict(), "id": f"t{i + 1}"} for i, t in enumerate(all_tables)],
        "statements": stmts_json, "checks": [{k: v for k, v in c.items() if k not in ("refs", "rows")} for c in ver["checks"]],
    }


# The pages worth OCR: the filing body, from the first Item heading to the SIGNATURES heading, plus pages printed as
# "F-1", "F-2", ... (financial statements some filers put after the signatures). No Item heading at all: every page.
def _ocr_window(pages: list[dict]) -> set[int]:
    first = next((p["page"] for p in pages if p["sections_started"]), None)
    if first is None:
        return {p["page"] for p in pages}
    end = next((p["page"] for p in pages if p["page"] > first and any(SIGNATURES.match(h["text"].strip()) for h in p.get("headings", []))), len(pages))
    return set(range(first, end + 1)) | {p["page"] for p in pages if (p.get("page_label") or "").upper().startswith("F-")}


# Statement pages where our reading failed: a row of numbers without a label, or a row of a known field (statements.FIELDS)
# whose numbers did not add up. Only these pages are re-read by Docling.
def _recheck_pages(statements: dict) -> list[int]:
    pages = set()
    for kind, st in statements.items():
        if not st.get("verifiable", True):
            continue
        matchers = [m for k, (s, ms) in FIELDS.items() if s == kind for m in ms]
        for r in st["rows"]:
            if not any(v for v in r["values"] if v is not None):
                continue
            label, section, ver = norm(r["label"]), norm(r.get("section") or ""), r["verified"] or []
            unverified = not ver or not all(ok for v, ok in zip(r["values"], ver) if v is not None)
            if not label or (unverified and any(m(label, section, r) for m in matchers)):
                pages.add(r["page"])
    return sorted(pages)


# The whole pipeline for one PDF: extract, OCR text-less pages, optional Docling recheck, statuses and warnings.
# `types`: document types from config; `forms`: the types that will be used (None = all) - Docling is not spent on others.
def run(path: str, types: dict, second_opinion: bool = False, forms: set[str] | None = None) -> dict:
    res = extract(path, types)
    res["pipeline"] = {"stages": ["extraction"], "ocr_pages": []}
    warnings = res["warnings"]
    if not res["pages"]:
        warnings.append("the PDF has no pages")
    if res["quality"]["damaged"]:
        empty = len(res["quality"]["blank_pages"])
        warnings.append(f"the file is damaged (cut off or corrupt; it had to be repaired to open): {empty} of {res['page_count']} pages have no content")

    window = _ocr_window(res["pages"])
    ocr = [p for p in res["quality"]["scanned_pages"] + res["quality"]["drawn_text_pages"] if p in window]
    unknown = res["doc_type"] == "other" and bool({1, 2} & set(ocr))  # the form is on the first pages, which have no text
    if forms is not None and res["doc_type"] not in forms and not unknown:
        ocr = []
    if len(ocr) > MAX_OCR_PAGES:
        warnings.append(f"{len(ocr)} pages have no text layer; OCR capped at {MAX_OCR_PAGES} pages")
        ocr = sorted(ocr)[:MAX_OCR_PAGES]
    if ocr:
        try:
            from . import docling_check

            t = time.perf_counter()
            got = docling_check.ocr_pages(path, ocr)
            for p in ocr:
                page = res["pages"][p - 1]
                page["text"] = got[p]["text"]
                page["ocr"] = True
                for k, tb in enumerate(got[p]["tables"]):
                    tid = f"o{p}_{k + 1}"
                    res["tables"].append({"id": tid, "pages": [p], "kind": None, "title": "", "unit": None,
                                          "scale": None, "columns": [], "rows": [], "cells": tb["cells"], "flags": ["docling table (OCR page)"]})
                    page["tables"].append(tid)
            res["pipeline"]["stages"].append("docling_ocr")
            res["pipeline"].update(ocr_pages=ocr, ocr_seconds=round(time.perf_counter() - t, 1))
            if unknown:
                res["doc_type"] = doc_type([p["text"] for p in res["pages"]], types)
            empty = [p for p in ocr if not got[p]["text"].strip()]
            if empty:
                warnings.append(f"OCR found no text on page(s) {empty}")
            if 2 * len(ocr) >= len(res["pages"]):
                warnings.append(f"scanned document: {len(ocr)} of {len(res['pages'])} pages were read by OCR; headings on OCR pages "
                                "are not indexed, so Items, notes and statements there are reported as not found")
        except ImportError:
            warnings.append(f"{len(ocr)} pages have no text layer but Docling is not installed: they were not read")
        except Exception as e:  # keep the extraction result; report the failure
            warnings.append(f"OCR failed ({type(e).__name__}: {e}); {len(ocr)} text-less pages were not read")

    recheck = _recheck_pages(res["statements"]) if second_opinion and (forms is None or res["doc_type"] in forms) else []
    if recheck:
        try:
            from . import docling_check

            t = time.perf_counter()
            stats = docling_check.confirm(res["statements"], docling_check.page_tables(path, recheck))
            res["pipeline"].update(second_opinion_pages=recheck,
                                   second_opinion_seconds=round(time.perf_counter() - t, 1), second_opinion_stats=stats)
            res["pipeline"]["stages"].append("docling_recheck")
        except ImportError:
            warnings.append("recheck requested but Docling is not installed")
        except Exception as e:  # keep the extraction result; values keep their arithmetic status
            warnings.append(f"recheck failed ({type(e).__name__}: {e}); values keep their arithmetic status")
    for st in res["statements"].values():  # rows Docling did not look at: verified or unverified
        src = res["statements"].get(st.get("same_table_as") or "")
        for i, r in enumerate(st["rows"]):
            for key in ("label", "label_source", "status", "docling_values"):
                if src and key in src["rows"][i]:
                    r[key] = src["rows"][i][key]
            all_ok = r.get("verified") and all(r["verified"][i] for i, v in enumerate(r["values"]) if v is not None)
            r.setdefault("status", "verified" if all_ok and any(v is not None for v in r["values"]) else "unverified")

    if res["doc_type"] in ("10-K", "10-Q") and len(res["statements"]) < 3:
        warnings.append(f"{res['doc_type']} but only {len(res['statements'])} primary statements were found")
    failed = [x for x in res["checks"] if not x["passed"]]
    if failed:
        warnings.append(f"{len(failed)} arithmetic checks failed: " + "; ".join(f"{x['check']} (col {x['column']})" for x in failed[:5]))
    return res


# The config file: (the field lists, the whole config, a hash of the file). A change to the file changes the hash.
def load_config(path: Path = CONFIG) -> tuple[dict, dict, str]:
    raw = path.read_bytes()
    cfg = json.loads(raw)
    return cfg["fields"], cfg, hashlib.sha256(raw).hexdigest()


# Hash of a file's bytes, so the agent can tell whether a PDF changed.
def sha256(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return None


# What every metadata file says about the run: pipeline version, config and file hashes, and when.
def stamp(path: Path, config_hash: str) -> dict:
    return {"pipeline_version": __version__, "config_sha256": config_hash, "file_sha256": sha256(path),
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# One PDF -> (fields JSON or None, metadata). fast=True skips the Docling recheck (OCR of text-less pages still runs).
# config: (field lists, whole config, hash) from load_config, loaded once by the caller; None loads config/extraction.json.
def process(path: str | Path, fast: bool = False, config: tuple[dict, dict, str] | None = None) -> tuple[dict | None, dict]:
    t = time.perf_counter()
    path = Path(path)
    field_list, rules, config_hash = config or load_config()
    try:
        types = rules["document_types"]
        used = {d["name"] for d in types["types"] if any(isinstance(field_list.get(l), list) for l in d.get("field_lists", []))}
        res = run(str(path), types, second_opinion=not fast, forms=used)
        fields, meta = build(res, field_list, rules)
    except Exception as e:  # noqa: BLE001 - reported in the metadata, never hidden
        reason = f"{type(e).__name__}: {e}"
        for p in (str(path.resolve()), str(path)):  # no local paths in the output, also not as an escaped 'C:\\...'
            reason = reason.replace(p.replace("\\", "\\\\"), path.name).replace(p, path.name)
        return None, {"file": path.name, "form": None, "status": f"failed: {reason}", **stamp(path, config_hash),
                      "processing": {"seconds": round(time.perf_counter() - t, 1), "stages": [], "warnings": []}}
    pipe = res["pipeline"]
    meta.update(stamp(path, config_hash))
    meta["processing"] = {"seconds": round(time.perf_counter() - t, 1), "docling_recheck": not fast,
                          "recheck_pages": pipe.get("second_opinion_pages", []), "recheck_seconds": pipe.get("second_opinion_seconds"),
                          "recheck_result": pipe.get("second_opinion_stats"), "ocr_pages": pipe.get("ocr_pages", []),
                          "ocr_seconds": pipe.get("ocr_seconds"),
                          "arithmetic_checks": {"passed": sum(c["passed"] for c in res["checks"]),
                                                "failed": sum(not c["passed"] for c in res["checks"])},
                          "stages": pipe["stages"], "warnings": res["warnings"]}
    return fields, meta
