# PDF Extractor

Turns financial PDFs (10-K, 10-Q, earnings releases) into structured JSON. Extraction is deterministic, with no
language model involved. Each statement value is certified by arithmetic checks or by a second, independent reading,
or else flagged. A web app on top lets you upload PDFs and ask questions; the answers come from Claude (Anthropic API), working
from the extracted data.

## Results

Measured on 7 SEC filings (560 pages): 10-Ks from Amazon, Microsoft, Walmart, nVent and GE Vernova, plus a GE Vernova
10-Q and earnings release. Laptop CPU, no GPU.

| Metric | Result |
|---|---|
| Extraction time, finx only | 12.6 s for all 560 pages (about 22 ms per page) |
| Extraction time, with Docling second opinion | 41-60 s per filing |
| Peak memory | 130-200 MB (finx), 1.6-1.9 GB (Docling) |
| Arithmetic checks passing (totals, balance sheet, cash roll-forward, net income tie) | 410 / 410 |
| Statement values verified by arithmetic alone | 1,436 / 1,558 (92.2%) |
| Statement values certified with the second opinion | 1,557 / 1,558 (99.94%); the remaining one is flagged as a conflict |
| Planted wrong numbers caught (changes larger than 2 units) | 1,670 / 1,685 (99.1%) |
| Microsoft 10-K: line items vs. reference template | 45 / 45 correct for both years; 23 / 23 Item sections on the right page |
| Agreement with SEC XBRL data for the same filings | 579 / 582 values (99.5%) |

For comparison, Docling alone takes 15-22 s per page in fast mode and 29-57 s per page in accurate mode.

## How it works

1. **Read** every word with its position, font size and weight (PyMuPDF).
2. **Tables**: words on the same line form rows; numbers whose right edges align form columns. Header text sets each
   column's period, and lines such as "(In millions)" set the unit. Brackets, minus signs and dashes are normalised.
3. **Page index**: headings, 10-K/10-Q Item sections, contents pages, printed page numbers, document type.
4. **Statements**: finds the income statement, comprehensive income, balance sheet, cash flow and equity statements.
5. **Verification**: a value is marked `verified` only if an arithmetic check it takes part in passes. The rounding
   tolerance follows the printed decimals.
6. **Docling** (optional): OCR for scanned pages, and a second reading of the statement pages. Values that match
   become `confirmed`; values that differ become `conflict`. Docling never overwrites a finx value.

Every statement row ends with a status of `verified`, `confirmed`, `unverified` or `conflict`. Damaged, garbled,
empty and password-protected files produce explicit warnings or errors.

## Question routing: PDFs, SEC filings or both

Every uploaded PDF is used in every question; the sample filings can be added per question by ticking them.
One model call reads each question and lists every filing it asks about as
`{company, form, fiscal_year, fiscal_period, fields}`, naming the listed file that already is that filing, if any.
Code then takes each filing in order: an uploaded (or ticked) file is used as is; otherwise a matching sample filing is
added automatically (no SEC call, and the answer says so); otherwise the filing's figures are fetched from SEC. The
route follows: `pdf`, `sec` (e.g. "NVIDIA revenue in fiscal Q3 2025") or `both` (e.g. "compare our revenue in the
uploaded 10-K with Tesla's 2025 revenue"). SEC figures come only
from the matching filing's XBRL data (value, unit, exact dates, XBRL concept, accession number), and the answer cites
the PDF pages and filings it used. Years and quarters are the company's own fiscal labels. Q4 is not filed separately, so a Q4
question returns the 10-K's full-year figures. XBRL holds tagged figures only: no narrative text, no segment
breakdowns, no company-specific line items; attach the PDF for those.

## Repository layout

| Path | Contents |
|---|---|
| `backend/finx/` | Extraction pipeline and command line |
| `backend/api/` | FastAPI server for the web app; question answering with Claude |
| `backend/edgarx/` | SEC EDGAR XBRL client: figures of a given 10-K / 10-Q (used when a question needs SEC filings) |
| `frontend/` | React (Vite) web app |
| `samples/` | The 7 public filings the results above were measured on (Amazon, Microsoft, Walmart, nVent and GE Vernova 10-Ks, a GE Vernova 10-Q and earnings release); read at server start and offered in the app as optional sample filings |

## Quick start

Requirements: Python 3.12, Node 18+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # macOS/Linux: .venv/bin/python
cp .env.example .env                                         # set ANTHROPIC_API_KEY
.venv/Scripts/python -m uvicorn api.server:app --port 8000
```

```bash
cd frontend
npm install
npm run dev                                                  # http://localhost:5173
```

The first full run downloads Docling's models (about 1 GB).

## Configuration

Set in `backend/.env`.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none (required for `/extract`) | Anthropic API key |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model used to answer questions |
| `MAX_CONTEXT_CHARS` | `2400000` | Maximum document text sent per question; the answer names any document that had to be cut |
| `CLAUDE_MAX_TOKENS` | `64000` | Maximum answer length, including the model's thinking tokens |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed frontend origins, comma-separated |
| `SEC_USER_AGENT` | none (required for SEC answers) | Contact SEC requires on every request, e.g. `Company Name your.name@company.com` |

The frontend reads `VITE_API_URL` (default `http://localhost:8000`).

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/documents` | Upload PDFs (multipart field `files`, 50 MB each) |
| `GET` | `/documents` | List documents and their status |
| `GET` | `/documents/{id}` | Status of one document |
| `DELETE` | `/documents/{id}` | Delete a document |
| `POST` | `/documents/{id}/retry` | Re-process a failed document |
| `GET` | `/documents/{id}/export?format=markdown\|json\|tables` | Download the extraction |
| `POST` | `/extract` | `{doc_ids, instruction, format: table\|paragraph\|bullets\|keyvalue\|json\|custom, custom_format}` |

A document's status moves from `processing` to `ready_text` (finx done, a few seconds; questions work) to `ready`
(Docling OCR and second opinion done). Docling runs in a separate process, one document at a time.

## Command line

```bash
cd backend
.venv/Scripts/python -m finx.pipeline report.pdf -o output          # finx + Docling second opinion
.venv/Scripts/python -m finx.pipeline pdf_folder -o output --fast   # finx only
```

Options: `--backend pymupdf|pypdfium2|pdfplumber`, `--docling-mode fast|accurate`, `--no-ocr`, `-j N` (files in
parallel). The output is one JSON file per PDF: pages (text, headings, section, printed page label), tables (title,
unit, column periods, rows with raw and parsed values), statements with per-row status, checks, warnings and an
assurance summary.

## Limitations

- Label rules are in English and tuned on US filings. Numbers in European format (`1.234,5`) are not supported.
- Only the five primary statements get verification. Other tables are extracted without checks.
- A change of 1-2 units on a rounded total is within tolerance and cannot be caught by arithmetic. The second opinion
  reduces this risk.
- Two-column layouts and sideways-rotated text are not reconstructed reliably.
- Scanned pages need Docling OCR (about 30 s per page; at most 40 pages per document).

## License

MIT, see [LICENSE](LICENSE).

Dependencies: PyMuPDF (the default PDF reader) is AGPL-3.0, so a deployment that uses it must meet AGPL terms. pypdfium2, pdfplumber and Docling have permissive licences. To avoid PyMuPDF,
uninstall it and run with `--backend pypdfium2`. This is about 2x slower, and the damaged-file flag is no longer
reported.
