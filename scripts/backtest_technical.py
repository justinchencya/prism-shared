#!/usr/bin/env python3
"""
Point-in-time backtest of the technical-analysis engine (scripts/technical_structure.py).

For every trading day in the test window it truncates the price history to that day, runs
`analyze()` exactly as /technical-analysis and /research would have that evening, records the
calls (entry `setup.signal`, `exit_setup.signal`, covered-call / cash-secured-put timing and
strikes, swing plans), then scores them against what price actually did next.

Usage:
  backtest_technical.py <TICKER> [<TICKER> ...] [--years 3] [--step 1] [--gate]
                        [--csv rows.csv] [--json summary.json]

  --years N   test window (default 3); 3 more years of history are loaded as lookback
  --step K    evaluate every K-th session (default 1)
  --gate      also replay the SPY market gate point-in-time (≈2× slower)

What it reports (all on daily closes, no costs, no slippage):
  1. Entry signals — forward return at 5/10/20/60 sessions, hit rate and the 20-session drawdown
     (MAE20: mean worst close, and its worst decile) per `setup.signal`, also split by regime,
     vs the unconditional baseline over the same days. A good `add` beats baseline;
     a good `no_add` underperforms it (that is the drawdown it kept you out of).
  2. Exit signals — the same per `exit_setup.signal`. A good `trim` / `broken` is followed by
     below-baseline returns; `hold` should not be.
  3. Option timing — for each covered-call / cash-secured-put timing bucket, the share of
     signals where the suggested strike would have expired out of the money 20 sessions later
     (≈ a monthly), vs the keep rate a strike at the same % distance gets on a random day.
     That gap is the chart's edge; the strike distance alone is not.
  4. Swing plans — entered at the close (or when the trigger closes through within 10 sessions),
     first of target-1 / stop within 40 sessions; win rate and average R.

Statistics are reported both on every day ("days") and on the first day of each run of the same
signal ("episodes") — consecutive days with the same call are not independent samples.

Limits, stated: survivorship (you pick tickers that exist today), yfinance prices adjusted for
splits/dividends, chip model uses today's float, no earnings/news overlay, small samples for rare
signals (n is always printed — treat n < 20 as anecdote).
"""

import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import technical_structure as ts  # noqa: E402

HORIZONS = (5, 10, 20, 60)
OPT_H = 20          # option horizon in sessions (≈ one monthly)
SWING_H = 40        # sessions a swing plan has to hit target or stop
TRIGGER_WAIT = 10   # sessions a pending trigger has to fill
LOOKBACK = 756      # bars handed to analyze() — what the live commands fetch (3y)


def _arg(args, flag, default=None, cast=str):
    return cast(args[args.index(flag) + 1]) if flag in args else default


def load(ticker, years):
    import yfinance as yf
    t = yf.Ticker(ticker)
    hist = t.history(period=f"{int(years) + 4}y", interval="1d")
    hist = hist[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"])
    fs = None
    try:
        info = t.info or {}
        fs = info.get("floatShares") or info.get("sharesOutstanding")
    except Exception:  # noqa: BLE001
        pass
    return hist, fs


def replay(ticker, hist, fs, years, step, spy_hist=None):
    n = len(hist)
    start = max(LOOKBACK // 2, n - int(years * 252))
    rows = []
    c = hist["Close"].values
    for i in range(start, n, step):
        h = hist.iloc[max(0, i + 1 - LOOKBACK):i + 1]
        gate = None
        if spy_hist is not None:
            sh = spy_hist.loc[:hist.index[i]].iloc[-LOOKBACK:]
            ss, _ = ts.analyze(sh, None)
            gate = ts.gate_block(ss.get("setup"), ss.get("trend", {})) if ss else None
        s, _ = ts.analyze(h, fs, gate=gate, ticker=ticker)
        if not s or "setup" not in s:
            continue
        su, ex, ot, sw = s.get("setup", {}), s.get("exit_setup", {}), s.get("options_timing", {}), s.get("swing", {})
        row = {"ticker": ticker, "date": ts._date(hist.index[i]), "i": i, "close": float(c[i]),
               "regime": su.get("regime"), "entry": su.get("signal"), "exit": ex.get("signal"),
               "stretch": (s.get("stretch") or {}).get("state"),
               "cc_timing": (ot.get("cc") or {}).get("timing"), "cc_strike": (ot.get("cc") or {}).get("strike_at_or_above"),
               "csp_timing": (ot.get("csp") or {}).get("timing"), "csp_strike": (ot.get("csp") or {}).get("strike_at_or_below"),
               "gate": (gate or {}).get("state")}
        for side in ("long", "short"):
            pl = sw.get(side)
            if pl:
                row[f"{side}_entry"], row[f"{side}_stop"], row[f"{side}_t1"] = pl["entry"], pl["stop"], pl["targets"][0]
        for hz in HORIZONS:
            row[f"r{hz}"] = float(c[i + hz] / c[i] - 1) if i + hz < n else None
        # worst close in the next 20 sessions — the drawdown a buy here had to sit through
        row["mae20"] = float(c[i + 1:i + 21].min() / c[i] - 1) if i + 20 < n else None
        rows.append(row)
    return rows


def swing_outcome(hist, i, side, entry, stop, t1):
    """R multiple of a plan opened on day i, or None if it never filled / ran out of data."""
    c, hi, lo = hist["Close"].values, hist["High"].values, hist["Low"].values
    n = len(c)
    long = side == "long"
    fill = None
    if (long and c[i] >= entry * 0.999) or (not long and c[i] <= entry * 1.001):
        fill, f_i = c[i], i
    else:
        for j in range(i + 1, min(n, i + 1 + TRIGGER_WAIT)):
            if (long and c[j] >= entry) or (not long and c[j] <= entry):
                fill, f_i = c[j], j
                break
    if fill is None:
        return None
    risk = (fill - stop) if long else (stop - fill)
    if risk <= 0:
        return None
    for j in range(f_i + 1, min(n, f_i + 1 + SWING_H)):
        if (long and c[j] < stop) or (not long and c[j] > stop):
            return ((c[j] - fill) if long else (fill - c[j])) / risk
        if (long and hi[j] >= t1) or (not long and lo[j] <= t1):
            return ((t1 - fill) if long else (fill - t1)) / risk
    j = min(n - 1, f_i + SWING_H)
    if j - f_i < SWING_H:
        return None   # not enough future data to judge
    return ((c[j] - fill) if long else (fill - c[j])) / risk


def episodes(rows, key):
    out, prev = [], {}
    for r in rows:
        k = (r["ticker"], r[key])
        if prev.get(r["ticker"]) != r[key]:
            out.append(r)
        prev[r["ticker"]] = r[key]
        _ = k
    return out


def ret_table(rows, key, base):
    groups = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    table = []
    for sig, rs in sorted(groups.items(), key=lambda x: str(x[0])):
        line = {"signal": sig, "n": len(rs)}
        for hz in HORIZONS:
            v = [r[f"r{hz}"] for r in rs if r[f"r{hz}"] is not None]
            b = [r[f"r{hz}"] for r in base if r[f"r{hz}"] is not None]
            if v:
                line[f"mean_{hz}"] = float(np.mean(v))
                line[f"hit_{hz}"] = float(np.mean([x > 0 for x in v]))
                line[f"edge_{hz}"] = float(np.mean(v) - np.mean(b)) if b else None
        m = [r["mae20"] for r in rs if r.get("mae20") is not None]
        line["mae20"] = float(np.mean(m)) if m else None
        line["mae20_p10"] = float(np.percentile(m, 10)) if m else None
        table.append(line)
    return table


def option_table(rows, hist_by, side):
    """Keep rate of the suggested strike over OPT_H sessions vs the unconditional keep rate
    for a strike at the same % distance (computed from every day of the same ticker)."""
    key_t, key_k = f"{side}_timing", f"{side}_strike"
    # unconditional path extremes per ticker
    paths = {}
    for tk, h in hist_by.items():
        c = h["Close"].values
        ext = []
        for i in range(len(c) - OPT_H):
            seg = c[i + 1:i + 1 + OPT_H] / c[i] - 1
            ext.append(seg.max() if side == "cc" else seg.min())
        paths[tk] = np.array(ext)
    groups = {}
    for r in rows:
        k = r.get(key_k)
        h = hist_by[r["ticker"]]
        i = r["i"]
        if not k or i + OPT_H >= len(h):
            continue
        seg = h["Close"].values[i + 1:i + 1 + OPT_H]
        d = k / r["close"] - 1
        kept = bool(seg.max() < k) if side == "cc" else bool(seg.min() > k)
        base = paths[r["ticker"]]
        exp = float(np.mean(base < d)) if side == "cc" else float(np.mean(base > d))
        groups.setdefault(r[key_t], []).append((kept, exp, d))
    out = []
    for tm, v in sorted(groups.items(), key=lambda x: str(x[0])):
        kept = np.mean([x[0] for x in v])
        exp = np.mean([x[1] for x in v])
        out.append({"timing": tm, "n": len(v), "keep_rate": float(kept), "baseline_keep": float(exp),
                    "edge": float(kept - exp), "avg_strike_dist": float(np.mean([x[2] for x in v]))})
    return out


def swing_table(rows, hist_by):
    out = []
    for side in ("long", "short"):
        rs = []
        for r in episodes([x for x in rows if x.get(f"{side}_entry")], f"{side}_entry"):
            R = swing_outcome(hist_by[r["ticker"]], r["i"], side, r[f"{side}_entry"], r[f"{side}_stop"], r[f"{side}_t1"])
            if R is not None:
                rs.append(R)
        if rs:
            out.append({"side": side, "n": len(rs), "win_rate": float(np.mean([x > 0 for x in rs])),
                        "avg_R": float(np.mean(rs)), "median_R": float(np.median(rs))})
        else:
            out.append({"side": side, "n": 0})
    return out


def pct(x, nd=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}%"


def print_ret(title, table):
    print(f"\n{title}")
    print(f"  {'signal':<28}{'n':>6}  " + "  ".join(f"{'mean ' + str(h) + 'd':>9} {'edge':>7} {'hit':>5}" for h in HORIZONS)
          + f"  {'MAE20':>7} {'p10':>7}")
    for t in table:
        cells = []
        for h in HORIZONS:
            cells.append(f"{pct(t.get(f'mean_{h}')):>9} {pct(t.get(f'edge_{h}')):>7} "
                         f"{(t.get(f'hit_{h}') or 0) * 100:>4.0f}%")
        print(f"  {str(t['signal']):<28}{t['n']:>6}  " + "  ".join(cells)
              + f"  {pct(t.get('mae20')):>7} {pct(t.get('mae20_p10')):>7}")


def main():
    args = sys.argv[1:]
    tickers = [a.upper() for k, a in enumerate(args) if not a.startswith("-")
               and (k == 0 or args[k - 1] not in ("--years", "--step", "--csv", "--json"))]
    if not tickers:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    years = _arg(args, "--years", 3, float)
    step = _arg(args, "--step", 1, int)
    spy = None
    if "--gate" in args:
        spy, _ = load("SPY", years)
    rows, hist_by = [], {}
    for tk in tickers:
        hist, fs = load(tk, years)
        if len(hist) < 300:
            print(f"{tk}: only {len(hist)} bars — skipped", file=sys.stderr)
            continue
        hist_by[tk] = hist
        r = replay(tk, hist, fs, years, step, spy)
        print(f"{tk}: {len(r)} sessions replayed ({r[0]['date']} → {r[-1]['date']})" if r else f"{tk}: nothing", file=sys.stderr)
        rows += r
    if not rows:
        sys.exit(1)

    print(f"Backtest {', '.join(hist_by)} · {years:g}y · step {step} · gate {'on' if spy is not None else 'off'} · "
          f"daily closes, no costs")
    print("edge = mean forward return minus the all-days baseline over the same tickers and window")
    print("MAE20 = mean worst close over the next 20 sessions (p10 = the worst 10%) — the drawdown a buy had to sit through")
    summary = {"tickers": list(hist_by), "years": years, "step": step, "gate": spy is not None}
    # the entry gate's claim lives in the falling regime — show it on its own
    for r in rows:
        r["entry_by_regime"] = f"{r['regime']}:{r['entry']}"
    for key, title in (("entry", "ENTRY — setup.signal"), ("entry_by_regime", "ENTRY by regime"),
                       ("exit", "EXIT — exit_setup.signal")):
        for mode, rs in (("episodes", episodes(rows, key)), ("days", rows)):
            tb = ret_table(rs, key, rows)
            summary[f"{key}_{mode}"] = tb
            print_ret(f"{title} ({mode}: {'first day of each run' if mode == 'episodes' else 'every day'})", tb)

    print(f"\nOPTIONS — suggested strike out of the money after {OPT_H} sessions (episodes)")
    print(f"  {'':<5}{'timing':<13}{'n':>6} {'keep':>7} {'baseline':>9} {'edge':>7} {'avg dist':>9}")
    for side in ("cc", "csp"):
        tb = option_table(episodes(rows, f"{side}_timing"), hist_by, side)
        summary[f"options_{side}"] = tb
        for t in tb:
            print(f"  {side:<5}{str(t['timing']):<13}{t['n']:>6} {t['keep_rate'] * 100:>6.0f}% {t['baseline_keep'] * 100:>8.0f}% "
                  f"{pct(t['edge'], 0):>7} {pct(t['avg_strike_dist']):>9}")

    print(f"\nSWING — first of target-1 / stop within {SWING_H} sessions (episodes)")
    tb = swing_table(rows, hist_by)
    summary["swing"] = tb
    for t in tb:
        if t["n"]:
            print(f"  {t['side']:<6} n={t['n']:<5} win {t['win_rate'] * 100:.0f}% · avg R {t['avg_R']:+.2f} · median R {t['median_R']:+.2f}")
        else:
            print(f"  {t['side']:<6} n=0")
    print("\nn < 20 is anecdote. Survivorship, adjusted prices and today's float apply — see the docstring.")

    if _arg(args, "--csv"):
        keys = sorted({k for r in rows for k in r})
        with open(_arg(args, "--csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    if _arg(args, "--json"):
        Path(_arg(args, "--json")).write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
