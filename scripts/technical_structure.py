#!/usr/bin/env python3
"""
Chart-structure read for a ticker — the levels and patterns a technical analyst
talks about, computed from free yfinance daily OHLCV (pandas/numpy only).

Usage:
  technical_structure.py <TICKER> [--out <path.json>] [--chart <path.png>]   # prints a readable summary
  technical_structure.py <TICKER> [<TICKER> ...] [--chart-dir <dir>] [--json] [--no-gate]
                                   [--ledger tracking/ledger.json]           # several names; /technical-analysis uses this
  (also imported by fetch_ticker_stats.py → technicals.structure)

Blocks:
  trend        — MA stack (20/50/100/200) and slopes, swing structure
                 (higher highs / higher lows), ADX(14), last 50/200 cross
  macd         — MACD(12,26,9) DIF / DEA / histogram + last DIF/DEA cross
  divergences  — bearish / bullish: price makes a higher swing high (lower swing
                 low) while RSI(14) or MACD DIF makes a lower high (higher low)
  chips        — turnover-decay holder-cost distribution (the CYQ model) over
                 ~3y — chip peaks, average cost, profit ratio (share of holders in profit),
                 70%/90% cost ranges
  volume_profile — 12-month volume-by-price: POC + 70% value area
  anchored_vwap  — VWAP anchored at the 52-week high and low (average cost of
                 everyone who traded since that pivot)
  levels       — clustered support / resistance with their sources, the
                 `break_level` and the rule for a confirmed break
  recent_breaks — key levels crossed in the last 10 sessions, with volume ratio
  reversals    — bottoming (止跌) patterns in the last 20 sessions: hammer,
                 bullish engulfing, morning star, capitulation volume, double
                 bottom, base breakout — each with the level it formed at, a
                 confirmation price (trigger), an invalidation price (stop) and a
                 status: confirmed / pending / expired / failed
  tops         — the mirror image: topping patterns in the last 20 sessions —
                 shooting star, bearish engulfing, evening star, buying climax,
                 double top, top breakdown (distribution) — trigger = the close
                 below which the top confirms, stop = the high that voids it
  stretch      — overbought / oversold: RSI14, ATRs from the 20/50-day, Bollinger
                 %b, and a volatility squeeze flag
  exit_setup   — sell side for a held position: trim / trim_on_bounce / watch_top /
                 extended / at_resistance / broken / hold, with trigger,
                 invalidation and a trailing stop
  options_timing — covered-call and cash-secured-put timing (favorable / neutral /
                 unfavorable) with a strike guide from the level map
  swing        — long and short (bearish) swing plans: entry, stop, targets, R:R
  setup        — the entry gate every Market verdict leans on: regime (falling /
                 range / rising) → signal. In a falling regime the only "add" is a
                 confirmed bottoming pattern at a confluent level; otherwise wait.
  gate         — SPY's own setup (market gate). Closed → single-name adds wait;
                 index funds follow the user's dip rules instead.
  spacing      — minimum gap between buys = 2.5 × the 1-year average daily range
                 (clamped 3–20%), and, with --ledger, the last buy and whether
                 price is far enough below it

What this can't see: who is buying or selling. yfinance has no per-trade or
holder-level data; the chip model approximates holder cost from volume and
float, and 13F holdings lag ~45 days. A break below a big chip peak on heavy
volume is consistent with large holders exiting — it is not proof of it.
"""

import json
import math
import sys

import numpy as np
import pandas as pd

PIVOT_K = 5            # bars each side for a swing high/low — minor swings (S/R levels)
TREND_PIVOT_K = 10     # major swings for trend structure + chart markers
DIV_PIVOT_K = 8        # swings compared for divergences
CLUSTER_PCT = 0.015    # levels within 1.5% merge into one
CHIP_BINS = 240
DIVERGENCE_LOOKBACK = 150
BREAK_LOOKBACK = 10
REVERSAL_LOOKBACK = 20   # sessions scanned for bottoming patterns
CONFIRM_WINDOW = 3       # sessions a candle pattern has to confirm (close above its high)
SIGNAL_FRESH = 10        # a confirmed pattern older than this no longer counts as "now"
LEVEL_ATR = 1.0          # pattern low within this many ATRs of a level = formed at the level
INDEX_ETFS = {"SPY", "QQQ", "VOO", "IVV", "VTI", "DIA", "IWM", "RSP", "QQQM", "SPLG"}


def _r(x, nd=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _date(ts):
    return ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df, n=14):
    prev = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def adx(df, n=14):
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / n, min_periods=n, adjust=False).mean(), plus_di, minus_di


def pivots(series, kind, k=PIVOT_K):
    """Indices of confirmed swing highs ('high') or lows ('low')."""
    vals = series.values
    out = []
    for i in range(k, len(vals) - k):
        window = vals[i - k:i + k + 1]
        if kind == "high" and vals[i] == window.max():
            out.append(i)
        elif kind == "low" and vals[i] == window.min():
            out.append(i)
    # collapse equal-valued neighbours (flat tops/bottoms) to the last bar
    dedup = []
    for i in out:
        if dedup and i - dedup[-1] <= k and vals[i] == vals[dedup[-1]]:
            dedup[-1] = i
        else:
            dedup.append(i)
    return dedup


# ── trend ────────────────────────────────────────────────────────────────────

def trend_block(df, close):
    last = float(close.iloc[-1])
    out = {}
    mas = {}
    for n in (20, 50, 100, 200):
        if len(close) >= n:
            ma = close.rolling(n).mean()
            mas[n] = float(ma.iloc[-1])
            out[f"sma{n}"] = _r(mas[n])
            out[f"pct_vs_sma{n}"] = _r(last / mas[n] - 1, 4)
            if len(ma.dropna()) > 21:
                out[f"sma{n}_slope_20d"] = _r(float(ma.iloc[-1] / ma.iloc[-21] - 1), 4)
    if len(mas) == 4:
        chain = [last, mas[20], mas[50], mas[100], mas[200]]
        if all(a > b for a, b in zip(chain, chain[1:])):
            out["ma_stack"] = "bullish_aligned"      # price > 20 > 50 > 100 > 200
        elif all(a < b for a, b in zip(chain, chain[1:])):
            out["ma_stack"] = "bearish_aligned"      # price < 20 < 50 < 100 < 200
        else:
            out["ma_stack"] = "mixed"

    highs = pivots(df["High"], "high", TREND_PIVOT_K)
    lows = pivots(df["Low"], "low", TREND_PIVOT_K)
    structure = "insufficient_pivots"
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = df["High"].iloc[highs[-2]], df["High"].iloc[highs[-1]]
        l1, l2 = df["Low"].iloc[lows[-2]], df["Low"].iloc[lows[-1]]
        if h2 > h1 and l2 > l1:
            structure = "uptrend"            # higher highs + higher lows
        elif h2 < h1 and l2 < l1:
            structure = "downtrend"          # lower highs + lower lows
        elif h2 < h1 and l2 > l1:
            structure = "contracting_range"
        else:
            structure = "expanding_range"
        out["last_swing_highs"] = [{"date": _date(df.index[i]), "price": _r(df["High"].iloc[i])} for i in highs[-2:]]
        out["last_swing_lows"] = [{"date": _date(df.index[i]), "price": _r(df["Low"].iloc[i])} for i in lows[-2:]]
    out["swing_structure"] = structure   # from confirmed major pivots only — lags by TREND_PIVOT_K bars

    # Where price is now vs the last confirmed pivots: a close beyond them breaks
    # the pivot structure before a new pivot can confirm it.
    status = "inside_last_swings"
    if lows and last < float(df["Low"].iloc[lows[-1]]):
        status = "broke_below_last_swing_low"
    elif highs and last > float(df["High"].iloc[highs[-1]]):
        status = "broke_above_last_swing_high"
    out["structure_status"] = status

    a, pdi, mdi = adx(df)
    out["adx14"] = _r(a.iloc[-1], 1)
    out["plus_di"] = _r(pdi.iloc[-1], 1)
    out["minus_di"] = _r(mdi.iloc[-1], 1)
    adx_v = out["adx14"] or 0
    out["trend_strength"] = "strong" if adx_v >= 25 else "weak" if adx_v < 20 else "moderate"
    out["adx_direction"] = "up" if (out["plus_di"] or 0) > (out["minus_di"] or 0) else "down"

    # One-word read combining pivots, live break status, and the 200-day.
    above200 = mas.get(200) is not None and last > mas[200]
    if status == "broke_below_last_swing_low" or (structure == "downtrend" and not above200):
        out["trend"] = "downtrend"
    elif status == "broke_above_last_swing_high" or (structure == "uptrend" and above200):
        out["trend"] = "uptrend"
    else:
        out["trend"] = "range_or_transition"

    if len(close) >= 201:
        s50, s200 = close.rolling(50).mean(), close.rolling(200).mean()
        sign = np.sign(s50 - s200).dropna()
        flips = sign[sign != sign.shift()].iloc[1:]
        if len(flips):
            ts = flips.index[-1]
            out["last_50_200_cross"] = {
                "type": "golden_cross" if flips.iloc[-1] > 0 else "death_cross",
                "date": _date(ts),
                "sessions_ago": int(len(close) - 1 - close.index.get_loc(ts)),
            }
    return out


# ── MACD + divergences ───────────────────────────────────────────────────────

def macd_series(close):
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, dif - dea


def macd_block(close, dif, dea, hist):
    out = {"dif": _r(dif.iloc[-1], 3), "dea": _r(dea.iloc[-1], 3), "hist": _r(hist.iloc[-1], 3),
           "above_zero": bool(dif.iloc[-1] > 0)}
    sign = np.sign(hist).replace(0, np.nan).dropna()
    flips = sign[sign != sign.shift()].iloc[1:]
    if len(flips):
        ts = flips.index[-1]
        out["last_cross"] = {"type": "bullish" if flips.iloc[-1] > 0 else "bearish", "date": _date(ts),
                             "sessions_ago": int(len(close) - 1 - close.index.get_loc(ts))}
    return out


def divergences(df, indicators):
    n = len(df)
    start = max(0, n - DIVERGENCE_LOOKBACK)
    found = []
    for kind, col, label in (("high", "High", "bearish"), ("low", "Low", "bullish")):
        idx = [i for i in pivots(df[col], kind, DIV_PIVOT_K) if i >= start]
        if len(idx) < 2:
            continue
        i1, i2 = idx[-2], idx[-1]
        p1, p2 = float(df[col].iloc[i1]), float(df[col].iloc[i2])
        price_ext = p2 > p1 if kind == "high" else p2 < p1
        if not price_ext:
            continue
        for name, ser in indicators.items():
            v1, v2 = ser.iloc[i1], ser.iloc[i2]
            if not (np.isfinite(v1) and np.isfinite(v2)):
                continue
            ind_fails = v2 < v1 if kind == "high" else v2 > v1
            if ind_fails:
                found.append({
                    "type": label,                       # bearish: price higher high, indicator lower high
                    "indicator": name,
                    "pivot_1": {"date": _date(df.index[i1]), "price": _r(p1), "value": _r(v1, 2)},
                    "pivot_2": {"date": _date(df.index[i2]), "price": _r(p2), "value": _r(v2, 2)},
                    "sessions_since": int(n - 1 - i2),
                })
    return found


# ── chip distribution + volume profile ────────────────────────────────────────

def _spread(grid, lo, hi, peak):
    """Triangular weights over [lo, hi] peaking at `peak`, normalized to 1."""
    if hi - lo < 1e-9:
        w = np.zeros_like(grid)
        w[np.argmin(np.abs(grid - peak))] = 1.0
        return w
    w = np.where(grid < peak, (grid - lo) / max(peak - lo, 1e-9), (hi - grid) / max(hi - peak, 1e-9))
    w = np.where((grid >= lo) & (grid <= hi), np.clip(w, 0, None), 0.0)
    s = w.sum()
    if s <= 0:
        w = np.zeros_like(grid)
        w[np.argmin(np.abs(grid - peak))] = 1.0
        return w
    return w / s


def _peaks(grid, dist, last, top=4):
    sm = np.convolve(dist, np.ones(5) / 5, mode="same")
    cand = [i for i in range(1, len(sm) - 1) if sm[i] >= sm[i - 1] and sm[i] > sm[i + 1]]
    out = []
    for i in sorted(cand, key=lambda i: sm[i], reverse=True):
        price = grid[i]
        if any(abs(price / p["price"] - 1) < 0.04 for p in out):
            continue
        band = (grid >= price * 0.97) & (grid <= price * 1.03)
        out.append({"price": _r(price), "share_of_chips": _r(dist[band].sum(), 3),
                    "position": "below_price" if price < last else "above_price"})
        if len(out) == top:
            break
    return sorted(out, key=lambda p: p["price"])


def _quantile(grid, dist, q):
    c = np.cumsum(dist)
    return float(grid[min(np.searchsorted(c, q), len(grid) - 1)])


def chip_distribution(df, float_shares):
    """(price grid, normalized chip share per bin) — or (None, None) if no float/volume."""
    if not float_shares or float_shares <= 0:
        return None, None
    lo_all, hi_all = float(df["Low"].min()), float(df["High"].max())
    grid = np.linspace(lo_all, hi_all, CHIP_BINS)
    chips = np.zeros(CHIP_BINS)
    for h, l, c, v in zip(df["High"].values, df["Low"].values, df["Close"].values, df["Volume"].values):
        if not np.isfinite(v) or v <= 0:
            continue
        turnover = min(float(v) / float_shares, 1.0)
        chips *= (1 - turnover)
        chips += turnover * _spread(grid, float(l), float(h), (float(h) + float(l) + float(c)) / 3)
    total = chips.sum()
    if total <= 0:
        return None, None
    return grid, chips / total


def chips_block(df, float_shares):
    if not float_shares or float_shares <= 0:
        return None, "chips: float/shares outstanding unavailable — chip model skipped"
    grid, dist = chip_distribution(df, float_shares)
    if grid is None:
        return None, "chips: no volume data"
    last = float(df["Close"].iloc[-1])
    r70 = (_quantile(grid, dist, 0.15), _quantile(grid, dist, 0.85))
    r90 = (_quantile(grid, dist, 0.05), _quantile(grid, dist, 0.95))
    return {
        "model": "turnover-decay (CYQ) on daily volume / float — approximation, not holder data",
        "avg_cost": _r(float((grid * dist).sum())),
        "profit_ratio": _r(float(dist[grid <= last].sum()), 3),   # share of chips held below the current price
        "cost_range_70": [_r(r70[0]), _r(r70[1])],
        "cost_range_90": [_r(r90[0]), _r(r90[1])],
        "concentration_70": _r((r70[1] - r70[0]) / (r70[1] + r70[0]), 3),  # lower = tighter cost clustering
        "peaks": _peaks(grid, dist, last),
    }, None


def volume_profile(df, sessions=252, bins=60):
    d = df.iloc[-sessions:]
    grid = np.linspace(float(d["Low"].min()), float(d["High"].max()), bins)
    vol = np.zeros(bins)
    for h, l, v in zip(d["High"].values, d["Low"].values, d["Volume"].values):
        if np.isfinite(v) and v > 0:
            mask = (grid >= l) & (grid <= h)
            if mask.any():
                vol[mask] += v / mask.sum()
            else:
                vol[np.argmin(np.abs(grid - (h + l) / 2))] += v
    if vol.sum() <= 0:
        return None
    poc_i = int(vol.argmax())
    order = np.argsort(vol)[::-1]
    picked, acc = [], 0.0
    for i in order:
        picked.append(i)
        acc += vol[i]
        if acc >= 0.7 * vol.sum():
            break
    return {"window_sessions": len(d), "poc": _r(grid[poc_i]),
            "value_area_low": _r(grid[min(picked)]), "value_area_high": _r(grid[max(picked)])}


def anchored_vwap(df, sessions=252):
    d = df.iloc[-sessions:]
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    out = {}
    for name, ts in (("from_52w_high", d["High"].idxmax()), ("from_52w_low", d["Low"].idxmin())):
        seg_tp, seg_v = tp.loc[ts:], d["Volume"].loc[ts:]
        if seg_v.sum() > 0:
            out[name] = {"anchor_date": _date(ts), "vwap": _r(float((seg_tp * seg_v).sum() / seg_v.sum()))}
    return out


# ── levels + break level ──────────────────────────────────────────────────────

def _cluster(raw):
    raw = sorted(raw, key=lambda x: x[0])
    groups = []
    for price, src in raw:
        if groups and price / groups[-1]["prices"][0] - 1 <= CLUSTER_PCT:
            groups[-1]["prices"].append(price)
            groups[-1]["sources"].append(src)
        else:
            groups.append({"prices": [price], "sources": [src]})
    out = []
    for g in groups:
        kinds = {s.split(":")[0] for s in g["sources"]}
        out.append({"price": _r(float(np.mean(g["prices"]))), "sources": sorted(set(g["sources"])),
                    "confluence": len(kinds)})
    return out


def _raw_levels(df, trend, chips, vp, avwap, with_mas=True):
    """(price, source) candidates for support/resistance. MAs are today's values,
    so callers looking at past bars pass with_mas=False and add the MA as of that bar."""
    recent = df.iloc[-252:]
    raw = []
    for i in pivots(recent["Low"], "low"):
        raw.append((float(recent["Low"].iloc[i]), f"swing_low:{_date(recent.index[i])}"))
    for i in pivots(recent["High"], "high"):
        raw.append((float(recent["High"].iloc[i]), f"swing_high:{_date(recent.index[i])}"))
    if with_mas:
        for n in (50, 100, 200):
            if trend.get(f"sma{n}"):
                raw.append((trend[f"sma{n}"], f"sma{n}"))
    if chips:
        for p in chips["peaks"]:
            raw.append((p["price"], f"chip_peak:{p['share_of_chips']}"))
    if vp:
        for k in ("poc", "value_area_low", "value_area_high"):
            raw.append((vp[k], f"volume_profile:{k}"))
    recent_dates = {_date(ts) for ts in df.index[-3:]}
    for k, v in (avwap or {}).items():
        if v.get("anchor_date") in recent_dates:   # anchored at the last few bars = today's price, not a level
            continue
        raw.append((v["vwap"], f"anchored_vwap:{k}"))
    raw.append((float(recent["Low"].min()), "52w_low"))
    raw.append((float(recent["High"].max()), "52w_high"))
    return [r for r in raw if r[0] and r[0] > 0]


def levels_block(df, trend, chips, vp, avwap, atr14):
    last = float(df["Close"].iloc[-1])
    clusters = _cluster(_raw_levels(df, trend, chips, vp, avwap))
    supports = sorted([c for c in clusters if c["price"] < last * 0.995], key=lambda c: -c["price"])
    resist = sorted([c for c in clusters if c["price"] > last * 1.005], key=lambda c: c["price"])
    for c in supports + resist:
        c["pct_from_price"] = _r(c["price"] / last - 1, 4)

    # break level: the strongest-confluence support within 15% below; ties → nearest
    near = [c for c in supports if c["price"] >= last * 0.85]
    major = max(near, key=lambda c: (c["confluence"], c["price"])) if near else (supports[0] if supports else None)
    out = {"supports": supports[:5], "resistances": resist[:5], "atr14": _r(atr14),
           "atr_pct": _r(atr14 / last, 4) if atr14 else None}
    if major:
        out["break_level"] = {
            "price": major["price"],
            "pct_from_price": major["pct_from_price"],
            "sources": major["sources"],
            "confirmed_break_rule": ("daily close >1% below the level, or two consecutive closes below it, "
                                     "on volume ≥1.5× the 50-day average"),
        }
    return out


def recent_breaks(df, trend):
    """Key levels crossed in the last BREAK_LOOKBACK sessions."""
    close, vol = df["Close"], df["Volume"]
    v50 = vol.rolling(50).mean()
    n = len(df)
    if n < BREAK_LOOKBACK + 60:
        return []
    base = df.iloc[: n - BREAK_LOOKBACK]
    lows = pivots(base["Low"], "low")
    highs = pivots(base["High"], "high")
    levels = {}
    if lows:
        levels["last_swing_low"] = float(base["Low"].iloc[lows[-1]])
    if highs:
        levels["last_swing_high"] = float(base["High"].iloc[highs[-1]])
    out = []
    for name, lvl in levels.items():
        for i in range(n - BREAK_LOOKBACK, n):
            prev, cur = float(close.iloc[i - 1]), float(close.iloc[i])
            down = name == "last_swing_low" and prev >= lvl > cur
            up = name == "last_swing_high" and prev <= lvl < cur
            if down or up:
                out.append({"level": name, "price": _r(lvl), "direction": "break_down" if down else "break_out",
                            "date": _date(df.index[i]), "close": _r(cur),
                            "volume_ratio_vs_50d": _r(float(vol.iloc[i] / v50.iloc[i]), 2) if v50.iloc[i] else None})
    for n_ma in (50, 200):
        ma = close.rolling(n_ma).mean()
        for i in range(n - BREAK_LOOKBACK, n):
            p0, p1, m0, m1 = close.iloc[i - 1], close.iloc[i], ma.iloc[i - 1], ma.iloc[i]
            if not (np.isfinite(m0) and np.isfinite(m1)):
                continue
            if (p0 >= m0 and p1 < m1) or (p0 <= m0 and p1 > m1):
                out.append({"level": f"sma{n_ma}", "price": _r(m1),
                            "direction": "break_down" if p1 < m1 else "break_out",
                            "date": _date(df.index[i]), "close": _r(p1),
                            "volume_ratio_vs_50d": _r(float(vol.iloc[i] / v50.iloc[i]), 2) if v50.iloc[i] else None})
    return sorted(out, key=lambda x: x["date"])


# ── bottoming patterns (止跌形态) ──────────────────────────────────────────────

def _level_at(price, i, df, base_levels, atr_i, own_anchor="anchored_vwap:from_52w_low"):
    """The confluent level a pattern extreme at bar i sits on (within LEVEL_ATR × ATR), or None.
    Non-MA levels come from base_levels; MAs are taken as of bar i (no look-ahead).
    Works on mirrored (negated) prices too — see reversals_block(side="top")."""
    # Swings that form during or after the pattern are the pattern itself, not a level it held.
    # The 52-week extreme and the AVWAP anchored there are usually the pattern's own extreme — skip them.
    cutoff = _date(df.index[max(0, i - 5)])
    raw = [r for r in base_levels
           if not r[1].startswith(("52w_", own_anchor))
           and not (":" in r[1] and r[1].split(":", 1)[1][:4].isdigit() and r[1].split(":", 1)[1] >= cutoff)]
    close = df["Close"]
    for n in (50, 100, 200):
        if i + 1 >= n:
            raw.append((float(close.iloc[i + 1 - n:i + 1].mean()), f"sma{n}"))
    # Confluence = distinct kinds of level within one ATR of the low — a zone, not a single line.
    near = [r for r in raw if abs(r[0] - price) <= LEVEL_ATR * atr_i]
    if not near:
        return None
    kinds = {s.split(":")[0] for _, s in near}
    strong = len(kinds) >= 2 or "sma200" in kinds
    return {"price": _r(float(np.median([p for p, _ in near]))), "confluence": len(kinds),
            "sources": sorted({s for _, s in near}), "strong": bool(strong)}


def _status(df, i, trigger, stop, confirm_window=CONFIRM_WINDOW, already=False):
    """Lifecycle of a pattern ending at bar i: confirmed (a close above trigger before any
    close below stop), failed (a close below stop), pending (still inside its window),
    expired (window passed with neither)."""
    close = df["Close"].values
    n = len(df)
    confirmed_at = i if already else None
    for j in range(i + 1, n):
        if confirmed_at is None:
            if close[j] < stop:
                return "failed", None, j
            if close[j] > trigger:
                confirmed_at = j
            elif j - i >= confirm_window:
                return "expired", None, None
        elif close[j] < stop:
            return "failed", confirmed_at, j
    if confirmed_at is not None:
        return "confirmed", confirmed_at, None
    return "pending", None, None


TOP_NAMES = {"hammer": "shooting_star", "bullish_engulfing": "bearish_engulfing", "morning_star": "evening_star",
             "capitulation": "buying_climax", "double_bottom": "double_top", "base_breakout": "top_breakdown"}


def _mirror(df):
    """Price-negated copy: a top in df is a bottom in _mirror(df). Volume and ATR are unchanged."""
    m = df.copy()
    m["Open"], m["Close"], m["High"], m["Low"] = -df["Open"], -df["Close"], -df["Low"], -df["High"]
    return m


def reversals_block(df, base_levels, divs, side="bottom"):
    """Bottoming patterns (side="bottom") or their mirror-image topping patterns (side="top"):
    shooting star, bearish engulfing, evening star, buying climax, double top, top breakdown.
    For tops, `trigger` is the close BELOW which the top confirms, `stop` the high that voids it;
    `confirmed` = closed below trigger, `failed` = closed above stop."""
    top = side == "top"
    sg = -1.0 if top else 1.0
    if top:
        df = _mirror(df)
        base_levels = [(-p, src) for p, src in base_levels]
    own_anchor = "anchored_vwap:from_52w_high" if top else "anchored_vwap:from_52w_low"
    o, h, l, c, v = (df[k].values for k in ("Open", "High", "Low", "Close", "Volume"))
    a = atr(df).values
    v50 = df["Volume"].rolling(50).mean().values
    n = len(df)
    start = max(60, n - REVERSAL_LOOKBACK)
    out = []

    def declined(i):  # a real decline into bar i: down ≥1 ATR over 6 sessions, or a fresh 20-day low
        return (c[i - 1] < c[i - 6] - a[i]) or l[i] <= l[i - 20:i].min()

    def add(kind, i, trigger, stop, vol_ratio, already=False, note=None, window=CONFIRM_WINDOW):
        status, ci, fi = _status(df, i, trigger, stop, window, already)
        lvl = _level_at(stop, i, df, base_levels, a[i], own_anchor)
        if lvl and top:
            lvl = dict(lvl, price=_r(-lvl["price"]))
        out.append({"type": TOP_NAMES.get(kind, kind) if top else kind, "side": side,
                    "date": _date(df.index[i]), "sessions_ago": int(n - 1 - i),
                    "trigger": _r(sg * trigger), "stop": _r(sg * stop), "status": status,
                    "confirmed_date": _date(df.index[ci]) if ci is not None else None,
                    "confirmed_sessions_ago": int(n - 1 - ci) if ci is not None else None,
                    "failed_date": _date(df.index[fi]) if fi is not None else None, "at_level": lvl,
                    "volume_ratio_vs_50d": _r(vol_ratio, 2), **({"note": note} if note else {})})

    for i in range(start, n):
        if not (np.isfinite(a[i]) and a[i] > 0 and np.isfinite(v50[i]) and v50[i] > 0):
            continue
        rng = h[i] - l[i]
        body = abs(c[i] - o[i])
        lower, upper = min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])
        if rng > 0.5 * a[i] and lower >= 2 * max(body, 0.05 * rng) and upper <= max(body, 0.25 * rng) \
                and (c[i] - l[i]) / rng >= 0.6 and declined(i):
            add("hammer", i, h[i], l[i], v[i] / v50[i])
        if c[i - 1] < o[i - 1] and c[i] > o[i] and o[i] <= c[i - 1] and c[i] >= o[i - 1] \
                and body >= abs(o[i - 1] - c[i - 1]) and declined(i - 1):
            add("bullish_engulfing", i, h[i], min(l[i], l[i - 1]), max(v[i], v[i - 1]) / v50[i])
        b1 = o[i - 2] - c[i - 2]
        if b1 >= 0.5 * a[i - 2] and abs(c[i - 1] - o[i - 1]) <= 0.35 * b1 and c[i] > o[i] \
                and c[i] >= (o[i - 2] + c[i - 2]) / 2 and declined(i - 2):
            add("morning_star", i, c[i], min(l[i - 2:i + 1]), v[i] / v50[i], already=True)
        if v[i] >= 2.0 * v50[i] and l[i] <= l[i - 20:i].min() and (rng >= 1.5 * a[i - 1] or c[i - 1] - c[i] >= 1.5 * a[i - 1]):
            later = l[i + 1:]
            retest = [j for j in range(i + 1, n) if l[j] <= l[i] + a[i] and v[j] < 0.7 * v[i]]
            note = None
            if retest and later.min() >= l[i]:
                note = (f"retested on lighter volume {_date(df.index[retest[-1]])} and held "
                        + ("(lower high)" if top else "(higher low)"))
            add("capitulation", i, h[i], l[i], v[i] / v50[i], note=note, window=REVERSAL_LOOKBACK)

    # double bottom: a prior swing low and a recent low within 1 ATR of it, with a real bounce between
    lows = [p for p in pivots(df["Low"], "low", 3) if p >= n - 60]
    recent_i = int(np.argmin(l[n - 5:])) + n - 5
    cands = [p for p in lows if p <= recent_i - 5]
    for p1 in reversed(cands):
        p2 = next((p for p in lows if p > p1 + 4), None)
        p2 = p2 if p2 is not None and p2 >= n - 25 else recent_i
        if p2 <= p1 + 4 or p2 - p1 > 40:   # two lows of one base, not two unrelated dips
            continue
        neck = float(h[p1:p2 + 1].max())
        if abs(l[p2] - l[p1]) <= a[p2] and neck - max(l[p1], l[p2]) >= 1.5 * a[p2]:
            stop = float(min(l[p1], l[p2]))
            add("double_bottom", p2, neck, stop, v[p2] / v50[p2] if v50[p2] else np.nan,
                note=(f"{'highs' if top else 'lows'} {_date(df.index[p1])} ${sg * l[p1]:.2f} and "
                      f"{_date(df.index[p2])} ${sg * l[p2]:.2f}; confirmed on a close "
                      f"{'below' if top else 'above'} the ${sg * neck:.2f} neckline"),
                window=REVERSAL_LOOKBACK)
            # early trigger: a close above the second low's bar high — the aggressive entry, half size
            out[-1]["early_trigger"] = _r(sg * h[p2])
            break

    # base then breakout: ≥8 quiet sessions with no new low after a decline, then a high-volume close above the base
    for i in range(max(start, 40), n):
        w = slice(i - 10, i)
        top, bot = h[w].max(), l[w].min()
        if top - bot <= 3 * a[i - 1] and bot >= l[i - 30:i - 10].min() - 0.25 * a[i - 1] \
                and c[i - 10] < c[i - 30] and c[i] > top and v[i] >= 1.5 * v50[i]:
            add("base_breakout", i, top, bot, v[i] / v50[i], already=True)

    want = "bearish" if top else "bullish"
    for d in divs or []:
        if d["type"] == want and d["sessions_since"] <= REVERSAL_LOOKBACK:
            out.append({"type": f"{want}_divergence", "side": side, "indicator": d["indicator"],
                        "date": d["pivot_2"]["date"], "sessions_ago": d["sessions_since"], "status": "supporting",
                        "note": ("higher price high, lower indicator high" if top else
                                 "lower price low, higher indicator low") + " — supporting evidence, not a trigger"})
    return sorted(out, key=lambda x: x["sessions_ago"])


# ── entry gate ───────────────────────────────────────────────────────────────

def setup_block(df, s):
    """Regime → signal. Falling: add only on a confirmed bottoming pattern at a level."""
    t, lv = s.get("trend", {}), s.get("levels", {})
    close = df["Close"]
    last = float(close.iloc[-1])
    a = lv.get("atr14") or float(atr(df).iloc[-1])
    ret10 = last / float(close.iloc[-11]) - 1
    pos = {_date(ts): k for k, ts in enumerate(df.index)}
    fresh_breakdown = any(b["direction"] == "break_down" and (b.get("volume_ratio_vs_50d") or 0) >= 1.5
                          and len(df) - 1 - pos.get(b["date"], 0) <= 5 for b in s.get("recent_breaks", []))
    falling = (t.get("trend") == "downtrend"
               or ((t.get("pct_vs_sma20") or 0) < 0 and (t.get("sma20_slope_20d") or 0) < 0 and ret10 <= -2 * a / last)
               or fresh_breakdown)
    regime = "falling" if falling else ("rising" if t.get("trend") == "uptrend" else "range")

    pats = [p for p in s.get("reversals", []) if p.get("status") != "supporting"]
    at_lvl = [p for p in pats if p.get("at_level") and p["at_level"]["strong"]]
    # live = confirmed recently and price hasn't given the confirmation back (still within 1 ATR of the trigger)
    def given_back(p):  # a heavy-volume break_down after the confirmation voids it
        return any(b["direction"] == "break_down" and (b.get("volume_ratio_vs_50d") or 0) >= 1.5
                   and b["date"] > (p.get("confirmed_date") or p["date"]) for b in s.get("recent_breaks", []))
    live = [p for p in at_lvl if p["status"] == "confirmed" and last >= p["stop"]
            and p["confirmed_sessions_ago"] <= SIGNAL_FRESH and last >= p["trigger"] - a and not given_back(p)]
    pending = [p for p in at_lvl if p["status"] == "pending"]
    failed = [p for p in pats if p["status"] == "failed"]
    supports = lv.get("supports", [])
    strong_sup = [x for x in supports if x["confluence"] >= 2]
    bl = (lv.get("break_level") or {}).get("price")
    out = {"regime": regime, "atr14": _r(a)}

    def pat_txt(p):
        lvl = p["at_level"]
        return (f"{p['type'].replace('_', ' ')} {p['date']} at ${lvl['price']} ×{lvl['confluence']} "
                f"(trigger ${p['trigger']}, stop ${p['stop']}, {p['volume_ratio_vs_50d']}× vol)")

    if live:
        p = max(live, key=lambda p: (p["at_level"]["confluence"], -p["sessions_ago"]))
        extended = last > p["trigger"] + 2 * a
        out.update(signal="wait_pullback" if extended else "add",
                   basis=f"confirmed {pat_txt(p)}",
                   entry_zone=[_r(max(p["stop"], p["trigger"] - a)), _r(p["trigger"] + 0.5 * a)],
                   trigger=p["trigger"], stop=_r(p["stop"] * 0.99), pattern=p["type"])
        if extended:
            out["basis"] += f" — but price ran >2 ATR past the trigger; wait for a pullback toward ${p['trigger']}"
    elif regime == "falling":
        if pending:
            p = max(pending, key=lambda p: (p["at_level"]["confluence"], -p["sessions_ago"]))
            out.update(signal="wait_confirmation", basis=f"pending {pat_txt(p)} — buy only on a close above "
                       f"${p['trigger']}", trigger=p["trigger"], stop=_r(p["stop"] * 0.99), pattern=p["type"],
                       **({"early_trigger": p["early_trigger"]} if p.get("early_trigger") else {}))
        else:
            nxt = strong_sup[:2] or supports[:2]
            out.update(signal="no_add", basis="falling, no confirmed bottoming pattern at a level"
                       + (f" (latest {failed[0]['type'].replace('_', ' ')} {failed[0]['date']} failed)" if failed else ""),
                       watch=[{"price": x["price"], "confluence": x["confluence"]} for x in nxt])
    else:
        near = [x for x in strong_sup if last - x["price"] <= 1.5 * a]
        if near and (bl is None or last > bl):
            x = near[0]
            out.update(signal="add", basis=f"{regime}: holding ${x['price']} ×{x['confluence']} support, above the "
                       f"break level", entry_zone=[x["price"], _r(min(last, x["price"] + 1.5 * a))],
                       stop=_r((bl or x["price"]) * 0.99))
        else:
            x = strong_sup[0] if strong_sup else (supports[0] if supports else None)
            out.update(signal="wait_pullback", basis=f"{regime}: no confluent support within 1.5 ATR — let it come to you",
                       **({"entry_zone": [x["price"], _r(x["price"] + 0.5 * a)], "stop": _r((bl or x["price"]) * 0.99)}
                          if x else {}))
    return out


def gate_block(spy_setup, spy_trend):
    """Market gate from SPY's own setup: closed when SPY is falling below its 200-day without
    a confirmed bottom, caution when falling but above it."""
    if not spy_setup:
        return {"state": "unknown", "reason": "SPY read unavailable"}
    below200 = (spy_trend.get("pct_vs_sma200") or 0) < 0
    if spy_setup.get("signal") == "add" and spy_setup.get("pattern"):
        return {"state": "open", "reason": f"SPY {spy_setup['basis']}"}
    if spy_setup["regime"] == "falling" and below200:
        return {"state": "closed", "reason": "SPY falling and below its 200-day with no confirmed bottom — "
                                             "single-name adds wait; index funds follow the dip rules"}
    if spy_setup["regime"] == "falling":
        return {"state": "caution", "reason": "SPY falling but still above its 200-day — smaller tranches"}
    return {"state": "open", "reason": f"SPY {spy_setup['regime']}"}


def apply_gate(setup, gate, ticker):
    """Single-name adds wait while the gate is closed; index ETFs are the dip-buy vehicle."""
    if gate.get("state") == "closed" and setup.get("signal") == "add" and ticker.upper() not in INDEX_ETFS:
        setup = dict(setup, signal="wait_gate", basis=setup["basis"] + " — market gate closed (SPY)")
    return setup


def spacing_block(df, ticker, trades=None):
    """Minimum gap between buys = 2.5 × 1-year average daily range, clamped 3–20%."""
    d = df.iloc[-252:]
    prev = d["Close"].shift()
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - prev).abs(), (d["Low"] - prev).abs()], axis=1).max(axis=1)
    atr1y = float((tr / d["Close"]).mean())
    gap = min(max(round(2.5 * atr1y * 100), 3), 20) / 100
    out = {"atr_1y_pct": _r(atr1y, 4), "min_gap_pct": gap}
    buys = [t for t in (trades or []) if t.get("ticker", "").upper() == ticker.upper() and t.get("action") == "buy"
            and t.get("kind") == "fill" and t.get("price_per_share")]
    if buys:
        lb = max(buys, key=lambda t: t["date"])
        nxt = lb["price_per_share"] * (1 - gap)
        last = float(df["Close"].iloc[-1])
        out.update(last_buy={"date": lb["date"], "price": _r(lb["price_per_share"])}, next_buy_at_or_below=_r(nxt),
                   spacing_ok=bool(last <= nxt))
    return out


# ── stretch, sell side, options timing, swing plans ──────────────────────────

def stretch_block(df):
    """How far price has run from its means — overbought / oversold, and a volatility squeeze."""
    close = df["Close"]
    last = float(close.iloc[-1])
    a = float(atr(df).iloc[-1])
    sma20, sd20 = close.rolling(20).mean(), close.rolling(20).std()
    sma50 = close.rolling(50).mean()
    up, lo = sma20 + 2 * sd20, sma20 - 2 * sd20
    width = (up - lo) / sma20
    r14 = float(rsi(close).iloc[-1])
    out = {"rsi14": _r(r14, 1),
           "atr_from_sma20": _r((last - float(sma20.iloc[-1])) / a, 2),
           "atr_from_sma50": _r((last - float(sma50.iloc[-1])) / a, 2),
           "bb_pct_b": _r((last - float(lo.iloc[-1])) / float(up.iloc[-1] - lo.iloc[-1]), 2),
           "bb_width_pct": _r(float(width.iloc[-1]), 4)}
    w = width.iloc[-126:].dropna()
    out["squeeze"] = bool(len(w) > 60 and float(width.iloc[-1]) <= float(w.quantile(0.10)))
    ob = r14 >= 70 or (out["atr_from_sma20"] or 0) >= 3 or (out["bb_pct_b"] or 0) > 1
    os_ = r14 <= 30 or (out["atr_from_sma20"] or 0) <= -3 or (out["bb_pct_b"] or 1) < 0
    out["state"] = "overbought" if ob and not os_ else "oversold" if os_ and not ob else "neutral"
    return out


def exit_setup_block(df, s):
    """Sell side for a position already held — mirror of `setup`. Signal:
    trim (confirmed top pattern at resistance; trim_on_bounce when price already ran >2 ATR past it),
    watch_top (pending one), extended (stretched) / at_resistance (pressing confluent resistance) —
    don't chase, covered-call window; broken (falling regime — trade positions exit, core holdings
    pause adds + thesis check); hold."""
    last = float(df["Close"].iloc[-1])
    lv, st, su = s.get("levels", {}), s.get("stretch", {}), s.get("setup", {})
    a = lv.get("atr14") or float(atr(df).iloc[-1])
    tops = [p for p in s.get("tops", []) if p.get("status") != "supporting"]
    at_lvl = [p for p in tops if p.get("at_level") and p["at_level"]["strong"]]
    live = [p for p in at_lvl if p["status"] == "confirmed" and last <= p["stop"]
            and p["confirmed_sessions_ago"] <= SIGNAL_FRESH and last <= p["trigger"] + a]
    pending = [p for p in at_lvl if p["status"] == "pending"]
    res = [x for x in lv.get("resistances", []) if x["confluence"] >= 2]
    near_res = [x for x in res if x["price"] - last <= 1.0 * a]
    bl = (lv.get("break_level") or {}).get("price")
    if bl and bl >= last * 0.85:
        out = {"trail_stop": _r(bl * 0.99),
               "trail_stop_rule": "confirmed close below the break level (see levels.break_level.confirmed_break_rule)"}
    else:
        out = {"trail_stop": _r(last - 3 * a) if a else None,
               "trail_stop_rule": "no support within 15% (open air below) — 3 × ATR under price, not a level"}

    def pat_txt(p):
        lvl = p["at_level"]
        return (f"{p['type'].replace('_', ' ')} {p['date']} at ${lvl['price']} ×{lvl['confluence']} "
                f"(trigger ${p['trigger']}, voided above ${p['stop']}, {p['volume_ratio_vs_50d']}× vol)")

    if live:
        p = max(live, key=lambda p: (p["at_level"]["confluence"], -p["sessions_ago"]))
        late = last < p["trigger"] - 2 * a
        out.update(signal="trim_on_bounce" if late else "trim", basis=f"confirmed {pat_txt(p)}", trigger=p["trigger"],
                   invalidation=_r(p["stop"] * 1.01), pattern=p["type"])
        if late:
            out["basis"] += (f" — but price is already >2 ATR below the trigger; don't sell the washout, "
                             f"trim on a bounce toward ${p['trigger']}")
    elif su.get("regime") == "falling" and su.get("signal") in ("no_add", "wait_confirmation"):
        out.update(signal="broken", basis="falling regime — trade positions exit on the break rule; core holdings "
                                          "are never sold on a level alone: pause adds, check the thesis")
    elif pending:
        p = max(pending, key=lambda p: (p["at_level"]["confluence"], -p["sessions_ago"]))
        out.update(signal="watch_top", basis=f"pending {pat_txt(p)} — trim only on a close below ${p['trigger']}",
                   trigger=p["trigger"], invalidation=_r(p["stop"] * 1.01), pattern=p["type"])
    elif st.get("state") == "overbought" or near_res:
        why = []
        if st.get("state") == "overbought":
            why.append(f"stretched (RSI {st.get('rsi14')}, {st.get('atr_from_sma20')} ATR above the 20-day)")
        if near_res:
            why.append(f"pressing ${near_res[0]['price']} ×{near_res[0]['confluence']} resistance")
        ob = st.get("state") == "overbought"
        out.update(signal="extended" if ob else "at_resistance",
                   basis=" and ".join(why) + (" — don't chase; trim trading shares into strength, covered-call window"
                                              if ob else " — don't chase; overhead supply, covered-call window"),
                   overhead=near_res[0]["price"] if near_res else None)
    else:
        out.update(signal="hold", basis="no top pattern at a level, not stretched — let it run, trail the stop")
    return out


def options_timing_block(s):
    """When the chart favors selling premium: covered calls into strength / resistance, cash-secured puts
    at support after a bottom. Strike guides come from confluent levels; /options does the chain math."""
    last = s.get("price")
    lv, st, su, ex = s.get("levels", {}), s.get("stretch", {}), s.get("setup", {}), s.get("exit_setup", {})
    a = lv.get("atr14") or 0
    res = [x for x in lv.get("resistances", []) if x["confluence"] >= 2] or lv.get("resistances", [])
    sup = [x for x in lv.get("supports", []) if x["confluence"] >= 2] or lv.get("supports", [])
    gate = (s.get("gate") or {}).get("state")

    # covered call: you want the stock NOT to run through the strike
    above = [x["price"] for x in res if last + a <= x["price"] <= last + 6 * a]
    cc = {"strike_at_or_above": above[0] if above else (_r(last + 2 * a) if a else None),
          "strike_basis": "first confluent resistance ≥1 ATR above price" if above else "no level — 2 ATR above price"}
    if st.get("state") == "oversold" or ex.get("signal") == "trim_on_bounce":
        cc.update(timing="unfavorable", reason="washed out — selling calls into an oversold low gets little premium "
                                               "and a bounce can run through the strike; sell into the rebound")
    elif ex.get("signal") in ("trim", "watch_top", "extended", "at_resistance"):
        cc.update(timing="favorable", reason=f"exit setup {ex['signal']}: {ex.get('basis')}")
    elif su.get("signal") in ("add", "wait_confirmation"):
        cc.update(timing="unfavorable", reason="washed out / bottoming — a bounce can run through a call sold here; "
                                               "wait for the rebound into resistance")
    elif su.get("regime") == "rising" and not st.get("state") == "overbought":
        cc.update(timing="neutral", reason="uptrend with room — sell only far OTM, above the next resistance")
    else:
        cc.update(timing="neutral", reason="no strong edge from the chart")

    # cash-secured put: assignment should land you at a level you'd defend — the nearest confluent support
    # at/below the pattern stop (or price), within 3 ATR; past that there is no level, so say so
    ref = su["stop"] if su.get("stop") and su.get("signal") in ("add", "wait_gate", "wait_confirmation") else last
    if su.get("signal") == "no_add" and su.get("watch"):
        ref = min(ref, min(w["price"] for w in su["watch"]) * 1.001)
    cand = [x["price"] for x in sup if x["price"] <= ref and (not a or ref - x["price"] <= 3 * a)]
    csp = {"strike_at_or_below": _r(cand[0] if cand else ref - 1.5 * a) if (cand or a) else None,
           "strike_basis": "confluent support" if cand else "no level within 3 ATR — 1.5 ATR under the stop/price"}
    if su.get("signal") == "wait_confirmation":
        csp.update(timing="neutral", reason=f"bottom forming, not confirmed ({su.get('basis')}) — a put at/below the "
                                            "pattern stop is defensible; favorable once it confirms")
    elif su.get("signal") in ("add", "wait_gate") or (st.get("state") == "oversold" and su.get("regime") != "falling"):
        csp.update(timing="favorable", reason=f"setup {su.get('signal')}: {su.get('basis')} — strike at/below the pattern stop or support")
    elif su.get("signal") == "no_add":
        csp.update(timing="unfavorable", reason="falling with no bottom at a level — if you sell anyway, only below the "
                                                "lowest watch level, small, and with assignment you'd welcome")
    elif st.get("state") == "overbought":
        csp.update(timing="unfavorable", reason="stretched — puts are cheap and the pullback hasn't come; wait for support")
    else:
        csp.update(timing="neutral", reason="no strong edge from the chart — strike at/below the nearest confluent support")
    if gate == "closed" and csp.get("timing") == "favorable":
        csp.update(timing="neutral", reason=csp["reason"] + " — but the SPY gate is closed: size down")
    return {"cc": cc, "csp": csp, "squeeze": bool(st.get("squeeze")),
            "note": "squeeze = Bollinger width in its lowest 10% of 6 months: premium is thin and a big move is "
                    "more likely — a poor time to sell premium" if st.get("squeeze") else None}


def swing_block(s):
    """Trade plans in both directions: entry, stop, first/second target from the level map, reward:risk.
    `short` is a bearish swing (puts / put spreads, or trimming) — it never implies shorting core holdings."""
    last = s.get("price")
    lv, su, ex = s.get("levels", {}), s.get("setup", {}), s.get("exit_setup", {})
    a = lv.get("atr14") or 0
    res = [x["price"] for x in lv.get("resistances", [])]
    sup = [x["price"] for x in lv.get("supports", [])]
    out = {}

    def plan(direction, entry, stop, targets, basis):
        if not (entry and stop and targets) or (entry - stop) * (1 if direction == "long" else -1) <= 0:
            return None
        risk = abs(entry - stop)
        rr = abs(targets[0] - entry) / risk
        return {"entry": _r(entry), "stop": _r(stop), "targets": [_r(t) for t in targets[:2]],
                "reward_risk": _r(rr, 2), "quality": "good" if rr >= 2 else "marginal" if rr >= 1 else "poor",
                "basis": basis}

    if su.get("signal") in ("add", "wait_confirmation", "wait_gate", "wait_pullback") and su.get("stop"):
        entry = su.get("trigger") if su.get("signal") == "wait_confirmation" else \
            (su["entry_zone"][1] if su.get("entry_zone") else last)
        entry = entry or last
        tg = [r for r in res if entry + 0.5 * a <= r <= entry + 6 * a] or ([_r(entry + 2 * a)] if a else [])
        pl = plan("long", entry, su["stop"], tg, f"setup {su['signal']}: {su.get('basis')}")
        if pl:
            out["long"] = pl
    if ex.get("signal") in ("trim", "trim_on_bounce", "watch_top") and ex.get("invalidation"):
        entry = ex["trigger"] if ex["signal"] in ("watch_top", "trim_on_bounce") else min(last, ex["trigger"])
        tg = sorted([x for x in sup if entry - 6 * a <= x <= entry - 0.5 * a], reverse=True) \
            or ([_r(entry - 2 * a)] if a else [])
        pl = plan("short", entry, ex["invalidation"], tg, f"exit setup {ex['signal']}: {ex.get('basis')}")
        if pl:
            out["short"] = pl
    if not out:
        out["none"] = "no pattern-backed swing in either direction right now"
    return out


_SPY_CACHE = {}


def spy_gate():
    """Fetch SPY once per process and return its gate block."""
    if "gate" in _SPY_CACHE:
        return _SPY_CACHE["gate"]
    try:
        import yfinance as yf
        s, _ = analyze(yf.Ticker("SPY").history(period="3y", interval="1d"), None)
        _SPY_CACHE["gate"] = gate_block(s.get("setup"), s.get("trend", {}))
    except Exception as e:  # noqa: BLE001
        _SPY_CACHE["gate"] = {"state": "unknown", "reason": f"SPY read failed: {e}"}
    return _SPY_CACHE["gate"]


# ── entry point ──────────────────────────────────────────────────────────────

def analyze(hist, float_shares=None, gate=None, ticker=None):
    """`hist`: yfinance daily history (Open/High/Low/Close/Volume). Returns (block, notes).
    `gate`: SPY gate block (spy_gate()) — applied to `setup` when given."""
    notes = []
    df = hist[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"])
    if len(df) < 120:
        return {}, ["structure: <120 daily bars — too short for chart structure"]
    close = df["Close"]
    out = {"asof": _date(df.index[-1]), "price": _r(close.iloc[-1]), "bars": len(df)}
    blocks = [
        ("trend", lambda: trend_block(df, close)),
    ]
    for name, fn in blocks:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            notes.append(f"structure.{name}: {e}")
            out[name] = {}
    try:
        dif, dea, hist_ = macd_series(close)
        out["macd"] = macd_block(close, dif, dea, hist_)
        out["divergences"] = divergences(df, {"rsi14": rsi(close), "macd_dif": dif})
    except Exception as e:  # noqa: BLE001
        notes.append(f"structure.macd/divergences: {e}")
    chips = None
    try:
        chips, note = chips_block(df, float_shares)
        if note:
            notes.append(note)
        out["chips"] = chips
    except Exception as e:  # noqa: BLE001
        notes.append(f"structure.chips: {e}")
    vp = avwap = None
    try:
        vp = volume_profile(df)
        out["volume_profile"] = vp
        avwap = anchored_vwap(df)
        out["anchored_vwap"] = avwap
    except Exception as e:  # noqa: BLE001
        notes.append(f"structure.volume_profile/avwap: {e}")
    try:
        a = atr(df).iloc[-1]
        out["levels"] = levels_block(df, out.get("trend", {}), chips, vp, avwap, float(a) if np.isfinite(a) else None)
        out["recent_breaks"] = recent_breaks(df, out.get("trend", {}))
    except Exception as e:  # noqa: BLE001
        notes.append(f"structure.levels: {e}")
    base = []
    try:
        base = _raw_levels(df, out.get("trend", {}), chips, vp, avwap, with_mas=False)
        out["reversals"] = reversals_block(df, base, out.get("divergences"))
        out["setup"] = setup_block(df, out)
        if gate:
            out["gate"] = gate
            out["setup"] = apply_gate(out["setup"], gate, ticker or "")
    except Exception as e:  # noqa: BLE001
        notes.append(f"structure.reversals/setup: {e}")
    for name, fn in (("stretch", lambda: stretch_block(df)),
                     ("tops", lambda: reversals_block(df, base, out.get("divergences"), side="top")),
                     ("exit_setup", lambda: exit_setup_block(df, out)),
                     ("options_timing", lambda: options_timing_block(out)),
                     ("swing", lambda: swing_block(out))):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            notes.append(f"structure.{name}: {e}")
    return out, notes


def render_chart(df, s, float_shares, path, ticker, sessions=252):
    """Annotated daily K-line chart: candles + MAs + AVWAPs + levels + break line +
    swing pivots + divergence lines, with chip distribution, volume, MACD, RSI.
    Labels are English-only (matplotlib's default font has no CJK glyphs)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    d = df.iloc[-sessions:]
    x = np.arange(len(d))
    pos = {ts: i for i, ts in enumerate(d.index)}
    date_pos = {_date(ts): i for i, ts in enumerate(d.index)}
    close_all = df["Close"]
    last = float(d["Close"].iloc[-1])
    up_c, dn_c = "#16a34a", "#dc2626"

    fig = plt.figure(figsize=(16, 11), dpi=110)
    gs = GridSpec(4, 2, figure=fig, width_ratios=[5, 1], height_ratios=[5, 1.1, 1.4, 1.2], hspace=0.06, wspace=0.02)
    ax = fig.add_subplot(gs[0, 0])
    axc = fig.add_subplot(gs[0, 1], sharey=ax)
    axv = fig.add_subplot(gs[1, 0], sharex=ax)
    axm = fig.add_subplot(gs[2, 0], sharex=ax)
    axr = fig.add_subplot(gs[3, 0], sharex=ax)

    # candles
    o, h, l, c = (d[k].values for k in ("Open", "High", "Low", "Close"))
    col = np.where(c >= o, up_c, dn_c)
    ax.vlines(x, l, h, color=col, linewidth=0.7)
    ax.bar(x, np.maximum(np.abs(c - o), (h - l) * 0.002 + 1e-9), bottom=np.minimum(o, c), color=col, width=0.7)

    # moving averages
    for n, colr in ((20, "#f59e0b"), (50, "#2563eb"), (200, "#7c3aed")):
        ma = close_all.rolling(n).mean().reindex(d.index)
        ax.plot(x, ma.values, color=colr, linewidth=1.2, label=f"SMA{n}")

    # anchored VWAPs
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    for name, v in (s.get("anchored_vwap") or {}).items():
        i0 = date_pos.get(v["anchor_date"])
        if i0 is None:
            continue
        seg_tp, seg_v = tp.iloc[i0:], d["Volume"].iloc[i0:]
        av = (seg_tp * seg_v).cumsum() / seg_v.cumsum()
        ax.plot(x[i0:], av.values, color="#0891b2", linewidth=1, linestyle="-.",
                label=f"AVWAP {name.replace('from_', 'from ').replace('_', ' ')}")

    # swing pivots
    for i in pivots(d["High"], "high", TREND_PIVOT_K):
        ax.annotate("▼", (i, h[i]), textcoords="offset points", xytext=(0, 4), ha="center", fontsize=7, color="#475569")
    for i in pivots(d["Low"], "low", TREND_PIVOT_K):
        ax.annotate("▲", (i, l[i]), textcoords="offset points", xytext=(0, -10), ha="center", fontsize=7, color="#475569")

    # levels
    lv = s.get("levels", {})
    xr = len(d) + 1
    for cl, colr in ((lv.get("supports", [])[:3], up_c), (lv.get("resistances", [])[:3], dn_c)):
        for lvl in cl:
            ax.axhline(lvl["price"], color=colr, linestyle="--", linewidth=0.8, alpha=0.7)
            ax.text(xr, lvl["price"], f" ${lvl['price']:.0f} ×{lvl['confluence']}", color=colr, fontsize=8, va="center")
    bl = lv.get("break_level")
    if bl:
        ax.axhline(bl["price"], color="#b91c1c", linewidth=2)
        ax.text(2, bl["price"], f"BREAK LEVEL ${bl['price']:.2f}  ({bl['pct_from_price']:+.1%})",
                color="white", fontsize=9, va="center", fontweight="bold",
                bbox=dict(facecolor="#b91c1c", edgecolor="none", pad=2))
    ax.axhline(last, color="#0f172a", linewidth=0.6, linestyle=":")

    # recent breaks
    for b in s.get("recent_breaks", []):
        i = date_pos.get(b["date"])
        if i is not None:
            ax.annotate(f"{b['direction'].replace('_', ' ')}\n{b['level']}", (i, b["close"]),
                        xytext=(-60, -40 if b["direction"] == "break_down" else 30), textcoords="offset points",
                        fontsize=8, color="#b91c1c" if b["direction"] == "break_down" else up_c,
                        arrowprops=dict(arrowstyle="->", color="#64748b"))

    # bottoming patterns: label under the pattern bar, colored by status; trigger as a short tick
    st_col = {"confirmed": up_c, "pending": "#d97706", "failed": "#64748b", "expired": "#94a3b8"}
    abbr = {"hammer": "hammer", "bullish_engulfing": "engulf", "morning_star": "m-star", "capitulation": "capit.",
            "double_bottom": "dbl bottom", "base_breakout": "base b/o"}
    # only patterns still in play (confirmed/pending), newest of each type, labels staggered
    shown, seen = [], set()
    for p in s.get("reversals", []):
        if p["type"] in abbr and p["status"] in ("confirmed", "pending") and p["type"] not in seen:
            seen.add(p["type"])
            shown.append(p)
    for k, p in enumerate(shown[:4]):
        i = date_pos.get(p["date"])
        if i is None:
            continue
        colr = st_col.get(p["status"], "#64748b")
        ax.annotate(f"{abbr[p['type']]} · {p['status']}", (i, l[i]), xytext=(-40, -22 - 14 * k),
                    textcoords="offset points", ha="right", fontsize=7.5, color=colr, fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color=colr, lw=0.8))
        ax.hlines(p["trigger"], max(i - 3, 0), min(i + 6, len(d) - 1), color=colr, linewidth=1.4, linestyle=":")

    # topping patterns: label above the pattern bar
    tabbr = {"shooting_star": "shoot. star", "bearish_engulfing": "bear engulf", "evening_star": "e-star",
             "buying_climax": "climax", "double_top": "dbl top", "top_breakdown": "top b/d"}
    tcol = {"confirmed": dn_c, "pending": "#d97706", "failed": "#64748b", "expired": "#94a3b8"}
    tshown, tseen = [], set()
    for p in s.get("tops", []):
        if p["type"] in tabbr and p["status"] in ("confirmed", "pending") and p["type"] not in tseen:
            tseen.add(p["type"])
            tshown.append(p)
    for k, p in enumerate(tshown[:4]):
        i = date_pos.get(p["date"])
        if i is None:
            continue
        colr = tcol.get(p["status"], "#64748b")
        ax.annotate(f"{tabbr[p['type']]} · {p['status']}", (i, h[i]), xytext=(-40, 22 + 14 * k),
                    textcoords="offset points", ha="right", fontsize=7.5, color=colr, fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color=colr, lw=0.8))
        ax.hlines(p["trigger"], max(i - 3, 0), min(i + 6, len(d) - 1), color=colr, linewidth=1.4, linestyle=":")

    # indicators
    dif, dea, hist_ = macd_series(close_all)
    dif, dea, hist_ = (ser.reindex(d.index) for ser in (dif, dea, hist_))
    r = rsi(close_all).reindex(d.index)
    axm.bar(x, hist_.values, color=np.where(hist_.values >= 0, up_c, dn_c), width=0.7, alpha=0.6)
    axm.plot(x, dif.values, color="#2563eb", linewidth=1, label="DIF")
    axm.plot(x, dea.values, color="#f59e0b", linewidth=1, label="DEA")
    axm.axhline(0, color="#94a3b8", linewidth=0.5)
    axr.plot(x, r.values, color="#7c3aed", linewidth=1)
    for lvl_ in (70, 30):
        axr.axhline(lvl_, color="#94a3b8", linewidth=0.5, linestyle="--")
    axr.set_ylim(0, 100)

    # divergences: connect the two pivots on price and on the indicator
    for dv in s.get("divergences", []):
        i1, i2 = date_pos.get(dv["pivot_1"]["date"]), date_pos.get(dv["pivot_2"]["date"])
        if i1 is None or i2 is None:
            continue
        colr = dn_c if dv["type"] == "bearish" else up_c
        ax.plot([i1, i2], [dv["pivot_1"]["price"], dv["pivot_2"]["price"]], color=colr, linewidth=1.8)
        ax.annotate(f"{dv['type']} divergence ({dv['indicator']})", (i2, dv["pivot_2"]["price"]),
                    xytext=(10, 18 if dv["type"] == "bearish" else -22), textcoords="offset points", fontsize=8, color=colr)
        tgt = axr if dv["indicator"] == "rsi14" else axm
        tgt.plot([i1, i2], [dv["pivot_1"]["value"], dv["pivot_2"]["value"]], color=colr, linewidth=1.8)

    vcol = np.where(c >= o, up_c, dn_c)
    axv.bar(x, d["Volume"].values / 1e6, color=vcol, width=0.7, alpha=0.6)
    axv.plot(x, d["Volume"].rolling(50).mean().values / 1e6, color="#0f172a", linewidth=0.8)

    # chip distribution (horizontal), split profit (below price) vs trapped (above)
    grid, dist = chip_distribution(df, float_shares)
    if grid is not None:
        bw = (grid[1] - grid[0]) * 0.9
        axc.barh(grid, dist, height=bw, color=np.where(grid <= last, "#f87171", "#60a5fa"))
        for p in (s.get("chips") or {}).get("peaks", []):
            axc.text(dist.max() * 1.02, p["price"], f"{p['share_of_chips']:.0%}", fontsize=7, va="center")
        axc.set_title("Chip distribution\n(red = in profit, blue = trapped)", fontsize=8)
    axc.axhline(last, color="#0f172a", linewidth=0.6, linestyle=":")
    axc.set_xticks([])
    plt.setp(axc.get_yticklabels(), visible=False)

    # cosmetics
    lo_y = min(l.min(), bl["price"] if bl else l.min()) * 0.97
    ax.set_ylim(lo_y, h.max() * 1.03)
    ax.set_xlim(-1, len(d) + 8)
    ticks = np.linspace(0, len(d) - 1, 9).astype(int)
    axr.set_xticks(ticks)
    axr.set_xticklabels([_date(d.index[i]) for i in ticks], fontsize=8)
    for a in (ax, axv, axm):
        plt.setp(a.get_xticklabels(), visible=False)
    ax.legend(loc="upper left", fontsize=8, ncol=3, framealpha=0.8)
    axm.legend(loc="upper left", fontsize=7)
    axv.set_ylabel("Vol (M)", fontsize=8)
    axm.set_ylabel("MACD", fontsize=8)
    axr.set_ylabel("RSI14", fontsize=8)
    for a in (ax, axv, axm, axr):
        a.grid(alpha=0.15)
    t = s.get("trend", {})
    ch = s.get("chips") or {}
    sub = (f"trend {t.get('trend')} · pivots {t.get('swing_structure')} · {t.get('structure_status')} · "
           f"ADX {t.get('adx14')} {t.get('adx_direction')}")
    if ch:
        sub += f" · chips avg cost ${ch['avg_cost']:.0f}, {ch['profit_ratio']:.0%} in profit"
    su = s.get("setup") or {}
    if su:
        sub += f"\nsetup: {su.get('regime')} → {su.get('signal', '').upper().replace('_', ' ')}"
        if su.get("pattern"):
            sub += f" · {su['pattern'].replace('_', ' ')}"
        if su.get("trigger"):
            sub += f" · trigger ${su['trigger']}"
        if su.get("stop"):
            sub += f" · stop ${su['stop']}"
    ex = s.get("exit_setup") or {}
    if ex:
        sub += f" | exit: {ex.get('signal', '').upper()}"
        if ex.get("pattern"):
            sub += f" · {ex['pattern'].replace('_', ' ')}"
    title = f"{ticker}  ${last:.2f}  ({s.get('asof')})\n{sub}"
    ax.set_title(title.replace("$", r"\$"), fontsize=11, loc="left")   # "$" would start mathtext
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def summarize(ticker, s):
    """Human-readable digest for the CLI."""
    t, lv, ch = s.get("trend", {}), s.get("levels", {}), s.get("chips") or {}
    lines = [f"{ticker} @ ${s['price']} ({s['asof']})"]
    lines.append(f"Trend: {t.get('trend')} (pivots {t.get('swing_structure')}, {t.get('structure_status')}) · MA stack {t.get('ma_stack')} · ADX {t.get('adx14')} ({t.get('trend_strength')}, {t.get('adx_direction')})")
    lines.append("  MAs: " + "  ".join(f"{n}d ${t.get(f'sma{n}')} ({(t.get(f'pct_vs_sma{n}') or 0):+.1%})"
                                       for n in (20, 50, 100, 200) if t.get(f"sma{n}")))
    if t.get("last_50_200_cross"):
        c = t["last_50_200_cross"]
        lines.append(f"  Last 50/200 cross: {c['type']} {c['date']} ({c['sessions_ago']} sessions ago)")
    m = s.get("macd", {})
    if m:
        lc = m.get("last_cross", {})
        lines.append(f"MACD: DIF {m['dif']} DEA {m['dea']} hist {m['hist']} · last cross {lc.get('type')} {lc.get('date')}")
    for d in s.get("divergences", []):
        tag = d["type"]
        lines.append(f"Divergence ({tag}, {d['indicator']}): price {d['pivot_1']['price']}→{d['pivot_2']['price']} "
                     f"vs {d['indicator']} {d['pivot_1']['value']}→{d['pivot_2']['value']} ({d['pivot_2']['date']}, "
                     f"{d['sessions_since']} sessions ago)")
    if ch:
        lines.append(f"Chips: avg cost ${ch['avg_cost']} · {ch['profit_ratio']:.0%} in profit · 70% range "
                     f"${ch['cost_range_70'][0]}–${ch['cost_range_70'][1]}")
        lines.append("  Peaks: " + "  ".join(f"${p['price']} ({p['share_of_chips']:.0%})" for p in ch["peaks"]))
    vp = s.get("volume_profile")
    if vp:
        lines.append(f"Volume profile 12m: POC ${vp['poc']} · value area ${vp['value_area_low']}–${vp['value_area_high']}")
    for k, v in (s.get("anchored_vwap") or {}).items():
        lines.append(f"AVWAP {k} ({v['anchor_date']}): ${v['vwap']}")
    for side in ("supports", "resistances"):
        for c in lv.get(side, [])[:3]:
            lines.append(f"{side[:-1].capitalize()}: ${c['price']} ({c['pct_from_price']:+.1%}) ×{c['confluence']} — {', '.join(c['sources'])}")
    bl = lv.get("break_level")
    if bl:
        lines.append(f"Break level: ${bl['price']} ({bl['pct_from_price']:+.1%}) — {', '.join(bl['sources'])}")
        lines.append(f"  confirmed if: {bl['confirmed_break_rule']} · ATR14 ${lv.get('atr14')}")
    for b in s.get("recent_breaks", []):
        lines.append(f"Recent: {b['direction']} {b['level']} ${b['price']} on {b['date']} (vol {b['volume_ratio_vs_50d']}× 50d)")
    for p in s.get("reversals", []) + s.get("tops", []):
        tag = "Top" if p.get("side") == "top" else "Pattern"
        if p["status"] == "supporting":
            lines.append(f"{tag}: {p['type'].replace('_', ' ')} ({p['indicator']}) {p['date']} — supporting")
            continue
        lvl = p.get("at_level")
        at = f"at ${lvl['price']} ×{lvl['confluence']}" if lvl else "not at a level"
        st = p["status"]
        if p.get("confirmed_date") and p.get("failed_date"):
            st = f"confirmed {p['confirmed_date']}, failed {p['failed_date']}"
        elif p.get("confirmed_date"):
            st = f"confirmed {p['confirmed_date']}"
        elif p.get("failed_date"):
            st = f"failed {p['failed_date']} (never confirmed)"
        lines.append(f"{tag}: {p['type']} {p['date']} {at} · {st} · trigger ${p['trigger']} "
                     f"{'void above' if tag == 'Top' else 'stop'} ${p['stop']} · vol {p['volume_ratio_vs_50d']}×"
                     + (f" · {p['note']}" if p.get("note") else ""))
    st_ = s.get("stretch")
    if st_:
        lines.append(f"STRETCH: {st_['state']} · RSI {st_['rsi14']} · {st_['atr_from_sma20']:+} ATR vs 20d, "
                     f"{st_['atr_from_sma50']:+} vs 50d · BB %b {st_['bb_pct_b']}" + (" · SQUEEZE" if st_["squeeze"] else ""))
    su = s.get("setup")
    if su:
        extra = ""
        if su.get("entry_zone"):
            extra += f" · zone ${su['entry_zone'][0]}–${su['entry_zone'][1]}"
        if su.get("trigger") and su.get("signal") != "add":
            extra += f" · trigger ${su['trigger']}"
        if su.get("early_trigger"):
            extra += f" (early: close above ${su['early_trigger']}, half size)"
        if su.get("stop"):
            extra += f" · stop (confirmed close below) ${su['stop']}"
        if su.get("watch"):
            extra += " · watch " + ", ".join(f"${w['price']} ×{w['confluence']}" for w in su["watch"])
        lines.append(f"SETUP: {su['regime']} → {su['signal'].upper()} — {su['basis']}{extra}")
    ex = s.get("exit_setup")
    if ex:
        extra = ""
        if ex.get("trigger"):
            extra += f" · trigger (close below) ${ex['trigger']}"
        if ex.get("invalidation"):
            extra += f" · void on close above ${ex['invalidation']}"
        if ex.get("trail_stop"):
            extra += f" · trail stop ${ex['trail_stop']}"
        lines.append(f"EXIT (if held): {ex['signal'].upper()} — {ex['basis']}{extra}")
    ot = s.get("options_timing")
    if ot:
        cc, csp = ot["cc"], ot["csp"]
        lines.append(f"OPTIONS: covered call {cc['timing']} (strike ≥ ${cc.get('strike_at_or_above')}) — {cc['reason']}")
        lines.append(f"         cash-secured put {csp['timing']} (strike ≤ ${csp.get('strike_at_or_below')}) — {csp['reason']}")
        if ot.get("note"):
            lines.append(f"         {ot['note']}")
    for side, pl in (s.get("swing") or {}).items():
        if side == "none":
            lines.append(f"SWING: {pl}")
        else:
            lines.append(f"SWING {side}: entry ${pl['entry']} · stop ${pl['stop']} · targets "
                         + ", ".join(f"${t}" for t in pl["targets"]) + f" · R:R {pl['reward_risk']} ({pl['quality']})")
    g = s.get("gate")
    if g:
        lines.append(f"GATE (SPY): {g['state']} — {g['reason']}")
    sp = s.get("spacing")
    if sp:
        line = f"SPACING: min gap {sp['min_gap_pct']:.0%} (2.5 × 1y avg range {sp['atr_1y_pct']:.1%})"
        if sp.get("last_buy"):
            line += (f" · last buy ${sp['last_buy']['price']} {sp['last_buy']['date']} → next buy ≤ "
                     f"${sp['next_buy_at_or_below']} ({'ok' if sp['spacing_ok'] else 'too close'})")
        lines.append(line)
    return "\n".join(lines)


def _load_trades(path):
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
        import ledger as lg
        return lg.stock_trades(lg.load(__import__("pathlib").Path(path)))
    except Exception as e:  # noqa: BLE001
        print(f"NOTE ledger: {e}", file=sys.stderr)
        return []


def run(ticker, gate=None, trades=None, chart_path=None):
    """One ticker end to end: fetch, analyze, spacing, optional chart. Returns (structure, notes)."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    hist = t.history(period="3y", interval="1d")
    info = {}
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        pass
    float_shares = info.get("floatShares") or info.get("sharesOutstanding")
    s, notes = analyze(hist, float_shares, gate=gate, ticker=ticker)
    if not s:
        return s, notes
    df = hist[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"])
    try:
        s["spacing"] = spacing_block(df, ticker, trades)
    except Exception as e:  # noqa: BLE001
        notes.append(f"spacing: {e}")
    if chart_path:
        try:
            render_chart(df, s, float_shares, chart_path, ticker)
            s["chart"] = chart_path
        except Exception as e:  # noqa: BLE001
            notes.append(f"chart: {e}")
    s["notes"] = notes
    return s, notes


def main():
    args = sys.argv[1:]
    flags_with_value = {"--out", "--chart", "--chart-dir", "--ledger"}
    tickers, skip = [], False
    for k, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in flags_with_value:
            skip = True
        elif not a.startswith("-"):
            tickers.append(a.upper())
    if not tickers:
        print("usage: technical_structure.py <TICKER> [<TICKER> ...] [--out <path.json>] [--chart <path.png>] "
              "[--chart-dir <dir>] [--json] [--no-gate] [--ledger <ledger.json>]", file=sys.stderr)
        sys.exit(1)
    val = lambda f: args[args.index(f) + 1] if f in args else None  # noqa: E731
    out_path, chart_path, chart_dir, ledger_path = val("--out"), val("--chart"), val("--chart-dir"), val("--ledger")
    gate = None if "--no-gate" in args else spy_gate()
    trades = _load_trades(ledger_path) if ledger_path else None
    results, failed = {}, False
    for tk in tickers:
        cp = chart_path if (chart_path and len(tickers) == 1) else \
            (str(__import__("pathlib").Path(chart_dir) / f"chart_{tk}.png") if chart_dir else None)
        s, notes = run(tk, gate, trades, cp)
        if not s:
            print(f"{tk}: " + "; ".join(notes), file=sys.stderr)
            failed = True
            continue
        results[tk] = s
        if "--json" not in args:
            print(summarize(tk, s) + ("\n" if len(tickers) > 1 else ""))
            for n in notes:
                print(f"NOTE {n}", file=sys.stderr)
    if out_path:
        with open(out_path, "w") as f:
            json.dump(results[tickers[0]] if len(tickers) == 1 and tickers[0] in results else results, f, indent=2)
    if "--json" in args:
        print(json.dumps(results, indent=2))
    if failed and not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
