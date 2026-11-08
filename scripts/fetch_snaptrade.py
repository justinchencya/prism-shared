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
    accounts[]    — per connected account: institution, masked number,
                    balance (total, cash + breakdown + cross-check — see
                    derive_cash), positions[], options[], recent activities[]
    totals        — market value / cash (+ collateral / free / reported) /
                    options market value across accounts
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
  - Symbols are canonicalized (scripts/symbols.py — Fidelity's BRKB → BRK-B);
    the broker's spelling is kept beside it as raw_symbol / raw_underlying.

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

sys.path.insert(0, str(Path(__file__).parent))
from symbols import canonical  # noqa: E402

BASE = "https://api.snaptrade.com"
API_PREFIX = "/api/v1"
DEFAULT_LOOKBACK_DAYS = 90
ACTIVITIES_PAGE_SIZE = 250
OPTION_MULTIPLIER = 100        # shares per standard contract
MINI_OPTION_MULTIPLIER = 10    # shares per mini contract
CASH_TOLERANCE = 1.00          # $ — cash derivations must agree within this


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
        "symbol": canonical(sym["symbol"]),
        "raw_symbol": sym["symbol"],
        "description": sym["description"],
        "units": units,
        "price": price,
        "market_value": market_value,
        "currency": sym["currency"],
        "average_purchase_price": position.get("average_purchase_price"),
        "open_pnl": position.get("open_pnl"),
        "cash_equivalent": bool(position.get("cash_equivalent")),
    }


def normalize_option(holding: dict) -> dict:
    """One open option contract.

    SnapTrade quotes `price` per share but `average_purchase_price` per
    contract, so the two need different scaling to be comparable. `units` is
    signed: negative means short (written), which is what a covered call or a
    cash-secured put looks like. market_value keeps that sign — a short
    contract is a liability, not an asset.
    """
    contract = dig(holding, "symbol", "option_symbol", default={}) or {}
    underlying = contract.get("underlying_symbol") or {}
    multiplier = MINI_OPTION_MULTIPLIER if contract.get("is_mini_option") else OPTION_MULTIPLIER

    units = holding.get("units")
    price = holding.get("price")
    market_value = None
    if isinstance(units, (int, float)) and isinstance(price, (int, float)):
        market_value = round(units * price * multiplier, 2)

    direction = ""
    if isinstance(units, (int, float)) and units:
        direction = "Short" if units < 0 else "Long"

    return {
        "underlying": canonical(underlying.get("symbol")),
        "raw_underlying": underlying.get("symbol") or "",
        "description": underlying.get("description") or "",
        "occ_symbol": (contract.get("ticker") or "").strip(),
        "option_type": (contract.get("option_type") or "").upper(),
        "direction": direction,
        "strike": contract.get("strike_price"),
        "expiration_date": contract.get("expiration_date") or "",
        "contracts": units,
        "multiplier": multiplier,
        "price": price,
        "market_value": market_value,
        "average_purchase_price": holding.get("average_purchase_price"),
        "currency": dig(underlying, "currency", "code", default="USD"),
    }


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def derive_cash(total, positions: list, options: list, reported, buying_power,
                account_label: str, warnings: list) -> dict:
    """Actual cash holdings, derived — not SnapTrade's cash line.

    Fidelity's core sweep (SPAXX) units — which SnapTrade reports as both the
    cash-equivalent position and /balances `cash` — can lag the account's real
    cash by thousands of dollars, while the account total and the securities
    prices are current. So:

      cash                 = total − non-cash positions − net options value
                             (the "Cash and credits" figure Fidelity shows;
                             includes cash reserved as option collateral)
      collateral_reserved  = Σ short puts: strike × multiplier × |contracts|
      free                 = SnapTrade buying_power (settled, unreserved)

    Cross-check: buying_power + collateral_reserved must equal `cash` within
    CASH_TOLERANCE. It is validation only — it never overrides `cash`. On a
    margin account (different reserve rules) it is expected to disagree, which
    surfaces as a warning rather than a wrong number. Short calls reserve no
    cash here — they are assumed covered by held shares.
    """
    investments = None
    if _num(total) and all(_num(p["market_value"]) for p in positions
                           if not p["cash_equivalent"]) \
            and all(_num(o["market_value"]) for o in options):
        investments = round(sum(p["market_value"] for p in positions
                                if not p["cash_equivalent"])
                            + sum(o["market_value"] for o in options), 2)

    collateral = round(sum(o["strike"] * o["multiplier"] * abs(o["contracts"])
                           for o in options
                           if o["option_type"] == "PUT" and o["direction"] == "Short"
                           and _num(o["strike"]) and _num(o["contracts"])), 2)

    method_a = round(total - investments, 2) if investments is not None else None
    method_b = round(buying_power + collateral, 2) if _num(buying_power) else None

    if method_a is not None:
        cash = method_a
    else:
        cash = reported if _num(reported) else None
        warnings.append(f"cash({account_label}): could not derive cash from total − "
                        f"investments; fell back to SnapTrade's reported cash")

    agree = None
    if method_a is not None and method_b is not None:
        agree = abs(method_a - method_b) <= CASH_TOLERANCE
        if not agree:
            warnings.append(f"cash({account_label}): derivations disagree — total − "
                            f"investments = {method_a:,.2f} vs buying_power + collateral = "
                            f"{method_b:,.2f}; using the former")

    lag = round(reported - cash, 2) if _num(reported) and _num(cash) else None
    return {
        "cash": cash,
        "cash_breakdown": {
            "collateral_reserved": collateral,
            "free": round(buying_power, 2) if _num(buying_power) else None,
        },
        "cash_reported": reported,
        "cash_check": {
            "total_minus_investments": method_a,
            "buying_power_plus_collateral": method_b,
            "agree": agree,
            "reported_lag": lag,
        },
    }


def fetch_options(client: SnapTradeClient, account_id: str, warnings: list) -> list:
    """Open option contracts. Absent for brokerages that don't expose options —
    a 404/400 there means "not supported", not a failure worth warning about."""
    try:
        raw = client.get(f"/accounts/{account_id}/options")
    except urllib.error.HTTPError as err:
        if err.code not in (400, 404):
            warnings.append(f"options({account_id}): {http_error_detail(err)}")
        return []
    except Exception as err:
        warnings.append(f"options({account_id}): {err}")
        return []
    return raw if isinstance(raw, list) else []


def normalize_activity(activity: dict) -> dict:
    sym = dig(activity, "symbol", "symbol") or dig(activity, "symbol", "raw_symbol") or ""
    if isinstance(sym, dict):  # some payloads nest one level deeper
        sym = sym.get("symbol") or ""
    trade_date = activity.get("trade_date") or activity.get("settlement_date") or ""
    return {
        "date": str(trade_date)[:10],
        "type": activity.get("type") or "",
        "symbol": canonical(sym),
        "raw_symbol": sym,
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

        reported_cash = buying_power = None
        try:
            for bal in client.get(f"/accounts/{account_id}/balances") or []:
                if _num(bal.get("cash")):
                    reported_cash = round((reported_cash or 0.0) + bal["cash"], 2)
                if _num(bal.get("buying_power")):
                    buying_power = round((buying_power or 0.0) + bal["buying_power"], 2)
        except Exception as err:
            warnings.append(f"balances({account_id}): {err}")

        options = sorted((normalize_option(o)
                          for o in fetch_options(client, account_id, warnings)),
                         key=lambda o: (o["expiration_date"], o["underlying"]))

        activities = [normalize_activity(a)
                      for a in fetch_activities(client, account_id, start_date, end_date, warnings)]
        activities.sort(key=lambda a: a["date"], reverse=True)

        positions = sorted((normalize_position(p) for p in raw_positions or []),
                           key=lambda p: p["market_value"] or 0, reverse=True)
        total = dig(raw, "balance", "total", "amount")
        cash = derive_cash(total, positions, options, reported_cash, buying_power,
                           raw.get("name") or account_id, warnings)

        accounts.append({
            "id": account_id,
            "name": raw.get("name") or "",
            "number_masked": mask_number(raw.get("number")),
            "institution": raw.get("institution_name") or "",
            "type": dig(raw, "meta", "type", default=raw.get("raw_type") or ""),
            "sync_status": dig(raw, "sync_status", "holdings", "last_successful_sync", default=""),
            "balance": {
                "total": total,
                "currency": dig(raw, "balance", "total", "currency", default="USD"),
                **cash,
            },
            "positions": positions,
            "options": options,
            "activities": activities,
        })

    total_value = sum(a["balance"]["total"] for a in accounts
                      if isinstance(a["balance"]["total"], (int, float)))
    def total_of(get):
        return round(sum(v for v in (get(a["balance"]) for a in accounts) if _num(v)), 2)
    total_options = sum(o["market_value"] for a in accounts for o in a["options"]
                        if isinstance(o["market_value"], (int, float)))

    snapshot = {
        "fetched_at": now_iso(),
        "source": "snaptrade",
        "lookback_days": lookback,
        "totals": {"market_value": round(total_value, 2),
                   "cash": total_of(lambda b: b["cash"]),
                   "cash_collateral_reserved": total_of(lambda b: b["cash_breakdown"]["collateral_reserved"]),
                   "cash_free": total_of(lambda b: b["cash_breakdown"]["free"]),
                   "cash_reported": total_of(lambda b: b["cash_reported"]),
                   "options_market_value": round(total_options, 2), "currency": "USD"},
        "accounts": accounts,
        "warnings": warnings,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2) + "\n")
    position_count = sum(len(a["positions"]) for a in accounts)
    option_count = sum(len(a["options"]) for a in accounts)
    log(f"wrote {out_path} — {len(accounts)} account(s), {position_count} position(s), "
        f"{option_count} option(s), {len(warnings)} warning(s)")
    for w in warnings:
        log(f"  warning: {w}")


if __name__ == "__main__":
    main()
