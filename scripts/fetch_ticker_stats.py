#!/usr/bin/env python3
"""
Fetch standardized financial snapshot + historical trend data for a ticker.

Usage:
  fetch_ticker_stats.py <TICKER> <output-path>

Writes:
  <output-path>   JSON with:
    snapshot      — current price, PE ratios, market cap, EV/revenue, margins, EPS estimates
    history       — quarterly revenue/margins/YoY growth (up to 8 quarters)
    price_history — monthly closing prices for 3 years
    technicals    — entry-timing/positioning lens computed in pandas (no ta-lib):
                    50/200-day MAs + distance, RSI(14), trailing returns,
                    relative strength vs SPY, volume trend, 52-week position
    edgar_cross_check — annual revenue from EDGAR 10-K filings (sanity check)

  All technicals are derived price math — they inform *when / at what level* to
  enter, never *whether the thesis holds*. The relative-strength-vs-SPY block is
  the closest free proxy for where capital is flowing. This is a long-term
  holder's tool: not a buy/sell signal generator.

Sources:
  Primary:   Yahoo Finance via yfinance (pip install yfinance)
  Secondary: SEC EDGAR company facts API (free, no auth) — annual revenue cross-check

Handles gracefully: missing fields become null; exits 0 on partial failures.
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# SEC EDGAR requires a contact email in the User-Agent. Read it from the env so
# the engine carries no personal address; falls back to a generic placeholder.
UA = f"Prism researcher ({os.environ.get('EDGAR_CONTACT_EMAIL', 'prism-user@example.com')})"


def log(msg: str) -> None:
    print(f"fetch_ticker_stats: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_num(val) -> bool:
    if val is None:
        return False
    try:
        return not math.isnan(float(val))
    except (TypeError, ValueError):
        return False


def get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def ts_to_period(ts) -> str:
    """Convert a pandas Timestamp to 'YYYY-QN' label."""
    try:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.fromisoformat(str(ts)[:10])
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    except Exception:
        return str(ts)[:10]


def fetch_snapshot(info: dict, notes: list) -> dict:
    snap = {}

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    snap["price"] = round(float(price), 2) if is_num(price) else None

    mc = info.get("marketCap")
    snap["market_cap_b"] = round(float(mc) / 1e9, 2) if is_num(mc) else None

    for field, key in [("trailing_pe", "trailingPE"), ("forward_pe", "forwardPE")]:
        val = info.get(key)
        snap[field] = round(float(val), 2) if is_num(val) else None
        if not is_num(val):
            notes.append(f"{field}: not available (may be negative earnings or missing from yfinance)")

    rev = info.get("totalRevenue")
    snap["revenue_ttm_b"] = round(float(rev) / 1e9, 3) if is_num(rev) else None

    rg = info.get("revenueGrowth")
    snap["revenue_growth_yoy"] = round(float(rg), 4) if is_num(rg) else None

    gm = info.get("grossMargins")
    snap["gross_margin"] = round(float(gm), 4) if is_num(gm) else None

    om = info.get("operatingMargins")
    snap["operating_margin"] = round(float(om), 4) if is_num(om) else None

    ev = info.get("enterpriseValue")
    rev2 = info.get("totalRevenue")
    ebitda = info.get("ebitda")
    snap["ev_revenue_ttm"] = round(float(ev) / float(rev2), 2) if is_num(ev) and is_num(rev2) else None
    snap["ev_ebitda"] = round(float(ev) / float(ebitda), 2) if is_num(ev) and is_num(ebitda) else None

    fwd_eps = info.get("forwardEps")
    snap["eps_fwd_y1"] = round(float(fwd_eps), 2) if is_num(fwd_eps) else None

    snap["week52_high"] = round(float(info["fiftyTwoWeekHigh"]), 2) if is_num(info.get("fiftyTwoWeekHigh")) else None
    snap["week52_low"] = round(float(info["fiftyTwoWeekLow"]), 2) if is_num(info.get("fiftyTwoWeekLow")) else None

    return snap


def fetch_quarterly_history(t, notes: list) -> dict:
    history = {}

    # yfinance >=0.2 uses quarterly_income_stmt; older versions used quarterly_financials
    qis = None
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            qis = getattr(t, attr, None)
            if qis is not None and not qis.empty:
                break
        except Exception:
            pass

    if qis is None or qis.empty:
        notes.append("quarterly_income_stmt: empty or unavailable")
        return history

    try:
        # Sort columns oldest → newest, keep at most 8 quarters
        cols_sorted = sorted(qis.columns, key=lambda c: c.to_pydatetime() if hasattr(c, "to_pydatetime") else c)
        qis = qis[cols_sorted].iloc[:, -8:]
        periods = [ts_to_period(c) for c in qis.columns]

        def extract_row(labels: list, divisor: float = 1.0, decimals: int = 3):
            for label in labels:
                if label in qis.index:
                    row = qis.loc[label]
                    return [
                        {"period": p, "value": round(float(v) / divisor, decimals) if is_num(v) else None}
                        for p, v in zip(periods, row)
                    ]
            return None

        rev_records = extract_row(["Total Revenue", "Revenue"], divisor=1e9)
        if rev_records:
            history["quarterly_revenue_b"] = rev_records
        else:
            notes.append("quarterly_revenue: no 'Total Revenue' or 'Revenue' row in income statement")

        gp_records = extract_row(["Gross Profit"], divisor=1e9)
        if gp_records and history.get("quarterly_revenue_b"):
            rev_map = {r["period"]: r["value"] for r in history["quarterly_revenue_b"]}
            history["quarterly_gross_margin"] = [
                {"period": r["period"],
                 "value": round(r["value"] / rev_map[r["period"]], 4)
                 if r["value"] is not None and rev_map.get(r["period"]) else None}
                for r in gp_records
            ]

        oi_records = extract_row(["Operating Income", "EBIT"], divisor=1e9)
        if oi_records and history.get("quarterly_revenue_b"):
            rev_map = {r["period"]: r["value"] for r in history["quarterly_revenue_b"]}
            history["quarterly_operating_margin"] = [
                {"period": r["period"],
                 "value": round(r["value"] / rev_map[r["period"]], 4)
                 if r["value"] is not None and rev_map.get(r["period"]) else None}
                for r in oi_records
            ]

        # YoY revenue growth — needs at least 5 quarters (compares quarter N to quarter N-4)
        rev_list = history.get("quarterly_revenue_b", [])
        if len(rev_list) >= 5:
            yoy = []
            for i in range(4, len(rev_list)):
                cur = rev_list[i]["value"]
                prior = rev_list[i - 4]["value"]
                if cur is not None and prior:
                    yoy.append({"period": rev_list[i]["period"], "value": round((cur - prior) / prior, 4)})
                else:
                    yoy.append({"period": rev_list[i]["period"], "value": None})
            history["quarterly_revenue_growth_yoy"] = yoy

    except Exception as e:
        notes.append(f"quarterly_history: {e}")

    return history


def fetch_price_history(t, notes: list) -> dict:
    try:
        hist = t.history(period="3y", interval="1mo")
        if hist is None or hist.empty:
            notes.append("price_history: empty")
            return {}
        monthly = []
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            close = row.get("Close") if hasattr(row, "get") else row["Close"]
            monthly.append({"date": date_str, "close": round(float(close), 2) if is_num(close) else None})
        return {"monthly_close": monthly}
    except Exception as e:
        notes.append(f"price_history: {e}")
        return {}


def _pct(numer, denom):
    """Safe percentage-change as a decimal (e.g. 0.0432 = +4.32%). None on bad input."""
    try:
        if denom in (None, 0) or numer is None:
            return None
        return round(float(numer) / float(denom) - 1.0, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _trailing_return(closes, lookback_days: int):
    """Return over the last `lookback_days` trading sessions, as a decimal."""
    if closes is None or len(closes) <= lookback_days:
        return None
    return _pct(closes.iloc[-1], closes.iloc[-1 - lookback_days])


def fetch_technicals(t, ticker: str, notes: list) -> dict:
    """Entry-timing / positioning lens computed from daily price data in pandas.

    All values are price-derived math (no ta-lib): moving averages and distance
    from them, Wilder RSI(14), trailing returns, relative strength vs SPY, and a
    volume trend. These inform *when / at what level* to enter — not the thesis.
    Degrades gracefully: any failure appends a note and the block is partial.
    """
    tech: dict = {}
    try:
        import pandas as pd  # noqa: F401 — bundled with yfinance

        hist = t.history(period="2y", interval="1d")
        if hist is None or hist.empty or "Close" not in hist:
            notes.append("technicals: daily price history empty")
            return tech

        close = hist["Close"].dropna()
        if len(close) < 30:
            notes.append("technicals: <30 daily closes — too short for indicators")
            return tech

        last = float(close.iloc[-1])
        asof = close.index[-1]
        tech["asof"] = asof.strftime("%Y-%m-%d") if hasattr(asof, "strftime") else str(asof)[:10]
        tech["price"] = round(last, 2)

        # Moving averages (need enough history; null if not)
        for win, key in [(50, "sma50"), (200, "sma200")]:
            if len(close) >= win:
                ma = float(close.rolling(win).mean().iloc[-1])
                tech[key] = round(ma, 2)
                tech[f"pct_vs_{key}"] = _pct(last, ma)
            else:
                tech[key] = None
                tech[f"pct_vs_{key}"] = None

        # 52-week position from daily data
        window = close.iloc[-252:] if len(close) >= 252 else close
        hi, lo = float(window.max()), float(window.min())
        tech["week52_high"] = round(hi, 2)
        tech["week52_low"] = round(lo, 2)
        tech["pct_from_52w_high"] = _pct(last, hi)   # ≤0: below the high
        tech["pct_from_52w_low"] = _pct(last, lo)    # ≥0: above the low

        # Wilder RSI(14)
        try:
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - 100 / (1 + rs)
            rsi_last = rsi.iloc[-1]
            tech["rsi14"] = round(float(rsi_last), 1) if is_num(rsi_last) else None
        except Exception as e:
            notes.append(f"technicals.rsi14: {e}")
            tech["rsi14"] = None

        # Trailing returns (≈21 trading days / month)
        tech["return_1m"] = _trailing_return(close, 21)
        tech["return_3m"] = _trailing_return(close, 63)
        tech["return_6m"] = _trailing_return(close, 126)
        tech["return_12m"] = _trailing_return(close, 252)

        # Relative strength vs SPY — closest free proxy for capital flow direction.
        # Positive excess return = outperforming the index over that window.
        try:
            import yfinance as yf
            spy = yf.Ticker("SPY").history(period="1y", interval="1d")
            if spy is not None and not spy.empty and "Close" in spy:
                spy_close = spy["Close"].dropna()
                rel = {"benchmark": "SPY"}
                for days, key in [(63, "excess_return_3m"), (126, "excess_return_6m")]:
                    tr = _trailing_return(close, days)
                    sr = _trailing_return(spy_close, days)
                    rel[key] = round(tr - sr, 4) if tr is not None and sr is not None else None
                tech["rel_strength_vs_spy"] = rel
            else:
                notes.append("technicals.rel_strength: SPY history empty")
        except Exception as e:
            notes.append(f"technicals.rel_strength: {e}")

        # Volume trend — recent 20-session avg vs prior ~3-month avg
        try:
            if "Volume" in hist:
                vol = hist["Volume"].dropna()
                if len(vol) >= 63:
                    v20 = float(vol.iloc[-20:].mean())
                    v3m = float(vol.iloc[-63:].mean())
                    tech["volume"] = {
                        "avg_20d": round(v20),
                        "avg_3m": round(v3m),
                        "ratio_20d_vs_3m": round(v20 / v3m, 2) if v3m else None,
                    }
        except Exception as e:
            notes.append(f"technicals.volume: {e}")

    except Exception as e:
        notes.append(f"technicals: {e}")

    return tech


def fetch_edgar_cross_check(ticker: str, notes: list):
    """Fetch reported annual revenue from EDGAR company facts as a sanity check on yfinance figures."""
    try:
        cik_data = get_json("https://www.sec.gov/files/company_tickers.json")
        cik = None
        for entry in cik_data.values():
            if str(entry.get("ticker", "")).upper() == ticker.upper():
                cik = str(entry["cik_str"]).zfill(10)
                break
        if not cik:
            notes.append(f"edgar: no CIK found for {ticker}")
            return None

        facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}

        for label in [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
        ]:
            rev_data = us_gaap.get(label)
            if not rev_data:
                continue
            usd = (rev_data.get("units") or {}).get("USD") or []
            annual = [r for r in usd if r.get("form") == "10-K" and r.get("fp") == "FY"]
            if not annual:
                continue
            annual.sort(key=lambda r: r.get("end", ""))
            # Deduplicate by fiscal year (keep the entry with the latest 'end' date per year)
            seen: dict = {}
            for r in annual:
                yr = r["end"][:4]
                if yr not in seen or r["end"] > seen[yr]["end"]:
                    seen[yr] = r
            recent = sorted(seen.values(), key=lambda r: r["end"])[-4:]
            return {
                "label": label,
                "annual_revenue_b": [
                    {"period": r["end"][:4], "value": round(r["val"] / 1e9, 3)}
                    for r in recent
                ],
            }

        notes.append("edgar: no recognized revenue label found in us-gaap facts")
        return None
    except Exception as e:
        notes.append(f"edgar_cross_check: {e}")
        return None


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: fetch_ticker_stats.py <TICKER> <output-path>", file=sys.stderr)
        sys.exit(1)

    ticker = sys.argv[1].strip().upper()
    out_path = Path(sys.argv[2]).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    notes: list = []

    try:
        import yfinance as yf  # noqa: F401 — validate import before doing network work
    except ImportError:
        log("ERROR: yfinance not installed — run: pip install yfinance")
        result = {
            "ticker": ticker,
            "fetched_at": now_iso(),
            "snapshot": {},
            "history": {},
            "price_history": {},
            "technicals": {},
            "edgar_cross_check": None,
            "notes": ["yfinance not installed — run: pip install yfinance"],
        }
        out_path.write_text(json.dumps(result, indent=2))
        sys.exit(1)

    import yfinance as yf

    log(f"fetching yfinance snapshot for {ticker}…")
    t = yf.Ticker(ticker)
    info = t.info or {}

    snapshot = fetch_snapshot(info, notes)
    log(f"fetching quarterly financials for {ticker}…")
    history = fetch_quarterly_history(t, notes)
    log(f"fetching 3-year price history for {ticker}…")
    price_history = fetch_price_history(t, notes)
    log(f"computing technicals (MAs, RSI, returns, RS vs SPY) for {ticker}…")
    technicals = fetch_technicals(t, ticker, notes)
    log(f"fetching EDGAR annual revenue cross-check for {ticker}…")
    edgar = fetch_edgar_cross_check(ticker, notes)

    result = {
        "ticker": ticker,
        "fetched_at": now_iso(),
        "snapshot": snapshot,
        "history": history,
        "price_history": price_history,
        "technicals": technicals,
        "edgar_cross_check": edgar,
        "notes": notes,
    }

    out_path.write_text(json.dumps(result, indent=2))
    for n in notes:
        log(f"NOTE {n}")
    log(f"wrote {out_path}")
    print(str(out_path))


if __name__ == "__main__":
    main()
