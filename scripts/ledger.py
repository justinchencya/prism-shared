#!/usr/bin/env python3
"""
The trade ledger — tracking/ledger.json, the brokerage's own activity history.

The broker is the record of what traded. /sync-portfolio (via
scripts/fetch_snaptrade.py --ledger) merges every fetched activity into the
ledger; nothing is entered by hand. The first sync pulls the account's full
history, later syncs merge the lookback window. Rows are keyed by SnapTrade's
activity id, so re-fetching is idempotent and the ledger is append-only.

Usage:
  ledger.py [ledger.json] [snapshot.json] [--json]
    Summary + cross-check: ledger-implied shares per symbol vs the snapshot's
    positions, and the opening lots the history needs.

Library (used by fetch_snaptrade.py, generate_dashboard.py, check_consistency.py):
  merge(ledger, activities, account)  → (ledger, n_added)
  stock_trades(ledger)                → dated stock trade dicts (fills + share transfers)
  opening_units(trades, positions)    → {symbol: units held before the ledger starts}
  cross_check(trades, positions)      → warnings where ledger shares ≠ snapshot shares

Trade shape (what the dashboard consumes):
  {id, date, ticker, action: buy|sell, shares, price_per_share, amount_usd, kind}
  kind: "fill" (BUY/SELL/REI), "transfer" (share-bearing non-trade rows such
  as conversion deposits and merger adjustments, valued at the row's amount),
  or "opening" (synthesized by the caller from opening_units()).

Rules:
  - Stocks only. Option rows (option_symbol set, or CALL/PUT in an empty-symbol
    row) stay in the ledger but are not stock trades.
  - The cash sweep (cash_equivalent positions, money-market rows) is skipped.
  - Rows whose symbol isn't a quote symbol (CUSIP-keyed legs of corporate
    actions, e.g. a merger's old-share payout) are skipped.
  - Holdings that predate the ledger: opening = snapshot units − ledger net,
    and at least the deepest running deficit (sells before any recorded buy).
    Within a day, buys apply before sells — the broker's intraday order is
    unknown. The caller prices openings at the first close on/after the
    ledger's history_start.

Stdlib only.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from symbols import canonical, is_quote_symbol  # noqa: E402

ROOT = Path(__file__).parent.parent
OPTION_ROW = re.compile(r"\b(CALL|PUT)\b")
CASH_ROW = re.compile(r"MONEY MARKET|CORE ACCOUNT", re.I)
STOCK_FILL_TYPES = {"BUY": "buy", "SELL": "sell", "REI": "buy"}
SHARE_TOL = 0.01


def empty() -> dict:
    return {"updated_at": None, "history_start": None, "activities": []}


def load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return empty()
    data.setdefault("activities", [])
    data.setdefault("history_start", None)
    return data


def merge(ledger: dict, activities: list, account: str = "") -> tuple:
    """Add activities not already in the ledger (by id). Returns (ledger, n_added)."""
    seen = {a["id"] for a in ledger.get("activities", []) if a.get("id")}
    added = 0
    for a in activities:
        if not a.get("id") or a["id"] in seen:
            continue
        ledger["activities"].append({**a, "account": a.get("account") or account})
        seen.add(a["id"])
        added += 1
    ledger["activities"].sort(key=lambda a: (a.get("date") or "", a.get("id")))
    dates = [a["date"] for a in ledger["activities"] if a.get("date")]
    if dates:
        ledger["history_start"] = min([ledger.get("history_start") or dates[0], dates[0]])
    return ledger, added


def cash_symbols(positions: list) -> set:
    return {canonical(p.get("symbol")) for p in positions if p.get("cash_equivalent")}


def _is_option(a: dict) -> bool:
    return bool(a.get("option")) or (not a.get("symbol") and bool(OPTION_ROW.search(a.get("description") or "")))


def stock_trades(ledger: dict, positions: list = ()) -> list:
    cash = cash_symbols(positions)
    out = []
    for a in ledger.get("activities", []):
        sym, units = canonical(a.get("symbol")), a.get("units") or 0
        if not sym or not units or _is_option(a) or sym in cash or not is_quote_symbol(sym):
            continue
        if CASH_ROW.search(a.get("description") or ""):
            continue
        typ = (a.get("type") or "").upper()
        if typ in STOCK_FILL_TYPES:
            action, kind = STOCK_FILL_TYPES[typ], "fill"
            if typ != "REI":
                action = "buy" if units > 0 else "sell"
            price = a.get("price") or (abs(a.get("amount") or 0) / abs(units))
        elif typ in ("DIVIDEND", "CONTRIBUTION", "WITHDRAWAL", "FEE", "TAX", "JOURNALED"):
            continue
        else:   # share-bearing corporate action / transfer (LOAN, ADJUSTMENT, SPLIT, SPINOFF, ...)
            action, kind = ("buy" if units > 0 else "sell"), "transfer"
            price = abs(a.get("amount") or 0) / abs(units) or None
        shares = abs(units)
        out.append({"id": a["id"], "date": a["date"], "ticker": sym, "action": action,
                    "shares": shares, "price_per_share": price,
                    "amount_usd": round(shares * price, 2) if price else None, "kind": kind})
    # chronological; within a day buys before sells (intraday order unknown)
    out.sort(key=lambda t: (t["date"], t["action"] != "buy", t["id"]))
    return out


def _net_and_deficit(trades: list) -> tuple:
    net, low = {}, {}
    for t in trades:
        s = t["ticker"]
        net[s] = net.get(s, 0) + (t["shares"] if t["action"] == "buy" else -t["shares"])
        low[s] = min(low.get(s, 0), net[s])
    return net, low


def opening_units(trades: list, positions: list) -> dict:
    """{symbol: shares held before the ledger begins} — what reconciles the
    ledger with the snapshot and keeps every running position non-negative."""
    held = {canonical(p.get("symbol")): p.get("units") or 0 for p in positions if not p.get("cash_equivalent")}
    net, low = _net_and_deficit(trades)
    out = {}
    for s in set(net) | set(held):
        need = max(held.get(s, 0) - net.get(s, 0), -low.get(s, 0), 0)
        if need > SHARE_TOL:
            out[s] = round(need, 6)
    return out


def cross_check(trades: list, positions: list) -> list:
    """Warnings where opening + ledger net ≠ the snapshot's share count."""
    held = {canonical(p.get("symbol")): p.get("units") or 0 for p in positions if not p.get("cash_equivalent")}
    net, _ = _net_and_deficit(trades)
    opening = opening_units(trades, positions)
    out = []
    for s in sorted(set(net) | set(held)):
        implied = opening.get(s, 0) + net.get(s, 0)
        if abs(implied - held.get(s, 0)) > SHARE_TOL:
            out.append(f"ledger({s}): history implies {implied:g} shares, snapshot holds "
                       f"{held.get(s, 0):g} — a sell precedes its buy in the record, or rows are missing")
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lpath = Path(args[0]) if args else ROOT / "tracking" / "ledger.json"
    spath = Path(args[1]) if len(args) > 1 else ROOT / "tracking" / "brokerage-snapshot.json"
    ledger = load(lpath)
    try:
        snap = json.loads(spath.read_text())
    except (OSError, ValueError):
        snap = {}
    positions = [p for a in snap.get("accounts", []) for p in a.get("positions", [])]
    trades = stock_trades(ledger, positions)
    res = {"history_start": ledger.get("history_start"), "updated_at": ledger.get("updated_at"),
           "activities": len(ledger["activities"]), "stock_trades": len(trades),
           "opening_units": opening_units(trades, positions), "warnings": cross_check(trades, positions)}
    if "--json" in sys.argv:
        print(json.dumps(res, indent=2))
        return
    print(f"ledger: {res['activities']} activities since {res['history_start']} "
          f"(updated {res['updated_at']}), {res['stock_trades']} stock trades")
    if res["opening_units"]:
        print("opening lots (held before history start): " +
              ", ".join(f"{k} {v:g}" for k, v in sorted(res["opening_units"].items())))
    for w in res["warnings"]:
        print("warning: " + w)
    if not res["warnings"]:
        print("cross-check: ledger shares match the snapshot for every symbol")


if __name__ == "__main__":
    main()
