"""
# claude.py - answer a question over the extracted documents with Claude (Anthropic API)

The documents are sent as page text plus the tables finx found (with periods, units and check status), so the model
reads numbers that are already in the right columns. The only AI step in the product; extraction itself uses none.
The document text is marked for prompt caching, so follow-up questions on the same documents cost less.
`answer()` is shared with the SEC path (`sec_path.py`), which passes SEC figures as JSON instead of PDF pages.

Settings (backend/.env): ANTHROPIC_API_KEY (required), CLAUDE_MODEL, MAX_CONTEXT_CHARS, CLAUDE_MAX_TOKENS.
"""
import json
import logging
import os
import re
import time

import anthropic

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "1200000"))  # about 300k tokens; a long 10-K fits whole
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "64000"))  # includes thinking; streamed, so no HTTP timeout

FORMAT_HINTS = {
    "table": (
        "Return ONLY a JSON object: {\"columns\": [..], \"rows\": [[..], ..]}. "
        "Add a final column named \"Source\" with 'document, p.N' for each row. No prose, no code fences."
    ),
    "paragraph": "Write clear prose paragraphs. Cite sources inline as (document, p.N).",
    "bullets": "Write a bulleted list. Cite sources inline as (document, p.N).",
    "keyvalue": "Write one 'Field: Value' per line. Cite sources as (document, p.N) after each value.",
    "json": "Return ONLY valid JSON (no code fences). Include a \"source\" field with document and page where relevant.",
}

SYSTEM = (
    "You extract information from documents. Use ONLY the provided documents. "
    "Never guess: if something is not in the documents, write 'Not found'. When asked to calculate, show the formula "
    "and the printed numbers used. Page markers look like [Page N]. Always cite document name and page "
    "(for SEC data: company, form and filing date). "
    "Tables are given with their unit; copy numbers exactly as printed and state the unit. "
    "A table's 'Check' column says whether a row's numbers passed an arithmetic check (verified), "
    "matched a second reading (confirmed), or could not be checked (unverified)."
)

log = logging.getLogger("uvicorn.error")


# All documents as one text with <document> and [Page N] markers, cut at MAX_CHARS (returns text, was_cut).
def build_context(docs: list[dict]) -> tuple[str, bool]:
    parts, used, truncated = [], 0, False
    for d in docs:
        parts.append(f"<document name=\"{d['name']}\">")
        for p in d["pages"]:
            chunk = f"\n[Page {p['page']}]\n{p['markdown']}\n"
            if used + len(chunk) > MAX_CHARS:
                truncated = True
                break
            parts.append(chunk)
            used += len(chunk)
        parts.append("</document>")
    return "".join(parts), truncated


# Answer a question over uploaded PDFs.
def run_extraction(docs: list[dict], instruction: str, fmt: str, custom_format: str = "") -> dict:
    context, truncated = build_context(docs)
    return answer(context, instruction, fmt, custom_format, truncated)


# Ask Claude over a context text; returns {format, text, truncated, output_truncated, timing, data?, warning?}.
def answer(context: str, instruction: str, fmt: str, custom_format: str = "", truncated: bool = False) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server (add it to backend/.env).")
    hint = FORMAT_HINTS.get(fmt) or f"Format the answer as: {custom_format or fmt}"
    content = [
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},  # same documents -> cache hit
        {"type": "text", "text": f"Extract the following information:\n{instruction}\n\nOutput format: {hint}"},
    ]
    t0 = time.perf_counter()
    # Server-side fallback: if a safety classifier declines, the API retries on Anthropic's recommended model.
    with anthropic.Anthropic().beta.messages.stream(
        model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": content}],
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    llm_s = time.perf_counter() - t0
    u = msg.usage
    log.info("extract: model=%s context_chars=%d input_tokens=%d cache_read=%d output_tokens=%d llm_time=%.1fs",
             msg.model, len(context), u.input_tokens, u.cache_read_input_tokens or 0, u.output_tokens, llm_s)
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    cut_off = msg.stop_reason == "max_tokens"
    out = {"format": fmt, "text": text, "truncated": truncated, "output_truncated": cut_off,
           "timing": {"llm_seconds": round(llm_s, 2), "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "cache_read_tokens": u.cache_read_input_tokens or 0, "model": msg.model}}
    if msg.stop_reason == "refusal":
        out["warning"] = "Claude declined to answer this request."
    elif cut_off:
        out["warning"] = f"The answer hit the {MAX_TOKENS}-token output limit and is incomplete."
    if fmt in ("table", "json"):
        if cut_off:
            # Cut-off JSON cannot parse; say so instead of silently returning data=None.
            out["data"] = None
            out["warning"] += " The JSON was cut off, so only the raw text is returned."
        else:
            cleaned = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            try:
                out["data"] = json.loads(cleaned)
            except json.JSONDecodeError:
                out["data"] = None
    return out
