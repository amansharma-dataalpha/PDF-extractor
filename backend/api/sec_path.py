"""
# sec_path.py - answer a question from SEC filings when no PDF is selected

Three steps, called by `/extract` when the request has no documents:
1. **read the question**: one small Claude call turns it into requests - {company (ticker), form, fiscal_year,
   fiscal_period, fields} - in exactly that JSON shape. It only chooses what to fetch; it never supplies numbers.
2. **fetch**: `edgarx.query.filing_figures` pulls each filing's figures from the SEC's XBRL data (needs SEC_USER_AGENT).
3. **answer**: the same Claude answer step as the PDF path (`claude.answer`), with the SEC figures as its only source.

The result has the usual answer fields plus `source: "sec"`, the requests Claude read and the SEC data used.
"""
import json
from datetime import date

import anthropic

from api.claude import MODEL, answer
from edgarx.query import filing_figures
from edgarx.sec_client import SecClient
from edgarx.xbrl_fields import CONCEPTS

READ_SYSTEM = (
    "Turn the user's question about US public companies into SEC filing requests. company: the stock ticker "
    "(e.g. NVDA). fiscal_year and fiscal_period: the company's own fiscal labels, as printed on its filings. "
    "form: 10-Q for Q1-Q3, 10-K for FY or Q4. fields: the field ids that answer the question, from this list "
    "(empty list = the main figures):\n" + "\n".join(f"{c.id}: {c.label}" for c in CONCEPTS.values()) +
    "\nOne request per company and period. No company named: return no requests."
)
REQUEST = {"type": "object", "additionalProperties": False,
           "required": ["company", "form", "fiscal_year", "fiscal_period", "fields"],
           "properties": {"company": {"type": "string"}, "form": {"type": "string", "enum": ["10-K", "10-Q"]},
                          "fiscal_year": {"type": "integer"},
                          "fiscal_period": {"type": "string", "enum": ["Q1", "Q2", "Q3", "Q4", "FY"]},
                          "fields": {"type": "array", "items": {"type": "string", "enum": sorted(CONCEPTS)}}}}


# Ask Claude which companies, filings and figures the question is about (structured JSON, no numbers).
def read_requests(instruction: str) -> list[dict]:
    # shortcut: "latest quarter" is resolved from today's date only, upgrade by listing the company's recent filings
    msg = anthropic.Anthropic().beta.messages.create(
        model=MODEL, max_tokens=16000, system=READ_SYSTEM,
        messages=[{"role": "user", "content": f"Today is {date.today()}.\n\n{instruction}"}],
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object", "additionalProperties": False, "required": ["requests"],
            "properties": {"requests": {"type": "array", "items": REQUEST}}}}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if msg.stop_reason != "end_turn" or not text:  # declined or cut off: no usable request
        raise RuntimeError(f"Could not read the question (model stopped: {msg.stop_reason}). Rephrase it or tick a PDF.")
    return json.loads(text)["requests"]


# Answer the question from SEC filings: read it, fetch the figures, then let Claude write the answer.
def run_sec(instruction: str, fmt: str, custom_format: str = "") -> dict:
    requests = read_requests(instruction)
    if not requests:
        raise RuntimeError("No company found in the question. Name a company (e.g. NVIDIA) or tick a PDF.")
    client = SecClient()
    data = [filing_figures(client, **r) for r in requests]
    note = "SEC XBRL data. Values are in whole units (not millions). Each figure's label is the company's exact XBRL concept: describe figures by it.\n"
    out = answer(note + json.dumps(data, indent=1), instruction, fmt, custom_format)
    out.update(source="sec", sec_requests=requests, sec_data=data)
    return out
