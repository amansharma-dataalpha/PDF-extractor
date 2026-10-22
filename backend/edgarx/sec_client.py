"""
# sec_client.py - talks to the SEC EDGAR servers politely

- No API key is needed, but SEC requires a contact (name + e-mail) in the User-Agent.
  The client refuses to start without one (`SEC_USER_AGENT` or the `user_agent` argument).
- Stays under the SEC limit of 10 requests per second (default 8).
- Retries busy/failed requests with growing pauses.
- Saves every JSON answer on disk (`.cache/edgarx`) and reuses it until it gets too old.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

DATA = "https://data.sec.gov"
WWW = "https://www.sec.gov"


# Raised when the client is set up wrongly (e.g. no contact User-Agent).
class SecConfigError(RuntimeError):
    """Missing or unusable configuration (e.g. no contact User-Agent)."""


# Raised when SEC answers with an error (or cannot be reached: status 0).
class SecHttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"SEC returned HTTP {status} for {url}: {body[:200]}")
        self.status, self.url = status, url


# A CIK as the 10-digit, zero-padded text SEC uses in its URLs.
def cik10(cik: int | str) -> str:
    return str(int(cik)).zfill(10)


# The SEC connection: contact header, speed limit, retries and disk cache.
class SecClient:
    def __init__(self, user_agent: str | None = None, cache_dir: str | Path | None = None, max_rps: float = 8.0,
                 timeout: float = 30.0, retries: int = 4):
        ua = (user_agent or os.getenv("SEC_USER_AGENT") or "").strip()
        if not ua or not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", ua):
            raise SecConfigError(
                "SEC requires a contact in the User-Agent. Set SEC_USER_AGENT to e.g. 'Your Company your.name@company.com' "
                "(or pass user_agent=). Nothing is sent without it.")
        if max_rps > 10:
            raise SecConfigError("max_rps must be <= 10 (SEC fair-access limit)")
        self.user_agent = ua
        self.min_interval = 1.0 / max_rps
        self.timeout, self.retries = timeout, retries
        self.cache_dir = Path(cache_dir) if cache_dir else Path(os.getenv("EDGARX_CACHE", ".cache/edgarx"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self.requests_made = 0

    # Wait long enough since the previous request to respect the speed limit.
    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    # Download one URL; retry network errors and "busy" answers (429/5xx), each time waiting twice as long.
    def _fetch(self, url: str, stream: bool = False) -> requests.Response:
        delay = 1.0
        for attempt in range(self.retries + 1):
            self._throttle()
            self.requests_made += 1
            try:
                r = self.session.get(url, timeout=self.timeout, stream=stream)
            except requests.RequestException as e:
                if attempt == self.retries:
                    raise SecHttpError(0, url, str(e)) from e
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            raise SecHttpError(r.status_code, url, r.text if not stream else "")
        raise SecHttpError(0, url, "retries exhausted")

    # Cache file for a URL: the SHA-1 of the URL + ".json".
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    # JSON for a URL: from the disk cache while younger than ttl_hours, otherwise downloaded and saved.
    def get_json(self, url: str, ttl_hours: float = 6.0, refresh: bool = False) -> Any:
        p = self._cache_path(url)
        if not refresh and p.exists() and (time.time() - p.stat().st_mtime) < ttl_hours * 3600:
            return json.loads(p.read_text(encoding="utf-8"))
        data = self._fetch(url).json()
        p.write_text(json.dumps(data), encoding="utf-8")
        return data

    # Every listed ticker -> {"cik": 10-digit text, "title": company name}.
    def tickers(self) -> dict[str, dict]:
        raw = self.get_json(f"{WWW}/files/company_tickers.json", ttl_hours=24)
        return {v["ticker"].upper(): {"cik": cik10(v["cik_str"]), "title": v["title"]} for v in raw.values()}

    # Accept a ticker ('MSFT') or a CIK ('789019' / '0000789019'); return {'cik', 'ticker', 'title'}.
    def resolve(self, company: str) -> dict:
        c = company.strip()
        if c.isdigit():
            subs = self.submissions(c)
            return {"cik": cik10(c), "ticker": (subs.get("tickers") or [None])[0], "title": subs.get("name")}
        t = self.tickers().get(c.upper().replace(".", "-"))
        if not t:
            raise SecHttpError(404, f"ticker {company!r}", "unknown ticker (try the CIK)")
        return {"cik": t["cik"], "ticker": c.upper(), "title": t["title"]}

    # The company's profile and list of recent filings.
    def submissions(self, cik: int | str, refresh: bool = False) -> dict:
        return self.get_json(f"{DATA}/submissions/CIK{cik10(cik)}.json", ttl_hours=1, refresh=refresh)

    # Every XBRL number the company ever filed.
    def companyfacts(self, cik: int | str, refresh: bool = False) -> dict:
        return self.get_json(f"{DATA}/api/xbrl/companyfacts/CIK{cik10(cik)}.json", ttl_hours=6, refresh=refresh)

    # One XBRL tag for one company (all periods).
    def companyconcept(self, cik: int | str, taxonomy: str, tag: str, refresh: bool = False) -> dict:
        return self.get_json(f"{DATA}/api/xbrl/companyconcept/CIK{cik10(cik)}/{taxonomy}/{tag}.json", ttl_hours=6, refresh=refresh)

    # One XBRL tag for every company in one period, e.g. period='CY2025Q4I' or 'CY2025'.
    def frames(self, taxonomy: str, tag: str, unit: str, period: str, refresh: bool = False) -> dict:
        return self.get_json(f"{DATA}/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json", ttl_hours=24, refresh=refresh)

    # Save a filing document to disk in chunks (not cached as JSON).
    def download(self, url: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._fetch(url, stream=True)
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return dest
