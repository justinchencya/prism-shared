#!/usr/bin/env python3
"""
Daily watch over option contracts you already hold: what's near expiry, what's
nearly fully profitable, what's in the money — and what to do about it.
Read-only: no orders, no repo writes (except --out).

Usage:
  options_watch.py [--snapshot path] [--out path.json]
                   [--capture 0.75] [--expiry-days 7] [--long-days 21] [--delta 0.50]
                   [--no-roll]

Holdings come from a brokerage snapshot (default tracking/brokerage-snapshot.json).
For a scheduled run, fetch a fresh one to a scratch path first and pass --snapshot:
  python3 scripts/fetch_snaptrade.py /tmp/snap.json && options_watch.py --snapshot /tmp/snap.json
The output states how old the snapshot is — a stale one is flagged, not hidden.

Flags
  short contracts: CLOSE_EARLY (≥capture of the premium kept), EXPIRING (≤expiry-days),
                   ITM / HIGH_DELTA (assignment risk), EX_DIV (short call ITM before an ex-dividend),
                   STALE (expiry already passed)
  long contracts:  DECAY (OTM and ≤long-days left), EXPIRING (≤expiry-days), STALE
For flagged short contracts it drafts a ROLL: buy-to-close now + sell a later expiry
(7–45 days past the current expiry), preferring a net credit and |delta| ≤ 0.30, scored by P(OTM);
an out-of-the-money roll never moves the strike closer to the money. CLOSE_EARLY gets no roll — the
answer there is close, then run /options for a fresh pick.
Quiet by design: with nothing flagged it says so in one line.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import options_pick as op  # noqa: E402
import options_screen as os_  # noqa: E402
import symbols  # noqa: E402

bs, implied_vol = os_.bs, os_.implied_vol


def spot_of(yf, tk):
    return float(yf.Ticker(symbols.quote_symbol(tk)).history(period="5d")["Close"].iloc[-1])


def ex_div(yf, tk):
    try:
        cal = yf.Ticker(symbols.quote_symbol(tk)).calendar or {}
        d = cal.get("Ex-Dividend Date")
        return d if isinstance(d, dt.date) else None
    except Exception:  # noqa: BLE001
        return None


def roll_candidate(ctx, o, close_fill, a, itm):
    """Best roll for a short contract: later expiry, net credit preferred, |delta| ≤ 0.30."""
    call = o["option_type"] == "CALL"
    exp0 = dt.date.fromisoformat(o["expiration_date"])
    today = dt.date.today()
    base = max(exp0, today)
    S = ctx.spot
    best = []
    for e in ctx.t.options:
        ed = dt.date.fromisoformat(e)
        if not (7 <= (ed - base).days <= 45):
            continue
        qs, T = op.chain_quotes(ctx, e, call, 100, 0.20)
        for K, q in qs.items():
            if not (0.80 * S <= K <= 1.20 * S):
                continue
            _, d, p_itm = bs(S, K, T, ctx.r, q["iv"], call)
            if abs(d) > 0.30:
                continue
            if not itm and ((call and K < o["strike"]) or (not call and K > o["strike"])):
                continue            # an out-of-the-money roll never moves the strike closer to the money
            net = q["sell"] - close_fill                       # per share; >0 = credit
            dte = (ed - today).days
            ann = q["sell"] / (S if call else K) * 365 / max(dte, 1)
            best.append({"expiry": e, "dte": dte, "strike": K, "new_credit": round(q["sell"], 2),
                         "net_credit_per_share": round(net, 2), "net_credit_per_contract": round(net * 100),
                         "delta": round(d, 3), "p_itm": round(p_itm, 3), "ann_yield_new_leg": round(ann, 3),
                         "stale_quote": q["stale"],
                         "strike_change": round(K - o["strike"], 2),
                         "score": round((1 - p_itm) * ann * (1.0 if net >= 0 else 0.5), 4)})
    if not best:
        return None
    best.sort(key=lambda x: -x["score"])
    r = best[0]
    r["is_credit"] = r["net_credit_per_share"] >= 0
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=str(ROOT / "tracking" / "brokerage-snapshot.json"))
    ap.add_argument("--out")
    ap.add_argument("--capture", type=float, default=0.75)
    ap.add_argument("--expiry-days", type=int, default=7)
    ap.add_argument("--long-days", type=int, default=21)
    ap.add_argument("--delta", type=float, default=0.50)
    ap.add_argument("--no-roll", action="store_true")
    a = ap.parse_args()

    import yfinance as yf

    snap = os_.load_snapshot(a.snapshot)
    if not snap:
        sys.exit("no brokerage snapshot — run /sync-portfolio (or fetch_snaptrade.py) first")
    today = dt.date.today()
    fetched = dt.datetime.fromisoformat(snap["fetched_at"].replace("Z", "+00:00"))
    age_h = (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds() / 3600
    try:
        r = float(yf.Ticker("^IRX").history(period="5d")["Close"].iloc[-1]) / 100
    except Exception:  # noqa: BLE001
        r = 0.04
    opts = [o for acct in snap.get("accounts", []) for o in acct.get("options", [])]
    held = {}
    for acct in snap.get("accounts", []):
        for p in acct.get("positions", []):
            if not p.get("cash_equivalent"):
                k = symbols.canonical(p["symbol"])
                held[k] = held.get(k, 0) + p["units"]
    out = {"asof": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "snapshot_fetched_at": snap["fetched_at"],
           "snapshot_age_hours": round(age_h, 1), "stale_snapshot": age_h > 30, "contracts": len(opts),
           "items": [], "ok": [], "warnings": []}
    spots, ctxs = {}, {}
    for o in opts:
        u = symbols.canonical(o["underlying"])
        if u in spots:
            continue
        try:
            spots[u] = spot_of(yf, u)
        except Exception as e:  # noqa: BLE001
            out["warnings"].append(f"{u}: spot unavailable ({type(e).__name__})")

    for o in opts:
        u = symbols.canonical(o["underlying"])
        S = spots.get(u)
        exp = dt.date.fromisoformat(o["expiration_date"])
        dte = (exp - today).days
        short = o["direction"] == "Short"
        call = o["option_type"] == "CALL"
        n = abs(o["contracts"])
        mark = abs(o["market_value"]) / n                                  # $ per contract now
        credit = o.get("average_purchase_price") or 0                      # $ per contract at open (short: credit; long: cost)
        row = {"occ": o["occ_symbol"].strip(), "underlying": u, "side": "short" if short else "long",
               "type": o["option_type"], "strike": o["strike"], "expiry": o["expiration_date"], "dte": dte,
               "contracts": n, "mark_per_contract": round(mark, 2), "spot": round(S, 2) if S else None, "flags": []}
        itm = None
        delta = None
        if S and dte > 0:
            itm = (S > o["strike"]) if call else (S < o["strike"])
            iv = implied_vol(o["price"], S, o["strike"], dte / 365, r, call)
            if iv:
                _, delta, _p = bs(S, o["strike"], dte / 365, r, iv, call)
                row.update(iv=round(iv, 3), delta=round(delta, 3))
            row["itm"] = itm
            row["pct_to_strike"] = round(o["strike"] / S - 1, 4)
        elif S:
            itm = (S > o["strike"]) if call else (S < o["strike"])
        if short:
            cap = (1 - mark / credit) if credit else None
            row.update(credit_per_contract=round(credit, 2), profit_captured=round(cap, 3) if cap is not None else None)
            if dte < 0:
                row["flags"].append("STALE")
            if cap is not None and cap >= a.capture and dte > 0:
                row["flags"].append("CLOSE_EARLY")
            if 0 <= dte <= a.expiry_days:
                row["flags"].append("EXPIRING")
            if itm and dte >= 0:
                row["flags"].append("ITM")
            elif delta is not None and abs(delta) > a.delta:
                row["flags"].append("HIGH_DELTA")
            if call and itm and u in held:
                ed = ex_div(yf, u)
                if ed and today <= ed <= exp:
                    row["flags"].append("EX_DIV")
                    row["ex_dividend"] = ed.isoformat()
            if call:
                row["shares_held"] = held.get(u, 0)
                row["covered"] = held.get(u, 0) >= 100 * n
        else:
            row.update(cost_per_contract=round(credit, 2), pnl_vs_cost=round(mark - credit, 2))
            if dte < 0:
                row["flags"].append("STALE")
            if 0 <= dte <= a.expiry_days:
                row["flags"].append("EXPIRING")
            elif 0 <= dte <= a.long_days and itm is False:
                row["flags"].append("DECAY")
        if not row["flags"]:
            out["ok"].append(f"{row['occ']} ({'short' if short else 'long'}, {dte}d, {row['strike']:g})")
            continue
        if short and not a.no_roll and S and any(f in row["flags"] for f in ("ITM", "HIGH_DELTA", "EXPIRING")) and dte >= 0:
            try:
                if u not in ctxs:
                    ctxs[u] = op.load_ctx(u, a.snapshot)
                row["roll"] = roll_candidate(ctxs[u], o, mark / 100 * 1.02, a, bool(itm))   # close at ~mark + 2%
            except SystemExit as e:
                out["warnings"].append(f"{u}: roll skipped ({e})")
            except Exception as e:  # noqa: BLE001
                out["warnings"].append(f"{u}: roll skipped ({type(e).__name__}: {e})")
        out["items"].append(row)

    out["flagged"] = len(out["items"])
    out["items"].sort(key=lambda x: (0 if "ITM" in x["flags"] or "STALE" in x["flags"] else 1, x["dte"]))
    out["summary"] = (f"{len(out['items'])} of {len(opts)} option contracts need a look"
                      if out["items"] else f"all {len(opts)} option contracts fine — nothing to do")
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=2, default=str))
    json.dump(out, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    main()
