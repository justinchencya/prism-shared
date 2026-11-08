#!/usr/bin/env python3
"""
Intent-driven option pick: ONE primary trade + a runner-up, scored against what
the user is actually trying to do. Read-only (no orders, no writes except --out).

Two families, opposite goals:

  INCOME   — you want the option to EXPIRE WORTHLESS (keep the premium)
    cc       sell a covered call against shares you hold
    csp      sell a cash-secured put on a name you'd tolerate owning
    score  = annualized yield × P(expires OTM)  (+15% if the strike sits behind a
             confluent chart level; ×0.5 if earnings fall inside the window)

  VALUE    — you want the option to GAIN VALUE
    hedge    buy downside protection on shares you hold: long put, put spread, or
             collar (long put + short call). Valued the day after the event
             (default: next earnings) with an IV-crush assumption.
    bullish  buy upside on a view (target price + horizon): long call or call
             debit spread. Valued at your horizon date, IV held flat.

Usage:
  options_pick.py cc      TICKER [--contracts N] [--max-delta 0.20]
  options_pick.py csp     TICKER [--budget 30000] [--max-delta 0.20]
  options_pick.py hedge   TICKER [--hedge-shares 500] [--event YYYY-MM-DD] [--no-collar]
  options_pick.py bullish TICKER --target 250|+15% --horizon-days 45 [--budget 5000]
  common: [--min-oi 100] [--max-spread 0.20] [--snapshot path] [--out path.json]

Fills are estimated, not mid: buys at mid + 25% of the spread, sells at mid − 25%.
Greeks/IV are solved here (Black-Scholes, no dividends, european) — screening
grade. Scenario P&L is model output under stated assumptions, not a forecast.
"""

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import options_screen as os_  # noqa: E402
import symbols  # noqa: E402
import technical_structure as ts  # noqa: E402

bs, implied_vol = os_.bs, os_.implied_vol
HEDGE_SHOCKS = [-30, -20, -10, -5, 0, 5, 10, 20]
IV_CRUSH = 0.75          # post-earnings IV as a fraction of pre-event IV (assumption)


# ───────────────────────── shared plumbing ─────────────────────────
def load_ctx(tk, snap_path):
    import yfinance as yf
    t = yf.Ticker(symbols.quote_symbol(tk))
    hist = t.history(period="3y", interval="1d")
    if hist.empty:
        sys.exit(f"{tk}: no price history")
    if not t.options:
        sys.exit(f"{tk}: no listed options")
    struct, _ = ts.analyze(hist)
    spot = float(hist["Close"].iloc[-1])
    snap = os_.load_snapshot(snap_path)
    view = os_.portfolio_view(snap)
    longs = []
    if snap:
        for a in snap.get("accounts", []):
            for o in a.get("options", []):
                if symbols.canonical(o["underlying"]) == tk:
                    longs.append({k: o[k] for k in ("direction", "option_type", "strike", "expiration_date", "contracts")})
    try:
        r = float(yf.Ticker("^IRX").history(period="5d")["Close"].iloc[-1]) / 100
    except Exception:  # noqa: BLE001
        r = 0.04
    return SimpleNamespace(t=t, tk=tk, hist=hist, struct=struct or {}, spot=spot, view=view, r=r,
                           rv=os_.realized_vol(hist["Close"]), earn=os_.next_earnings(t), held_options=longs,
                           levels=(struct or {}).get("levels", {}))


def chain_quotes(ctx, expiry, call, min_oi, max_spread, buy_side_only=False):
    """{strike: quote} for one expiry/side, liquid rows only."""
    ch = ctx.t.option_chain(expiry)
    df = ch.calls if call else ch.puts
    T = (dt.date.fromisoformat(expiry) - dt.date.today()).days / 365.0
    out = {}
    for row in df.itertuples():
        bid, ask, last = float(row.bid or 0), float(row.ask or 0), float(row.lastPrice or 0)
        stale = not (bid > 0 and ask > 0)
        mid = last if stale else 0.5 * (bid + ask)
        oi = int(row.openInterest) if row.openInterest == row.openInterest else 0
        if mid <= 0.05 or oi < min_oi:
            continue
        spread = 0.0 if stale else ask - bid
        if not stale and spread / mid > max_spread:
            continue
        K = float(row.strike)
        iv = implied_vol(mid, ctx.spot, K, T, ctx.r, call) or float(row.impliedVolatility or 0)
        if not iv:
            continue
        out[K] = {"K": K, "bid": bid, "ask": ask, "mid": mid, "stale": stale, "oi": oi, "iv": iv,
                  "buy": mid + 0.25 * spread, "sell": max(mid - 0.25 * spread, 0.01)}
    return out, T


def leg(sign, call, q, T):
    """sign +1 long / −1 short. cash = what you pay (+) or receive (−) per share."""
    return {"sign": sign, "call": call, "K": q["K"], "iv": q["iv"], "T": T, "oi": q["oi"], "stale": q["stale"],
            "fill": q["buy"] if sign > 0 else q["sell"]}


def net_cost(legs):
    return sum(l["sign"] * l["fill"] for l in legs)


def value(legs, S, elapsed, ivmult=1.0, r=0.04):
    """Structure value per share at spot S after `elapsed` years (intrinsic once a leg expires)."""
    v = 0.0
    for l in legs:
        rem = l["T"] - elapsed
        v += l["sign"] * bs(S, l["K"], max(rem, 0), r, l["iv"] * ivmult, l["call"])[0]
    return v


def solve_breakeven(legs, cost, elapsed, ivmult, r, lo, hi, increasing=True):
    f = lambda S: value(legs, S, elapsed, ivmult, r) - cost  # noqa: E731
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if (f(mid) > 0) == (fhi > 0):
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def pick_two(cands, key="score"):
    """Primary = best score. Runner-up = best of a *different* structure, else a different expiry."""
    cands = sorted(cands, key=lambda c: -c[key])
    if not cands:
        return None, None, []
    p = cands[0]
    ru = next((c for c in cands[1:] if c["structure"] != p["structure"]), None) \
        or next((c for c in cands[1:] if c["expiry"] != p["expiry"]), None) \
        or (cands[1] if len(cands) > 1 else None)
    return p, ru, cands


def level_note(ctx, K, call):
    return os_.level_context(ctx.levels, K, ctx.spot, "call" if call else "put")


def base_out(ctx, intent, extra=None):
    lv = ctx.levels
    o = {"intent": intent, "ticker": ctx.tk, "asof": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "spot": round(ctx.spot, 2), "risk_free": round(ctx.r, 4), "next_earnings": ctx.earn.isoformat() if ctx.earn else None,
         "rv30": round(ctx.rv, 3) if ctx.rv else None, "rsi14": round(float(ts.rsi(ctx.hist["Close"]).iloc[-1]), 1),
         "ma_stack": ctx.struct.get("trend", {}).get("ma_stack"), "atr_pct": lv.get("atr_pct"),
         "break_level": (lv.get("break_level") or {}).get("price"),
         "supports": [x["price"] for x in lv.get("supports", [])[:3]],
         "resistances": [x["price"] for x in lv.get("resistances", [])[:3]],
         "shares_held": ctx.view["shares"].get(ctx.tk, 0), "snapshot_fetched_at": ctx.view["fetched_at"],
         "held_options_on_name": ctx.held_options, "warnings": []}
    o.update(extra or {})
    return o


# ───────────────────────── INCOME: cc / csp ─────────────────────────
def income(ctx, side, a):
    call = side == "cc"
    args = SimpleNamespace(min_dte=a.min_dte, max_dte=a.max_dte, delta_lo=0.03, delta_hi=a.max_delta,
                           min_oi=a.min_oi, max_spread=a.max_spread, top=400, per_expiry=400)
    # screen_chain keeps one strike per expiry; call it per-expiry-agnostic with a large top and
    # then re-rank with the income score (so every expiry gets considered).
    cands, avail = os_.screen_chain(ctx.t, ctx.tk, ctx.spot, ctx.r, ctx.struct, ctx.rv, ctx.earn, ctx.view,
                                    args, call, not call)
    rows = cands["cc" if call else "csp"]
    out = base_out(ctx, side, {"cc_contracts_available": avail, "cash_free": ctx.view["cash_free"]})
    if call and avail < 1:
        out["warnings"].append(f"fewer than 100 unencumbered shares of {ctx.tk} — no covered call possible")
        return out
    if call:
        n = min(a.contracts or avail, avail)
    for c in rows:
        lc = c["levels"]
        shield = lc["levels_in_the_way"] >= 1 and (call or lc.get("strike_beyond_break_level"))
        c["score"] = round(c["ann_yield"] * (1 - c["p_itm"]) * (1.15 if shield else 1.0)
                           * (0.5 if c["earnings_in_window"] else 1.0), 4)
        c["structure"] = "covered call" if call else "cash-secured put"
        if call:
            c["contracts"] = n
            c["premium_total"] = round(c["mid"] * 100 * n)
            c["upside_forgone_if_called"] = round((c["strike"] - ctx.spot) / ctx.spot, 4)
        else:
            k = int(a.budget // (c["strike"] * 100)) if a.budget else c["contracts_affordable"]
            c["contracts"] = max(k or 0, 0)
            c["premium_total"] = round(c["mid"] * 100 * c["contracts"])
            c["collateral_total"] = round(c["strike"] * 100 * c["contracts"])
            c["if_assigned"] = {"cost_basis": c["breakeven"], "vs_spot": round(c["breakeven"] / ctx.spot - 1, 4)}
    if not call:
        cheapest = min((c["collateral_per_contract"] for c in rows), default=None)
        rows = [c for c in rows if c["contracts"] >= 1]
        if not rows and cheapest:
            out["warnings"].append(f"budget ${a.budget:,.0f} can't secure one contract: the cheapest screened strike "
                                   f"needs ${cheapest:,} of collateral")
    p, ru, ranked = pick_two(rows)
    out.update(primary=p, runner_up=ru, considered=len(rows), assumption="goal: option expires worthless; "
               f"|delta| ≤ {a.max_delta}; fills at mid (not spread-adjusted)")
    return out


# ───────────────────────── VALUE: hedge ─────────────────────────
def hedge(ctx, a):
    today = dt.date.today()
    event = dt.date.fromisoformat(a.event) if a.event else ctx.earn
    held = ctx.view["shares"].get(ctx.tk, 0)
    shares = int(a.hedge_shares or held)
    n = max(shares // 100, 1)
    out = base_out(ctx, "hedge", {"event": event.isoformat() if event else None, "hedged_shares": n * 100,
                                  "position_value": round(n * 100 * ctx.spot)})
    if not event or event < today:
        out["warnings"].append("no upcoming event date — valuing at +5 days, no IV-crush assumption")
        event_d = today + dt.timedelta(days=5)
        crush = 1.0
    else:
        event_d, crush = event, IV_CRUSH
    eval_el = ((event_d - today).days + 1) / 365.0
    exps = [e for e in ctx.t.options if 1 <= (dt.date.fromisoformat(e) - event_d).days <= a.max_after]
    cands = []
    for e in exps:
        puts, T = chain_quotes(ctx, e, False, a.min_oi, a.max_spread)
        calls, _ = chain_quotes(ctx, e, True, a.min_oi, a.max_spread) if not a.no_collar else ({}, T)
        S = ctx.spot
        pk = sorted(k for k in puts if 0.85 * S <= k <= 1.02 * S)
        structs = []
        for k1 in pk:
            structs.append(("protective put", [leg(+1, False, puts[k1], T)]))
            for k2 in (k for k in sorted(puts) if 0.60 * S <= k <= 0.93 * k1):
                structs.append(("put spread", [leg(+1, False, puts[k1], T), leg(-1, False, puts[k2], T)]))
            for kc in (k for k in sorted(calls) if 1.04 * S <= k <= 1.25 * S):
                structs.append(("collar", [leg(+1, False, puts[k1], T), leg(-1, True, calls[kc], T)]))
        for name, legs in structs:
            cost = net_cost(legs)
            pnl = {s: value(legs, S * (1 + s / 100), eval_el, crush, ctx.r) - cost for s in HEDGE_SHOCKS}
            protect = (pnl[-20] + pnl[-10]) / 2
            bleed = max(-(pnl[0] + pnl[10] + pnl[20]) / 3, 0.001 * S)
            floor_k = legs[0]["K"]
            cands.append({
                "structure": name, "expiry": e, "dte": (dt.date.fromisoformat(e) - today).days,
                "legs": [{"action": "buy" if l["sign"] > 0 else "sell", "type": "call" if l["call"] else "put",
                          "strike": l["K"], "fill": round(l["fill"], 2), "iv": round(l["iv"], 3)} for l in legs],
                "net_cost_per_share": round(cost, 2), "net_cost_pct_of_position": round(cost / S, 4),
                "deductible_pct": round((S - floor_k) / S, 4),
                "pnl_per_share": {str(k): round(v, 2) for k, v in pnl.items()},
                "protect": round(protect, 2), "bleed": round(bleed, 2), "score": round(protect / bleed, 3) if protect > 0 else round(protect / bleed, 3),
                "upside_cap": (round(legs[1]["K"] / S - 1, 4) if name == "collar" else None),
                "stale_quote": any(l["stale"] for l in legs),
                "level": level_note(ctx, floor_k, False),
            })
    # honest framing: a hedge that pays little in -10/-20 isn't a hedge
    cands = [c for c in cands if c["protect"] > 0 and c["deductible_pct"] <= a.max_deductible]
    p, ru, _ = pick_two(cands)
    for c in (p, ru):
        if c:
            c["contracts"] = n
            c["net_cost_total"] = round(c["net_cost_per_share"] * 100 * n)
            c["pnl_total"] = {k: round(v * 100 * n) for k, v in c["pnl_per_share"].items()}
    out.update(primary=p, runner_up=ru, considered=len(cands), shocks_pct=HEDGE_SHOCKS,
               assumption=(f"valued the day after {event_d} with post-event IV at {int(crush*100)}% of today's; "
                           f"floor within {a.max_deductible:.0%}; score = avg P&L at −10%/−20% ÷ avg loss at 0%/+10%/+20% (cost, decay, IV crush, call cap)"))
    return out


# ───────────────────────── VALUE: bullish ─────────────────────────
def parse_target(s, spot):
    s = s.strip()
    return spot * (1 + float(s.rstrip("%")) / 100) if s.endswith("%") else float(s)


def bullish(ctx, a):
    today = dt.date.today()
    S = ctx.spot
    target = parse_target(a.target, S)
    H = a.horizon_days
    hdate = today + dt.timedelta(days=H)
    scen = {"target": target, "halfway": S + (target - S) / 2, "flat": S, "down10": S * 0.9}
    w = dict(zip(scen, a.weights))
    out = base_out(ctx, "bullish", {"target": round(target, 2), "target_pct": round(target / S - 1, 4),
                                    "horizon_date": hdate.isoformat(), "scenario_weights": w})
    exps = [e for e in ctx.t.options if H + 7 <= (dt.date.fromisoformat(e) - today).days <= H + a.max_after]
    cands = []
    for e in exps:
        calls, T = chain_quotes(ctx, e, True, a.min_oi, a.max_spread)
        el = H / 365.0
        ks = sorted(k for k in calls if 0.85 * S <= k <= 1.30 * S)
        structs = []
        for k1 in ks:
            d1 = bs(S, k1, T, ctx.r, calls[k1]["iv"], True)[1]
            if not (0.25 <= d1 <= 0.85):
                continue
            structs.append(("long call", [leg(+1, True, calls[k1], T)], d1))
            for k2 in (k for k in ks if 0.97 * target <= k <= 1.15 * target and k > k1):
                structs.append(("call spread", [leg(+1, True, calls[k1], T), leg(-1, True, calls[k2], T)], d1))
        for name, legs, d1 in structs:
            cost = net_cost(legs)
            if cost <= 0.05:
                continue
            pnl = {n: value(legs, px, el, 1.0, ctx.r) - cost for n, px in scen.items()}
            ev = sum(w[n] * pnl[n] / cost for n in scen)
            be_h = solve_breakeven(legs, cost, el, 1.0, ctx.r, 0.5 * S, 2.0 * target)
            be_x = solve_breakeven(legs, cost, T, 1.0, ctx.r, 0.5 * S, 2.0 * target)
            cands.append({
                "structure": name, "expiry": e, "dte": (dt.date.fromisoformat(e) - today).days,
                "legs": [{"action": "buy" if l["sign"] > 0 else "sell", "type": "call", "strike": l["K"],
                          "fill": round(l["fill"], 2), "iv": round(l["iv"], 3)} for l in legs],
                "long_delta": round(d1, 2), "net_cost_per_share": round(cost, 2), "max_loss_per_contract": round(cost * 100),
                "max_gain_per_contract": round(((legs[1]["K"] - legs[0]["K"]) - cost) * 100) if name == "call spread" else None,
                "pnl_per_share": {n: round(v, 2) for n, v in pnl.items()},
                "return_on_premium": {n: round(v / cost, 2) for n, v in pnl.items()},
                "breakeven_at_horizon": round(be_h, 2) if be_h else None,
                "breakeven_at_expiry": round(be_x, 2) if be_x else None,
                "breakeven_pct_vs_spot": round(be_h / S - 1, 4) if be_h else None,
                "ev_return": round(ev, 3), "score": round(ev, 3),
                "stale_quote": any(l["stale"] for l in legs),
                "level": level_note(ctx, legs[-1]["K"], True),
            })
    # comparator: shares
    shares_ret = {n: round(px / S - 1, 4) for n, px in scen.items()}
    p, ru, _ = pick_two(cands)
    k = None
    for c in (p, ru):
        if c:
            k = max(int(a.budget // c["max_loss_per_contract"]), 1) if a.budget else 1
            c["contracts"] = k
            c["premium_total"] = c["max_loss_per_contract"] * k
            c["pnl_total"] = {n: round(v * 100 * k) for n, v in c["pnl_per_share"].items()}
    out.update(primary=p, runner_up=ru, considered=len(cands), shares_comparator_return=shares_ret,
               scenarios={n: round(px, 2) for n, px in scen.items()},
               assumption=("valued at the horizon date with IV held flat; score = scenario-weighted return on premium "
                           f"(weights {w}); long leg delta 0.25–0.85; spread short leg ≥ 0.97× target"))
    return out



# ───────────────────────── plain-English thesis ─────────────────────────
def _d(x):
    return f"${x:,.0f}"


def _p(x, sign=False):
    return f"{x*100:+.1f}%" if sign else f"{x*100:.1f}%"


def explain(c, ctx, intent):
    e = _explain(c, ctx, intent)
    if not e:
        return e
    notes = []
    if ctx.earn and dt.date.today() <= ctx.earn <= dt.date.fromisoformat(c["expiry"]):
        notes.append(f"{ctx.tk} reports earnings {ctx.earn} — inside this trade's window, so a gap move is the main risk/driver.")
    if intent == "hedge":
        notes.append("Plan to close the hedge the day after the event; the scenario table is valued then (IV crush assumed).")
    if intent == "bullish":
        notes.append(f"Plan to reassess at {c['horizon_date']} and sell before expiry; time decay accelerates in the last weeks.")
    if intent in ("cc", "csp"):
        notes.append("Option is 'won' by doing nothing; consider buying it back early once ~75% of the premium is captured.")
    e["notes"] = notes
    return e


def _explain(c, ctx, intent):
    """The trade in plain English, from the numbers. Returns {headline, bet, how_you_win, how_you_lose, key_numbers}."""
    S, tk, n, E = ctx.spot, ctx.tk, c.get("contracts", 1), c["expiry"]
    sh = 100 * n
    st = c["structure"]
    if st == "covered call":
        K, prem = c["strike"], c["premium_total"]
        return {
            "headline": f"Sell {n} {tk} ${K:g} call expiring {E} for ~{_d(prem)}",
            "bet": f"You're betting {tk} stays below ${K:g} ({_p(c['pct_otm'])} above today's ${S:,.2f}) through {E}.",
            "how_you_win": f"It stays below ${K:g}: the call expires worthless and you keep the full {_d(prem)} (~{_p(c['yield_static'])} of the shares' value) while still owning the {sh} shares. Model odds of that: ~{_p(1 - c['p_itm'])}.",
            "how_you_lose": f"It closes above ${K:g}: your {sh} shares are sold at ${K:g} (premium kept, so ${K + c['mid']:,.2f}/share all-in) and you miss every dollar above that. If it falls instead, the premium only cushions {_p(c['mid']/S)}; you keep the downside of owning the stock (share breakeven ${c['breakeven']:,.2f}).",
            "key_numbers": {"premium_total": prem, "assigned_price": K, "assigned_all_in": round(K + c["mid"], 2),
                            "upside_forgone_above": K, "p_expire_worthless": round(1 - c["p_itm"], 3)},
        }
    if st == "cash-secured put":
        K, prem, col = c["strike"], c["premium_total"], c["collateral_total"]
        return {
            "headline": f"Sell {n} {tk} ${K:g} put expiring {E} for ~{_d(prem)} (locks {_d(col)})",
            "bet": f"You're betting {tk} does not close below ${K:g} ({_p(-c['pct_otm'])} under today's ${S:,.2f}) on {E}.",
            "how_you_win": f"It stays above ${K:g}: the put expires worthless, you keep {_d(prem)} (~{_p(c['yield_on_collateral'])} on the {_d(col)} set aside, ~{_p(c['ann_yield'])} annualized) and never buy anything. Model odds: ~{_p(1 - c['p_itm'])}.",
            "how_you_lose": f"It closes below ${K:g}: you must buy {sh} shares at ${K:g}, effective cost ${c['breakeven']:,.2f} after premium ({_p(c['breakeven']/S - 1, True)} vs today). That's only a real loss if the stock keeps falling or you can't hold it: every $1 below ${c['breakeven']:,.2f} costs you ${sh:,} on paper.",
            "key_numbers": {"premium_total": prem, "collateral": col, "buy_price_if_assigned": K,
                            "effective_cost_if_assigned": c["breakeven"], "p_expire_worthless": round(1 - c["p_itm"], 3)},
        }
    legs = c["legs"]
    cost = c["net_cost_per_share"] * sh
    if st == "protective put":
        K = legs[0]["strike"]
        return {
            "headline": f"Buy {n} {tk} ${K:g} put expiring {E} for ~{_d(cost)}",
            "bet": f"Insurance, not a bet: you pay {_d(cost)} ({_p(c['net_cost_pct_of_position'])} of the {sh} shares) for the right to sell them at ${K:g} until {E}.",
            "how_you_win": f"{tk} falls below ${K:g} ({_p(-c['deductible_pct'] if c['deductible_pct'] else 0, True)} from today's ${S:,.2f}): the put gains a dollar for every dollar the stock drops further, so your loss on the shares stops at about {_p(c['deductible_pct'] + c['net_cost_pct_of_position'])} including the premium.",
            "how_you_lose": f"{tk} flat or up: the {_d(cost)} is spent and gone (and some of it evaporates after earnings when implied volatility drops). That's the price of insurance.",
            "key_numbers": {"cost_total": round(cost), "protection_starts_below": K, "worst_case_pct_incl_cost": round(c["deductible_pct"] + c["net_cost_pct_of_position"], 4)},
        }
    if st == "put spread":
        K1, K2 = legs[0]["strike"], legs[1]["strike"]
        mx = ((K1 - K2) - c["net_cost_per_share"]) * sh
        return {
            "headline": f"Buy {n} {tk} ${K1:g} put / sell {n} ${K2:g} put, expiring {E}, for ~{_d(cost)}",
            "bet": f"Cheaper insurance for a moderate drop: you pay {_d(cost)} ({_p(c['net_cost_pct_of_position'])} of the {sh} shares) to be protected between ${K1:g} and ${K2:g}.",
            "how_you_win": f"{tk} falls below ${K1:g} ({_p(-c['deductible_pct'], True)} from today's ${S:,.2f}): the spread gains $1 per dollar of drop down to ${K2:g}, maxing out at {_d(mx)}. Selling the lower ${K2:g} put is what makes it cheaper.",
            "how_you_lose": f"Flat or up: you lose the {_d(cost)}. And below ${K2:g} the protection is used up, so a crash past ${K2:g} ({_p(K2/S - 1, True)}) is unhedged again; the shares keep falling at full speed.",
            "key_numbers": {"cost_total": round(cost), "protected_between": [K1, K2], "max_hedge_payoff": round(mx)},
        }
    if st == "collar":
        K1, Kc = legs[0]["strike"], legs[1]["strike"]
        credit = cost < 0
        # built outside the f-strings: a backslash inside an f-string expression needs Python 3.12+
        flat_outcome = (f"a {_d(-cost)} credit" if credit
                        else f"a {_d(cost)} loss (the put's cost less the little the call brings in)")
        return {
            "headline": f"Buy {n} {tk} ${K1:g} put / sell {n} ${Kc:g} call, expiring {E}, for {'a ~' + _d(-cost) + ' credit' if credit else '~' + _d(cost) + ' net'}",
            "bet": f"You're trading your upside above ${Kc:g} ({_p(Kc/S - 1, True)}) for protection below ${K1:g} ({_p(K1/S - 1, True)}) until {E}; the call you sell pays for the put.",
            "how_you_win": f"{tk} drops below ${K1:g}: the put covers every dollar below it, so the {sh} shares can't lose more than about {_p(c['deductible_pct'] + c['net_cost_pct_of_position'])} through {E}.",
            "how_you_lose": f"{tk} rallies past ${Kc:g}: your shares are capped there (called away at ${Kc:g}) and you miss the rest. Since you're bullish, that cap is the real cost of this structure; if it ends flat you net {flat_outcome}.",
            "key_numbers": {"net_cost_total": round(cost), "floor": K1, "cap": Kc},
        }
    if st == "long call":
        K = legs[0]["strike"]
        return {
            "headline": f"Buy {n} {tk} ${K:g} call expiring {E} for ~{_d(c['premium_total'])}",
            "bet": f"You're betting {tk} rises well above today's ${S:,.2f} by your horizon; it needs to be above ${c['breakeven_at_horizon']:,.2f} ({_p(c['breakeven_pct_vs_spot'], True)}) at the horizon just to break even, ${c['breakeven_at_expiry']:,.2f} if held to expiry {E}.",
            "how_you_win": f"{tk} reaches your target ${c['target_price']:,.2f} by {c['horizon_date']}: the model values the call at about {_d(c['pnl_total']['target'] + c['premium_total'])} ({c['return_on_premium']['target']:+.0%} on premium).",
            "how_you_lose": f"{tk} stays flat or falls: time decay eats it; flat at the horizon is ~{c['return_on_premium']['flat']:+.0%} and the worst case is losing all {_d(c['premium_total'])}. Expiring {E} is deliberately after your horizon so you aren't forced to be right by the last day.",
            "key_numbers": {"max_loss": c["premium_total"], "breakeven_at_horizon": c["breakeven_at_horizon"], "breakeven_at_expiry": c["breakeven_at_expiry"]},
        }
    if st == "call spread":
        K1, K2 = legs[0]["strike"], legs[1]["strike"]
        mxg = c["max_gain_per_contract"] * n
        return {
            "headline": f"Buy {n} {tk} ${K1:g} call / sell {n} ${K2:g} call, expiring {E}, for ~{_d(c['premium_total'])}",
            "bet": f"You're betting {tk} climbs from ${S:,.2f} toward ${K2:g} or beyond by {c['horizon_date']}; selling the ${K2:g} call makes it cheaper than a plain call but caps the win.",
            "how_you_win": f"{tk} at/above ${K2:g}: at expiry the spread is worth its maximum, ${K2 - K1:g}/share → total gain up to {_d(mxg)} on a {_d(c['premium_total'])} outlay. At your target ${c['target_price']:,.2f} on {c['horizon_date']} (before expiry) the model shows ~{c['return_on_premium']['target']:+.0%} on premium. Break-even at the horizon: ${c['breakeven_at_horizon']:,.2f} ({_p(c['breakeven_pct_vs_spot'], True)}).",
            "how_you_lose": f"Flat or down: you lose up to the {_d(c['premium_total'])} you paid (that's the max loss). Above ${K2:g} you get no extra: a bigger rally than your target is not captured.",
            "key_numbers": {"max_loss": c["premium_total"], "max_gain": mxg, "breakeven_at_horizon": c["breakeven_at_horizon"], "gain_capped_above": K2},
        }
    return {}


# ───────────────────────── CLI ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("intent", choices=["cc", "csp", "hedge", "bullish"])
    ap.add_argument("ticker")
    ap.add_argument("--contracts", type=int)
    ap.add_argument("--budget", type=float)
    ap.add_argument("--max-delta", type=float, default=0.20)
    ap.add_argument("--min-dte", type=int, default=14)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--hedge-shares", type=int)
    ap.add_argument("--event")
    ap.add_argument("--no-collar", action="store_true")
    ap.add_argument("--max-deductible", type=float, default=0.08, help="max distance of the put strike below spot")
    ap.add_argument("--target")
    ap.add_argument("--horizon-days", type=int)
    ap.add_argument("--weights", default="0.45,0.20,0.20,0.15", help="target,halfway,flat,down10")
    ap.add_argument("--max-after", type=int, default=45, help="days of expiry allowed past the event/horizon")
    ap.add_argument("--min-oi", type=int, default=100)
    ap.add_argument("--max-spread", type=float, default=0.20)
    ap.add_argument("--snapshot", default=str(ROOT / "tracking" / "brokerage-snapshot.json"))
    ap.add_argument("--out")
    a = ap.parse_args()
    a.weights = [float(x) for x in a.weights.split(",")]
    tk = symbols.canonical(a.ticker)
    if a.intent == "bullish" and not (a.target and a.horizon_days):
        sys.exit("bullish needs --target (price or +N%) and --horizon-days")
    ctx = load_ctx(tk, a.snapshot)
    res = {"cc": lambda: income(ctx, "cc", a), "csp": lambda: income(ctx, "csp", a),
           "hedge": lambda: hedge(ctx, a), "bullish": lambda: bullish(ctx, a)}[a.intent]()
    for k in ("primary", "runner_up"):
        c = res.get(k)
        if c:
            if a.intent == "bullish":
                c["target_price"], c["horizon_date"] = res["target"], res["horizon_date"]
            c["plain_english"] = explain(c, ctx, a.intent)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=2, default=str))
    json.dump(res, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    main()
