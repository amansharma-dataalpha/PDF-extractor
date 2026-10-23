"""
# statements.py - find the financial statements and name their rows

1. **find_statements**: among all tables, pick the income statement, comprehensive income, balance sheet,
   cash flow and equity statement. Each table gets points for a matching title ("CONSOLIDATED BALANCE SHEETS"),
   typical rows ("Total assets"), size, and being in Item 8; the best table above a threshold wins.
   A statement that continues onto the next page is joined into one.
2. **field_answers**: give statement rows standard names ("is.total_revenue", "bs.total_assets", ...) so they can
   be compared with other sources. A field is "found" only when a label matches; two equally good rows make it
   "ambiguous" (the first row's value is shown, the others are listed in `candidates`); nothing is ever calculated.
"""
from __future__ import annotations

import re

from .tables import Table

OWNERS = r"(stockholders|shareholders|share ?owners|shareowners)['’]?"
TITLES = {
    "income_statement": re.compile(
        r"\b(income statements?|statements? of (consolidated )?(operations|income|earnings)|statements? of (consolidated )?(net )?(income|earnings|operations) and comprehensive)\b", re.I),
    "comprehensive_income": re.compile(r"\bcomprehensive (income|loss|earnings)\b", re.I),
    "balance_sheet": re.compile(r"\b(balance sheets?|statements? of (consolidated )?financial (position|condition)|statement of financial position)\b", re.I),
    "cash_flow": re.compile(r"\b(cash flows? statements?|statements? of (consolidated )?cash flows?)\b", re.I),
    "equity": re.compile(rf"\b(statements? of (consolidated )?(changes in )?{OWNERS} equity|(stockholders|shareholders)['’]? equity statements?|statements? of (consolidated )?changes in equity|statements? of equity)\b", re.I),
}
SIGNALS = {  # rows that typically appear in each statement
    "income_statement": [r"^(total )?(net )?(revenues?|sales)\b", r"^net (income|earnings|loss)\b", r"(income|earnings|loss) before (provision for )?income taxes", r"per share|^basic|^diluted"],
    "comprehensive_income": [r"comprehensive (income|loss)", r"foreign currency translation|other comprehensive"],
    "balance_sheet": [r"^total assets$", r"^total (current )?liabilities\b", r"total (stockholders|shareholders|share ?owners)['’]? equity|^total equity", r"^cash and cash equivalents"],
    "cash_flow": [r"operating activities", r"investing activities", r"financing activities", r"(end|beginning) of (period|year)"],
    "equity": [r"^balances?,? (at |as of )?(beginning|end|[a-z]+ \d)|^balance at|^balances? as of", r"dividends", r"repurchase|common stock"],
}
LAST_ROW = {  # the row that ends a statement; without it the statement continues on the next page
    "balance_sheet": re.compile(r"^total liabilities,? (and|&) (stockholders|shareholders|share ?owners|equity|members|partners)", re.I),
    "cash_flow": re.compile(r"(end|close) of (the )?(period|year)", re.I),
    "income_statement": re.compile(r"per share|^diluted", re.I),
}
CORE = ("income_statement", "balance_sheet", "cash_flow")


def _context_text(t: Table, page_headings: list[dict]) -> str:  # title, column headings and headings just above the table
    near = [h["text"] for h in page_headings if h["y"] <= t.top + 2 and t.top - h["y"] < 260]
    return " ".join([t.title] + t.header_lines + near[-4:])


# How strongly a table looks like a given statement.
def score(t: Table, kind: str, page_headings: list[dict], section: str | None) -> float:
    ctx = _context_text(t, page_headings)
    s = 4.0 if TITLES[kind].search(ctx) else 0.0
    # "statements of operations and comprehensive income" is an income statement, not a separate one
    if kind == "comprehensive_income" and TITLES["income_statement"].search(ctx) and not re.search(r"statements? of comprehensive", ctx, re.I):
        s -= 3
    labels = [r.label.lower() for r in t.rows]
    s += 1.5 * sum(1 for pat in SIGNALS[kind] if any(re.search(pat, l, re.I) for l in labels))
    s += 1 if len(t.rows) >= 8 else 0
    s += 1 if section in ("Item 8", "Part I Item 1") else -1.5 if section in ("Item 7", "Part I Item 2", "Item 6") else 0
    return s


# Pick the best table for each statement; join a statement that runs onto the next page(s).
def find_statements(tables: list[Table], pages_meta: dict[int, dict]) -> dict[str, list[Table]]:
    found: dict[str, list[Table]] = {}
    for kind in (*CORE, "comprehensive_income", "equity"):
        if kind == "comprehensive_income" and "income_statement" in found:
            inc = found["income_statement"][0]
            if re.search(r"and comprehensive (income|loss)", _context_text(inc, pages_meta.get(inc.page, {}).get("headings", [])), re.I):
                found[kind] = found["income_statement"]  # one combined statement serves as both
                continue
        cluster = [g[0].page for k, g in found.items() if k in CORE]
        best, best_s = None, 0.0
        for t in tables:
            meta = pages_meta.get(t.page, {})
            if meta.get("is_toc"):
                continue
            s = score(t, kind, meta.get("headings", []), meta.get("section"))
            if kind not in CORE and cluster:  # the main statements sit together; a similar table in the notes is not one
                lo, hi = min(cluster), max(cluster)
                s += 2 if lo - 1 <= t.page <= hi + 3 else (-2 if t.page > hi + 6 or t.page < lo - 6 else 0)
            if s > best_s:
                best, best_s = t, s
        if best is None or best_s < 6:
            continue
        group, end, idx = [best], LAST_ROW.get(kind), tables.index(best)
        while end and not any(end.search(r.label) for g in group for r in g.rows) and idx + 1 < len(tables) and len(group) < 3:
            nxt = tables[idx + 1]
            other = any(TITLES[k].search(_context_text(nxt, pages_meta.get(nxt.page, {}).get("headings", []))) for k in TITLES if k != kind)
            if nxt.page not in (group[-1].page, group[-1].page + 1) or other or len(nxt.columns) != len(best.columns):
                break
            group.append(nxt)
            idx += 1
        for g in group:
            g.kind = g.kind or kind
        found[kind] = group
    return found


# Join the pieces of a multi-page statement into one table.
def merge(group: list[Table]) -> Table:
    if len(group) == 1:
        return group[0]
    head = group[0]
    return Table(head.page, head.top, group[-1].bottom, head.title, head.unit or next((g.unit for g in group if g.unit), None),
                 head.scale or next((g.scale for g in group if g.scale), None), head.columns, [r for g in group for r in g.rows],
                 head.header_lines, [g.page for g in group], head.kind, [f for g in group for f in g.flags])


# ---------------------------------------------------------------------------------------------------- field names
def norm(label: str) -> str:  # lower case, no "(Note 4)", plain dashes and spaces
    s = re.sub(r"\((?:see )?notes?\s*[\d, and]+\)", "", label.lower().replace("’", "'").replace("‘", "'"), flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[–—−]", " ", s)).strip(" :.,;")


def _m(*pats):  # a matcher: true when the label matches any of the patterns
    comp = [re.compile(p, re.I) for p in pats]
    return lambda label, section, row=None: any(c.search(label) for c in comp)


def _in(section_pat: str, *pats):  # a matcher that also needs the row's section heading to match (e.g. "current liabilities")
    m = _m(*pats)
    return lambda label, section, row=None: m(label, section) and bool(re.search(section_pat, section, re.I))


def _per_share(row) -> bool:  # small values printed with decimals (4.93): an amount per share, not a share count (1,483.6)
    vals = [v for v in (row or {}).get("values", []) if v is not None]
    raws = [v for v in (row or {}).get("raw", []) if re.search(r"\d", v)]
    return bool(vals) and all(abs(v) < 100 for v in vals) and all("." in v for v in raws)


EQUITY = r"(stockholders|shareholders|share ?owners|shareowners|members|partners)['’]?"
SHARES_SECTION = r"weighted|average shares|shares used|number of shares"
# field id -> (statement, matchers in priority order); a matcher gets the normalised label, its section heading and the
# row. The wording rules come first; looser key-word and structure rules (section heading, decimals) come after them.
FIELDS: dict[str, tuple[str, list]] = {
    "is.total_revenue": ("income_statement", [_m(r"^total (net )?revenues?( and other income)?$"), _m(r"^(net )?(revenues?|sales)$", r"^net sales$"),
                                              _m(r"^total (net )?sales$", r"^(net )?operating revenues$")]),
    "is.cost_of_revenue": ("income_statement", [_m(r"^total costs? of (revenues?|sales)( and services)?$"), _m(r"^costs? of (revenues?|sales|goods sold)( and services)?$")]),
    "is.gross_margin": ("income_statement", [_m(r"^gross (margin|profit)$")]),
    "is.rnd": ("income_statement", [_m(r"^research and development( expenses?)?$")]),
    "is.sales_marketing": ("income_statement", [_m(r"^(sales and marketing|selling and marketing|selling)( expenses?)?$")]),
    "is.gna": ("income_statement", [_m(r"^general and administrative( expenses?)?$")]),
    "is.operating_income": ("income_statement", [_m(r"^operating (income|profit|loss)( \((loss|income)\))?$", r"^(income|loss) \(?(loss)?\)? ?from operations$", r"^income from operations$")]),
    "is.other_income": ("income_statement", [_m(r"^other (income|expense)( \(?(expense|income)\)?)?,? ?(net)?$", r"^other \(expense\) income,? ?(net)?$"),
                                             _m(r"^other \(?(income|expense)\)?\s*(/|or)?\s*\(?(income|expense|loss)?\)?,? ?net$")]),
    "is.pretax_income": ("income_statement", [_m(r"^(income|earnings|loss|\(loss\) income|income \(loss\)|earnings \(loss\)) before (provision for |benefit from )?(income )?taxes( on income)?( and .*)?$", r"^income \(loss\) before income taxes.*$"),
                                              _m(r"^(income|earnings) before (provision for )?income tax$")]),
    "is.income_tax": ("income_statement", [_m(r"^(provision|benefit|\(benefit\)) (\(benefit\) )?(for|from) (income )?taxes$", r"^income tax(es)? ?(expense|provision)?( \(benefit\))?$", r"^provision for income taxes$", r"^provision \(benefit\) for income taxes$")]),
    "is.net_income": ("income_statement", [_m(r"^net (income|earnings|loss)( \(loss\))?$", r"^net \(loss\) income$", r"^consolidated net (income|earnings)$")]),
    "is.eps_basic": ("income_statement", [lambda l, s, row=None: bool(re.match(r"^basic\b", l)) and bool(re.search(r"per .*share|earnings", l + " " + s)) and not re.search(SHARES_SECTION, s),
                                          _m(r"^basic (earnings|net income) per .*share.*$"),
                                          lambda l, s, row=None: l == "basic" and _per_share(row)]),
    "is.eps_diluted": ("income_statement", [lambda l, s, row=None: bool(re.match(r"^diluted\b", l)) and bool(re.search(r"per .*share|earnings", l + " " + s)) and not re.search(SHARES_SECTION, s),
                                            lambda l, s, row=None: l == "" and bool(re.match(r"^diluted .*per share", s)),  # label printed as the heading
                                            lambda l, s, row=None: l == "diluted" and _per_share(row)]),
    "is.shares_basic": ("income_statement", [lambda l, s, row=None: bool(re.match(r"^basic\b", l)) and bool(re.search(r"weighted|shares", s + " " + l)) and not re.search(r"per .*share", l),
                                             _m(r"^weighted.average .*basic$", r"^average shares outstanding,? basic$"),
                                             lambda l, s, row=None: l == "basic" and not _per_share(row)]),
    "is.shares_diluted": ("income_statement", [lambda l, s, row=None: bool(re.match(r"^diluted\b", l)) and bool(re.search(r"weighted|shares", s + " " + l)) and not re.search(r"per .*share", l),
                                               _m(r"^weighted.average .*diluted$", r"^average shares outstanding,? diluted$"),
                                               lambda l, s, row=None: l == "diluted" and not _per_share(row)]),
    "ci.comprehensive_income": ("comprehensive_income", [_m(r"^(total )?comprehensive (income|loss|\(loss\) income|income \(loss\)|earnings)$")]),
    "bs.cash": ("balance_sheet", [_m(r"^cash(,)? (and )?(cash )?equivalents?(,? and restricted cash)?$", r"^cash$", r"^cash and equivalents$")]),
    "bs.short_term_investments": ("balance_sheet", [_m(r"^short.term investments$"), _in(r"^current assets", r"^marketable securities$"),
                                                    _m(r"^marketable securities$")]),
    "bs.receivables": ("balance_sheet", [_m(r"^(accounts (and notes )?receivable|trade (accounts )?receivables?|(current )?receivables)\b.*$")]),
    "bs.inventories": ("balance_sheet", [_m(r"^inventor(y|ies)\b.*$")]),
    "bs.total_current_assets": ("balance_sheet", [_m(r"^total current assets$"), _m(r"^current assets$")]),
    "bs.ppe": ("balance_sheet", [_m(r"^property,? plant,? and equipment\b.*$", r"^property and equipment\b.*$")]),
    "bs.goodwill": ("balance_sheet", [_m(r"^goodwill\b.*$")]),
    "bs.intangibles": ("balance_sheet", [_m(r"^intangible assets?\b.*$", r"^intangibles,? net$"), _m(r"^(identifiable|acquired|other) intangible assets?\b.*$")]),
    "bs.total_assets": ("balance_sheet", [_m(r"^total assets$")]),
    "bs.accounts_payable": ("balance_sheet", [_m(r"^accounts payable\b.*$", r"^trade (accounts )?payables?\b.*$")]),
    "bs.current_debt": ("balance_sheet", [_m(r"^current (portion|maturities) of long.term debt\b.*$"), _m(r"^short.term (debt|borrowings)\b.*$"),
                                          _in(r"^current liabilities", r"^(term )?debt$", r"^current portion of (term )?debt\b.*$")]),
    "bs.total_current_liabilities": ("balance_sheet", [_m(r"^total current liabilities$"), _m(r"^current liabilities$")]),
    "bs.long_term_debt": ("balance_sheet", [_m(r"^long.term (debt|borrowings)\b.*$"), _in(r"^non.?current liabilities", r"^(term )?debt$")]),
    "bs.total_liabilities": ("balance_sheet", [_m(r"^total liabilities$")]),
    "bs.retained_earnings": ("balance_sheet", [_m(r"^(retained earnings|accumulated deficit)\b.*$"), _m(r"^reinvested earnings\b.*$")]),
    "bs.total_equity": ("balance_sheet", [_m(rf"^total {EQUITY} equity$"), _m(r"^total equity$")]),
    "bs.total_liabilities_equity": ("balance_sheet", [_m(rf"^total liabilities,? (and|&) {EQUITY} equity$", r"^total liabilities and equity$", r"^total liabilities,? redeemable noncontrolling interest.*$")]),
    "cf.operating": ("cash_flow", [lambda l, s, row=None: bool(re.match(r"^(net cash|cash)\b.*\boperat(ing activities|ions)\b", l)) and not re.search(r"discontinued|continuing|adjustments", l)]),
    "cf.investing": ("cash_flow", [lambda l, s, row=None: bool(re.match(r"^(net cash|cash)\b.*\binvesting\b", l)) and not re.search(r"discontinued|continuing", l)]),
    "cf.financing": ("cash_flow", [lambda l, s, row=None: bool(re.match(r"^(net cash|cash)\b.*\bfinancing\b", l)) and not re.search(r"discontinued|continuing", l)]),
    "cf.fx_effect": ("cash_flow", [_m(r"effect of (foreign )?(currency )?exchange rate")]),
    "cf.net_change": ("cash_flow", [_m(r"^net (increase|decrease|change)\b.*\bcash\b", r"^(\(?increase\)?|\(?decrease\)?)\b.*\bin cash\b", r"^change in cash")]),
    "cf.cash_beginning": ("cash_flow", [_m(r"\bcash\b.*beginning of (the )?(period|year)"), _m(r"\bcash\b.*\bbeginning balances?$")]),
    "cf.cash_end": ("cash_flow", [_m(r"\bcash\b.*(end of (the )?(period|year)|as of [a-z]+ \d{1,2}$)"), _m(r"\bcash\b.*\bending balances?$")]),
    "cf.capex": ("cash_flow", [_m(r"^(additions to|purchases? of|payments for|capital expenditures?)\b.*(property|plant|equipment)", r"^capital expenditures?"),
                               _m(r"^(purchases?|payments) related to (property|plant|equipment)")]),
    "cf.depreciation": ("cash_flow", [_m(r"^depreciation\b.*$")]),
    "cf.sbc": ("cash_flow", [_m(r"^(stock|share).based compensation.*$")]),
    "cf.dividends": ("cash_flow", [lambda l, s, row=None: bool(re.match(r"^((common stock )?(cash )?dividends paid|cash dividends)", l)) and "noncontrolling" not in l,
                                   lambda l, s, row=None: bool(re.match(r"^(payments? (for|of) dividends|dividends? payments?|dividends?( common\b.*| paid\b.*)?$)", l))
                                   and not re.search(r"noncontrolling|declared|received|not paid", l)]),
    "cf.buybacks": ("cash_flow", [_m(r"^common stock repurchased$", r"^repurchases? of (common|ordinary)? ?(stock|shares).*$", r"^purchases? of (company|common) stock.*$", r"^purchases? of common stock for treasury$"),
                                  _m(r"^purchases? of stock for treasury$", r"^payments? (related to|for) repurchases? of (common|ordinary) (stock|shares)")]),
}


# One answer per field and statement column: found / ambiguous / not_present, with value, period, page and label.
def field_answers(res: dict) -> list[dict]:
    out: list[dict] = []
    for fid, (stmt_kind, matchers) in FIELDS.items():
        st = res["statements"].get(stmt_kind)
        hits: list[dict] = []
        for m in matchers if st else []:
            hits = [r for r in st["rows"] if m(norm(r["label"]), norm(r.get("section") or ""), r)]
            if hits:
                break
        if not hits:
            out.append({"field_id": fid, "status": "not_present", "period": None, "value": None})
            continue
        row, ver = hits[0], hits[0].get("verified") or [False] * len(hits[0]["values"])
        for i, col in enumerate(st["columns"]):
            if i < len(row["values"]) and row["values"][i] is not None:
                out.append({"field_id": fid, "status": "found" if len(hits) == 1 else "ambiguous", "period": {"header": col["header"], **col["period"]},
                            "value": row["values"][i], "raw": row["raw"][i], "unit": st.get("unit"), "scale": st.get("scale"),
                            "label_as_printed": row["label"], "page": row["page"], "candidates": [r["label"] for r in hits[1:]],
                            "verified": bool(ver[i]), "status_detail": row.get("status")})
    return out
