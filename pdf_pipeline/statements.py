"""
# statements.py - find the financial statements, name their rows and prove the numbers add up

1. **find_statements**: among all tables, pick the income statement, comprehensive income, balance sheet,
   cash flow and equity statement. Each table gets points for a matching title ("CONSOLIDATED BALANCE SHEETS"),
   typical rows ("Total assets"), size, and being in Item 8; the best table above a threshold wins.
   A statement that continues onto the next page is joined into one.
2. **FIELDS**: standard names for statement rows ("is.total_revenue", "bs.total_assets", ...) with the label rules
   that find them; config/extraction.json refers to these names.
3. **verify**: a number is marked verified only if an arithmetic check it takes part in passes:
   - subtotals: every "Total ..." / result row equals the rows directly above it
     (e.g. Operating income = Gross profit - expenses; Total assets = Total current assets + the other assets),
   - balance sheet: total assets = total liabilities and equity (= liabilities + equity),
   - cash flow: operating + investing + financing + FX = net change; beginning cash + change = ending cash,
   - across statements: net income on the income statement = net income on the cash flow (same period).
   Printed numbers are rounded, so small differences are allowed - half of the smallest printed step per number,
   at most 3 steps (1 step when the rows do not start at a section boundary). A "Net ..." row that adds up to nothing
   is treated as an ordinary line, not a failed total; an ordinary row that equals the sum of the rows above is
   verified too. Equity statements are grids (components x movements) and are not checked.

A filing always adds up, so a failed check means we misread a row. Nothing is ever calculated into the output.
"""
from __future__ import annotations

import re

from .layout import Table
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
    "bs.current_debt": ("balance_sheet", [_m(r"^current (portion|maturities) of long.term debt\b.*$", r"^long.term debt due within one year\b"), _m(r"^short.term (debt|borrowings)\b.*$"),
                                          _in(r"^current liabilities", r"^(term )?debt$", r"^current portion of (term )?debt\b.*$")]),
    "bs.total_current_liabilities": ("balance_sheet", [_m(r"^total current liabilities$"), _m(r"^current liabilities$")]),
    "bs.long_term_debt": ("balance_sheet", [_m(r"^long.term (debt|borrowings)\b(?!.*\b(due within|current))"), _in(r"^non.?current liabilities", r"^(term )?debt$")]),
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


# ---------------------------------------------------------------------------------------------------- arithmetic checks
# Rows each check needs, found by their label (first or last match, see concept_rows).
CONCEPTS = {
    "income_statement": {"net_income": r"^net (income|earnings|loss|income \(loss\)|\(loss\) income)( attributable to [a-z .,&]+)?$"},
    "balance_sheet": {
        "total_assets": r"^total assets$",
        "total_liabilities": r"^total liabilities$",
        "total_equity": r"^total (stockholders|shareholders|share ?owners)['’]? equity$|^total equity$",
        "total_le": r"^total liabilities,? (and|&) (stockholders|shareholders|share ?owners|equity)",
    },
    "cash_flow": {
        # section totals start "Net cash ..." / "Cash provided by ..." (not "Cash paid for operating leases")
        "operating": r"^(net cash|cash (provided|used|generated|flows?|from))\b.*\boperat(ing|ions)\b",
        "investing": r"^(net cash|cash (provided|used|generated|flows?|from))\b.*\binvesting\b",
        "financing": r"^(net cash|cash (provided|used|generated|flows?|from))\b.*\bfinancing\b",
        "fx": r"(effect|impact) of (foreign )?(currency )?exchange rate|foreign currency effect|effect of currency",
        "net_change": r"^net (increase|decrease|change|\(decrease\) increase|increase \(decrease\)|decrease \(increase\)).*\bcash\b|^(increase|decrease|change) in cash",
        "begin": r"\bcash\b.*\bbeginning of (the )?(period|year)",
        "net_income": r"^(consolidated )?net (income|earnings|loss|income \(loss\)|\(loss\) income)$",
    },
}
# Labels of rows that are totals or results of the rows above them.
RESULT = re.compile(
    r"^((total )?(current|non-?current|long-term|noncurrent) (assets|liabilities)$|"
    r"total|subtotal|net\b|gross (profit|margin)|operating (income|loss|profit)|income from operations|"
    r"(income|earnings|loss|\(loss\) income|income \(loss\)|profit) (before|from continuing)|(other )?comprehensive (income|loss)|"
    r"(income|earnings) \(loss\)$|cash (provided by|from|used in)|ebitda)"
)
MINUS = re.compile(r"\b(cost|costs|expense|expenses|provision|tax|taxes|less:|charges|impairment)\b")  # rows usually subtracted


# Label in lower case with plain quotes and single spaces, for matching.
def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", label.lower().replace("’", "'").replace("‘", "'")).strip(" :")


# Row number of each concept in a statement (section totals: the last match, after "continuing operations" parts).
def concept_rows(t: Table, kind: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, pat in CONCEPTS.get(kind, {}).items():
        hits = [i for i, r in enumerate(t.rows) if re.search(pat, _norm(r.label))]
        if hits and name in ("operating", "investing", "financing"):
            out[name] = ([i for i in hits if not re.search(r"continuing|discontinued", _norm(t.rows[i].label))] or hits)[-1]
        elif hits:
            out[name] = hits[0]
    return out


# The smallest step the printed numbers can show: 1 for whole numbers, 0.1 for one decimal, ...
def _ulp(table: Table, refs: list[tuple[int, int]]) -> float:
    d = 0
    for i, col in refs:
        r = table.rows[i]
        if col < len(r.raw):
            m = re.search(r"\.(\d+)", r.raw[col] or "")
            d = max(d, len(m.group(1)) if m else 0)
    return 10.0 ** -d


# True if a and b differ by no more than honest rounding: half a unit per printed number, capped (3 units by default).
def _close(a: float, b: float, n_terms: int, ulp: float = 1.0, cap: float = 3.0) -> bool:
    return abs(a - b) <= min(0.5 * (n_terms + 1), cap) * ulp + 1e-9


# Sign conventions a printed total may follow: plain sum, first minus the rest, or expense rows subtracted.
def _sign_patterns(run: list[tuple[int, float]], labels: list[str]):
    yield "sum", [1] * len(run)
    yield "first_minus_rest", [1] + [-1] * (len(run) - 1)
    signs = [-1 if MINUS.search(labels[j]) else 1 for j, _ in run]
    if any(s < 0 for s in signs):
        yield "by_label", signs


# Subtotal check: every total/result row must equal a run of the still-open rows directly above it.
# A matched run "closes" into its total (so Total assets = Total current assets + other assets), and a total must use the
# same sign convention in every period column - a column that needs a different one is what a flipped sign looks like.
def subtotal_checks(t: Table) -> list[dict]:
    results = []
    labels = [_norm(r.label) for r in t.rows]
    for col in range(len(t.columns)):
        items: list[tuple[int, float]] = []  # rows still open: (row number, value)
        for i, r in enumerate(t.rows):
            v = r.values[col] if col < len(r.values) else None
            if v is None:
                continue
            is_result = bool(RESULT.search(labels[i]))
            if items:
                hit = None
                for k in range(2, len(items) + 1):
                    run = items[-k:]
                    if all(x == 0 for _, x in run):
                        continue
                    # a run starting at a natural boundary gets the normal rounding allowance; any other start only a
                    # 1-unit allowance and at most 10 rows, because arbitrary starts invite coincidental matches
                    j0 = run[0][0]
                    aligned = (k == len(items) or j0 == 0 or t.rows[j0].section != t.rows[j0 - 1].section
                               or bool(RESULT.search(labels[j0 - 1])) or bool(RESULT.search(labels[j0])))
                    if (not aligned and k > 10) or (not is_result and k > 6):
                        continue
                    ulp = _ulp(t, [(j, col) for j, _ in run] + [(i, col)])
                    for name, signs in _sign_patterns(run, labels):
                        if (is_result or name == "sum") and _close(sum(s * x for s, (_, x) in zip(signs, run)), v, k, ulp, 3.0 if aligned else 1.0):
                            hit = (run, name)
                            break
                    if hit:
                        break
                if hit is None and is_result and labels[i].startswith(("total", "subtotal")):
                    # a total of a single line ("Total equity" over one "Retained earnings" row) equals that line
                    if _close(items[-1][1], v, 0, _ulp(t, [(items[-1][0], col), (i, col)]), 1.0):
                        hit = ([items[-1]], "single_component")
                # a "Net ..." row that sums nothing is usually a plain line ("Net sales"), not a failed total
                if hit or labels[i].startswith(("total", "subtotal")):
                    results.append({"check": "subtotal", "column": col, "row": i, "label": r.label, "passed": hit is not None,
                                    "pattern": hit[1] if hit else None, "rows": [j for j, _ in hit[0]] + [i] if hit else [i]})
                if hit and is_result:  # only totals close their rows; a plain row that equals a sum must not hide them
                    del items[-len(hit[0]):]
            items.append((i, v))
    by_row: dict[int, list[dict]] = {}
    for r in results:
        by_row.setdefault(r["row"], []).append(r)
    for rs in by_row.values():  # the same total must follow one sign convention in every column
        if len({r["pattern"] for r in rs if r["passed"]}) > 1:
            for r in rs:
                if r["passed"]:
                    r["passed"], r["pattern"] = False, "inconsistent_sign_convention"
    return results


# Accounting identities: balance sheet balances, cash rolls forward, net income ties between statements.
def core_checks(st: dict[str, Table]) -> list[dict]:
    res = []
    bs, cf, inc = st.get("balance_sheet"), st.get("cash_flow"), st.get("income_statement")

    def val(t, i, col):  # value of row i in column col, or None
        return t.rows[i].values[col] if t and i is not None and col < len(t.rows[i].values) else None

    def add(name, a, b, col, refs, n=2):  # record a check comparing a with b
        if a is None or b is None:
            return
        ulp = 1.0
        for ref in refs:
            kind, i = ref[0], ref[1]
            if i is not None and kind in st:
                ulp = min(ulp, _ulp(st[kind], [(i, ref[2] if len(ref) > 2 else col)]))
        res.append({"check": name, "column": col, "passed": _close(a, b, n, ulp), "lhs": a, "rhs": b, "refs": refs})

    if bs:
        c = concept_rows(bs, "balance_sheet")
        for col in range(len(bs.columns)):
            ta, tle = val(bs, c.get("total_assets"), col), val(bs, c.get("total_le"), col)
            tl, te = val(bs, c.get("total_liabilities"), col), val(bs, c.get("total_equity"), col)
            add("assets_eq_liabilities_and_equity", ta, tle, col, [("balance_sheet", c.get("total_assets")), ("balance_sheet", c.get("total_le"))])
            if tl is not None and te is not None and tle is not None:
                add("liabilities_plus_equity_eq_total", tl + te, tle, col,
                    [("balance_sheet", c.get("total_liabilities")), ("balance_sheet", c.get("total_equity")), ("balance_sheet", c.get("total_le"))])
    if cf:
        c = concept_rows(cf, "cash_flow")
        cash_rows = [i for i, r in enumerate(cf.rows) if re.search(r"\bcash\b", _norm(r.label)) and not re.search(r"paid|supplemental|interest|taxes", _norm(r.label))]
        chg_rows = [i for i in cash_rows if re.search(r"^(net )?(\(?(increase|decrease)\)? ?)+(\(?(increase|decrease)\)? ?)*(\bin\b|\bof\b)?.*\bcash\b|^(net )?change in cash", _norm(cf.rows[i].label))]
        for col in range(len(cf.columns)):
            v = lambda i: val(cf, i, col)  # noqa: E731
            op, inv, fin = v(c.get("operating")), v(c.get("investing")), v(c.get("financing"))
            if None in (op, inv, fin):
                continue
            base = op + inv + fin + (v(c.get("fx")) or 0.0)
            core_refs = [("cash_flow", c.get(k)) for k in ("operating", "investing", "financing", "fx") if c.get(k) is not None]
            # several "net change" rows can exist (before / after cash held for sale): any one that matches
            cands = [i for i in chg_rows if v(i) is not None] or ([c["net_change"]] if "net_change" in c else [])
            hit = next((i for i in cands if _close(base, v(i), 4, _ulp(cf, [(i, col)]))), None)
            chg_i = hit if hit is not None else (cands[0] if cands else None)
            if chg_i is None:
                continue
            add("cash_rollforward", base, v(chg_i), col, core_refs + [("cash_flow", chg_i)], 4)
            beg_i = c.get("begin")
            if beg_i is None or v(beg_i) is None:
                continue
            # simplest case first: beginning cash + any "net change" row = some later cash row
            direct = next(((ci, ei) for ci in cands for ei in cash_rows if ei > beg_i and ei != ci and v(ei) is not None
                           and _close(v(beg_i) + v(ci), v(ei), 2, _ulp(cf, [(beg_i, col), (ci, col), (ei, col)]))), None)
            if direct:
                add("cash_begin_plus_change_eq_end", v(beg_i) + v(direct[0]), v(direct[1]), col,
                    [("cash_flow", beg_i), ("cash_flow", direct[0]), ("cash_flow", direct[1])])
                continue
            # otherwise allow adjustment rows in between (e.g. "Less: cash held for sale")
            lo = min(beg_i, chg_i)
            tail = [i for i in cash_rows if i > max(beg_i, chg_i)] or [i for i in cash_rows if i > lo]
            best = None
            for end_i in [i for i in tail if v(i) is not None]:
                adj = sum((-abs(v(i)) if _norm(cf.rows[i].label).startswith("less") else v(i))
                          for i in range(lo + 1, end_i) if i not in (beg_i, chg_i) and v(i) is not None and i in cash_rows)
                a = next((a for a in (0.0, adj, -adj) if _close(v(beg_i) + v(chg_i) + a, v(end_i), 3, _ulp(cf, [(beg_i, col), (chg_i, col), (end_i, col)]))), None)
                if a is not None:
                    best = (end_i, a)
                    break
            if best:
                used = [("cash_flow", beg_i), ("cash_flow", chg_i), ("cash_flow", best[0])]
                used += [("cash_flow", i) for i in range(lo + 1, best[0]) if i in cash_rows and i not in (beg_i, chg_i)] if best[1] else []
                add("cash_begin_plus_change_eq_end", v(beg_i) + v(chg_i) + best[1], v(best[0]), col, used)
            elif tail:
                add("cash_begin_plus_change_eq_end", v(beg_i) + v(chg_i), v(tail[-1]), col, [("cash_flow", beg_i), ("cash_flow", chg_i), ("cash_flow", tail[-1])])
    if inc and cf:
        ci, cc = concept_rows(inc, "income_statement"), concept_rows(cf, "cash_flow")
        for col, ccol in _match_periods(inc, cf):  # like periods only: a 10-Q has 3- and 6-month columns
            a, b = val(inc, ci.get("net_income"), col), val(cf, cc.get("net_income"), ccol)
            if a is not None and b is not None:
                res.append({"check": "net_income_ties_to_cash_flow", "column": col, "passed": abs(a - b) <= 1e-9, "lhs": a, "rhs": b,
                            "refs": [("income_statement", ci.get("net_income"), col), ("cash_flow", cc.get("net_income"), ccol)]})
    return res


# Earnings per share = net income / weighted shares, within the rounding of the three printed numbers. It verifies EPS
# rows, which no sum can check. Net income in millions with shares in thousands (or the reverse) is tried too. A number
# is verified only if one printed step up or down in it would break the check: the EPS nearly always (it has two
# decimals), a share count or net income almost never (they have more digits than the EPS can pin down), so those are
# still left to the other checks and to Docling. Only passing checks are returned: a diluted EPS that uses an adjusted
# net income, or one rounding cannot pin down (few printed share digits), stays unverified.
def eps_checks(t: Table) -> list[dict]:
    dicts = [{"values": r.values, "raw": r.raw} for r in t.rows]

    def find(fid: str) -> int | None:  # row of a standard field, by the same rules as the output uses
        for m in FIELDS[fid][1]:
            hits = [i for i, r in enumerate(t.rows) if m(norm(r.label), norm(r.section or ""), dicts[i])]
            if hits:
                return hits[0]
        return None

    def val(i: int, col: int) -> float | None:  # value of row i in column col, or None
        return t.rows[i].values[col] if col < len(t.rows[i].values) else None

    incomes = [i for i, r in enumerate(t.rows) if re.search(CONCEPTS["income_statement"]["net_income"], _norm(r.label))]
    out = []
    for eps_id, shares_id in (("is.eps_basic", "is.shares_basic"), ("is.eps_diluted", "is.shares_diluted")):
        e, s = find(eps_id), find(shares_id)
        if e is None or s is None or e == s:
            continue
        for col in range(len(t.columns)):
            eps, shares = val(e, col), val(s, col)
            if eps is None or not shares:
                continue
            for n in incomes:
                income = val(n, col)
                if not income:
                    continue
                steps = {e: _ulp(t, [(e, col)]), n: _ulp(t, [(n, col)]), s: _ulp(t, [(s, col)])}

                def fits(v: float, inc: float, sh: float) -> bool:  # half a printed EPS step + what half a step of the others can move it
                    tol = 0.5 * steps[e] + abs(v) * 0.5 * (steps[n] / abs(inc) + steps[s] / abs(sh))
                    return any(abs(inc / sh * f - v) <= tol + 1e-9 for f in (1.0, 1e3, 1e-3))

                if not fits(eps, income, shares):
                    continue
                vals = {e: eps, n: income, s: shares}
                pinned = []  # the numbers this check tells apart from their printed neighbours
                for i in (e, s, n):
                    if all(not fits(*[vals[j] + (d * steps[i] if j == i else 0) for j in (e, n, s)]) for d in (1, -1)):
                        pinned.append(i)
                if e not in pinned:  # rounding lets 6.32 and 6.33 both fit: the check proves nothing
                    continue
                out.append({"check": "eps_equals_net_income_over_shares", "statement": "income_statement", "column": col,
                            "passed": True, "lhs": income / shares, "rhs": eps,
                            "refs": [("income_statement", i, col) for i in pinned]})
                break
    return out


# Column pairs of two statements that cover the same period (same year and, when both say, same number of months).
def _match_periods(a: Table, b: Table) -> list[tuple[int, int]]:
    pairs = []
    for i, ca in enumerate(a.columns):
        for j, cb in enumerate(b.columns):
            pa, pb = ca.period, cb.period
            if not pa.get("year") or pa.get("year") != pb.get("year") or pa.get("change") or pb.get("change"):
                continue
            if pa.get("months") and pb.get("months") and pa["months"] != pb["months"]:
                continue
            pairs.append((i, j))
            break
    return pairs


# Run all checks; returns the check results and, per statement, which values are verified ([row][column]).
def verify(st: dict[str, Table]) -> dict:
    checks = []
    masks = {k: [[False] * len(t.columns) for _ in t.rows] for k, t in st.items()}
    done: dict[int, str] = {}
    for kind, t in st.items():
        if kind == "equity":
            continue
        if id(t) in done:  # one combined statement listed under two names: check it once
            masks[kind] = masks[done[id(t)]]
            continue
        done[id(t)] = kind
        for r in subtotal_checks(t):
            r["statement"] = kind
            checks.append(r)
            if r["passed"]:
                for i in r["rows"]:
                    masks[kind][i][r["column"]] = True
    for r in core_checks(st) + (eps_checks(st["income_statement"]) if "income_statement" in st else []):
        checks.append(r)
        if r["passed"]:
            for ref in r["refs"]:
                if ref[1] is not None:
                    masks[ref[0]][ref[1]][ref[2] if len(ref) > 2 else r["column"]] = True
    return {"checks": checks, "masks": masks}
