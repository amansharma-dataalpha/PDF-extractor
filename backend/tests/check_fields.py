"""
# check_fields.py - how many of the required fields are in each pipeline JSON?

    .venv/Scripts/python -m finx.pipeline tests/testdata/pdfs -o tests/output     # 1. run the whole pipeline
    .venv/Scripts/python tests/check_fields.py                                    # 2. count the fields in its JSON files
    (both from the backend folder)

- The fields per filing type are in `testdata/spec/fields_by_form.json`: 10-K 118 (73 sections + 45 numbers),
  10-Q 89 (44 sections + 45 numbers). The filing type comes from the JSON (`doc_type`).
- A field is **found** when the JSON has it: an Item in the page index, a statement in `statements`, a note as a
  "Note ..." heading, a number field with a value, or - for cover, audit, controls, signature and exhibit fields,
  which have no named field - their wording in the page text. Otherwise it is **not found**.
- Uses only the JSON files: no AI, no SEC data. Prints one table per filing type and the fields not found per filing.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from finx.statements import field_answers  # noqa: E402

OUT = HERE / "output"
FIELDS = json.loads((HERE / "testdata" / "spec" / "fields_by_form.json").read_text(encoding="utf-8"))
STATEMENTS = {"fs.income_statement": "income_statement", "fs.comprehensive_income": "comprehensive_income",
              "fs.balance_sheet": "balance_sheet", "fs.cash_flow": "cash_flow", "fs.equity": "equity"}
# Words in a "Note ..." heading that identify each note.
NOTES = {"note.accounting_policies": r"accounting policies|basis of presentation", "note.eps": r"per share",
         "note.debt": r"debt|borrowings", "note.income_taxes": r"income tax", "note.revenue": r"revenue",
         "note.leases": r"lease", "note.contingencies": r"contingenc", "note.equity": r"equity",
         "note.segments": r"segment", "note.goodwill": r"goodwill"}
# Wording that shows each cover, audit, controls, signature and exhibit field is in the text (cover tables interleave
# their two columns, so some labels allow a few words in between).
TEXT = {
    "cover.company_name": r"exact name of registrant|commission file number.{0,40}\b(corporation|inc\.|company)\b",
    "cover.fiscal_year_end": r"for the fiscal year ended", "cover.period_end": r"for the quarterly period ended",
    "cover.commission_file_number": r"commission file (number|no\.)",
    "cover.state_of_incorporation": r"state or other jurisdiction|state of incorporation",
    "cover.irs_ein": r"employer\W+(\w+\W+){0,5}identification|i\.r\.s\. id\b",
    "cover.address": r"address of principal executive offices|\b(way|street|drive|avenue|parkway|road)\W+[a-z ]+, [a-z ]+ \d{5}",
    "cover.phone": r"telephone number|\(\d{3}\)\s*\d{3}-\d{4}", "cover.securities_12b": r"trading\W+(\w+\W+){0,5}symbol",
    "cover.filer_status": r"large accelerated filer", "cover.shell_company": r"shell company",
    "cover.icfr_attestation": r"attestation to its management", "cover.restatement_flag": r"correction of an error|error to previously issued",
    "cover.public_float": r"aggregate market value",
    "cover.shares_outstanding": r"shares of (the registrant's )?common stock outstanding|outstanding as of",
    "cover.docs_incorporated": r"documents incorporated by reference",
    "fs.notes": r"notes to (the )?(unaudited )?(condensed )?(consolidated )?financial statements",
    "audit.report": r"report of independent registered public accounting firm",
    "audit.firm_name": r"ernst & young|pricewaterhousecoopers|kpmg|deloitte|grant thornton|bdo usa|rsm us",
    "audit.opinion_type": r"present fairly, in all material respects", "audit.cams": r"critical audit matter",
    "audit.tenure": r"served as the company.s auditor since|served as our auditor since", "audit.pcaob_id": r"pcaob id",
    "controls.disclosure_controls": r"disclosure controls and procedures.{0,200}?\b(were|are) (not )?effective",
    "controls.mgmt_icfr": r"management.s (annual )?report on internal control|report of management on internal control",
    "controls.auditor_icfr": r"effective internal control over financial reporting as of",
    "sign.page": r"\bsignatures?\b", "sign.date": r"\bdate[d]?:?\s+[a-z]+ \d{1,2}, \d{4}", "sign.ceo": r"chief executive officer",
    "sign.cfo": r"chief financial officer", "sign.cao": r"(principal|chief) accounting officer", "sign.directors": r"\bdirector\b",
    "exhibit.31": r"\b31\.0?1\b", "exhibit.32": r"\b32\.0?1\b", "exhibit.97": r"\b97(\.1)?\b.{0,80}(recovery|clawback)",
    "exhibit.101": r"inline xbrl|ixbrl|101\.ins",
}


# A page's text on one line (wording often breaks across lines).
def _text(page: dict) -> str:
    return " ".join(page["text"].split())


# Is this section field in the JSON? (named section, statement or note heading, or its wording in the page text)
def section_found(field: dict, res: dict) -> bool:
    field_id = field["id"]
    if field_id in STATEMENTS:
        return STATEMENTS[field_id] in res["statements"]
    if field_id.startswith("item."):
        part, _, item = field_id[5:].rpartition("_")  # item.1a -> "Item 1A", item.p2_1a -> "Part II Item 1A"
        name = (f"Part {'I' * int(part[1])} " if part else "") + f"Item {item.upper()}"
        return any(s == name for p in res["pages"] for s in p["sections_started"])
    if field_id in NOTES:  # a "Note ..." heading, or a note title broken over two lines in the text
        heading = any(re.match(r"note\s+\d", h["text"], re.I) and re.search(NOTES[field_id], h["text"], re.I)
                      for p in res["pages"] for h in p["headings"])
        title = rf"\bnote\s+\d+\s*\W{{0,3}}\s*[^.]{{0,80}}?({NOTES[field_id]})"
        return heading or any(re.search(title, _text(p), re.I) for p in res["pages"])
    return any(re.search(TEXT[field_id], _text(p), re.I) for p in res["pages"])


# Count found / not found per filing, print a table per filing type and list what was not found.
def main() -> int:
    rows, missing = {"10-K": [], "10-Q": []}, {}
    for path in sorted(OUT.glob("*.json")):
        res = json.loads(path.read_text(encoding="utf-8"))
        form = res["doc_type"]
        if form not in FIELDS:
            print(f"{path.name}: {form}, not a 10-K or 10-Q - skipped")
            continue
        numbers = {a["field_id"] for a in field_answers(res) if a["status"] in ("found", "ambiguous")}
        sec_ok = [f["id"] for f in FIELDS[form]["sections"] if section_found(f, res)]
        num_ok = [f["id"] for f in FIELDS[form]["numbers"] if f["id"] in numbers]
        n_sec, n_num = len(FIELDS[form]["sections"]), len(FIELDS[form]["numbers"])
        rows[form].append((path.stem, len(sec_ok), n_sec, len(num_ok), n_num))
        missing[path.stem] = [f["id"] for f in FIELDS[form]["sections"] + FIELDS[form]["numbers"] if f["id"] not in sec_ok + num_ok]
    for form, items in rows.items():
        if not items:
            continue
        total = len(FIELDS[form]["sections"]) + len(FIELDS[form]["numbers"])
        print(f"\n{form}: {total} fields ({len(FIELDS[form]['sections'])} sections + {len(FIELDS[form]['numbers'])} numbers)")
        print(f"  {'filing':22} {'sections found':>15} {'numbers found':>14} {'found':>7} {'not found':>10} {'found %':>8}")
        for name, s, ns, n, nn in items:
            print(f"  {name:22} {f'{s}/{ns}':>15} {f'{n}/{nn}':>14} {s + n:>7} {ns + nn - s - n:>10} {100 * (s + n) / (ns + nn):>7.1f}%")
        found = sum(s + n for _, s, _, n, _ in items)
        print(f"  {'all ' + form:22} {'':>15} {'':>14} {found:>7} {total * len(items) - found:>10} {100 * found / (total * len(items)):>7.1f}%")
    print("\nnot found:")
    for name, ids in missing.items():
        print(f"  {name}: {', '.join(ids) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
