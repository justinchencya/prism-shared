#!/usr/bin/env python3
"""
Fetch institutional-breadth trend for a ticker from SEC 13F filings.

Usage:
  fetch_13f_breadth.py <TICKER> <output-path> [--cusip <CUSIP>] [--quarters N]

Writes:
  <output-path>   JSON with one record per calendar quarter (oldest -> newest):
    period          — quarter end date (YYYY-MM-DD)
    holders         — distinct institutions (13F filers, deduped by CIK) reporting
                      the CUSIP for that period
    new_holders     — CIKs present this quarter but not the previous one
    exited_holders  — CIKs present last quarter but gone this quarter
    complete        — false while the period's 45-day filing window (plus a
                      publication buffer) hasn't fully elapsed; treat partial
                      quarters as a floor, not a reading

  This is a *breadth* lens: how many professional managers hold the name and
  whether that set is expanding or contracting quarter over quarter. It measures
  confirmation, not discovery — 13F positions are up to 45 days stale at filing
  and describe last quarter's book. One quarter is noise; direction across 2-3+
  consecutive quarters is the signal. Like the technicals block, this never
  drives the Thesis verdict on its own.

Source:
  SEC EDGAR full-text search (free, no auth beyond the contact-email User-Agent):
    https://efts.sec.gov/LATEST/search-index?q="<CUSIP>"&forms=13F-HR&startdt=..&enddt=..
  Each hit is one 13F-HR (or amendment) whose info table contains the CUSIP.
  We page through all hits, keep those whose period_ending matches the target
  quarter, and dedupe by filer CIK (this also collapses 13F-HR/A amendments).

CUSIP resolution:
  13F info tables key on CUSIP, not ticker, and no free API maps ticker->CUSIP
  directly. The script keeps a lazily-grown cache at .claude/cusip-map.json.
  For an uncached ticker, pass --cusip (the caller looks it up once — it's on
  the company's IR page, prospectuses, or any 13F aggregator). Foreign issuers
  use a CINS with a letter prefix (e.g. Seagate: G7997R103) — pass it verbatim.

Handles gracefully: transient EDGAR 5xx get retried with backoff; a quarter
that still fails is recorded with nulls + a note; exits 0 on partial failures.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"
CUSIP_MAP_PATH = Path(__file__).resolve().parent.parent / ".claude" / "cusip-map.json"

# EDGAR FTS hard-caps results at 10,000 per query; page size is 100.
PAGE_SIZE = 100
MAX_FROM = 9_900
# Filing window: 13F-HR is due 45 days after quarter end; we scan 100 days to
# catch late filers and most amendments. Publication buffer before a quarter
# counts as complete: 45-day deadline + a few days of straggler indexing.
WINDOW_DAYS = 100
COMPLETE_AFTER_DAYS = 50
# Youngest quarter worth querying at all — before this almost nothing is filed.
MIN_AGE_DAYS = 14


def log(msg: str) -> None:
    print(f"fetch_13f_breadth: {msg}", file=sys.stderr)


def load_dotenv(repo_root: Path) -> None:
    """Load KEY=VALUE pairs from <repo-root>/.env into os.environ if not already set."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def ua() -> str:
    # SEC requires a contact email in the User-Agent. Read it lazily (after
    # load_dotenv) so EDGAR_CONTACT_EMAIL from .env is honored; the engine
    # carries no personal address — generic fallback when unset.
    return f"Prism researcher ({os.environ.get('EDGAR_CONTACT_EMAIL', 'prism-user@example.com')})"


def get_json(url: str, timeout: int = 20, retries: int = 4, backoff: float = 2.0) -> dict:
    """GET + parse JSON. EDGAR FTS intermittently returns transient 5xx / error
    bodies; retry those with linear backoff. HTTP 4xx won't resolve on retry
    (bad query, throttled window, blocked UA) — bail fast so a dead run
    degrades in seconds, not minutes."""
    req = urllib.request.Request(url, headers={"User-Agent": ua(), "Accept": "application/json"})
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if "hits" not in data:  # transient {"message": "Internal server error"} body
                raise ValueError(f"unexpected response: {str(data)[:120]}")
            return data
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
        except Exception as e:  # noqa: BLE001 — retry any other transient failure
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_err


def load_cusip_map() -> dict:
    try:
        return json.loads(CUSIP_MAP_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_cusip_map(mapping: dict) -> None:
    CUSIP_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSIP_MAP_PATH.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def resolve_cusip(ticker: str, override: str) -> str:
    mapping = load_cusip_map()
    if override:
        cusip = override.strip().upper()
        if mapping.get(ticker) != cusip:
            mapping[ticker] = cusip
            save_cusip_map(mapping)
            log(f"cached CUSIP for {ticker}: {cusip} -> {CUSIP_MAP_PATH}")
        return cusip
    if ticker in mapping:
        return mapping[ticker]
    log(f"ERROR: no CUSIP cached for {ticker} in {CUSIP_MAP_PATH}.")
    log("Look it up once (IR page, prospectus, or any 13F aggregator) and re-run with "
        f"--cusip <CUSIP>. Foreign issuers use a letter-prefixed CINS — pass it verbatim.")
    sys.exit(1)


def quarter_ends(n: int, today: date) -> list:
    """Most recent n calendar quarter ends at least MIN_AGE_DAYS old, oldest first."""
    ends = []
    y, m = today.year, today.month
    q_month = ((m - 1) // 3) * 3  # last month of the *previous* quarter
    if q_month == 0:
        y, q_month = y - 1, 12
    while len(ends) < n:
        last_day = (date(y, q_month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if (today - last_day).days >= MIN_AGE_DAYS:
            ends.append(last_day)
        q_month -= 3
        if q_month == 0:
            y, q_month = y - 1, 12
    return list(reversed(ends))


def holders_for_quarter(cusip: str, q_end: date) -> tuple:
    """(distinct filer CIKs, truncated?) for 13F-HRs (incl. /A) whose info table
    contains the CUSIP for period q_end. truncated=True when the result set hit
    EDGAR's 10,000-hit cap — the count is then a floor, not a reading."""
    startdt = (q_end + timedelta(days=1)).isoformat()
    enddt = (q_end + timedelta(days=WINDOW_DAYS)).isoformat()
    period = q_end.isoformat()
    ciks: set = set()
    truncated = False
    offset = 0
    while True:
        params = {
            "q": f'"{cusip}"',
            "forms": "13F-HR",
            "startdt": startdt,
            "enddt": enddt,
            "hits": str(PAGE_SIZE),
            "from": str(offset),
        }
        data = get_json(f"{EDGAR_FTS}?{urllib.parse.urlencode(params)}")
        hits = (data.get("hits") or {}).get("hits") or []
        for h in hits:
            src = h.get("_source") or {}
            if src.get("period_ending") != period:
                continue  # straggler filing for a different period inside this window
            filer_ciks = src.get("ciks") or []
            if filer_ciks:
                ciks.add(str(filer_ciks[0]).lstrip("0"))
        total = ((data.get("hits") or {}).get("total") or {}).get("value") or 0
        truncated = truncated or total > MAX_FROM + PAGE_SIZE
        offset += PAGE_SIZE
        if offset >= min(total, MAX_FROM) or not hits:
            break
        time.sleep(0.1)  # be polite to EDGAR between pages (their cap is 10 req/s)
    return ciks, truncated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("output")
    ap.add_argument("--cusip", default="")
    ap.add_argument("--quarters", type=int, default=5)
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent)
    ticker = args.ticker.strip().upper()
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cusip = resolve_cusip(ticker, args.cusip)

    today = datetime.now(timezone.utc).date()
    notes: list = []
    quarters: list = []
    prev_ciks = None

    for q_end in quarter_ends(args.quarters, today):
        complete = (today - q_end).days >= COMPLETE_AFTER_DAYS
        log(f"scanning 13F filings for {ticker} ({cusip}), period {q_end}…")
        try:
            ciks, truncated = holders_for_quarter(cusip, q_end)
        except Exception as e:  # noqa: BLE001 — one bad quarter must not kill the series
            notes.append(f"{q_end}: fetch failed after retries: {e}")
            quarters.append({"period": q_end.isoformat(), "holders": None,
                             "new_holders": None, "exited_holders": None,
                             "complete": complete})
            prev_ciks = None  # can't diff across a gap
            continue
        if truncated:
            notes.append(f"{q_end}: hit EDGAR's 10,000-result cap — holders is a floor "
                         "and new/exited diffs for adjacent quarters are unreliable")
        quarters.append({
            "period": q_end.isoformat(),
            "holders": len(ciks),
            "new_holders": len(ciks - prev_ciks) if prev_ciks is not None else None,
            "exited_holders": len(prev_ciks - ciks) if prev_ciks is not None else None,
            "complete": complete,
        })
        prev_ciks = ciks

    if quarters and not quarters[-1]["complete"]:
        notes.append(f"{quarters[-1]['period']}: filing window still open — holders is a "
                     "floor (early filers only), not a reading")

    result = {
        "ticker": ticker,
        "cusip": cusip,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "SEC EDGAR full-text search over 13F-HR filings, deduped by filer CIK",
        "quarters": quarters,
        "notes": notes,
    }
    out_path.write_text(json.dumps(result, indent=2))
    for n in notes:
        log(f"NOTE {n}")
    log(f"wrote {out_path}")
    print(str(out_path))


if __name__ == "__main__":
    main()
