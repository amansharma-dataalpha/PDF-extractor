"""
# edgarx - company financial numbers straight from SEC EDGAR XBRL

- Free, no API key; SEC only asks for a contact e-mail in `SEC_USER_AGENT`.
- Separate from finx (which reads PDFs); `cross_check` compares the two.
- Files: `sec_client.py` (talks to SEC), `xbrl_fields.py` (field list), `financials.py` (features), `cli.py` (command line).
"""
from .sec_client import SecClient, SecConfigError, SecHttpError  # noqa: F401
from .financials import company_financials, compare_companies, cross_check, download_filing, list_filings  # noqa: F401
