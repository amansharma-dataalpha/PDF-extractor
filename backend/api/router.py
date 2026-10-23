"""
# router.py - decide where each question is answered from, then answer it

Every `/extract` question goes through two Claude calls:
1. **route** (one call on the question): lists every filing the question asks about -
   {company (ticker), form, fiscal_year, fiscal_period, fields} - and for each one names the listed file that IS that
   filing, if any (`found_in`). It is shown the files in the analysis (all uploads plus the ticked samples) and all
   other sample filings (names and the start of page 1). It chooses; it never supplies numbers.
2. Plain code then takes each asked filing in this order: already in the analysis -> nothing to do; one of the sample
   filings -> that sample is added to the analysis (no SEC call); otherwise -> fetched from SEC (`edgarx.query`).
   The route follows: `pdf` (files only), `sec` (SEC only) or `both`.
3. **answer** (`claude.answer`): over every file in the analysis and the SEC figures, in one pass - it never asks back.

The result has the usual answer fields plus `route`, `samples_added`, the SEC requests and the SEC data used.
"""
import json
from datetime import date

import anthropic

from api.claude import MODEL, answer, build_context
from edgarx.query import filing_figures
from edgarx.sec_client import SecClient
from edgarx.xbrl_fields import CONCEPTS

ROUTE_SYSTEM = (
    "List every SEC filing the user's question asks about (a company's 10-K or 10-Q for a period). For each: company "
    "is the stock ticker (e.g. NVDA); fiscal_year and fiscal_period are the company's own fiscal labels, as printed on "
    "its filings; form is 10-Q for Q1-Q3 and 10-K for FY or Q4; fields are the field ids that answer the question, from "
    "this list (empty list = the main figures):\n" + "\n".join(f"{c.id}: {c.label}" for c in CONCEPTS.values()) +
    "\nfound_in: the exact name of the listed file that is this same filing (same company, form and period) - look in "
    "the files in the analysis first, then in the other sample filings; an empty string when no listed file is it. "
    "A question only about the files in the analysis, naming no other filing, has no requests."
)
REQUEST = {"type": "object", "additionalProperties": False,
           "required": ["company", "form", "fiscal_year", "fiscal_period", "fields", "found_in"],
           "properties": {"company": {"type": "string"}, "form": {"type": "string", "enum": ["10-K", "10-Q"]},
                          "fiscal_year": {"type": "integer"},
                          "fiscal_period": {"type": "string", "enum": ["Q1", "Q2", "Q3", "Q4", "FY"]},
                          "fields": {"type": "array", "items": {"type": "string", "enum": sorted(CONCEPTS)}},
                          "found_in": {"type": "string"}}}
PLAN = {"type": "object", "additionalProperties": False, "required": ["requests"],
        "properties": {"requests": {"type": "array", "items": REQUEST}}}


# A file as the router sees it: its name and the start of page 1 (company name and period on a 10-K / 10-Q cover).
def _summary(d: dict) -> str:
    return f"- {d['name']}: {d['pages'][0]['markdown'][:1000]!r}" if d["pages"] else f"- {d['name']}"


# The one call on the question: every filing it asks about, and which listed file (if any) already is that filing.
def route(instruction: str, docs: list[dict], samples: list[dict]) -> list[dict]:
    # shortcut: "latest quarter" is resolved from today's date only, upgrade by listing the company's recent filings
    files = "\n".join(map(_summary, docs)) or "none"
    others = "\n".join(_summary(s) for s in samples if s["name"] not in {d["name"] for d in docs}) or "none"
    msg = anthropic.Anthropic().beta.messages.create(
        model=MODEL, max_tokens=16000, system=ROUTE_SYSTEM,
        messages=[{"role": "user", "content": f"Today is {date.today()}.\nFiles in the analysis:\n{files}\n\n"
                                              f"Other sample filings:\n{others}\n\nQuestion:\n{instruction}"}],
        output_config={"format": {"type": "json_schema", "schema": PLAN}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if msg.stop_reason != "end_turn" or not text:  # declined or cut off: no usable plan
        raise RuntimeError(f"Could not read the question (model stopped: {msg.stop_reason}). Rephrase it.")
    return json.loads(text)["requests"]


# Answer a question: find each asked filing (analysis -> samples -> SEC), gather it all, then let Claude answer.
def run_question(docs: list[dict], samples: list[dict], instruction: str, fmt: str, custom_format: str = "") -> dict:
    requests = route(instruction, docs, samples)
    in_analysis, by_name = {d["name"] for d in docs}, {s["name"]: s for s in samples}
    added, fetch = [], []
    for r in requests:
        name = r.pop("found_in")
        if name in in_analysis:  # 1. an upload or a ticked sample: already in the answer's context
            continue
        if name in by_name:  # 2. an unticked sample filing: add it, no SEC call
            added.append(by_name[name])
            in_analysis.add(name)
        else:  # 3. not in any file: fetch from SEC
            fetch.append(r)
    docs = docs + added
    if not docs and not fetch:
        raise RuntimeError("No company found in the question. Name a company (e.g. NVIDIA) or upload a PDF.")
    parts, truncated, data = [], [], None
    if docs:
        text, truncated = build_context(docs)
        parts.append(text)
    if fetch:
        client = SecClient()
        data = [filing_figures(client, **r) for r in fetch]
        parts.append("SEC XBRL data. Values are in whole units (not millions). Each figure's label is the company's "
                     "exact XBRL concept: describe figures by it.\n" + json.dumps(data, indent=1))
    out = answer("\n\n".join(parts), instruction, fmt, custom_format, truncated)
    out.update(route="both" if docs and fetch else "sec" if fetch else "pdf", samples_added=[s["name"] for s in added],
               sec_requests=fetch, sec_data=data)
    return out
