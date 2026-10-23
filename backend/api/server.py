"""
# server.py - FastAPI server for the frontend in ../frontend

    .venv/Scripts/python -m uvicorn api.server:app --port 8000      (run it from the backend folder)

Each uploaded PDF goes through two stages, one PDF at a time:
1. **finx** (seconds, no AI): text, headings, tables, statements and arithmetic checks -> status "ready_text";
   questions can be asked right away. A scanned PDF has no text yet and waits for stage 2.
2. **full run** (about a minute): finx again plus Docling OCR of scanned pages and the Docling second opinion on the
   statements -> status "ready". It runs as a separate process, so a Docling crash cannot stop the server.

Storage on disk: data/<id>/ source.pdf, meta.json, result.json (finx JSON), pages.json (text sent to Claude), doc.md.
Endpoints: POST/GET /documents, GET/DELETE /documents/{id}, POST /documents/{id}/retry,
GET /documents/{id}/export?format=markdown|json|tables, POST /extract (asks Claude over the selected PDFs; with no PDF selected, over SEC filings - see sec_path.py).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND / ".env")  # before importing claude.py, which reads its settings at import time

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402
import anthropic  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from api.claude import run_extraction  # noqa: E402
from api.sec_path import run_sec  # noqa: E402
from finx.pipeline import run as finx_run  # noqa: E402

DATA = BACKEND / "data"
DATA.mkdir(exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
FULL_RUN_TIMEOUT_S = 30 * 60
_full_lock = threading.Lock()  # Docling uses every core and ~1.5 GB: one full run at a time


def _dir(doc_id: str) -> Path:
    if not doc_id.isalnum():
        raise HTTPException(400, "bad id")
    return DATA / doc_id


def _meta(doc_id: str) -> dict:
    p = _dir(doc_id) / "meta.json"
    if not p.exists():
        raise HTTPException(404, "document not found")
    return json.loads(p.read_text(encoding="utf-8"))


# Write then rename, so a reader (list, extract) never sees a half-written file.
def _write_text(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_meta(doc_id: str, meta: dict):
    _write_text(_dir(doc_id) / "meta.json", json.dumps(meta))


# ------------------------------------------------------------------------------------------------ finx JSON -> text
def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


# Column title with its period, e.g. "2026 (12 months ended June 30, 2026)".
def _column_title(c: dict, i: int) -> str:
    head, p = c.get("header") or f"Column {i + 1}", c.get("period") or {}
    if p.get("months") and p.get("year"):
        return f"{head} ({p['months']} months ended {p.get('date') or ''} {p['year']})".replace("  ", " ").replace(" )", ")")
    return head


# One table as Markdown: title and unit, then a row per line item (section headings as their own rows).
def _table_md(t: dict, rows: list[dict], checked: bool) -> str:
    cols = ["Line item"] + [_column_title(c, i) for i, c in enumerate(t["columns"])] + (["Check"] if checked else [])
    unit = f" (in {t['unit']})" if t.get("unit") else ""
    out = [f"Table: {t.get('title') or 'untitled'}{unit}", "| " + " | ".join(map(_cell, cols)) + " |", "|" + "---|" * len(cols)]
    section = ""
    for r in rows:
        if r.get("section") and r["section"] != section:
            section = r["section"]
            out.append(f"| **{_cell(section)}** |" + " |" * (len(cols) - 1))
        cells = [r["label"]] + list(r["raw"]) + ([r.get("status") or ""] if checked else [])
        out.append("| " + " | ".join(map(_cell, cells)) + " |")
    return "\n".join(out)


# Per-page text for Claude: the page's text, then the tables that start on it (statements with their check status).
def _pages_for_llm(res: dict) -> list[dict]:
    tables = {t["id"]: t for t in res["tables"]}
    stmts = {k: s for k, s in res["statements"].items() if not s.get("same_table_as")}
    pages = []
    for p in res["pages"]:
        parts = [p["text"]]
        for tid in p.get("tables", []):
            t = tables.get(tid)
            if not t or t["pages"][0] != p["page"]:
                continue  # a table spanning pages is shown once, on its first page
            st = stmts.get(t.get("kind") or "")
            same = st is not None and st["pages"] == t["pages"]
            parts.append(_table_md(t, st["rows"] if same else t["rows"], same))
        pages.append({"page": p["page"], "markdown": "\n\n".join(x for x in parts if x.strip())})
    return pages


# Save one finx result and return the numbers shown in the file list.
def _save_result(d: Path, res: dict) -> dict:
    pages = _pages_for_llm(res)
    _write_text(d / "result.json", json.dumps(res, ensure_ascii=False))
    _write_text(d / "pages.json", json.dumps(pages, ensure_ascii=False))
    _write_text(d / "doc.md", "\n\n".join(f"<!-- page {p['page']} -->\n{p['markdown']}" for p in pages))
    return {"pages": res["page_count"], "tables": len(res["tables"]), "doc_type": res["doc_type"],
            "statements": len([s for s in res["statements"].values() if not s.get("same_table_as")]),
            "certified_pct": res["assurance"]["certified_pct"], "warnings": res["warnings"],
            "chars": sum(len(p["markdown"]) for p in pages)}


# ------------------------------------------------------------------------------------------------ the two stages
# Stage 1: finx only (seconds) -> "ready_text". A PDF with no text at all (scanned) waits for stage 2's OCR.
def _quick(doc_id: str):
    d = _dir(doc_id)
    try:
        if _meta(doc_id)["status"] != "processing":
            return
        res = finx_run(str(d / "source.pdf"), second_opinion=False, ocr=False)
        if not any(p["text"].strip() for p in res["pages"]):
            return
        info = _save_result(d, res)
        meta = _meta(doc_id)
        if meta["status"] == "processing":  # the full run did not finish first
            meta.update(status="ready_text", **info)
            _write_meta(doc_id, meta)
    except Exception:  # noqa: BLE001
        return  # deleted meanwhile, or unreadable: the full run (stage 2) decides the final status


# Stage 2: finx + Docling OCR + second opinion in a separate process -> "ready".
def _full(doc_id: str):
    d = _dir(doc_id)
    if not (d / "meta.json").exists():
        return  # deleted before it started
    out_dir = d / "full"
    try:
        with _full_lock:
            p = subprocess.run([sys.executable, "-m", "finx.pipeline", str(d / "source.pdf"), "-o", str(out_dir)],
                               cwd=BACKEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=FULL_RUN_TIMEOUT_S, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if not (out_dir / "source.json").exists():
            lines = [x for x in (p.stdout + p.stderr).strip().splitlines() if x.strip()]
            raise RuntimeError(lines[-1].split("FAILED", 1)[-1].strip() if lines else f"exit code {p.returncode}")
        info = _save_result(d, json.loads((out_dir / "source.json").read_text(encoding="utf-8")))
        meta = _meta(doc_id)
        meta.pop("error", None)
        meta.update(status="ready", **info)
    except Exception as e:  # noqa: BLE001
        if not d.exists():
            return  # deleted while running; nothing to record
        meta = _meta(doc_id)  # pick up a "ready_text" status written by stage 1
        if meta["status"] == "ready_text":
            meta["error"] = f"Docling check failed, the finx extraction is still usable: {e}"
        else:
            meta.update(status="error", error=str(e))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    if d.exists():
        _write_meta(doc_id, meta)


# Stage 1 for every document first (seconds), then stage 2 one PDF at a time (minutes).
def _run_queue(doc_ids: list[str]):
    for i in doc_ids:
        _quick(i)
    for i in doc_ids:
        _full(i)


# Work cut off by a restart: "processing" documents become errors, "ready_text" ones resume stage 2.
def _recover_interrupted() -> list[str]:
    resume = []
    for p in DATA.glob("*/meta.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") == "processing":
            meta.update(status="error", error="interrupted by restart")
            _write_text(p, json.dumps(meta))
        elif meta.get("status") == "ready_text" and not meta.get("error"):
            resume.append(meta["id"])
    return resume


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue = _recover_interrupted()
    if queue:
        threading.Thread(target=_run_queue, args=(queue,), daemon=True).start()
    yield


app = FastAPI(title="PDF Extractor", lifespan=lifespan)
ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")  # the frontend's address(es)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------------------------------------ endpoints
def _validate_pdf(f: UploadFile) -> None:
    name = f.filename or "file"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, f"{name} is not a PDF")
    f.file.seek(0, 2)
    size = f.file.tell()
    f.file.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if f.file.read(4) != b"%PDF":
        raise HTTPException(400, f"{name} is not a valid PDF (missing %PDF header)")
    f.file.seek(0)


@app.post("/documents")
async def upload(bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    # Validate every file before saving any, so one bad file can't leave others stuck at "processing".
    for f in files:
        _validate_pdf(f)
    out = []
    for f in files:
        doc_id = uuid.uuid4().hex[:12]
        d = _dir(doc_id)
        d.mkdir()
        with open(d / "source.pdf", "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        meta = {"id": doc_id, "name": f.filename, "status": "processing"}
        _write_meta(doc_id, meta)
        out.append(meta)
    bg.add_task(_run_queue, [m["id"] for m in out])
    return out


@app.get("/documents")
def list_docs():
    metas = [json.loads(p.read_text(encoding="utf-8")) for p in DATA.glob("*/meta.json")]
    return sorted(metas, key=lambda m: m["name"].lower())


@app.get("/documents/{doc_id}")
def get_doc(doc_id: str):
    return _meta(doc_id)


@app.post("/documents/{doc_id}/retry")
def retry_doc(doc_id: str, bg: BackgroundTasks):
    meta = _meta(doc_id)
    failed = meta["status"] == "error" or (meta["status"] == "ready_text" and meta.get("error"))
    if not failed:
        raise HTTPException(409, f"document is {meta['status']}, only failed documents can be retried")
    if not (_dir(doc_id) / "source.pdf").exists():
        raise HTTPException(404, "original PDF is missing; upload it again")
    meta = {"id": meta["id"], "name": meta["name"], "status": "processing"}
    _write_meta(doc_id, meta)
    bg.add_task(_run_queue, [doc_id])
    return meta


@app.delete("/documents/{doc_id}")
def delete_doc(doc_id: str):
    _meta(doc_id)
    shutil.rmtree(_dir(doc_id))
    return {"ok": True}


@app.get("/documents/{doc_id}/export")
def export(doc_id: str, format: str = "markdown"):
    meta = _meta(doc_id)
    if meta["status"] not in ("ready", "ready_text"):
        raise HTTPException(409, f"document is {meta['status']}")
    d = _dir(doc_id)
    if format == "markdown":
        return PlainTextResponse((d / "doc.md").read_text(encoding="utf-8"))
    if format in ("json", "tables"):
        res = json.loads((d / "result.json").read_text(encoding="utf-8"))
        return JSONResponse(res if format == "json" else {"tables": res["tables"], "statements": res["statements"]})
    raise HTTPException(400, "format must be markdown | json | tables")


class ExtractRequest(BaseModel):
    doc_ids: list[str]
    instruction: str
    format: Literal["table", "paragraph", "bullets", "keyvalue", "json", "custom"] = "table"
    custom_format: str = ""


@app.post("/extract")
def extract(req: ExtractRequest):
    docs = []
    for i in req.doc_ids:
        meta = _meta(i)
        if meta["status"] not in ("ready", "ready_text"):
            raise HTTPException(409, f"{meta['name']} is {meta['status']}")
        docs.append({"name": meta["name"], "pages": json.loads((_dir(i) / "pages.json").read_text(encoding="utf-8"))})
    try:
        if not docs:  # no PDF selected: answer from SEC filings
            return run_sec(req.instruction, req.format, req.custom_format)
        return run_extraction(docs, req.instruction, req.format, req.custom_format)
    except (RuntimeError, ValueError) as e:  # missing settings, SEC errors, unknown field ids
        raise HTTPException(500, str(e))
    except anthropic.RateLimitError:
        raise HTTPException(429, "Claude API rate limit reached. Wait a moment and try again.")
    except anthropic.AuthenticationError:
        raise HTTPException(401, "Claude API rejected the API key. Check ANTHROPIC_API_KEY in backend/.env.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Claude API. Check the network connection.")
