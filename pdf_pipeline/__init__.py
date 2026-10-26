"""
# pdf_pipeline - turn 10-K / 10-Q PDFs into JSON with the required fields, without any AI language model

Use it from code:

    from pdf_pipeline import process
    fields, metadata = process("NVDA_10K_FY2026.pdf")          # fields is None when nothing was extracted

Files, in the order the data flows through them:
- `read.py`          read every word on every page, with its position, size and boldness
- `layout.py`        words -> lines -> rows (label + numbers) -> tables; page index (text, headings, Items, form)
- `statements.py`    find the five financial statements, name their rows, check that our reading adds up
- `docling_check.py` Docling: re-reads only the statement pages we misread, OCR for pages without text
- `fields.py`        fills each required field by its rules in config/extraction.json; builds the metadata
- `pipeline.py`      runs everything for one PDF: `process(path)` -> (fields, metadata)

The command line for many PDFs is `extract.py` at the repo root.
"""

__version__ = "0.5.0"  # written into every metadata file; raise it when the output can change

from .pipeline import process  # noqa: E402 - after __version__, which pipeline.py imports
