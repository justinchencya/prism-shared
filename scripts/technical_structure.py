#!/usr/bin/env python3
"""
Chart-structure read for a ticker — the levels and patterns a technical analyst
talks about, computed from free yfinance daily OHLCV (pandas/numpy only).

Usage:
  technical_structure.py <TICKER> [--out <path.json>] [--chart <path.png>]   # prints a readable summary
  (also imported by fetch_ticker_stats.py → technicals.structure)

Blocks:
  trend        — 趋势: MA stack (20/50/100/200) and slopes, swing structure
                 (higher highs / higher lows), ADX(14), last 50/200 cross
  macd         — MACD(12,26,9) DIF / DEA / histogram + last DIF/DEA cross
  divergences  — 顶背离 / 底背离: price makes a higher swing high (lower swing
                 low) while RSI(14) or MACD DIF makes a lower high (higher low)
  chips        — 筹码分布: turnover-decay cost distribution (the CYQ model) over
                 ~3y — chip peaks (筹码峰), average cost, profit ratio (获利盘),
                 70%/90% cost ranges
  volume_profile — 12-month volume-by-price: POC + 70% value area
  anchored_vwap  — VWAP anchored at the 52-week high and low (average cost of
                 everyone who traded since that pivot)
  levels       — clustered support / resistance with their sources, the
                 `break_level` (破位线) and the rule for a confirmed break
  recent_breaks — key levels crossed in the last 10 sessions, with volume ratio

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
            out["ma_stack"] = "bullish_aligned"      # 多头排列
        elif all(a < b for a, b in zip(chain, chain[1:])):
            out["ma_stack"] = "bearish_aligned"      # 空头排列
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
                    "type": label,                       # bearish = 顶背离, bullish = 底背离
                    "indicator": name,
                    "pivot_1": {"date": _date(df.index[i1]), "price": _r(p1), "value": _r(v1, 2)},
                    "pivot_2": {"date": _date(df.index[i2]), "price": _r(p2), "value": _r(v2, 2)},
                    "sessions_since": int(n - 1 - i2),
                })
    return found


# ── chips (筹码分布) + volume profile ─────────────────────────────────────────

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
        "profit_ratio": _r(float(dist[grid <= last].sum()), 3),   # 获利比例: share of chips in profit
        "cost_range_70": [_r(r70[0]), _r(r70[1])],
        "cost_range_90": [_r(r90[0]), _r(r90[1])],
        "concentration_70": _r((r70[1] - r70[0]) / (r70[1] + r70[0]), 3),  # lower = tighter 筹码集中
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


# ── levels + 破位 ─────────────────────────────────────────────────────────────

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


def levels_block(df, trend, chips, vp, avwap, atr14):
    last = float(df["Close"].iloc[-1])
    recent = df.iloc[-252:]
    raw = []
    for i in pivots(recent["Low"], "low"):
        raw.append((float(recent["Low"].iloc[i]), f"swing_low:{_date(recent.index[i])}"))
    for i in pivots(recent["High"], "high"):
        raw.append((float(recent["High"].iloc[i]), f"swing_high:{_date(recent.index[i])}"))
    for n in (50, 100, 200):
        if trend.get(f"sma{n}"):
            raw.append((trend[f"sma{n}"], f"sma{n}"))
    if chips:
        for p in chips["peaks"]:
            raw.append((p["price"], f"chip_peak:{p['share_of_chips']}"))
    if vp:
        for k in ("poc", "value_area_low", "value_area_high"):
            raw.append((vp[k], f"volume_profile:{k}"))
    for k, v in (avwap or {}).items():
        raw.append((v["vwap"], f"anchored_vwap:{k}"))
    raw.append((float(recent["Low"].min()), "52w_low"))
    raw.append((float(recent["High"].max()), "52w_high"))

    clusters = _cluster([r for r in raw if r[0] and r[0] > 0])
    supports = sorted([c for c in clusters if c["price"] < last * 0.995], key=lambda c: -c["price"])
    resist = sorted([c for c in clusters if c["price"] > last * 1.005], key=lambda c: c["price"])
    for c in supports + resist:
        c["pct_from_price"] = _r(c["price"] / last - 1, 4)

    # 破位线: the strongest-confluence support within 15% below; ties → nearest
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


# ── entry point ──────────────────────────────────────────────────────────────

def analyze(hist, float_shares=None):
    """`hist`: yfinance daily history (Open/High/Low/Close/Volume). Returns (block, notes)."""
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
    ax.set_title(f"{ticker}  ${last:.2f}  ({s.get('asof')})\n{sub}", fontsize=11, loc="left")
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
        tag = "顶背离 bearish" if d["type"] == "bearish" else "底背离 bullish"
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
        lines.append(f"破位 break level: ${bl['price']} ({bl['pct_from_price']:+.1%}) — {', '.join(bl['sources'])}")
        lines.append(f"  confirmed if: {bl['confirmed_break_rule']} · ATR14 ${lv.get('atr14')}")
    for b in s.get("recent_breaks", []):
        lines.append(f"Recent: {b['direction']} {b['level']} ${b['price']} on {b['date']} (vol {b['volume_ratio_vs_50d']}× 50d)")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("usage: technical_structure.py <TICKER> [--out <path.json>] [--chart <path.png>]", file=sys.stderr)
        sys.exit(1)
    ticker = args[0].upper()
    out_path = args[args.index("--out") + 1] if "--out" in args else None
    chart_path = args[args.index("--chart") + 1] if "--chart" in args else None
    import yfinance as yf
    t = yf.Ticker(ticker)
    hist = t.history(period="3y", interval="1d")
    info = {}
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        pass
    float_shares = info.get("floatShares") or info.get("sharesOutstanding")
    s, notes = analyze(hist, float_shares)
    if not s:
        print("; ".join(notes), file=sys.stderr)
        sys.exit(1)
    if chart_path:
        try:
            render_chart(hist[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"]),
                         s, float_shares, chart_path, ticker)
            s["chart"] = chart_path
        except Exception as e:  # noqa: BLE001
            notes.append(f"chart: {e}")
    s["notes"] = notes
    if out_path:
        with open(out_path, "w") as f:
            json.dump(s, f, indent=2)
    print(summarize(ticker, s))
    for n in notes:
        print(f"NOTE {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
