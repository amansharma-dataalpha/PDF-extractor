"""
# extract.py - turn filing PDFs into JSON with only the required fields

    .venv/Scripts/python extract.py            # every PDF in input/ (and its sub-folders) -> output/json/ and output/metadata/
    .venv/Scripts/python extract.py --fast     # skip the Docling recheck of misread statement pages
    .venv/Scripts/python extract.py input/NVDA_10K_FY2026.pdf      # one file
    .venv/Scripts/python extract.py --workers 4                    # 4 PDFs at a time (each worker loads Docling once, if needed)
    .venv/Scripts/python extract.py --resume                       # skip PDFs already done with this version and field list

- Each PDF is read by `pdf_pipeline/pipeline.py` (no AI): text, Items, notes, tables, statements, arithmetic checks, plus
  Docling OCR for text-less pages and Docling's re-reading of the statement pages we misread (usually none or one or two).
- `pdf_pipeline/fields.py` then keeps only the fields listed for the filing's type in `config/extraction.json`,
  filled by the rules in the same file.
- Writes `output/json/<pdf name>.json` (document info + the fields found, with their content) and
  `output/metadata/<pdf name>.json` (score found/listed, found and not found field names, notes, processing time,
  pipeline version, config and file hashes). Every document type with a field list is extracted (10-K, 10-Q, press
  release, 8-K, proxy statement, Form 10, call transcript, slides); any other document gets only a metadata file saying so.
- Every PDF runs in a worker process. A worker that crashes (Docling can, now and then) is replaced and its PDF tried
  once more; a PDF that takes longer than --timeout is stopped. Either way the PDF gets a "failed" metadata file and
  the other PDFs carry on. Files are written whole or not at all.
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time
from collections import Counter
from multiprocessing.connection import wait
from pathlib import Path

from pdf_pipeline import __version__
from pdf_pipeline.fields import check_config
from pdf_pipeline.pipeline import load_config, process, sha256, stamp

ROOT = Path(__file__).resolve().parent


# Write a JSON file in one go: a crash mid-write never leaves half a file behind.
def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# Write one PDF's results (no field file when nothing was extracted) and return its one-line summary.
def _save(pdf: Path, name: str, out: Path, fields: dict | None, meta: dict) -> str:
    (out / "json" / f"{name}.json").unlink(missing_ok=True)  # no stale field file from an earlier run
    if fields:
        _write(out / "json" / f"{name}.json", fields)
    _write(out / "metadata" / f"{name}.json", meta)
    return f"{pdf.name:30} {meta['form'] or '-':14} {meta.get('score', meta.get('status', '')):>8}  {meta['processing']['seconds']:>6} s"


# The metadata of a PDF whose worker crashed or ran out of time, and its summary line.
def _failed(pdf: Path, name: str, out: Path, reason: str, config_hash: str, seconds: float = 0.0) -> str:
    meta = {"file": pdf.name, "form": None, "status": f"failed: {reason}", **stamp(pdf, config_hash),
            "processing": {"seconds": round(seconds, 1), "stages": [], "warnings": []}}
    return _save(pdf, name, out, None, meta)


# A worker process: takes one PDF at a time from the parent, sends back its summary line, stops on None.
def _worker(conn, out: str, fast: bool) -> None:
    config = load_config()  # once per worker
    while (job := conn.recv()) is not None:
        pdf = Path(job[0])
        conn.send(_save(pdf, job[1], Path(out), *process(pdf, fast, config)))


# Run all PDFs on `workers` worker processes. The parent knows which PDF each worker holds, so a crashed or stuck
# worker costs only that PDF: a crash is retried once in a new worker, a PDF over `timeout` seconds is stopped.
def run_all(jobs: list[tuple[Path, str]], out: Path, fast: bool, workers: int, timeout: float, config_hash: str) -> None:
    ctx = mp.get_context("spawn")
    todo, tries = list(reversed(jobs)), Counter()

    # Start one worker process, connected to the parent by a pipe.
    def start() -> dict:
        a, b = ctx.Pipe()
        p = ctx.Process(target=_worker, args=(b, str(out), fast), daemon=True)
        p.start()
        b.close()
        return {"proc": p, "conn": a, "job": None, "since": 0.0}

    # Hand a worker the next PDF (or nothing, when all are handed out) and note when it started.
    def give(w: dict) -> None:
        w["job"] = todo.pop() if todo else None
        if w["job"]:
            w["since"] = time.monotonic()
            w["conn"].send((str(w["job"][0]), w["job"][1]))

    pool = [start() for _ in range(min(workers, len(jobs)))]
    for w in pool:
        give(w)
    while any(w["job"] for w in pool):
        busy = [w for w in pool if w["job"]]
        ready = wait([w["conn"] for w in busy] + [w["proc"].sentinel for w in busy], timeout=5)
        for i, w in enumerate(pool):
            if not w["job"]:
                continue
            if w["conn"] in ready:
                try:
                    print(w["conn"].recv(), flush=True)
                    give(w)
                    continue
                except (EOFError, OSError):
                    pass  # the worker died while sending: handled as a crash below
            late = time.monotonic() - w["since"] > timeout
            if w["proc"].is_alive() and not late:
                continue
            w["proc"].kill()
            w["proc"].join()
            pdf, name = w["job"]
            tries[name] += 1
            if late:
                print(_failed(pdf, name, out, f"timed out after {timeout:.0f} s", config_hash, timeout), flush=True)
            elif tries[name] < 2:
                todo.append(w["job"])  # Docling crashes now and then: one more try in a fresh worker
            else:
                print(_failed(pdf, name, out, f"the worker process crashed twice (exit code {w['proc'].exitcode})", config_hash), flush=True)
            pool[i] = start()
            give(pool[i])
    for w in pool:
        w["conn"].send(None)
        w["proc"].join(timeout=10)


# True when the PDF's metadata says it was already done with this pipeline version, field list and file.
def _done(pdf: Path, name: str, out: Path, config_hash: str) -> bool:
    try:
        m = json.loads((out / "metadata" / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (m.get("pipeline_version") == __version__ and m.get("config_sha256") == config_hash
            and m.get("file_sha256") == sha256(pdf) and not str(m.get("status", "")).startswith("failed"))


# Output names: the PDF's name, plus a short hash of its path when two PDFs in one run share a name.
def _names(pdfs: list[Path]) -> list[str]:
    seen = Counter(p.stem.lower() for p in pdfs)
    return [p.stem if seen[p.stem.lower()] == 1 else f"{p.stem}__{hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:8]}"
            for p in pdfs]


# Command line: PDFs (default: everything in input/) -> output/json and output/metadata.
def main() -> int:
    ap = argparse.ArgumentParser(description="Filing PDFs -> JSON with only the required fields (no AI)")
    ap.add_argument("pdfs", nargs="*", help="PDF files or folders, searched with their sub-folders (default: input/)")
    ap.add_argument("-o", "--out", default=str(ROOT / "output"))
    ap.add_argument("--fast", action="store_true", help="skip the Docling recheck of misread statement pages")
    ap.add_argument("--workers", type=int, default=1, help="PDFs processed at a time (each worker that uses Docling needs about 1.2 GB)")
    ap.add_argument("--timeout", type=float, default=1200, help="seconds one PDF may take before it is stopped (OCR of 40 pages can take 20 min)")
    ap.add_argument("--resume", action="store_true", help="skip PDFs already done with this pipeline version, field list and file")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "json").mkdir(parents=True, exist_ok=True)
    (out / "metadata").mkdir(parents=True, exist_ok=True)
    pdfs = [f for p in map(Path, a.pdfs or [ROOT / "input"])
            for f in (sorted(x for x in p.rglob("*") if x.suffix.lower() == ".pdf" and x.is_file()) if p.is_dir() else [p])]
    sys.stdout.reconfigure(errors="replace")
    if not pdfs:
        print("no PDF files found", file=sys.stderr)
        return 2
    field_list, rules, config_hash = load_config()
    errors, missing = check_config(field_list, rules)
    for m in missing:
        print(f"note: no rule for field {m} (it will be reported as not found)", file=sys.stderr)
    if errors:
        print("config errors:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 2
    jobs = list(zip(pdfs, _names(pdfs)))
    if a.resume:
        jobs = [(p, n) for p, n in jobs if not _done(p, n, out, config_hash)]
        print(f"{len(pdfs) - len(jobs)} PDFs already done, {len(jobs)} to go", flush=True)
    if jobs:
        run_all(jobs, out, a.fast, max(1, a.workers), a.timeout, config_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
