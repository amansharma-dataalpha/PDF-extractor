"""
# api - the web server behind the frontend in ../frontend

    .venv/Scripts/python -m uvicorn api.server:app --port 8000      (run it from the backend folder)

- **server.py**: upload, list, export and delete PDFs; each PDF is read by finx (no AI), then re-checked by Docling.
- **claude.py**: answers a question over the extracted pages with Claude (needs ANTHROPIC_API_KEY in backend/.env).
- **router.py**: one call lists the filings each question asks about; each is taken from the uploads, else a sample
  filing, else fetched from SEC (via edgarx; needs SEC_USER_AGENT) - then the answer is written from that.
"""
