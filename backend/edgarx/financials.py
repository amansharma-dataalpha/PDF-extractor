"""
# financials.py - company numbers from SEC XBRL, and a cross-check of finx results

- Turns a company's raw XBRL facts into one clean value per field and period (newest first).
- The same period appears in many filings (the next 10-K repeats it, restatements replace it), so the choice is explicit:
  `prefer="latest"` = most recently filed value (restated), `prefer="original"` = first filed value (as first reported).
- The filing's own `fy` / `fp` describe the FILING, not the number, so they are never used to pick periods.
- Features: `company_financials`, `compare_companies`, `list_filings`, `download_filing`, `cross_check`.
- `cross_check` compares a finx PDF extraction with the XBRL numbers of the SAME filing (accession number).
  Outcome per value: `match`, `match_alt_tag` (another tag of the field), `match_sign` (opposite sign, only for
  lines printed either way), `mismatch` (a lead to investigate) or `no_xbrl_value` (the company did not tag it).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .sec_client import SecClient
from .xbrl_fields import ANNUAL_FORMS, CONCEPTS, QUARTERLY_FORMS, Concept

DEFAULT_FIELDS = ["is.total_revenue", "is.gross_margin", "is.operating_income", "is.net_income", "is.eps_diluted",
                  "bs.cash", "bs.total_assets", "bs.total_liabilities", "bs.total_equity",
                  "cf.operating", "cf.capex", "cf.dividends", "cf.buybacks"]
CASH_FIELDS = ("cf.cash_beginning", "cf.cash_end")
ANCHORS = ("bs.total_assets", "is.net_income", "cf.operating", "is.total_revenue")


# One reported number with its period and the filing it came from.
@dataclass
class Observation:
    tag: str
    taxonomy: str
    unit: str
    start: str | None
    end: str
    months: int | None  # None for instants
    value: float
    accn: str
    form: str
    filed: str
    fy: int | None
    fp: str | None
    frame: str | None
    tag_rank: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("tag_rank")
        return d


# ================================================================================ picking values from XBRL facts

# 3 / 6 / 9 / 12 for the usual reporting periods (52/53-week years included), else None.
def duration_months(start: str, end: str) -> int | None:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    for months, lo, hi in ((3, 80, 100), (6, 170, 200), (9, 260, 290), (12, 350, 380)):
        if lo <= days <= hi:
            return months
    return None


# Every usable fact for a field (all its tags), optionally only from one filing (accn).
def observations(facts: dict, concept: Concept, taxonomies=("us-gaap", "ifrs-full"), accn: str | None = None) -> list[Observation]:
    out: list[Observation] = []
    for rank, tag in enumerate(concept.tags):
        for tax in taxonomies:
            node = facts.get("facts", {}).get(tax, {}).get(tag)
            if not node:
                continue
            for e in node.get("units", {}).get(concept.unit, []):
                if "end" not in e or e.get("val") is None or (accn and e.get("accn") != accn):
                    continue
                start = e.get("start")
                months = duration_months(start, e["end"]) if start else None
                if concept.kind == "duration" and (not start or months is None):
                    continue
                if concept.kind == "instant" and start:
                    continue
                out.append(Observation(tag, tax, concept.unit, start, e["end"], months, float(e["val"]), e.get("accn", ""),
                                       e.get("form", ""), e.get("filed", ""), e.get("fy"), e.get("fp"), e.get("frame"), rank))
    return out


# True when `a` should replace `b`: higher-priority tag first, then the latest / original filing.
def _better(a: Observation, b: Observation, prefer: str) -> bool:
    if a.tag_rank != b.tag_rank:
        return a.tag_rank < b.tag_rank
    return a.filed < b.filed if prefer == "original" else a.filed > b.filed


def series(facts: dict, field_id: str, period: str = "annual", prefer: str = "latest", forms: set[str] | None = None,
           accn: str | None = None) -> list[Observation]:
    """One observation per period for a field, newest period first.

    period: "annual" (12-month durations / instants reported in a 10-K), "quarterly" (3-month, 10-Q),
            "ytd" (6- and 9-month year-to-date, 10-Q).
    Per period the highest-priority tag wins; within that tag `prefer` picks the filing.
    """
    c = CONCEPTS[field_id]
    allowed = forms or (ANNUAL_FORMS if period == "annual" else QUARTERLY_FORMS)
    want_months = {"annual": {12}, "quarterly": {3}, "ytd": {6, 9}}[period]
    best: dict[tuple, Observation] = {}
    for o in observations(facts, c, accn=accn):
        if o.form not in allowed:
            continue
        if c.kind == "duration" and o.months not in want_months:
            continue
        key = (o.start, o.end)
        if key not in best or _better(o, best[key], prefer):
            best[key] = o
    return sorted(best.values(), key=lambda o: (o.end, o.start or ""), reverse=True)


# Instant value on a given date from any filing form (used for cash at the start of a period).
def instant_at(facts: dict, field_id: str, end: str, prefer: str = "latest") -> Observation | None:
    best = None
    for o in observations(facts, CONCEPTS[field_id]):
        if o.end != end or o.start:
            continue
        if best is None or _better(o, best, prefer):
            best = o
    return best


# Date of the cash instant for a cash-flow period: its end (cash_end) or the day before it starts (cash_beginning).
def _cash_date(field_id: str, period: Observation) -> str:
    if field_id == "cf.cash_end":
        return period.end
    return (date.fromisoformat(period.start) - timedelta(days=1)).isoformat()


# Result rows for a field: value, period, provenance, and the printed-sign value for payment concepts.
def field_periods(facts: dict, field_id: str, period: str = "annual", last: int = 3, prefer: str = "latest") -> list[dict]:
    c = CONCEPTS[field_id]
    if field_id not in CASH_FIELDS:
        return [_row(c, o) for o in series(facts, field_id, period, prefer)[:last]]
    # cash flow periods come from a duration anchor; beginning/end are instants at its start-1 / end
    rows: list[dict] = []
    anchor = series(facts, "cf.net_change", period, prefer) or series(facts, "cf.operating", period, prefer)
    for a in anchor[:last]:
        o = instant_at(facts, field_id, _cash_date(field_id, a), prefer)
        if o:
            rows.append(_row(c, o, period_start=a.start, period_end=a.end, months=a.months))
    return rows


# One output row (JSON) for an observation.
def _row(c: Concept, o: Observation, period_start: str | None = None, period_end: str | None = None, months: int | None = None) -> dict:
    r = {
        "field_id": c.id, "label": c.label, "statement": c.statement,
        "period": {"start": period_start or o.start, "end": period_end or o.end, "months": months or o.months,
                   "instant": c.kind == "instant" and period_start is None},
        "value": o.value, "unit": o.unit,
        "source": {"concept": f"{o.taxonomy}:{o.tag}", "accn": o.accn, "form": o.form, "filed": o.filed, "frame": o.frame},
    }
    if c.payment:
        r["value_as_printed_sign"] = -o.value  # statements print these outflows as negatives
        r["note"] = "XBRL reports payments as positive amounts; value_as_printed_sign is the cash-flow-statement sign"
    return r


# ================================================================================ features

# The requested field ids (or the default list); unknown ids are an error.
def _fields(fields: list[str] | None) -> list[str]:
    ids = fields or DEFAULT_FIELDS
    unknown = [f for f in ids if f not in CONCEPTS]
    if unknown:
        raise ValueError(f"unknown field ids: {unknown}. Known: {sorted(CONCEPTS)}")
    return ids


# Structured numbers straight from the company's XBRL facts, in the filing's own units (USD, not millions).
def company_financials(client: SecClient, company: str, fields: list[str] | None = None, period: str = "annual", last: int = 3,
                       prefer: str = "latest", refresh: bool = False) -> dict:
    who = client.resolve(company)
    facts = client.companyfacts(who["cik"], refresh=refresh)
    out: dict = {}
    missing: list[str] = []
    for fid in _fields(fields):
        rows = field_periods(facts, fid, period, last, prefer)
        if rows:
            out[fid] = rows
        else:
            missing.append(fid)
    return {
        "source": "sec_edgar_xbrl", "company": {**who, "entity": facts.get("entityName")}, "period_type": period, "prefer": prefer,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fields": out, "not_available": missing,
        "notes": ["Values are as tagged in XBRL, in whole currency units (USD) - not scaled to millions.",
                  "XBRL covers financial statements and notes tagged by the filer; narrative text and page numbers are not in it."],
    }


# The newest row ending in end_year (or simply the newest row when end_year is None).
def _first_in_year(rows: list[dict], end_year: int | None) -> dict | None:
    for r in rows:  # newest first
        if end_year is None or r["period"]["end"].startswith(str(end_year)):
            return r
    return None


# Several companies side by side on the same fields for one period (default: each company's latest).
# Period ends are shown because fiscal years differ (Microsoft ends in June, Walmart in January).
def compare_companies(client: SecClient, companies: list[str], fields: list[str] | None = None, period: str = "annual",
                      end_year: int | None = None, prefer: str = "latest") -> dict:
    ids = _fields(fields)
    table: dict[str, dict] = {fid: {} for fid in ids}
    meta = {}
    for comp in companies:
        data = company_financials(client, comp, ids, period, last=8, prefer=prefer)
        key = data["company"].get("ticker") or data["company"]["cik"]
        meta[key] = data["company"]
        for fid, rows in data["fields"].items():
            pick = _first_in_year(rows, end_year)
            if pick:
                table[fid][key] = {"value": pick["value"], "unit": pick["unit"], "period_end": pick["period"]["end"],
                                   "months": pick["period"]["months"], "concept": pick["source"]["concept"], "accn": pick["source"]["accn"]}
    return {"source": "sec_edgar_xbrl", "period_type": period, "end_year": end_year or "latest", "companies": meta, "table": table,
            "notes": ["Fiscal year ends differ between companies; check period_end before comparing."]}


# The company's most recent filings (optionally only some forms), with the document URL.
def list_filings(client: SecClient, company: str, forms: list[str] | None = None, limit: int = 10) -> list[dict]:
    who = client.resolve(company)
    recent = client.submissions(who["cik"])["filings"]["recent"]
    rows = []
    for i in range(len(recent["accessionNumber"])):
        form = recent["form"][i]
        if forms and form not in forms:
            continue
        accn = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        rows.append({
            "form": form, "filed": recent["filingDate"][i], "report_date": recent["reportDate"][i], "accession": accn,
            "primary_document": doc, "description": recent["primaryDocDescription"][i],
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(who['cik'])}/{accn.replace('-', '')}/{doc}",
        })
        if len(rows) >= limit:
            break
    return rows


# Save a filing (a row from list_filings) as "<form>_<date>_<document>" in dest_dir.
def download_filing(client: SecClient, filing: dict, dest_dir: str | Path = "downloads") -> Path:
    name = f"{filing['form'].replace('/', '-')}_{filing['report_date'] or filing['filed']}_{filing['primary_document']}"
    return client.download(filing["url"], Path(dest_dir) / name)


# ================================================================================ cross-check with finx

# Allowed difference for a printed number: half of its last printed digit, in whole units.
def _tol(raw: str | None, scale: float) -> float:
    m = re.search(r"\.(\d+)", raw or "")
    return 0.5 * scale * (10 ** -(len(m.group(1)) if m else 0)) + 1e-6


# (end year, months, instant) a finx column corresponds to; year None when it cannot be placed.
def _targets(a: dict, is_q: bool) -> tuple[int | None, int | None, bool]:
    year, months = a["period"].get("year"), a["period"].get("months")
    instant = a["field_id"].startswith("bs.") or a["field_id"] in CASH_FIELDS
    if a["field_id"] == "cf.cash_beginning" and year:
        year -= 1  # beginning cash is the instant one year-end earlier (annual and year-to-date columns)
        if is_q and months == 3:
            return None, None, True
    return year, (None if instant else months), instant


# Multiplier for the finx unit ("millions" -> 1e6); per-share values are never scaled.
def _scale(a: dict, c: Concept) -> float:
    from finx.tables import SCALE

    return SCALE.get(a.get("unit") or "", 1.0) if c.unit != "USD/shares" else 1.0


# First XBRL candidate equal to the finx value (primary tag first) -> (status, observation), else ("mismatch", None).
def _best_match(c: Concept, cands: list[Observation], finx_abs: float, tol: float) -> tuple[str, Observation | None]:
    primary_rank = min(o.tag_rank for o in cands)
    for o in sorted(cands, key=lambda o: o.tag_rank):
        status = "match" if o.tag_rank == primary_rank else "match_alt_tag"
        tries = [(o.value, status)]
        if c.payment:
            tries.append((-o.value, status))
        if c.sign_ambiguous:
            tries.append((-o.value, "match_sign"))
        for v, st in tries:
            if abs(finx_abs - v) <= tol:
                return st, o
    return "mismatch", None


# Compare every finx answer with the XBRL facts (of one filing when accn is given) -> (counts, details).
def _compare(facts: dict, answers: list[dict], is_q: bool, accn: str | None) -> tuple[dict, list[dict]]:
    counts = {"match": 0, "match_alt_tag": 0, "match_sign": 0, "mismatch": 0, "no_xbrl_value": 0}
    details: list[dict] = []
    obs_cache: dict[str, list] = {}
    for a in answers:
        fid = a["field_id"]
        c = CONCEPTS[fid]
        year, months, instant = _targets(a, is_q)
        if year is None:
            continue
        if fid not in obs_cache:
            obs_cache[fid] = observations(facts, c, accn=accn)
        cands = [o for o in obs_cache[fid]
                 if o.end.startswith(str(year)) and ((o.start is None) if instant else (o.months == months and o.start is not None))]
        if fid in CASH_FIELDS and cands:
            # cash at the start/end of a cash-flow period is the instant on that period's own dates, taken from the
            # cash-flow duration in the same filing (a 10-Q also carries other 2025 dates, e.g. the prior year-end)
            col_year, col_months = a["period"]["year"], a["period"].get("months")
            anchors = [o for o in observations(facts, CONCEPTS["cf.operating"], accn=accn)
                       if o.end.startswith(str(col_year)) and o.months == col_months]
            if anchors:
                want = _cash_date(fid, anchors[0])
                cands = [o for o in cands if o.end == want]
        if not cands:
            counts["no_xbrl_value"] += 1
            details.append({"field_id": fid, "period": a["period"], "finx_value": a["value"], "status": "no_xbrl_value", "page": a["page"]})
            continue
        # several instants in one year (quarter ends): keep the latest end date for balance-sheet columns
        if instant and fid not in CASH_FIELDS:
            last = max(o.end for o in cands)
            cands = [o for o in cands if o.end == last]
        sc = _scale(a, c)
        finx_abs = a["value"] * sc
        status, hit = _best_match(c, cands, finx_abs, _tol(a.get("raw"), sc))
        counts[status] += 1
        if status == "match":
            continue
        x = hit or sorted(cands, key=lambda o: o.tag_rank)[0]
        details.append({"field_id": fid, "period": a["period"], "finx_value": a["value"], "finx_value_in_units": finx_abs,
                        "xbrl_value": x.value, "xbrl_concept": f"{x.taxonomy}:{x.tag}", "xbrl_period_end": x.end, "xbrl_accn": x.accn,
                        "all_xbrl_candidates": sorted({(f"{o.tag}", o.value) for o in cands}), "status": status,
                        "label_as_printed": a["label_as_printed"], "page": a["page"], "finx_verified": a["verified"]})
    return counts, details


# Filings (oldest first) of the right type that report anchor fields for the given years.
def _candidate_filings(facts: dict, years: set[int], is_q: bool) -> list[dict]:
    forms = QUARTERLY_FORMS if is_q else ANNUAL_FORMS
    found: dict[str, dict] = {}
    for fid in ANCHORS:
        for o in observations(facts, CONCEPTS[fid]):
            if o.form in forms and int(o.end[:4]) in years:
                found.setdefault(o.accn, {"accn": o.accn, "form": o.form, "filed": o.filed})
    return sorted(found.values(), key=lambda f: f["filed"])


# Compare finx's extracted statement values with the company's XBRL numbers for the SAME filing.
def cross_check(client: SecClient, finx_json: dict, company: str) -> dict:
    from finx.statements import field_answers

    who = client.resolve(company)
    facts = client.companyfacts(who["cik"])
    all_answers = field_answers(finx_json)
    answers = [a for a in all_answers if a["status"] == "found" and a["period"] and a["field_id"] in CONCEPTS
               and a["period"].get("year") and not a["period"].get("change")]
    skipped_ambiguous = sum(1 for a in all_answers if a["status"] == "ambiguous")
    is_q = finx_json.get("doc_type") == "10-Q"
    years = {a["period"]["year"] for a in answers}
    # the filing whose numbers agree with the PDF most is the one the PDF is
    best = None
    for f in _candidate_filings(facts, years, is_q):
        counts, details = _compare(facts, answers, is_q, f["accn"])
        score = counts["match"] + counts["match_alt_tag"] + counts["match_sign"]
        if best is None or score > best[0]:
            best = (score, f, counts, details)
    if best is None:  # nothing to restrict to: compare against every filing's values
        counts, details = _compare(facts, answers, is_q, None)
        best = (0, None, counts, details)
    _, filing, counts, details = best
    compared = sum(counts[k] for k in ("match", "match_alt_tag", "match_sign", "mismatch"))
    ok = counts["match"] + counts["match_alt_tag"] + counts["match_sign"]
    return {
        "company": who, "source_file": finx_json.get("source_file"), "doc_type": finx_json.get("doc_type"),
        "xbrl_filing_used": filing, "counts": counts, "ambiguous_fields_skipped": skipped_ambiguous,
        "strict_match_pct": round(100 * counts["match"] / compared, 1) if compared else None,
        "agreement_pct": round(100 * ok / compared, 1) if compared else None,
        "details": details,
    }
