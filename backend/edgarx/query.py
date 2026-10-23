"""
# query.py - the figures of one SEC filing, as JSON (no AI)

- Input: a company (ticker or CIK), a form (10-K or 10-Q), the company's fiscal year and quarter, and optional fields.
- Finds that filing in the company's XBRL data and returns its figures: value, unit, exact dates, XBRL concept and the
  filing it came from; the label is the concept's exact XBRL label. Comparative periods in the same filing are included.
- Q4 is not filed separately (it sits inside the 10-K's full year), so a Q4 request returns the full-year figures
  with a note; nothing is ever calculated.
- Used by the web server's SEC path (`api/sec_path.py`): `filing_figures(client, "NVDA", "10-Q", 2025, "Q3")`.
"""
from __future__ import annotations

from collections import Counter

from .financials import CASH_FIELDS, _cash_date, _fields, _row, observations
from .sec_client import SecClient
from .xbrl_fields import CONCEPTS


# Find the filing the user asked for, using the filing's own fiscal year and quarter labels (originals before amendments).
# Its period end is the date most of its figures carry: cover-page facts and notes about later events have other dates.
def _pick_filing(facts: dict, form: str, fiscal_year: int, fiscal_period: str) -> dict | None:
    filings: dict[str, dict] = {}
    for taxonomy, tags in facts.get("facts", {}).items():
        if taxonomy == "dei":  # cover-page facts (e.g. shares outstanding) are dated after the period
            continue
        for tag in tags.values():
            for entries in tag.get("units", {}).values():
                for e in entries:
                    if e.get("fy") == fiscal_year and e.get("fp") == fiscal_period and e.get("form", "").startswith(form):
                        f = filings.setdefault(e["accn"], {"accn": e["accn"], "form": e["form"], "filed": e["filed"],
                                                           "fiscal_year": fiscal_year, "fiscal_period": fiscal_period, "ends": Counter()})
                        f["ends"][e["end"]] += 1
    filing = min(filings.values(), key=lambda f: (f["form"].endswith("/A"), f["filed"]), default=None)
    if filing:
        filing["period_end"] = filing.pop("ends").most_common(1)[0][0]
    return filing


# One field's figures from this filing, all from the single XBRL concept the filing uses for its own period
# (so "cash" never mixes balance-sheet cash with cash-flow cash that also counts restricted cash).
def _field_rows(facts: dict, field_id: str, months: set[int], filing: dict) -> list[dict]:
    c, accn, end = CONCEPTS[field_id], filing["accn"], filing["period_end"]
    if field_id in CASH_FIELDS:  # cash at the start / end of each cash-flow period, not every cash balance
        return _cash_rows(facts, c, months, filing)
    obs = [o for o in observations(facts, c, accn=accn) if o.months is None or o.months in months]
    current = [o for o in obs if o.end == end]
    if not current:
        return []
    tag = min(current, key=lambda o: o.tag_rank).tag
    return [_row(c, o) for o in obs if o.tag == tag]


# Cash at the start and end of each cash-flow period, both dates from one cash concept (its label says whether it
# includes restricted cash; it can differ from the concept of the period's change line).
def _cash_rows(facts: dict, c, months: set[int], filing: dict) -> list[dict]:
    accn = filing["accn"]
    anchors = [o for o in observations(facts, CONCEPTS["cf.net_change"], accn=accn) if o.months in months]
    cash = observations(facts, CONCEPTS["cf.cash_end"], accn=accn)
    rows = []
    for a in anchors:
        for tag in c.tags:  # first concept that has both the opening and the closing balance
            have = {o.end: o for o in cash if o.tag == tag}
            begin, close = have.get(_cash_date("cf.cash_beginning", a)), have.get(a.end)
            if begin and close:
                o = begin if c.id == "cf.cash_beginning" else close
                rows.append(_row(c, o, period_start=a.start, period_end=a.end, months=a.months))
                break
    return rows


# The requested figures from one filing: a 10-K gives 12-month figures, a 10-Q gives the quarter and year-to-date.
def filing_figures(client: SecClient, company: str, form: str, fiscal_year: int, fiscal_period: str,
                   fields: list[str] | None = None) -> dict:
    who = client.resolve(company)
    facts = client.companyfacts(who["cik"])
    note = None
    if fiscal_period == "Q4":
        form, fiscal_period = "10-K", "FY"
        note = "Q4 is not reported separately; these are the full-year figures from the 10-K."
    filing = _pick_filing(facts, form, fiscal_year, fiscal_period)
    if not filing:
        # shortcut: a ticker moved to a new entity (e.g. a holding-company reorganization) is not followed back, ask for the old CIK
        return {"company": who, "error": f"no {form} for fiscal {fiscal_period} {fiscal_year} in SEC data for {who['title']} "
                                          f"(CIK {who['cik']}); if the company reorganized, ask again with the earlier company's CIK"}
    filing["url"] = f"https://www.sec.gov/Archives/edgar/data/{int(who['cik'])}/{filing['accn'].replace('-', '')}/"
    months = {12} if fiscal_period == "FY" else {3, 6, 9}  # a 10-Q has the quarter and the year to date
    figures, missing = {}, []
    for f in _fields(fields):
        rows = _field_rows(facts, f, months, filing)
        for r in rows:
            tax, tag = r["source"]["concept"].split(":")
            # the concept's exact meaning, e.g. "Stockholders' Equity Attributable to Parent" (not total equity)
            r["label"] = facts["facts"][tax][tag].get("label") or tag
            figures[(f, r["period"]["start"], r["period"]["end"])] = r
        if not rows:
            missing.append(f)
    return {"company": who, "filing": filing, "note": note, "figures": list(figures.values()), "not_in_sec_data": missing}
