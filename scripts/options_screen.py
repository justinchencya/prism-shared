#!/usr/bin/env python3
"""
Options income screen — covered calls + cash-secured puts, anchored to the chart
structure Prism already computes. Read-only: no orders, no writes except --out.

Usage:
  options_screen.py [--tickers NVDA,TSM] [--side cc|csp|both]
                    [--min-dte 14] [--max-dte 60] [--delta-lo 0.10] [--delta-hi 0.30]
                    [--min-oi 100] [--max-spread 0.15] [--top 3]
                    [--snapshot tracking/brokerage-snapshot.json] [--out path.json]

Default universe = every held equity/ETF in the brokerage snapshot. --tickers
overrides (use it for names you'd like to be paid to buy but don't hold).

Per ticker it combines:
  • Yahoo Finance option chain (bid/ask/OI/volume; ~15-min delayed, no greeks)
  • Black-Scholes IV solved from the bid/ask mid, delta / P(ITM) from that IV
    (european approximation, no dividend yield — fine for 14-60 DTE screening)
  • technical_structure.analyze(): clustered support / resistance, break level, ATR
  • next earnings date (strikes whose expiry spans earnings are flagged)
  • the snapshot: shares already encumbered by short calls, free cash for puts,
    and the status of every open short contract (profit captured, delta, DTE)

What it can't see: historical IV (no free source) → there is no IV rank; the
`iv_rv` ratio (implied vs 30d realized vol) is the premium-richness proxy.
Quotes are stale outside market hours — rows priced off lastPrice are flagged.
"""

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import symbols  # noqa: E402
import technical_structure as ts  # noqa: E402

SQRT2 = math.sqrt(2.0)


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def bs(S, K, T, r, sigma, call):
    """(price, delta, p_itm) — european, no dividends."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if call else max(K - S, 0)
        return intrinsic, (1.0 if (call and S > K) else -1.0 if (not call and S < K) else 0.0), float(intrinsic > 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2), ncdf(d1), ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1), ncdf(d1) - 1.0, ncdf(-d2)


def implied_vol(price, S, K, T, r, call):
    """Bisection IV; None when the price is outside no-arbitrage bounds."""
    if price <= 0 or T <= 0:
        return None
    lo, hi = 0.01, 5.0
    if bs(S, K, T, r, hi, call)[0] < price or bs(S, K, T, r, lo, call)[0] > price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs(S, K, T, r, mid, call)[0] > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def parse_occ(occ):
    """'NVDA  261218C00260000' → (underlying, expiry date, 'C'/'P', strike)."""
    occ = occ.replace(" ", "")
    tail = occ[-15:]
    return occ[:-15], dt.datetime.strptime(tail[:6], "%y%m%d").date(), tail[6], int(tail[7:]) / 1000.0


def load_snapshot(path):
    p = Path(path)
    if not p.exists():
        return None
    s = json.loads(p.read_text())
    return s if s.get("fetched_at") else None


def portfolio_view(snap):
    """shares held, shares already short-called, free cash, total value."""
    view = {"shares": {}, "short_call_contracts": {}, "short_put_collateral": {},
            "cash_free": None, "cash_total": None, "total": None, "open_shorts": [], "fetched_at": None}
    if not snap:
        return view
    view["fetched_at"] = snap["fetched_at"]
    tot = snap.get("totals", {})
    view["total"] = tot.get("market_value")
    view["cash_total"] = tot.get("cash")
    view["cash_free"] = tot.get("cash") - tot.get("cash_collateral_reserved", 0) if tot.get("cash") is not None else None
    for a in snap.get("accounts", []):
        for p in a.get("positions", []):
            if not p.get("cash_equivalent"):
                k = symbols.canonical(p["symbol"])
                view["shares"][k] = view["shares"].get(k, 0) + p["units"]
        for o in a.get("options", []):
            if o["direction"] != "Short":
                continue
            u = symbols.canonical(o["underlying"])
            n = abs(o["contracts"])
            if o["option_type"] == "CALL":
                view["short_call_contracts"][u] = view["short_call_contracts"].get(u, 0) + n
            else:
                view["short_put_collateral"][u] = view["short_put_collateral"].get(u, 0) + n * o["strike"] * o["multiplier"]
            view["open_shorts"].append(o)
    return view


def realized_vol(close, n=30):
    r = np.log(close / close.shift(1)).dropna()
    return float(r.tail(n).std() * math.sqrt(252)) if len(r) >= n else None


def next_earnings(t):
    try:
        cal = t.calendar or {}
        d = cal.get("Earnings Date") or []
        d = [x for x in d if x >= dt.date.today()]
        return min(d) if d else None
    except Exception:  # noqa: BLE001
        return None


def level_context(levels, strike, spot, side):
    """How the strike sits against the chart. Calls: resistances between spot and
    strike (the strike has to be paid through them). Puts: supports between strike
    and spot (they must break first)."""
    if side == "call":
        between = [x for x in levels.get("resistances", []) if spot < x["price"] <= strike * 1.005]
    else:
        between = [x for x in levels.get("supports", []) if strike * 0.995 <= x["price"] < spot]
    best = max(between, key=lambda x: x["confluence"], default=None)
    brk = (levels.get("break_level") or {}).get("price")
    return {
        "levels_in_the_way": len(between),
        "strongest_level": ({"price": best["price"], "confluence": best["confluence"]} if best else None),
        "strike_beyond_break_level": (strike < brk) if (side == "put" and brk) else None,
    }


def screen_chain(t, ticker, spot, r, struct, rv, earn, view, args, want_cc, want_csp):
    today = dt.date.today()
    levels = struct.get("levels", {}) if struct else {}
    atr_pct = levels.get("atr_pct")
    out = {"cc": [], "csp": []}
    shares_free = view["shares"].get(ticker, 0) - 100 * view["short_call_contracts"].get(ticker, 0)
    cc_contracts = int(max(shares_free, 0) // 100)
    cash_free = view["cash_free"]
    expiries = [e for e in (t.options or [])
                if args.min_dte <= (dt.date.fromisoformat(e) - today).days <= args.max_dte]
    for e in expiries:
        dte = (dt.date.fromisoformat(e) - today).days
        T = dte / 365.0
        try:
            ch = t.option_chain(e)
        except Exception:  # noqa: BLE001
            continue
        for call, df in ((True, ch.calls), (False, ch.puts)):
            if (call and not want_cc) or (not call and not want_csp):
                continue
            for row in df.itertuples():
                K = float(row.strike)
                if call and K <= spot * 0.98 or (not call and K >= spot * 1.02):
                    continue                                   # only OTM / ~ATM
                bid, ask, last = float(row.bid or 0), float(row.ask or 0), float(row.lastPrice or 0)
                stale = not (bid > 0 and ask > 0)
                mid = last if stale else 0.5 * (bid + ask)
                if mid <= 0.05:
                    continue
                spread = None if stale else (ask - bid) / mid
                oi = int(row.openInterest or 0) if row.openInterest == row.openInterest else 0
                if oi < args.min_oi or (spread is not None and spread > args.max_spread):
                    continue
                iv = implied_vol(mid, spot, K, T, r, call) or float(row.impliedVolatility or 0)
                if not iv:
                    continue
                _, delta, p_itm = bs(spot, K, T, r, iv, call)
                if not (args.delta_lo <= abs(delta) <= args.delta_hi):
                    continue
                lc = level_context(levels, K, spot, "call" if call else "put")
                one_sd = spot * iv * math.sqrt(T)
                rec = {
                    "ticker": ticker, "expiry": e, "dte": dte, "strike": K, "mid": round(mid, 2),
                    "bid": bid, "ask": ask, "oi": oi, "volume": int(row.volume or 0) if row.volume == row.volume else 0,
                    "spread_pct": None if spread is None else round(spread, 3),
                    "stale_quote": stale, "iv": round(iv, 3), "iv_rv": round(iv / rv, 2) if rv else None,
                    "delta": round(delta, 3), "p_itm": round(p_itm, 3),
                    "pct_otm": round((K / spot - 1) * (1 if call else -1), 4),
                    "atrs_away": round(abs(K - spot) / (levels["atr14"]), 2) if levels.get("atr14") else None,
                    "sd_away": round(abs(K - spot) / one_sd, 2) if one_sd else None,
                    "earnings_in_window": bool(earn and earn <= dt.date.fromisoformat(e)),
                    "levels": lc,
                }
                if call:
                    rec["yield_static"] = round(mid / spot, 4)                       # unchanged
                    rec["yield_called"] = round((K - spot + mid) / spot, 4)          # assigned
                    rec["ann_yield"] = round(mid / spot * 365 / dte, 3)
                    rec["breakeven"] = round(spot - mid, 2)
                    rec["contracts_available"] = cc_contracts
                    out["cc"].append(rec)
                else:
                    rec["yield_on_collateral"] = round(mid / K, 4)
                    rec["ann_yield"] = round(mid / K * 365 / dte, 3)
                    rec["breakeven"] = round(K - mid, 2)
                    rec["effective_buy_discount"] = round(1 - (K - mid) / spot, 4)
                    rec["collateral_per_contract"] = round(K * 100)
                    rec["contracts_affordable"] = int(cash_free // (K * 100)) if cash_free else None
                    out["csp"].append(rec)
    for side in out:
        # rank: annualized yield, but only after the filters above; ties → farther from the money
        out[side].sort(key=lambda x: (-x["ann_yield"], -(x["sd_away"] or 0)))
        seen, keep = {}, []
        for x in out[side]:                                  # at most 1 strike per expiry per ticker in the top list
            if seen.setdefault((x["expiry"]), 0) < getattr(args, "per_expiry", 1) and len(keep) < args.top:
                seen[x["expiry"]] += 1
                keep.append(x)
        out[side] = keep
    return out, cc_contracts


def open_short_status(view, spots, r):
    rows = []
    today = dt.date.today()
    for o in view["open_shorts"]:
        u = symbols.canonical(o["underlying"])
        S = spots.get(u)
        exp = dt.date.fromisoformat(o["expiration_date"])
        dte = (exp - today).days
        credit = o.get("average_purchase_price")                 # per-contract $ credit at open
        mark = abs(o["market_value"]) / abs(o["contracts"])      # per-contract $ to close now
        row = {"occ": o["occ_symbol"].strip(), "underlying": u, "type": o["option_type"], "strike": o["strike"],
               "expiry": o["expiration_date"], "dte": dte, "contracts": abs(o["contracts"]),
               "credit_per_contract": credit, "close_cost_per_contract": round(mark, 2), "spot": S}
        if credit:
            row["profit_captured"] = round(1 - mark / credit, 3)
        if S and dte > 0:
            iv = implied_vol(o["price"], S, o["strike"], dte / 365, r, o["option_type"] == "CALL")
            if iv:
                _, d, p = bs(S, o["strike"], dte / 365, r, iv, o["option_type"] == "CALL")
                row.update(iv=round(iv, 3), delta=round(d, 3), p_itm=round(p, 3))
            row["itm"] = (S > o["strike"]) if o["option_type"] == "CALL" else (S < o["strike"])
            row["pct_to_strike"] = round((o["strike"] / S - 1), 4)
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--side", default="both", choices=["cc", "csp", "both"])
    ap.add_argument("--min-dte", type=int, default=14)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--delta-lo", type=float, default=0.10)
    ap.add_argument("--delta-hi", type=float, default=0.30)
    ap.add_argument("--min-oi", type=int, default=100)
    ap.add_argument("--max-spread", type=float, default=0.15)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--snapshot", default=str(ROOT / "tracking" / "brokerage-snapshot.json"))
    ap.add_argument("--out")
    args = ap.parse_args()

    import yfinance as yf

    snap = load_snapshot(args.snapshot)
    view = portfolio_view(snap)
    if args.tickers:
        universe = [symbols.canonical(x) for x in args.tickers.split(",") if x.strip()]
    else:
        universe = sorted(view["shares"])
    if not universe:
        sys.exit("no tickers: pass --tickers or sync a brokerage snapshot first")

    try:
        r = float(yf.Ticker("^IRX").history(period="5d")["Close"].iloc[-1]) / 100
    except Exception:  # noqa: BLE001
        r = 0.04
    result = {"asof": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "risk_free": r,
              "snapshot_fetched_at": view["fetched_at"], "cash_free": view["cash_free"],
              "cash_total": view["cash_total"], "portfolio_total": view["total"],
              "params": {k: v for k, v in vars(args).items() if k not in ("snapshot", "out", "tickers")},
              "tickers": {}, "warnings": []}
    spots = {}
    for tk in universe:
        qs = symbols.quote_symbol(tk)
        try:
            t = yf.Ticker(qs)
            hist = t.history(period="3y", interval="1d")
            if hist.empty:
                raise ValueError("no price history")
            spot = float(hist["Close"].iloc[-1])
            spots[tk] = spot
            if not t.options:
                result["warnings"].append(f"{tk}: no listed options")
                continue
            struct, _ = ts.analyze(hist)
            held = view["shares"].get(tk, 0)
            want_cc = args.side in ("cc", "both") and held >= 100
            want_csp = args.side in ("csp", "both")
            if not (want_cc or want_csp):
                continue
            rv = realized_vol(hist["Close"])
            earn = next_earnings(t)
            cands, cc_n = screen_chain(t, tk, spot, r, struct, rv, earn, view, args, want_cc, want_csp)
            lv = (struct or {}).get("levels", {})
            result["tickers"][tk] = {
                "spot": round(spot, 2), "asof_bar": (struct or {}).get("asof"), "shares_held": held,
                "shares_encumbered": int(100 * view["short_call_contracts"].get(tk, 0)),
                "cc_contracts_available": cc_n, "short_put_collateral": round(view["short_put_collateral"].get(tk, 0)),
                "next_earnings": earn.isoformat() if earn else None, "rv30": round(rv, 3) if rv else None,
                "rsi14": round(float(ts.rsi(hist["Close"]).iloc[-1]), 1),
                "ma_stack": (struct or {}).get("trend", {}).get("ma_stack"),
                "swing_structure": (struct or {}).get("trend", {}).get("swing_structure"),
                "atr_pct": lv.get("atr_pct"), "break_level": (lv.get("break_level") or {}).get("price"),
                "supports": [x["price"] for x in lv.get("supports", [])[:3]],
                "resistances": [x["price"] for x in lv.get("resistances", [])[:3]],
                **cands,
            }
            if any(c.get("stale_quote") for s in cands.values() for c in s):
                result["warnings"].append(f"{tk}: some quotes priced off lastPrice (market closed / no two-sided quote)")
        except Exception as e:  # noqa: BLE001
            result["warnings"].append(f"{tk}: {type(e).__name__}: {e}")
    for o in view["open_shorts"]:                        # spots for open-short underlyings outside the universe
        u = symbols.canonical(o["underlying"])
        if u not in spots:
            try:
                spots[u] = float(yf.Ticker(symbols.quote_symbol(u)).history(period="5d")["Close"].iloc[-1])
            except Exception as e:  # noqa: BLE001
                result["warnings"].append(f"{u}: spot for open-short status unavailable ({type(e).__name__})")
    result["open_shorts"] = open_short_status(view, spots, r)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
    json.dump(result, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
