"""
# finx - turn financial PDFs into structured JSON, without any AI language model

Files, in the order the data flows through them:
- `read_pdf.py`      read every word on every page, with its position, size and boldness
- `tables.py`        words -> lines -> rows (label + numbers) -> tables (columns, periods, units)
- `page_index.py`    page text, headings, sections ("Item 7"), contents pages, printed page numbers, document type
- `statements.py`    find the five financial statements; give their rows standard field names
- `check_numbers.py` prove the numbers add up (totals, balance sheet, cash flow) and mark them verified
- `docling_check.py` optional Docling: second reading of the statement pages, OCR for scanned pages
- `pipeline.py`      runs everything for a PDF or a folder and writes one JSON per PDF (the command line)

Run:  python -m finx.pipeline <pdf or folder> -o output/finx [--fast]
"""
