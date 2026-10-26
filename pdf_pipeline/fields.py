"""
# fields.py - keep only the required fields of a filing, with their content

Takes the full extraction of a 10-K or 10-Q (`pipeline.run`), the field list for its form
(`fields` in `config/extraction.json`, made from the team's Information_Extraction_List.xlsx) and the rules that fill
each field (`rules` in the same file: which statement rows, Item, note, tables or sentences), and returns two things:
- **fields**: every field that was found, with its content - statement rows (values per period, page, check status),
  section text (Items, notes, Item 1 sub-sections, audit report parts), tables, or the sentences that mention it.
- **metadata**: found and not found field names, the score (found / listed) and notes on how a field was filled.

How sections are cut (no keyword guessing on every heading):
- An **Item** starts at a line "Item N." / "Item N:" and ends where the next Item starts (in document order), so
  sub-headings inside an Item never end it.
- A **note** starts at a line "Note N: ..." and ends at "Note N+1"; numbers must rise one by one, so a line that only
  mentions "Note 12 of ..." is not taken for a heading.
- **Business model / strategy / competition** use their sub-heading inside Item 1 when there is one (up to the next
  sub-heading of the same or higher level), otherwise the whole Item 1 (noted in the metadata).

No AI and no calculations: a field is only filled with what the filing prints.
"""
from __future__ import annotations

import re
from collections import Counter

from .layout import cover_page
from .statements import FIELDS as STATEMENT_FIELDS, norm

ITEM_LINE = r"(?im)^\s*(?:part\s+i{1,3}\W{0,3})?item\s+{n}\s*[.:—–-]"
NOTE_LINE = re.compile(r"(?im)^\s*note\s+(\d{1,2})\s*[.:—–-]\s*(.*)$")
# notes numbered without the word "Note" ("10. Debt"): a short heading-like line, used only when no "Note N" run exists
NUMBERED_LINE = re.compile(r"(?m)^\s*(\d{1,2})\.\s+([A-Z][A-Za-z'’&,()/ -]{2,80})\s*$")


# ---------------------------------------------------------------------------------------------------- the document
# All page texts as one string, with the offset where each page starts.
def _doc(res: dict) -> tuple[str, list[tuple[int, int]]]:
    parts, starts, at = [], [], 0
    for p in res["pages"]:
        starts.append((at, p["page"]))
        parts.append(p["text"] + "\n")
        at += len(p["text"]) + 1
    return "".join(parts), starts


# Page number of a character offset in the document text.
def _page_at(starts: list[tuple[int, int]], offset: int) -> int:
    return max((p for at, p in starts if at <= offset), default=1)


# Where each Item starts in the document text, in order: [(name, offset)].
def _item_starts(res: dict, text: str, starts: list[tuple[int, int]]) -> list[tuple[str, int]]:
    out = []
    for (at, _), p in zip(starts, res["pages"]):
        pos = at
        for name in p["sections_started"]:
            n = name.split("Item ")[-1]
            m = re.compile(ITEM_LINE.replace("{n}", re.escape(n) + r"\b")).search(text, pos, at + len(p["text"]) + 1)
            out.append((name, m.start() if m else pos))
            pos = m.end() if m else pos
    return out


# Where one Item ("Item 1A", "Part II Item 1") starts and ends in the document text: up to the next Item, or None.
def _item_span(name: str, items: list[tuple[str, int]], text: str) -> tuple[int, int] | None:
    for i, (n, start) in enumerate(items):
        if n == name:
            return start, (items[i + 1][1] if i + 1 < len(items) else len(text))
    return None


# The text of one Item, with its first and last page.
def _item(name: str, items: list[tuple[str, int]], text: str, starts) -> dict | None:
    span = _item_span(name, items, text)
    if not span:
        return None
    start, end = span
    return {"text": text[start:end].strip(), "pages": [_page_at(starts, start), _page_at(starts, max(start, end - 1))]}


# The longest run of numbered headings 1, 2, 3 ... matching `line` from offset `start`: [(number, title, offset)].
# Numbering restarts at 1, so a list of notes printed before the notes themselves loses to the real, later run.
def _longest_run(line: re.Pattern, text: str, start: int = 0) -> list[tuple[int, str, int]]:
    runs, found = [], []
    for m in line.finditer(text, start):
        n = int(m.group(1))
        if n == 1:
            found = [(1, m.group(2).strip(), m.start())]
            runs.append(found)
        elif found and n == found[-1][0] + 1:
            found.append((n, m.group(2).strip(), m.start()))
    return max(reversed(runs), key=len, default=[])  # on a tie the later run


# The notes to the financial statements, in order: [(number, title, start, end)].
def _notes(text: str, items: list[tuple[str, int]]) -> list[tuple[int, str, int, int]]:
    best = _longest_run(NOTE_LINE, text)
    if len(best) < 3:  # no "Note N" headings: numbered headings after the "Notes to ... Financial Statements" title
        head = re.search(r"(?im)^\s*notes to (the )?(condensed )?(consolidated )?financial statements\s*$", text)
        best = _longest_run(NUMBERED_LINE, text, head.end() if head else 0)
    out = []
    for i, (n, title, start) in enumerate(best):
        later_items = [o for _, o in items if o > start]
        end = best[i + 1][2] if i + 1 < len(best) else (min(later_items) if later_items else len(text))
        nl = text.find("\n", start) + 1 or len(text)
        nxt = text[nl:text.find("\n", nl) if text.find("\n", nl) > 0 else len(text)]
        if len(nxt) < 60 and "." not in nxt:  # a heading wrapped onto a short second line, not the note's first sentence
            title += " " + nxt
        out.append((n, title, start, end))
    return out


# The text of the first note whose title matches, with its pages.
def _note(pattern: str, notes, text: str, starts) -> dict | None:
    for n, title, start, end in notes:
        if re.search(pattern, title, re.I):
            return {"note": n, "text": text[start:end].strip(), "pages": [_page_at(starts, start), _page_at(starts, max(start, end - 1))]}
    return None


# ---------------------------------------------------------------------------------------------------- statement rows
# One statement row as output: label, section, page, unit, value per period and its check status, plus when they apply:
# "label_source": "docling" (the label was missing in our reading and taken from Docling's), "docling_values" (what
# Docling read on a "conflict" row) and "layout_flags" (doubts about where a number sits, e.g. far from any column).
def _row(st: dict, row: dict) -> dict:
    values = [{"period": c["header"], "value": v} for c, v in zip(st["columns"], row["raw"]) if v != ""]
    extra = {k: row[k] for k in ("label_source", "docling_values") if row.get(k)}
    if row.get("flags"):
        extra["layout_flags"] = row["flags"]
    return {"statement": st["title"], "label": row["label"], "section": row.get("section") or None, "page": row["page"],
            "unit": st.get("unit"), "values": values, "check": row.get("status"), **extra}


# The row of a standard statement field (statements.FIELDS): the first matcher that finds rows gives its first row.
def _std_rows(res: dict, key: str) -> list[dict]:
    kind, matchers = STATEMENT_FIELDS[key]
    st = res["statements"].get(kind)
    for m in matchers if st else []:
        hits = [r for r in st["rows"] if m(norm(r["label"]), norm(r.get("section") or ""), r)]
        if hits:
            return [_row(st, hits[0])]
    return []


# Every row of one statement whose label matches any of the patterns.
def _label_rows(res: dict, statement: str, labels: list[str]) -> list[dict]:
    st = res["statements"].get(statement)
    return [_row(st, r) for r in st["rows"] if any(re.search(p, norm(r["label"]), re.I) for p in labels)] if st else []


# ---------------------------------------------------------------------------------------------------- tables, sentences
# One table as output (with "layout_flags" when a number sat far from any column or two shared one).
def _table(t: dict) -> dict:
    out = {"title": t.get("title"), "pages": t["pages"], "unit": t.get("unit"), "columns": [c["header"] for c in t["columns"]],
           "rows": [[r["label"], *r["raw"]] for r in t["rows"]]}
    return {**out, "layout_flags": t["flags"]} if t.get("flags") else out


# Labels of a table's rows, lower case.
def _labels(t: dict) -> list[str]:
    return [r["label"].lower() for r in t["rows"]]


# Sentences (with page) that match the pattern; at most `limit`, only from `pages` when given, never from `skip`;
# with `span` (start, end offsets in the document text, and `starts`) only the part of each page inside it.
def _sentences(res: dict, pattern: str, limit: int = 12, pages: set[int] | None = None, skip: set[int] = frozenset(),
               span: tuple[int, int] | None = None, starts=None) -> list[dict]:
    out = []
    for i, p in enumerate(res["pages"]):
        if p["is_toc"] or p["page"] in skip or (pages is not None and p["page"] not in pages):
            continue
        text = p["text"]
        if span:
            at = starts[i][0]
            text = text[max(0, span[0] - at):max(0, span[1] - at)]
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(“\"])", " ".join(text.split())):
            if re.search(pattern, s, re.I) and 20 < len(s) < 1500:
                out.append({"text": s, "page": p["page"]})
                if len(out) >= limit:
                    return out
    return out


# ---------------------------------------------------------------------------------------------------- sections, audit
# The section under the first heading that matches, up to the next heading of the same or higher level, searched
# inside `span` (e.g. Item 1; None = the whole document) and never past its end; else None.
def _heading_part(pattern: str, res: dict, span: tuple[int, int] | None, text: str, starts) -> dict | None:
    item_start, item_end = span or (0, len(text))
    lo, hi = _page_at(starts, item_start), _page_at(starts, max(item_start, item_end - 1))
    heads, pos = [], item_start
    for p in res["pages"]:
        if lo <= p["page"] <= hi:
            for h in p["headings"]:
                at = text.find(h["text"], pos, item_end)
                if at >= 0:
                    heads.append((at, h["text"], h["level"]))
                    pos = at
    for i, (at, head, level) in enumerate(heads):
        if re.search(pattern, head, re.I) and len(head) < 80:
            end = next((a for a, _, lv in heads[i + 1:] if lv <= level), item_end)
            return {"heading": head, "text": text[at:end].strip(), "pages": [_page_at(starts, at), _page_at(starts, max(at, end - 1))]}
    return None


# The audit report on the financial statements (not the one on internal control only): (start, end) offsets, or None.
def _audit_report(text: str) -> tuple[int, int] | None:
    heads = [m.start() for m in re.finditer(r"(?im)^\s*report of independent registered public accounting firm\s*$", text)]
    for i, start in enumerate(heads):
        end = heads[i + 1] if i + 1 < len(heads) else min(len(text), start + 20000)
        if re.search(r"(?im)^\s*opinions? on the (consolidated )?financial statements", text[start:end]):
            return start, end
    return None


# A part of the audit report between two line-start headings.
def _audit_part(text: str, starts, start_pat: str, end_pat: str) -> dict | None:
    span = _audit_report(text)
    if not span:
        return None
    body = text[span[0]:span[1]]
    m = re.search(rf"(?im)^\s*{start_pat}", body)
    if not m:
        return None
    e = re.search(rf"(?im)^\s*{end_pat}", body[m.end():])
    a, b = span[0] + m.start(), span[0] + m.end() + (e.start() if e else len(body) - m.end())
    firm = re.search(r"ernst & young llp|pricewaterhousecoopers llp|kpmg llp|deloitte & touche llp|grant thornton llp|bdo usa", body, re.I)
    return {"text": text[a:b].strip(), "pages": [_page_at(starts, a), _page_at(starts, max(a, b - 1))], "auditor": firm.group(0) if firm else None}


# ---------------------------------------------------------------------------------------------------- the fields
# True when a table passes a "tables" rule: some row matches row_any, every row_all pattern matches a row, at least n
# rows match each min_rows pattern, and the title, row labels or sections mention `mentions`.
def _table_ok(t: dict, rule: dict) -> bool:
    labels = _labels(t)
    has = lambda p: any(re.search(p, l, re.I) for l in labels)  # noqa: E731
    words = " ".join([t.get("title") or ""] + labels + [r.get("section") or "" for r in t["rows"]])
    return (("row_any" not in rule or has(rule["row_any"])) and all(has(p) for p in rule.get("row_all", []))
            and all(sum(bool(re.search(p, l, re.I)) for l in labels) >= n for p, n in rule.get("min_rows", {}).items())
            and ("mentions" not in rule or bool(re.search(rule["mentions"], words, re.I))))


# The content one rule of config/extraction.json finds in the filing (empty or None when it finds nothing).
def _apply(rule: dict, form: str, res: dict, ctx: dict):
    kind = rule["kind"]
    if kind == "rows":
        got = [r for p in rule["parts"] for r in (_std_rows(res, p) if isinstance(p, str) else _label_rows(res, p["statement"], p["labels"]))]
        return list({(r["label"], r["page"], str(r["values"])): r for r in got}.values())  # a row only once
    if kind == "tables":
        got = [_table(t) for t in res["tables"] if _table_ok(t, rule)]
        if "only_rows" in rule:
            got = [dict(t, rows=[r for r in t["rows"] if re.search(rule["only_rows"], r[0], re.I)]) for t in got]
        return got[:rule.get("limit", 3)]
    if kind == "table_rows":
        return [{"table": t["title"], "pages": t["pages"], "row": r} for t in map(_table, res["tables"])
                if re.search(rule.get("title", ""), t["title"] or "", re.I)
                for r in t["rows"] if re.match(rule["pattern"], r[0], re.I)][:rule.get("limit", 6)]
    if kind == "sentences":
        if "section" in rule:  # only the text under that heading, not the rest of its pages (e.g. a table beside it)
            part = _heading_part(rule["section"], res, None, ctx["text"], ctx["starts"])
            if not part:
                return []
            at = ctx["text"].find(part["text"])
            return _sentences(res, rule["pattern"], rule.get("limit", 12), None, ctx["skip"], (at, at + len(part["text"])), ctx["starts"])
        pages = ctx["scopes"][rule["pages"]] if "pages" in rule else None
        return _sentences(res, rule["pattern"], rule.get("limit", 12), pages, ctx["skip"])
    if kind == "item":
        return ctx["item"](rule["name"][form]) if form in rule["name"] else None
    if kind == "note":
        return _note(rule["title"], ctx["notes"], ctx["text"], ctx["starts"])
    if kind == "item1_part":
        span = ctx["item_span"]("Item 1")
        return _heading_part(rule["heading"], res, span, ctx["text"], ctx["starts"]) if span else None
    if kind == "section":
        return _heading_part(rule["heading"], res, None, ctx["text"], ctx["starts"])
    if kind == "audit_part":
        return _audit_part(ctx["text"], ctx["starts"], rule["start"], rule["end"])
    if kind == "not_in_filing":
        return None
    raise ValueError(f"unknown rule kind {kind!r} in extraction.json")


# Content for one field of the list, or (None, reason): its rules are tried in order and the first that finds
# something wins; a rule's "note" goes to the metadata when that rule is used. A rule with "forms" applies only to
# those document types.
def _fill(name: str, form: str, res: dict, ctx: dict, rules: dict):
    field_rules = rules["rules"].get(name.lower())
    if field_rules is None:
        return None, "no extraction rule for this field"
    field_rules = [r for r in field_rules if form in r.get("forms", [form])]
    for rule in field_rules:
        got = _apply(rule, form, res, ctx)
        if got:
            return got, rule.get("note")
    return None, next((r["note"] for r in field_rules if r["kind"] == "not_in_filing"), None)


KINDS = {"rows", "tables", "table_rows", "sentences", "item", "note", "item1_part", "section", "audit_part", "not_in_filing"}


# Problems in the two config files, as readable lines (empty = fine): checked once before any PDF is read, so a typo
# stops the run instead of failing every PDF. Fields without a rule are listed too; they are reported as not found.
def check_config(field_list: dict, rules: dict) -> tuple[list[str], list[str]]:
    errors, missing = [], []
    types = rules.get("document_types", {}).get("types", [])
    names = {d["name"] for d in types}
    for d in types:
        for lst in d.get("field_lists", []):
            if not isinstance(field_list.get(lst), list):
                errors.append(f"extraction.json: document type {d['name']!r} uses field list {lst!r}, which has no 'fields' entry")
                continue
            missing += [f"{lst}: {f['field']}" for f in field_list[lst] if f["field"].lower() not in rules.get("rules", {})]
        for k in ("cover", "names"):
            try:
                re.compile(d.get(k) or "")
            except re.error as e:
                errors.append(f"extraction.json: document type {d['name']!r} has a broken {k} pattern: {e}")
    for name, pat in rules.get("applies_if", {}).items():
        if name not in rules.get("rules", {}):
            errors.append(f"extraction.json: applies_if names {name!r}, which has no rules")
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"extraction.json: applies_if pattern for {name!r} is broken: {e}")
    for name, field_rules in rules.get("rules", {}).items():
        for r in field_rules:
            for f in set(r.get("forms", [])) - names:
                errors.append(f"extraction.json: {name!r} names unknown document type {f!r}")
            if r.get("kind") not in KINDS:
                errors.append(f"extraction.json: {name!r} has unknown rule kind {r.get('kind')!r}")
            for part in r.get("parts", []):
                if isinstance(part, str) and part not in STATEMENT_FIELDS:
                    errors.append(f"extraction.json: {name!r} uses unknown statement row {part!r}")
            if r.get("pages") and r["pages"] not in rules.get("page_scopes", {}):
                errors.append(f"extraction.json: {name!r} uses unknown page scope {r['pages']!r}")
            for pat in [r.get(k) for k in ("pattern", "row_any", "only_rows", "mentions", "title", "heading", "section", "start", "end")] \
                    + r.get("row_all", []) + list(r.get("min_rows", {})) + [l for p in r.get("parts", []) if isinstance(p, dict) for l in p["labels"]]:
                try:
                    re.compile(pat or "")
                except re.error as e:
                    errors.append(f"extraction.json: {name!r} has a broken pattern {pat!r}: {e}")
    return errors, missing


# Not-found fields that cannot be in this document, with the reason: {field: reason}. A field whose only rules for this
# document type are not_in_filing needs other documents; a field in config "applies_if" only applies when its pattern
# is on the document's first two pages (e.g. GE Vernova's own segments). The agent fetches these elsewhere.
def _not_applicable(missing: list[str], form: str, res: dict, rules: dict) -> dict[str, str]:
    first = " ".join(p["text"] for p in res["pages"][:2])
    out = {}
    for key in missing:
        name = re.sub(r" \(row \d+\)$", "", key).lower()
        own = [r for r in rules["rules"].get(name, []) if form in r.get("forms", [form])]
        if own and all(r["kind"] == "not_in_filing" for r in own):
            out[key] = own[0].get("note") or "not in a single document"
        elif name in rules.get("applies_if", {}) and not re.search(rules["applies_if"][name], first, re.I):
            out[key] = f"only for documents that mention {rules['applies_if'][name]!r} on their first pages"
    return out


# Pages of a named scope (config page_scopes): the union of the notes, Items and headed sections it lists; empty when
# none is found.
def _scope(spec: list[dict], form: str, notes, text: str, starts, item, res: dict) -> set[int]:
    pages: set[int] = set()
    for s in spec:
        part = (_note(s["note"], notes, text, starts) if "note" in s else item(s["item"][form]) if form in s.get("item", {})
                else _heading_part(s["heading"], res, None, text, starts) if "heading" in s else None)
        if part:
            pages |= set(range(part["pages"][0], part["pages"][1] + 1))
    return pages


# The output JSON (fields found) and the metadata for one filing, given its full extraction, the field list
# and the whole config/extraction.json (document types, rules ...).
def build(res: dict, field_list: dict, rules: dict) -> tuple[dict | None, dict]:
    form = res["doc_type"]
    doc = {"file": res["source_file"], "form": form, "pages": res["page_count"]}
    types = rules["document_types"]
    lists = [l for d in types["types"] if d["name"] == form for l in d.get("field_lists", []) if isinstance(field_list.get(l), list)]
    if not lists:
        blank = len(res["quality"]["blank_pages"]) == res["page_count"]
        if res["quality"]["damaged"] or blank:  # say why there is nothing, instead of calling it the wrong form
            why = "the file is damaged (cut off or corrupt)" if res["quality"]["damaged"] else "every page is blank"
            return None, {**doc, "status": f"failed: no readable content: {why}"}
        return None, {**doc, "status": f"skipped: no field list for document type {form!r}"}
    wanted = list({f["field"].lower(): f for l in lists for f in field_list[l]}.values())  # e.g. a transcript: call + call transcript
    text, starts = _doc(res)
    items = _item_starts(res, text, starts)
    notes = _notes(text, items)
    item = lambda n: _item(n, items, text, starts)  # noqa: E731
    # page sets: an empty set means "nothing to search" (e.g. no debt note), never "search everywhere"
    scopes = {k: _scope(v, form, notes, text, starts, item, res) for k, v in rules.get("page_scopes", {}).items()}
    ctx = {"text": text, "starts": starts, "notes": notes, "item": item, "item_span": lambda n: _item_span(n, items, text),
           "scopes": scopes, "skip": scopes.get(rules.get("skip_pages"), set())}
    at = (cover_page([p["text"] for p in res["pages"]], types) or (0, None))[0]  # the SEC cover page (page 1 unless a letter comes first)
    cover = " ".join(res["pages"][at]["text"].split()) if res["pages"] else ""
    # the name sits between the file number and "(Exact name of registrant ...)"; some covers print no such label
    company = re.search(r"file (?:number|no\.)\W*[\d-]+\.?\s+([^()]{2,80}?)\s*\(\s*exact name of registrant", cover, re.I) or \
        re.search(r"file (?:number|no\.)\W*[\d-]+\.?\s+([A-Z][A-Za-z0-9.,&' -]{1,60}?(?:INC\.?|CORPORATION|CORP\.?|COMPANY|CO\.?|Inc\.|Corporation|Company))\b", cover, re.I)
    period = re.search(r"(?:fiscal year|quarterly period|period) ended\s+([A-Za-z]+ \d{1,2},? \d{4})", cover, re.I)
    doc.update(company=company.group(1).strip(" _-") if company else None, period_end=period.group(1) if period else None)
    fields, found, missing, notes_out = {}, [], [], {}
    for f in wanted:
        content, note = _fill(f["field"], form, res, ctx, rules)
        key = f["field"] if f["field"] not in fields else f"{f['field']} (row {f['row']})"
        if note:
            notes_out[key] = note
        if content:
            fields[key] = {"row": f["row"], "group": f["group"], "content": content}
            found.append(key)
        else:
            missing.append(key)
    total = len(wanted)
    na = _not_applicable(missing, form, res, rules)
    applicable = total - len(na)
    checks = Counter(r.get("check") for f in fields.values() if isinstance(f["content"], list)
                     for r in f["content"] if isinstance(r, dict) and "check" in r)
    meta = {**doc, "score": f"{len(found)}/{total}", "score_pct": round(100 * len(found) / total, 1) if total else None,
            "score_applicable": f"{len(found)}/{applicable}",
            "score_applicable_pct": round(100 * len(found) / applicable, 1) if applicable else None,
            "found": found, "not_found": missing, "not_applicable": na, "notes": notes_out,
            "statement_rows": {k or "none": v for k, v in checks.items()}}  # how many output rows are verified, confirmed ...
    return {"document": doc, "fields": fields}, meta
