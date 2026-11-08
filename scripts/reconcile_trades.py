#!/usr/bin/env python3
"""
Reconcile the brokerage snapshot's activities against tracking/trades.json.
Read-only: it never writes trades.json (owned by /log-trade, mirrors Notion 1:1).

Usage:
  reconcile_trades.py [snapshot.json] [trades.json] [--json]

  Defaults: tracking/brokerage-snapshot.json, tracking/trades.json (repo-relative).
  Prints a human summary; --json prints the full result.

Matching rules (deterministic — the command reports this output, it does not
re-derive matches):
  - Scope: stock BUY/SELL activities inside the snapshot's lookback window.
    Option trades are counted and skipped (trades.json logs stocks only); the
    cash sweep (cash_equivalent / money-market rows) is skipped; broker-
    initiated rows (ESPP / SPP purchases, conversions, reinvestments) are
    listed separately as `automatic`, not as unlogged.
  - Symbol: canonical() on both sides (BRKB == BRK-B). Never economic_group():
    GOOGL and GOOG are different securities.
  - Direction: BUY ↔ buy/add, SELL ↔ sell/trim.
  - Date: the broker's date is usually the posting date, up to a trading day
    after the trade (Fidelity via SnapTrade gives trade_date == settlement
    date), but logs are sometimes dated a day late too. A broker row matches a
    log entry dated d when it falls within LOG_LATE_TRADING_DAYS before to
    SETTLE_LAG_TRADING_DAYS after d, in NYSE trading days.
  - Quantity: each log entry's shares are a capacity. One log row can absorb
    several partial fills, one fill can draw on several log rows, and no share
    is matched twice.
  - Order: pass 1 pairs fills with log rows at the same price (±0.25%) that
    can absorb the whole fill; pass 2 matches what's left by date proximity,
    then price. Price-first keeps a busy day's fills from absorbing a
    neighbouring day's log rows.

Output buckets: unlogged, partially_logged, automatic, logged_not_at_broker
(log entries in the window with shares no broker fill absorbed), pending (log
entries too recent for the broker to have posted), skipped counts, and the
per-symbol net residual — the in-window share drift between the two sides.
"""

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from symbols import canonical  # noqa: E402
from dashboard_due import is_trading_day  # noqa: E402

ROOT = Path(__file__).parent.parent
SETTLE_LAG_TRADING_DAYS = 2
LOG_LATE_TRADING_DAYS = 1
PRICE_MATCH = 0.0025
OPTION_ROW = re.compile(r"\b(CALL|PUT)\b")
CASH_ROW = re.compile(r"MONEY MARKET|CORE ACCOUNT", re.I)
AUTOMATIC_ROW = re.compile(r"\bESPP|\bSPP\b|CONVERSION|REINVEST", re.I)


def add_trading_days(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    n = abs(n)
    while n > 0:
        d += timedelta(days=step)
        if is_trading_day(d):
            n -= 1
    return d


def _price_gap(a, b) -> float:
    return abs(a - b) / b if a and b else 1.0


def _tol(qty: float) -> float:
    return max(0.01, 0.005 * abs(qty))


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def reconcile(snapshot: dict, trades: list) -> dict:
    as_of = _d(snapshot.get("fetched_at") or date.today().isoformat())
    lookback = int(snapshot.get("lookback_days") or 90)
    start = as_of - timedelta(days=lookback)

    cash_syms = {canonical(p.get("symbol")) for a in snapshot.get("accounts", [])
                 for p in a.get("positions", []) if p.get("cash_equivalent")}

    skipped = {"options": 0, "cash_sweep": 0, "other_types": {}}
    automatic, fills = [], []
    for a in snapshot.get("accounts", []):
        for x in a.get("activities", []):
            typ, desc = (x.get("type") or "").upper(), x.get("description") or ""
            if typ not in ("BUY", "SELL"):
                skipped["other_types"][typ] = skipped["other_types"].get(typ, 0) + 1
                continue
            sym = canonical(x.get("symbol"))
            if not sym and OPTION_ROW.search(desc):
                skipped["options"] += 1
                continue
            if sym in cash_syms or CASH_ROW.search(desc):
                skipped["cash_sweep"] += 1
                continue
            row = {"date": str(x.get("date"))[:10], "symbol": sym,
                   "raw_symbol": x.get("raw_symbol") or x.get("symbol") or "",
                   "direction": "buy" if typ == "BUY" else "sell",
                   "units": abs(x.get("units") or 0), "price": x.get("price"),
                   "amount": x.get("amount"), "account": a.get("number_masked", ""),
                   "description": desc}
            if AUTOMATIC_ROW.search(desc):
                automatic.append(row)
            else:
                fills.append(row)

    log = []
    for t in trades:
        if not t.get("date") or _d(t["date"]) < start - timedelta(days=7) or _d(t["date"]) > as_of:
            continue
        shares = t.get("shares")
        log.append({"id": t.get("id"), "date": t["date"][:10], "symbol": canonical(t.get("ticker")),
                    "direction": "buy" if t.get("action") in ("buy", "add") else "sell",
                    "shares": shares, "remaining": float(shares) if shares else None,
                    "price": t.get("price_per_share"), "amount_usd": t.get("amount_usd")})

    def eligible(f, e):
        if e["symbol"] != f["symbol"] or e["direction"] != f["direction"]:
            return False
        ed, bd = _d(e["date"]), _d(f["date"])
        return add_trading_days(ed, -LOG_LATE_TRADING_DAYS) <= bd <= add_trading_days(ed, SETTLE_LAG_TRADING_DAYS)

    def size(e, f):
        if e["remaining"] is None:              # no share count logged: size it from dollars
            px = f["price"] or 0
            e["remaining"] = (e["amount_usd"] or 0) / px if px else 0.0
        return e["remaining"]

    order = sorted(fills, key=lambda r: (r["date"], -r["units"]))
    need = {id(f): f["units"] for f in order}
    links = {id(f): [] for f in order}

    def take(f, e):
        amt = min(need[id(f)], e["remaining"])
        e["remaining"] -= amt
        need[id(f)] -= amt
        links[id(f)].append(e["id"])

    # pass 1 — same price, one log row absorbs the whole fill
    for f in order:
        for e in sorted((e for e in log if eligible(f, e)),
                        key=lambda e: abs((_d(e["date"]) - _d(f["date"])).days)):
            if (_price_gap(f["price"], e.get("price")) <= PRICE_MATCH
                    and size(e, f) >= need[id(f)] - _tol(f["units"])):
                take(f, e)
                break
    # pass 2 — whatever is left, nearest date first, then nearest price
    for f in order:
        if need[id(f)] <= _tol(f["units"]):
            continue
        for e in sorted((e for e in log if eligible(f, e)),
                        key=lambda e: (abs((_d(e["date"]) - _d(f["date"])).days),
                                       _price_gap(f["price"], e.get("price")))):
            if need[id(f)] <= _tol(f["units"]):
                break
            if size(e, f) > _tol(e["shares"] or e["remaining"] or 1):
                take(f, e)

    unlogged, partial = [], []
    for f in order:
        if need[id(f)] <= _tol(f["units"]):
            continue
        rec = {**f, "unlogged_units": round(need[id(f)], 6), "matched_ids": links[id(f)]}
        (partial if links[id(f)] else unlogged).append(rec)

    unseen, pending = [], []
    for e in log:
        rem = e["remaining"] if e["remaining"] is not None else float(e["shares"] or 0)
        if rem <= _tol(e["shares"] or rem or 1) or _d(e["date"]) < start:
            continue
        rec = {k: e[k] for k in ("id", "date", "symbol", "direction", "shares")}
        rec["unmatched_shares"] = round(rem, 6)
        if add_trading_days(_d(e["date"]), SETTLE_LAG_TRADING_DAYS) > as_of:
            pending.append(rec)
        else:
            unseen.append(rec)

    residual = {}
    for r in unlogged + partial:
        residual[r["symbol"]] = residual.get(r["symbol"], 0) + r["unlogged_units"] * (1 if r["direction"] == "buy" else -1)
    for r in unseen:
        residual[r["symbol"]] = residual.get(r["symbol"], 0) - r["unmatched_shares"] * (1 if r["direction"] == "buy" else -1)
    residual = {k: round(v, 6) for k, v in sorted(residual.items()) if abs(v) > 0.01}

    by_date = lambda r: r["date"]  # noqa: E731
    return {
        "as_of": as_of.isoformat(),
        "window": {"start": start.isoformat(), "end": as_of.isoformat()},
        "counts": {"broker_fills": len(fills), "matched": len(fills) - len(unlogged) - len(partial),
                   "unlogged": len(unlogged), "partially_logged": len(partial),
                   "automatic": len(automatic), "logged_not_at_broker": len(unseen),
                   "pending": len(pending)},
        "unlogged": sorted(unlogged, key=by_date, reverse=True),
        "partially_logged": sorted(partial, key=by_date, reverse=True),
        "automatic": sorted(automatic, key=by_date, reverse=True),
        "logged_not_at_broker": sorted(unseen, key=by_date, reverse=True),
        "pending": sorted(pending, key=by_date, reverse=True),
        "skipped": skipped,
        "net_residual_by_symbol": residual,
    }


def _summary(res: dict) -> str:
    c = res["counts"]
    lines = [f"Window {res['window']['start']} → {res['window']['end']}: {c['broker_fills']} broker stock fills, "
             f"{c['matched']} matched, {c['unlogged']} unlogged, {c['partially_logged']} partially logged, "
             f"{c['automatic']} automatic, {c['logged_not_at_broker']} logged-not-at-broker, {c['pending']} pending."]
    fmt = lambda r: f"  {r['date']} {r['direction'].upper():4} {r['symbol']:<8} {r.get('unlogged_units', r['units']):g} @ {r['price']}"  # noqa: E731
    for key, title in (("unlogged", "Unlogged"), ("partially_logged", "Partially logged (unlogged units)"),
                       ("automatic", "Automatic (broker-initiated)")):
        if res[key]:
            lines.append(f"{title}:")
            lines += [fmt(r) for r in res[key]]
    for key, title in (("logged_not_at_broker", "Logged, not at broker"), ("pending", "Pending (too recent to post)")):
        if res[key]:
            lines.append(f"{title}:")
            lines += [f"  {r['date']} {r['direction'].upper():4} {r['symbol']:<8} {r['unmatched_shares']:g} ({r['id']})"
                      for r in res[key]]
    s = res["skipped"]
    lines.append(f"Skipped: {s['options']} option fills, {s['cash_sweep']} cash-sweep rows, "
                 f"{sum(s['other_types'].values())} non-trade rows.")
    if res["net_residual_by_symbol"]:
        lines.append("In-window share drift (broker − log): " +
                     ", ".join(f"{k} {v:+g}" for k, v in res["net_residual_by_symbol"].items()))
    elif not (res["unlogged"] or res["partially_logged"] or res["logged_not_at_broker"]):
        lines.append("Trade log is consistent with the brokerage.")
    return "\n".join(lines)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    snap = Path(args[0]) if args else ROOT / "tracking" / "brokerage-snapshot.json"
    trades_path = Path(args[1]) if len(args) > 1 else ROOT / "tracking" / "trades.json"
    snapshot = json.loads(snap.read_text())
    if not snapshot.get("fetched_at"):
        sys.exit("reconcile: snapshot is the empty template — nothing to reconcile")
    trades = json.loads(trades_path.read_text()).get("trades", []) if trades_path.exists() else []
    res = reconcile(snapshot, trades)
    print(json.dumps(res, indent=2) if "--json" in sys.argv else _summary(res))


if __name__ == "__main__":
    main()
