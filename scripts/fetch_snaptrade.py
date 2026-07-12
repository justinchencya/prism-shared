#!/usr/bin/env python3
"""
Fetch a normalized brokerage snapshot from SnapTrade (Personal API key).

Usage:
  fetch_snaptrade.py [output-path] [--lookback-days N]

  output-path       defaults to tracking/brokerage-snapshot.json (repo-relative)
  --lookback-days   activity history window, default 90

Reads:
  SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY from the environment or
  <repo-root>/.env. These are *Personal* API key credentials (one individual's
  own brokerage connections) — requests are HMAC-signed with the consumerKey
  and SnapTrade resolves the user from the key itself; there is no
  userId/userSecret pair and the Register User endpoint is never called.

Writes:
  <output-path>   JSON snapshot:
    fetched_at    — ISO-8601 UTC timestamp (consumers judge staleness from this)
    accounts[]    — per connected account: institution, masked number, total
                    balance, cash, positions[], recent activities[]
    totals        — market value / cash summed across accounts
    warnings[]    — per-source degradations (a dead sub-endpoint never crashes
                    the whole fetch)

Exit codes:
  0  snapshot written (possibly with warnings)
  2  credentials missing — the caller's "not runnable in this environment" signal
  1  fatal (credentials rejected, network dead, no accounts readable)

Design:
  - stdlib only (urllib, hmac, hashlib). No third-party packages. Python 3.8+.
  - Standardized data layer: no LLM, no judgement — the /sync-portfolio command
    reasons over the snapshot.
  - Account numbers are masked to the last 4 digits before writing; the full
    number never touches disk.

API notes (https://docs.snaptrade.com/docs/requests):
  Every request carries query params clientId + timestamp and a Signature
  header = base64(HMAC-SHA256(consumerKey, canonical-JSON of
  {"content": <body or null>, "path": <path>, "query": <query-string>})).
"""

import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

BASE = "https://api.snaptrade.com"
API_PREFIX = "/api/v1"
DEFAULT_LOOKBACK_DAYS = 90
ACTIVITIES_PAGE_SIZE = 250


def log(msg: str) -> None:
    print(f"fetch_snaptrade: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    log(msg)
    sys.exit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class SnapTradeClient:
    def __init__(self, client_id: str, consumer_key: str):
        self.client_id = client_id
        self.consumer_key = consumer_key.encode()

    def _sign(self, path: str, query: str, content=None) -> str:
        sig_object = {"content": content, "path": path, "query": query}
        sig_content = json.dumps(sig_object, separators=(",", ":"), sort_keys=True)
        digest = hmac.new(self.consumer_key, sig_content.encode(), sha256).digest()
        return b64encode(digest).decode()

    def get(self, endpoint: str, params: dict = None, timeout: int = 30):
        """Signed GET. endpoint is relative to /api/v1. Raises on HTTP/parse error."""
        path = API_PREFIX + endpoint
        query_params = {"clientId": self.client_id,
                        "timestamp": str(int(datetime.now(timezone.utc).timestamp()))}
        query_params.update(params or {})
        # Personal API key auth: no userId/userSecret anywhere — SnapTrade
        # resolves the user from the key. The signed query string must be
        # byte-identical to the one sent.
        query = urllib.parse.urlencode(query_params)
        req = urllib.request.Request(
            BASE + path + "?" + query,
            headers={"Signature": self._sign(path, query),
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())


def http_error_detail(err: urllib.error.HTTPError) -> str:
    try:
        return f"HTTP {err.code}: {err.read().decode()[:300]}"
    except Exception:
        return f"HTTP {err.code}"


def dig(obj, *keys, default=None):
    """Safely walk nested dicts; returns default on any miss."""
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def mask_number(number) -> str:
    s = str(number or "").strip()
    return ("****" + s[-4:]) if len(s) >= 4 else ""


def extract_symbol(position: dict) -> dict:
    """Position symbols nest as position.symbol.symbol.{symbol,description,...}."""
    inner = dig(position, "symbol", "symbol", default={})
    if not isinstance(inner, dict):
        inner = {}
    return {
        "symbol": inner.get("symbol") or dig(position, "symbol", "symbol") or "",
        "raw_symbol": inner.get("raw_symbol") or "",
        "description": inner.get("description") or "",
        "currency": dig(inner, "currency", "code", default="USD"),
    }


def normalize_position(position: dict) -> dict:
    sym = extract_symbol(position)
    units = position.get("units")
    if units is None:
        units = position.get("fractional_units")
    price = position.get("price")
    market_value = None
    if isinstance(units, (int, float)) and isinstance(price, (int, float)):
        market_value = round(units * price, 2)
    return {
        "symbol": sym["symbol"],
        "description": sym["description"],
        "units": units,
        "price": price,
        "market_value": market_value,
        "currency": sym["currency"],
        "average_purchase_price": position.get("average_purchase_price"),
        "open_pnl": position.get("open_pnl"),
    }


def normalize_activity(activity: dict) -> dict:
    sym = dig(activity, "symbol", "symbol") or dig(activity, "symbol", "raw_symbol") or ""
    if isinstance(sym, dict):  # some payloads nest one level deeper
        sym = sym.get("symbol") or ""
    trade_date = activity.get("trade_date") or activity.get("settlement_date") or ""
    return {
        "date": str(trade_date)[:10],
        "type": activity.get("type") or "",
        "symbol": sym,
        "units": activity.get("units"),
        "price": activity.get("price"),
        "amount": activity.get("amount"),
        "currency": dig(activity, "currency", "code", default="USD"),
        "description": activity.get("description") or "",
    }


def fetch_activities(client: SnapTradeClient, account_id: str,
                     start_date: str, end_date: str, warnings: list) -> list:
    """Account-scoped paginated endpoint, falling back to the legacy global one."""
    try:
        rows, offset = [], 0
        while True:
            page = client.get(f"/accounts/{account_id}/activities",
                              {"startDate": start_date, "endDate": end_date,
                               "offset": str(offset), "limit": str(ACTIVITIES_PAGE_SIZE)})
            data = page.get("data", []) if isinstance(page, dict) else page
            rows.extend(data)
            total = dig(page, "pagination", "total", default=len(rows)) if isinstance(page, dict) else len(rows)
            offset += len(data)
            if not data or offset >= total:
                return rows
    except urllib.error.HTTPError as err:
        if err.code not in (400, 404):
            warnings.append(f"activities({account_id}): {http_error_detail(err)}")
            return []
    except Exception as err:  # fall through to legacy endpoint on shape surprises too
        log(f"account activities endpoint failed ({err}); trying legacy /activities")
    try:
        legacy = client.get("/activities", {"startDate": start_date, "endDate": end_date,
                                            "accounts": account_id})
        return legacy if isinstance(legacy, list) else []
    except Exception as err:
        warnings.append(f"activities({account_id}): {err}")
        return []


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_path = Path(args[0]) if args else repo_root / "tracking" / "brokerage-snapshot.json"
    lookback = DEFAULT_LOOKBACK_DAYS
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--lookback-days" and i < len(sys.argv) - 1:
            lookback = int(sys.argv[i + 1])
        elif arg.startswith("--lookback-days="):
            lookback = int(arg.split("=", 1)[1])

    client_id = os.environ.get("SNAPTRADE_CLIENT_ID", "").strip()
    consumer_key = os.environ.get("SNAPTRADE_CONSUMER_KEY", "").strip()
    if not client_id or not consumer_key:
        die("SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY not set (env or .env) — "
            "SnapTrade is unavailable in this environment", code=2)

    client = SnapTradeClient(client_id, consumer_key)
    warnings = []

    try:
        raw_accounts = client.get("/accounts")
    except urllib.error.HTTPError as err:
        die(f"listing accounts failed — credentials rejected or API error ({http_error_detail(err)})")
    except Exception as err:
        die(f"listing accounts failed — network error ({err})")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        die("no accounts returned — is a brokerage connected to this SnapTrade account?")

    end_date = datetime.now(timezone.utc).date().isoformat()
    start_date = (datetime.now(timezone.utc).date() - timedelta(days=lookback)).isoformat()

    accounts = []
    for raw in raw_accounts:
        account_id = raw.get("id", "")
        log(f"fetching account {raw.get('name') or account_id}")

        try:
            raw_positions = client.get(f"/accounts/{account_id}/positions")
        except Exception as err:
            warnings.append(f"positions({account_id}): {err}")
            raw_positions = []

        cash = None
        try:
            for bal in client.get(f"/accounts/{account_id}/balances") or []:
                if isinstance(bal.get("cash"), (int, float)):
                    cash = round((cash or 0.0) + bal["cash"], 2)
        except Exception as err:
            warnings.append(f"balances({account_id}): {err}")

        activities = [normalize_activity(a)
                      for a in fetch_activities(client, account_id, start_date, end_date, warnings)]
        activities.sort(key=lambda a: a["date"], reverse=True)

        accounts.append({
            "id": account_id,
            "name": raw.get("name") or "",
            "number_masked": mask_number(raw.get("number")),
            "institution": raw.get("institution_name") or "",
            "type": dig(raw, "meta", "type", default=raw.get("raw_type") or ""),
            "sync_status": dig(raw, "sync_status", "holdings", "last_successful_sync", default=""),
            "balance": {
                "total": dig(raw, "balance", "total", "amount"),
                "currency": dig(raw, "balance", "total", "currency", default="USD"),
                "cash": cash,
            },
            "positions": sorted((normalize_position(p) for p in raw_positions or []),
                                key=lambda p: p["market_value"] or 0, reverse=True),
            "activities": activities,
        })

    total_value = sum(a["balance"]["total"] for a in accounts
                      if isinstance(a["balance"]["total"], (int, float)))
    total_cash = sum(a["balance"]["cash"] for a in accounts
                     if isinstance(a["balance"]["cash"], (int, float)))

    snapshot = {
        "fetched_at": now_iso(),
        "source": "snaptrade",
        "lookback_days": lookback,
        "totals": {"market_value": round(total_value, 2), "cash": round(total_cash, 2),
                   "currency": "USD"},
        "accounts": accounts,
        "warnings": warnings,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2) + "\n")
    position_count = sum(len(a["positions"]) for a in accounts)
    log(f"wrote {out_path} — {len(accounts)} account(s), {position_count} position(s), "
        f"{len(warnings)} warning(s)")
    for w in warnings:
        log(f"  warning: {w}")


if __name__ == "__main__":
    main()
