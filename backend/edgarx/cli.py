"""
# cli.py - use edgarx from the command line

Needs SEC_USER_AGENT="Company your.name@company.com" (SEC fair-access rule).

    python -m edgarx.cli facts MSFT --fields is.total_revenue,bs.total_assets --last 3
    python -m edgarx.cli compare MSFT AMZN WMT --fields is.total_revenue,is.net_income --end-year 2025
    python -m edgarx.cli filings MSFT --form 10-K --limit 3
    python -m edgarx.cli download MSFT --form 10-K --index 0 -d downloads
    python -m edgarx.cli crosscheck output/finx/MSFT_10K_FY2026.json MSFT
    python -m edgarx.cli fields
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .financials import company_financials, compare_companies, cross_check, download_filing, list_filings
from .sec_client import SecClient, SecConfigError, SecHttpError
from .xbrl_fields import CONCEPTS


# "a,b, c" -> ["a", "b", "c"] (None when nothing was given).
def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


# Parse the command, run it, print JSON. Exit code: 0 ok, 1 error, 2 configuration error.
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SEC EDGAR XBRL (free, no API key; needs a contact in SEC_USER_AGENT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("facts", help="financial fields for one company")
    f.add_argument("company")
    f.add_argument("--fields")
    f.add_argument("--period", default="annual", choices=["annual", "quarterly", "ytd"])
    f.add_argument("--last", type=int, default=3)
    f.add_argument("--prefer", default="latest", choices=["latest", "original"])

    c = sub.add_parser("compare", help="compare companies on the same fields")
    c.add_argument("companies", nargs="+")
    c.add_argument("--fields")
    c.add_argument("--period", default="annual", choices=["annual", "quarterly", "ytd"])
    c.add_argument("--end-year", type=int)

    l = sub.add_parser("filings", help="list recent filings")
    l.add_argument("company")
    l.add_argument("--form", help="comma separated, e.g. 10-K,10-Q")
    l.add_argument("--limit", type=int, default=10)

    d = sub.add_parser("download", help="download a filing's main document")
    d.add_argument("company")
    d.add_argument("--form", default="10-K")
    d.add_argument("--index", type=int, default=0, help="0 = most recent")
    d.add_argument("-d", "--dest", default="downloads")

    x = sub.add_parser("crosscheck", help="compare a finx JSON with the company's XBRL numbers")
    x.add_argument("finx_json")
    x.add_argument("company")
    x.add_argument("-o", "--out")

    sub.add_parser("fields", help="list supported field ids")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "fields":
            for fid, c in CONCEPTS.items():
                print(f"{fid:30} {c.statement:20} {c.tags[0]}")
            return 0
        client = SecClient()
        if a.cmd == "facts":
            out = company_financials(client, a.company, _csv(a.fields), a.period, a.last, a.prefer)
        elif a.cmd == "compare":
            out = compare_companies(client, a.companies, _csv(a.fields), a.period, a.end_year)
        elif a.cmd == "filings":
            out = list_filings(client, a.company, _csv(a.form), a.limit)
        elif a.cmd == "download":
            rows = list_filings(client, a.company, [a.form], a.index + 1)
            if len(rows) <= a.index:
                print(f"no {a.form} filing at index {a.index}", file=sys.stderr)
                return 1
            out = {"saved": str(download_filing(client, rows[a.index], a.dest)), "filing": rows[a.index]}
        else:
            out = cross_check(client, json.loads(Path(a.finx_json).read_text(encoding="utf-8")), a.company)
            if a.out:
                Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    except SecConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    except (SecHttpError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
