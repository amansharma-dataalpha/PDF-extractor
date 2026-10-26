"""
# layout.py - from the words on a page to lines, rows, tables and a page index

Tables (words -> lines -> rows -> tables):
1. **Lines**: words at the same height form one line.
2. **Rows**: each line is split into a text label on the left and numbers on the right
   ("Total revenue ... 331,839 281,724" -> label "Total revenue", values 331839 and 281724).
   Brackets mean negative, a dash means zero.
3. **Tables**: consecutive rows with numbers form a table. Numbers whose right edges line up form a column.
   The text above the columns gives each column's period ("2026", "Three months ended June 30");
   text such as "(In millions)" gives the unit.
Along the way it drops printed page numbers, joins labels that wrap onto a second line and remembers the section
heading above each row ("Current assets:").

Page index (`build_index`), for every page:
- the page text (without repeating headers/footers and page numbers),
- the headings (by font size, bold or capitals),
- which section the page is in ("Item 7", "Part I Item 2" ...) and which sections start on it,
- whether it is the table of contents, and the printed page number.
It also decides the document type (the "document_types" of config/extraction.json: 10-K, 10-Q, press release, proxy
statement ...; "other" when none fits): the SEC cover sentence on one of the first pages, else the type's name pattern on
the first two pages.

Pure geometry and text rules - no AI models.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .read import PageData, Word
CURRENCY = {"$", "€", "£", "US$", "¥", "C$", "A$"}  # $ euro pound yen ... signs printed on their own
DASHES = {"—", "–", "-", "--", "−", "—%"}  # a dash in a number column means zero
NUMBER = re.compile(r"^\(?(?:[-−–]?[$€£]?|[$€£][-−–])\(?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\)?%?\)?-?$|^\(?(?:[-−–]?[$€£]?|[$€£][-−–])\.\d+\)?%?\)?-?$")  # also -1,234 / –1,234 / 1,234- / $-5
YEAR_CELL = re.compile(r"^(19|20)\d\d$")  # a cell that is just a year, e.g. "2026"
YEAR = re.compile(r"(?<!\d)(19|20)\d\d(?!\d)")  # a year anywhere, also in "FY2026"
UNIT = re.compile(
    r"\bin\s+(thousands|millions|billions)\b"                                # (In millions) / $ in thousands
    r"|\((?:[A-Za-z$€£]{1,4}\s+)?(thousands|millions|billions)\)"  # (GBP thousands) / (thousands)
    r"|\b(?:USD|GBP|EUR|JPY|CAD|AUD|CHF|NZD|INR|CNY|US\$)\s+(thousands|millions|billions)\b"  # USD millions
    r"|[$€£]\s?(thousands|millions|billions)\b",
    re.I,
)
SCALE = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
MONTHS = re.compile(r"\b(three|six|nine|twelve)\s+months\b|\b(year|fiscal year|quarter)s?\s+ended\b", re.I)
DATE = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b", re.I)
CHANGE = re.compile(r"(%|percent|change|v\s?%|year-on-year|yoy|bps|variance)", re.I)  # a "% change" column, not a period


# A line of text: words that sit at the same height.
@dataclass
class Line:
    words: list[Word]
    page: int

    # The line's text, its edges (top, bottom, left x0, right x1) and its usual font size, all taken from its words.
    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def top(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def size(self) -> float:
        return statistics.median(w.size for w in self.words)

    @property
    def bold(self) -> bool:  # at least 80% of the letters are bold
        return sum(len(w.text) for w in self.words if w.bold) >= 0.8 * sum(len(w.text) for w in self.words)


# One number on a line: its position, the text as printed, and its value.
@dataclass
class Cell:
    x0: float
    x1: float  # right edge of the digits (a closing bracket or % sign is not counted): columns line up on this
    raw: str
    value: float | None


# A line split into label + numbers. kind: "data" (has numbers), "header" (only years), "label" (only text), "empty".
@dataclass
class Row:
    page: int
    top: float
    bottom: float
    x0: float  # left edge of the label (or of the first number when there is no label)
    label: str
    cells: list[Cell]
    kind: str
    line: Line | None = field(default=None, repr=False)


# One column of a table: where its numbers end, its heading text and the period it covers.
@dataclass
class Column:
    x1: float
    left: float
    header: str = ""
    period: dict = field(default_factory=dict)


# One table row: label, one value per column (None = empty), the printed text of each value, indent and section.
@dataclass
class TRow:
    label: str
    values: list[float | None]
    raw: list[str]
    indent: int
    page: int
    section: str = ""  # the heading above it, e.g. "Current assets:"
    flags: list[str] = field(default_factory=list)  # doubts about this row's layout, e.g. a number far from any column


# A table found on a page (or spread over consecutive pages once statements are merged).
@dataclass
class Table:
    page: int
    top: float
    bottom: float
    title: str
    unit: str | None
    scale: float | None
    columns: list[Column]
    rows: list[TRow]
    header_lines: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    kind: str | None = None  # income_statement, balance_sheet, ... (set when it is recognised as a statement)
    flags: list[str] = field(default_factory=list)

    # The table as plain JSON-ready data.
    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "pages": self.pages or [self.page], "unit": self.unit, "scale": self.scale,
                "columns": [{"header": c.header, "period": c.period} for c in self.columns],
                "rows": [{"label": r.label, "section": r.section, "indent": r.indent, "page": r.page, "values": r.values, "raw": r.raw,
                          "flags": r.flags}
                         for r in self.rows],
                "flags": self.flags}


# ---------------------------------------------------------------------------------------------------- words -> rows
# Group a page's words into lines: a word joins the current line if its centre is at (nearly) the same height.
def group_lines(page: PageData) -> list[Line]:
    words = sorted(page.words, key=lambda w: (w.yc, w.x0))
    if not words:
        return []
    heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    tol = max(1.5, 0.4 * (statistics.median(heights) if heights else 8))
    groups, ys = [[words[0]]], [words[0].yc]
    for w in words[1:]:
        if abs(w.yc - ys[-1]) <= tol:
            groups[-1].append(w)
            ys[-1] = statistics.fmean(x.yc for x in groups[-1])
        else:
            groups.append([w])
            ys.append(w.yc)
    return [Line(sorted(g, key=lambda w: w.x0), page.no) for g in groups]


# Read a printed number: "(4,901)" -> -4901, "18.00" -> 18.0, "12.5%" -> 12.5, "$64,696" -> 64696; anything else -> None.
def parse_number(tok: str) -> float | None:
    t = tok.strip()
    if not NUMBER.match(t):
        return None
    v = float(re.sub(r"[^\d.]", "", t))
    return -v if "(" in t or t.lstrip("($€£").startswith(("-", "−", "–")) or t.endswith("-") else v


# True for a number or a dash (a dash in a number column means zero).
def is_numeric_token(t: str) -> bool:
    return t in DASHES or parse_number(t) is not None


# Split a line into its text label and the numbers on its right; also decide the row's kind.
def split_row(line: Line) -> Row:
    toks: list[list] = []  # [x0, x1, text]
    for w in line.words:
        t = w.text
        if t in CURRENCY:
            continue
        # a stray ")" "%" "bps" belongs to the number before it: "(39) %", "(4,901 )"
        if t in (")", "%", ")%", "%)", "pts", "bps") and toks and is_numeric_token(toks[-1][2].rstrip(")%")):
            if t not in ("pts", "bps"):
                toks[-1][2] += t
                toks[-1][1] = w.x1
            continue
        if len(t) > 1 and t[0] in "$€£" and is_numeric_token(t[1:]):
            t = t[1:]  # "$1,234" glued to its currency sign
        if (toks and toks[-1][2] in ("-", "−", "–") and parse_number(t) is not None
                and not t.startswith(("-", "(")) and w.x0 - toks[-1][1] < 2.0
                and not (len(toks) > 1 and is_numeric_token(toks[-2][2]))):  # "3 - 40" is a range, not -40
            toks[-1][1], toks[-1][2] = w.x1, "-" + t  # a minus sign printed as its own piece, touching the number
            continue
        toks.append([w.x0, w.x1, t])
    n = i = len(toks)
    while i > 0 and is_numeric_token(toks[i - 1][2]):
        i -= 1
    while 0 < i < n and toks[i][0] - toks[i - 1][1] < 5.0:
        i += 1  # numbers that touch the words belong to the label ("Level 1", "Note 4", "5,000 shares")
    label = " ".join(t[2] for t in toks[:i]).strip()
    cells = []
    for x0, x1, t in toks[i:]:
        suffix = len(t) - len(t.rstrip(")%"))
        cells.append(Cell(x0, x1 - suffix * (x1 - x0) / max(len(t), 1), t, 0.0 if t in DASHES else parse_number(t)))
    header_label = len(label) < 60 or re.search(r"\b(in (thousands|millions|billions)|years? ended|months ended|as of|fiscal)\b", label, re.I)
    if cells and all(YEAR_CELL.match(c.raw) for c in cells) and header_label:
        kind = "header"
    else:
        kind = "data" if cells else "label" if label else "empty"
    return Row(line.page, line.top, line.bottom, toks[0][0] if toks else line.x0, label, cells, kind, line)


# Drop printed page numbers: a lone small number in a margin, or centred among the first/last rows of the page.
def strip_furniture(rows: list[Row], width: float, height: float) -> list[Row]:
    out = []
    for i, r in enumerate(rows):
        if r.kind in ("data", "header") and not r.label and len(r.cells) == 1 and r.cells[0].raw.isdigit() and len(r.cells[0].raw) <= 4:
            c = r.cells[0]
            centred = abs((c.x0 + c.x1) / 2 - width / 2) < 0.12 * width
            if r.top > 0.9 * height or r.bottom < 0.07 * height or (centred and (i < 4 or i >= len(rows) - 2)):
                continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------------------------------- units and periods
# Find the unit ("millions" -> 1e6) in the first text that states one.
def find_unit(texts: list[str]) -> tuple[str | None, float | None]:
    for t in texts:
        m = UNIT.search(t)
        if m:
            word = next(g for g in m.groups() if g).lower()
            return word, SCALE[word]
    return None, None


# Read a column heading into a period: year, number of months, date, or "this is a % change column".
def parse_period(header: str) -> dict:
    p: dict = {}
    yrs = re.findall(r"(?<!\d)(?:19|20)\d\d(?!\d)", header)
    if yrs:
        p["year"] = int(yrs[-1])
    m = MONTHS.search(header)
    if m:
        word = (m.group(1) or m.group(2) or "").lower()
        p["months"] = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "year": 12, "fiscal year": 12, "quarter": 3}.get(word)
    d = DATE.search(header)
    if d:
        p["date"] = d.group(0)
    if CHANGE.search(header) and "year" not in p:
        p["change"] = True
    return p


# ---------------------------------------------------------------------------------------------------- rows -> tables
# Columns: group the right edges of all numbers; edges within 9 points of each other are one column.
def _cluster_columns(rows: list[Row]) -> list[Column]:
    edges = sorted((c.x1, c.x0) for r in rows for c in r.cells)
    if not edges:
        return []
    groups = [[edges[0]]]
    for e in edges[1:]:
        if e[0] - groups[-1][-1][0] <= 9:
            groups[-1].append(e)
        else:
            groups.append([e])
    make = lambda gs: [Column(statistics.median(x for x, _ in g), min(x0 for _, x0 in g)) for g in gs]
    return make([g for g in groups if len(g) >= (1 if len(rows) < 4 else 2)]) or make(groups)


# The column whose right edge is closest to x1.
def _nearest(cols: list[Column], x1: float) -> int:
    return min(range(len(cols)), key=lambda i: abs(cols[i].x1 - x1))


# Column headings: heading phrases (words split at big gaps) go to every column they sit above.
def _header_text(cols: list[Column], header_rows: list[Row]) -> list[str]:
    parts: list[list[str]] = [[] for _ in cols]
    for r in header_rows:
        phrases: list[list] = []
        for w in (r.line.words if r.line else []):
            if phrases and w.x0 - phrases[-1][1] < 6:
                phrases[-1][1] = w.x1
                phrases[-1][2] += " " + w.text
            else:
                phrases.append([w.x0, w.x1, w.text])
        for x0, x1, text in phrases:
            if x1 < cols[0].left - 25:
                continue  # text left of the numbers, such as "Year Ended June 30,", is not a column heading
            for i in [i for i, c in enumerate(cols) if x0 <= c.x1 + 4 and x1 >= c.left - 4] or [_nearest(cols, x1)]:
                parts[i].append(text)
    return [" ".join(p) for p in parts]


# Rows that are column headings: walk up from the first number row until a units line, a title or prose.
def _header_rows(context: list[Row], cols: list[Column]) -> list[Row]:
    header_rows: list[Row] = []
    for r in reversed(context):
        if not r.line:
            continue
        if r.kind == "header":
            header_rows.insert(0, r)  # a year row (it may carry the units text too): keep it and keep going
            continue
        if UNIT.search(r.line.text) or len(r.line.words) > 14:
            # "(In millions) June 30, 2026 December 31, 2025": units and dates on the same line
            if YEAR.search(r.line.text) and r.line.x1 >= cols[0].left - 25 and len(r.line.words) <= 20:
                header_rows.insert(0, r)
            break
        in_cols = r.line.x1 >= cols[0].left - 25
        over_cols = r.line.x0 >= cols[0].left - 40  # column headings sit above the numbers, titles start at the margin
        if in_cols and r.kind in ("label", "data") and (over_cols or len(r.line.words) <= 4):
            header_rows.insert(0, r)
        elif not (r.kind == "label" and len(r.line.words) <= 6):
            break  # short text such as "Year Ended June 30," is skipped; anything else ends the headings
    return header_rows


# Build one table from its rows and the rows just above it (title, units, column headings).
def build_table(rows: list[Row], context: list[Row], page: int) -> Table | None:
    data = [r for r in rows if r.kind == "data"]
    if len(data) < 2:
        return None
    cols = _cluster_columns(data)
    if not cols:
        return None
    header_rows = _header_rows(context, cols)
    stub_text = [r.label for r in header_rows if r.label] or [r.label for r in context[-3:] if r.label]
    for c, h in zip(cols, _header_text(cols, header_rows)):
        c.header = h
        c.period = parse_period(h + " " + " ".join(s for s in stub_text[-3:] if not YEAR.search(s)))
    unit, scale = find_unit([r.line.text for r in context if r.line] + [r.label for r in rows[:3]])
    at = next((i for i in range(len(context) - 1, -1, -1)
               if context[i].kind == "label" and len(context[i].label) > 3 and not UNIT.search(context[i].label)
               and (not YEAR.search(context[i].label) or context[i].label.endswith(":"))  # a lead-in may name years
               and not re.search(r"\b(years?|months|quarters?|weeks) ended\b|^as of\b", context[i].label, re.I)), None)
    title = context[at].label if at is not None else ""
    # a lead-in that wrapped ("Revenue, classified by ... customers were" / "located, was as follows:") keeps its first
    # line; only a lead-in ending in ":" is joined, never prose, and never past the end of the sentence before it
    lead_in = title.endswith(":")
    while (lead_in and at and title[:1].islower() and context[at - 1].kind == "label"
           and not context[at - 1].label.endswith(".") and context[at].top - context[at - 1].bottom < 6):
        at -= 1
        title = context[at].label + " " + title
    if lead_in:
        title = re.split(r"(?<=\.)\s+(?=[A-Z])", title)[-1]  # only the lead-in sentence, not the end of the one before
    rows = join_wrapped(rows)
    base = min([r.x0 for r in rows if r.label] or [0])
    out: list[TRow] = []
    flags: list[str] = []
    section, section_x0 = "", 0.0
    pending: Row | None = None  # a text-only row: either a section heading or the start of the next label
    for r in rows:
        if r.kind == "label":
            if pending is not None:
                section, section_x0 = pending.label, pending.x0
            pending = r
            continue
        if r.kind != "data":
            continue
        label = r.label
        if pending is not None:
            # a pending text row is a heading, unless the number row continues it in lower case ("assets, and other")
            if not pending.label.endswith(":") and r.label and r.label[0].islower() and not pending.label.isupper():
                label = pending.label + " " + r.label
            else:
                section, section_x0 = pending.label, pending.x0
            pending = None
        if section and r.label and r.x0 <= section_x0 + 1 and not r.label.lower().startswith("total"):
            section = ""  # a heading only covers rows indented under it (and its own "Total ..." line)
        values: list[float | None] = [None] * len(cols)
        raw = [""] * len(cols)
        row_flags = []
        for c in r.cells:
            i = _nearest(cols, c.x1)
            if abs(cols[i].x1 - c.x1) > 14:
                row_flags.append(f"cell '{c.raw}' far from any column")
            if raw[i]:
                row_flags.append(f"two cells in one column: '{raw[i]}' was replaced by '{c.raw}'")
            values[i], raw[i] = c.value, c.raw
        flags += [f"p{r.page}: {f} on row '{label[:40]}'" for f in row_flags]
        out.append(TRow(label, values, raw, int(round((r.x0 - base) / 7)) if r.label else 0, r.page, section, row_flags))
    return Table(page, rows[0].top, rows[-1].bottom, title, unit, scale, cols, out,
                 [r.line.text for r in header_rows if r.line], [page], None, flags)


# Join a label that wraps onto a second line: the lines touch (no gap), the first does not end with ":", and the
# second starts in lower case or with a digit, or the first ends with a joining word ("and", "of" ...) or a comma.
def join_wrapped(rows: list[Row]) -> list[Row]:
    out: list[Row] = []
    i = 0
    while i < len(rows):
        r = rows[i]
        while (r.kind == "label" and not r.label.endswith(":") and i + 1 < len(rows)
               and rows[i + 1].kind in ("label", "data") and rows[i + 1].label
               and rows[i + 1].top - r.bottom < 0.3 * max(r.bottom - r.top, 6.0)
               and (rows[i + 1].label[0].islower() or rows[i + 1].label[0].isdigit()
                    or re.search(r"(\b(and|or|of|to|for|the|in|on|by|with|from|as|at|net)|[,(\-–/])$", r.label, re.I))):
            nxt = rows[i + 1]
            r = Row(nxt.page, r.top, nxt.bottom, min(r.x0, nxt.x0), r.label + " " + nxt.label, nxt.cells, nxt.kind, nxt.line)
            i += 1
        out.append(r)
        i += 1
    return out


# Title, units and period lines stay above a table as context; they are never pulled into it as a heading.
def _is_context_line(label: str) -> bool:
    return bool(UNIT.search(label) or YEAR.search(label) or re.search(
        r"\bstatements?\b|\bbalance sheets?\b|\b(years?|months|quarters?) ended\b|\(unaudited\)|\bas follows:$", label, re.I)
        or (label[:1].islower() and label.endswith(":")))  # the end of a lead-in sentence ("located, was as follows:")


# Split one page's rows into tables: a table is a run of number rows; prose, a big gap or an empty line ends it.
def page_tables(rows: list[Row]) -> list[Table]:
    tables: list[Table] = []
    i, n, last_end = 0, len(rows), 0
    while i < n:
        if rows[i].kind != "data":
            i += 1
            continue
        start = i
        while (start > last_end and rows[start - 1].kind == "label" and len(rows[start - 1].label) < 90
               and rows[i].top - rows[start - 1].bottom < 40 and not _is_context_line(rows[start - 1].label)):
            start -= 1  # pull section headings like "Current assets:" into the table
        j = i
        lh = max(8.0, rows[i].bottom - rows[i].top)
        while j + 1 < n:
            nxt = rows[j + 1]
            if nxt.top - rows[j].bottom > 3.2 * lh or nxt.kind == "empty":
                break
            if nxt.kind == "label" and len(nxt.label) > 110:
                # prose ends the table; one long label followed straight away by numbers does not
                after = rows[j + 2] if j + 2 < n else None
                if not (after and after.kind == "data" and after.top - nxt.bottom < 1.5 * lh):
                    break
            j += 1
        while j > i and rows[j].kind == "label":
            j -= 1  # trailing text rows belong to whatever follows
        t = build_table(rows[start:j + 1], rows[max(last_end, start - 8):start], rows[i].page)
        if t:
            tables.append(t)
        last_end = i = j + 1
    return tables


# ---------------------------------------------------------------------------------------------------- page index
ITEM = re.compile(r"^(?:PART\s+(?P<part>I{1,3}|IV)\b[.,:\s—–-]*)?ITEM\s+(?P<item>\d{1,2}[A-C]?)\s*[.:—–-]?\s*(?P<title>.*)$", re.I)
PART = re.compile(r"^PART\s+(I{1,3}|IV)\b\.?\s*(?:[—–-]\s*)?(.*)$", re.I)
NOTE = re.compile(r"^NOTE\s+(\d{1,2}|[A-Z])\b\s*[.:—–-]?\s*(.*)$", re.I)
ITEM_ORDER_10K = ["1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16"]


# The SEC cover page among the first pages: (page index from 0, type name), or None. `types` is "document_types" of
# config/extraction.json; a type's "cover" is the fixed sentence only its SEC cover page prints. A cover can sit behind
# a letter or glossy pages, so the first "cover_pages" pages are searched.
def cover_page(pages_text: list[str], types: dict) -> tuple[int, str] | None:
    for i, page in enumerate(pages_text[:types["cover_pages"]]):
        t = " ".join(page.split())
        hits = [(m.start(), d["name"]) for d in types["types"] if d.get("cover") and (m := re.search(d["cover"], t, re.I))]
        if hits:
            return i, min(hits)[1]
    return None


# Decide the document type from its page texts: the SEC cover sentence on one of the first pages, else the first type
# (in config order) whose "names" pattern is on the first two pages ("see our Form 10-K" in a 10-Q never wins over its
# cover). "other" when nothing matches.
def doc_type(pages_text: list[str], types: dict) -> str:
    cover = cover_page(pages_text, types)
    if cover:
        return cover[1]
    t = " ".join(pages_text[:2])
    return next((d["name"] for d in types["types"] if d.get("names") and re.search(d["names"], t, re.I)), "other")


def _shape(text: str) -> str:  # text with every number replaced by "#", so "Page 4" and "Page 5" look the same
    return re.sub(r"\d+", "#", text.lower()).strip()


def _in_band(line: Line, height: float) -> bool:  # in the top or bottom 9% of the page
    return line.bottom < 0.09 * height or line.top > 0.91 * height


# Running headers/footers: text in the top/bottom band that repeats on at least a quarter of the pages.
def furniture(pages_lines: list[list[Line]], heights: list[float]) -> set[str]:
    if len(pages_lines) < 4:
        return set()
    seen = Counter(t for lines, h in zip(pages_lines, heights) for t in {_shape(l.text) for l in lines if _in_band(l, h)})
    return {t for t, c in seen.items() if c >= max(3, 0.25 * len(pages_lines)) and t not in ("#", "")}


# The most common font size in the document (weighted by letters) = normal body text.
def body_size(pages_lines: list[list[Line]]) -> float:
    sizes: Counter = Counter()
    for lines in pages_lines:
        for l in lines:
            for w in l.words:
                sizes[round(w.size * 2) / 2] += len(w.text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


# ('9B', 'OTHER INFORMATION') if the line starts a 10-K/10-Q Item, else None.
# Accepts "ITEM 1. BUSINESS." and run-in headings ("ITEM 2. PROPERTIES. GE Vernova is ..."); rejects a sentence that
# only happens to start with "Item 1A." (a real heading is upper-case, bold or bigger) and lists like "ITEM 10, 11 and 14."
def item_heading(line: Line, body: float) -> tuple[str, str] | None:
    text = line.text.strip()
    m = ITEM.match(text)
    if not m or not m.group("item"):
        return None
    first = text.split(None, 1)[0] if not m.group("part") else text.split()[2] if len(text.split()) > 2 else "item"
    strong = first.isupper() or line.bold or line.size >= body * 1.05
    title = (m.group("title") or "").strip()
    sentence = re.split(r"(?<=[A-Za-z\]])\.\s+(?=[A-Z][a-z])", title, maxsplit=1)[0].rstrip(". ")
    if not strong or len(sentence.split()) > 14:
        return None
    if re.match(r"^[,\s]*\d|^(,|and\b|or\b|through\b|to\b)", title, re.I) or not re.search(r"[A-Za-z]{3}", title):
        return None
    return m.group("item").upper(), sentence


# Heading level of a line: 1 = Item/Part, 2 = big text or "Note 4", 3 = bold/capitals/run-in heading, None = not a heading.
def heading_level(line: Line, body: float, is_data: bool) -> int | None:
    text = line.text.strip()
    if not re.search(r"[A-Za-z]", text) or len(text) < 3:
        return None
    words = text.split()
    if item_heading(line, body) or (PART.match(text) and len(words) <= 8 and (line.bold or words[0].isupper())):
        return 1
    if is_data or text.endswith(","):
        return None
    # run-in heading: a capitalised phrase that opens a paragraph ("COMPETITION. We believe ..."), bold or all capitals
    m = re.match(r"^([A-Z][A-Za-z0-9 ,&'’/()-]{3,70}?)\.\s+[A-Z“\"]", text)
    if m and len(words) > 16:
        letters = re.sub(r"[^A-Za-z]", "", m.group(1))
        if (letters.isupper() or line.bold) and len(m.group(1).split()) <= 10 and len(letters) >= 4:
            return 3
    if len(words) > 16:
        return None
    if NOTE.match(text) and len(words) <= 14:
        return 2
    letters = re.sub(r"[^A-Za-z]", "", text)
    if line.size >= body * 1.15 and len(words) <= 14:
        return 2 if line.size >= body * 1.4 else 3
    caps = len(letters) >= 4 and letters.isupper() and len(words) <= 12
    if (line.bold or caps) and len(words) <= 12 and not text.endswith(".") and not text.startswith("("):
        return 3
    return None


# Printed page numbers: numbers in the top/bottom band (or a lone centred number) that follow ONE consistent
# offset from the PDF page index; stray numbers that break the pattern are ignored.
def detect_page_labels(pages_lines: list[list[Line]], heights: list[float], widths: list[float]) -> list[str | None]:
    cands: list[set[int]] = []
    for lines, h, w in zip(pages_lines, heights, widths):
        found: set[int] = set()
        for l in lines:
            toks = l.text.split()
            if not toks:
                continue
            if len(toks) == 1 and re.fullmatch(r"\d{1,4}", toks[0]) and abs((l.x0 + l.x1) / 2 - w / 2) < 0.15 * w:
                found.add(int(toks[0]))  # a lone centred number, wherever the footer sits
            elif l.bottom < 0.1 * h or l.top > 0.9 * h:
                found.update(int(t) for t in (toks[0], toks[-1]) if re.fullmatch(r"\d{1,4}", t))
        cands.append(found)
    offsets = Counter(no + 1 - c for no, cs in enumerate(cands) for c in cs)
    if not offsets:
        return [None] * len(cands)
    off, n = offsets.most_common(1)[0]
    if n < max(3, 0.25 * len(cands)):
        return [None] * len(cands)
    return [str(no + 1 - off) if (no + 1 - off) in cs else None for no, cs in enumerate(cands)]


def _section(dtype: str, part: str | None, item: str | None) -> str | None:  # "Item 7" or, in a 10-Q, "Part I Item 2"
    if item is None:
        return None
    return f"Part {part} Item {item}" if dtype == "10-Q" and part else f"Item {item}"


# Build the page index: text, headings, sections, table-of-contents flag and page number for every page.
def build_index(pages_lines: list[list[Line]], pages_rows_kinds: list[list[str]], widths: list[float], heights: list[float],
                types: dict) -> dict:
    furn = furniture(pages_lines, heights)
    body = body_size(pages_lines)
    dtype = doc_type([" ".join(l.text for l in lines) for lines in pages_lines], types)
    pages = []
    part, item = None, None
    for idx, (lines, kinds, w, h) in enumerate(zip(pages_lines, pages_rows_kinds, widths, heights)):
        # a table-of-contents page has several "Item 1. Business ... 4" lines that end in a page number
        is_toc = sum(1 for l in lines if ITEM.match(l.text.strip()) and re.search(r"\s\d{1,3}\s*$", l.text.strip())) >= 4
        running, body_lines = [], []
        for l, kind in zip(lines, kinds):
            if _shape(l.text) in furn and _in_band(l, h):
                running.append(l.text)  # repeating header/footer
            elif not (re.fullmatch(r"\d{1,4}", l.text.strip()) and (_in_band(l, h) or abs((l.x0 + l.x1) / 2 - w / 2) < 0.12 * w)):
                body_lines.append((l, kind))  # (a lone page number is dropped)
        for r in running:  # a running header such as "PART II Item 8" states the section directly
            m = re.search(r"\bPART\s+(I{1,3}|IV)\b", r, re.I)
            if m:
                part = m.group(1).upper()
            m = re.search(r"\bITEM\s+(\d{1,2}[A-C]?)\b", r, re.I)
            if m:
                item = m.group(1).upper()
        section_at_start = _section(dtype, part, item)
        headings, started = [], []
        for l, kind in body_lines:
            text = l.text.strip()
            lvl = heading_level(l, body, kind == "data")
            if lvl is None:
                continue
            if not is_toc and kind != "data":  # an Item line with a page number after it is a contents entry
                mp = PART.match(text)
                if mp:
                    part = mp.group(1).upper()
                ih = item_heading(l, body)
                if ih:
                    mi = ITEM.match(text)
                    if mi.group("part"):
                        part = mi.group("part").upper()
                    # 10-K Items come in order; an Item that jumps backwards is a cross-reference, not a new section
                    if dtype != "10-K" or item is None or (ih[0] in ITEM_ORDER_10K and item in ITEM_ORDER_10K
                                                           and ITEM_ORDER_10K.index(ih[0]) >= ITEM_ORDER_10K.index(item)):
                        item = ih[0]
                        started.append(_section(dtype, part, item))
            headings.append({"text": text, "level": lvl, "y": round(l.top, 1)})
        pages.append({"page": idx + 1, "page_label": None, "section": section_at_start, "sections_started": started,
                      "is_toc": is_toc, "headings": headings,
                      "text": "\n".join(l.text for l, _ in body_lines)})
    for pg, lab in zip(pages, detect_page_labels(pages_lines, heights, widths)):
        pg["page_label"] = lab
    return {"doc_type": dtype, "pages": pages}
