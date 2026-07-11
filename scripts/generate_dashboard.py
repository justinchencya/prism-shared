#!/usr/bin/env python3
"""Generate dashboard/index.html — research-to-trade performance tracker."""

import json
import os
import re
import sys
import time
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "reports"
TRACKING_DIR = ROOT / "tracking"
DASHBOARD_DIR = ROOT / "dashboard"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def load_trades() -> list:
    data = load_json(TRACKING_DIR / "trades.json", {"trades": []})
    return data.get("trades", [])


def load_portfolio() -> dict:
    data = load_json(TRACKING_DIR / "portfolio.json", {"positions": []})
    return {p["ticker"]: p for p in data.get("positions", [])}


def load_candidates() -> dict:
    data = load_json(TRACKING_DIR / "candidates.json", {"entries": []})
    return {e["ticker"]: e for e in data.get("entries", [])}


def load_journal() -> list:
    data = load_json(TRACKING_DIR / "journal.json", {"entries": []})
    return data.get("entries", [])


def load_hypotheticals() -> list:
    data = load_json(TRACKING_DIR / "hypotheticals.json", {"scenarios": []})
    return data.get("scenarios", [])


def _parse_frontmatter(text: str) -> dict:
    """Parse simple YAML frontmatter (key: value lines only)."""
    result = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip().strip('"')
    return result


def load_research_runs() -> list:
    runs = []
    if not REPORTS_DIR.exists():
        return runs
    for run_dir in sorted(REPORTS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        fr = run_dir / "final-report.md"
        if not fr.exists():
            continue
        fm = _parse_frontmatter(fr.read_text())
        slug = run_dir.name
        # derive date from directory name (YYYY-MM-DD-slug)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", slug)
        run_date = m.group(1) if m else fm.get("date", "")
        runs.append({
            "slug": slug,
            "date": run_date,
            "question": fm.get("question", slug),
            "investable": fm.get("investable", "false").lower() == "true",
        })
    return runs


def build_verdict_lookup(portfolio: dict, candidates: dict) -> dict:
    """Build {(ticker, run): verdict} from portfolio and candidates reports[]."""
    lookup = {}
    for ticker, entry in {**portfolio, **candidates}.items():
        for r in entry.get("reports", []):
            if r.get("run") and r.get("verdict"):
                lookup[(ticker, r["run"])] = r["verdict"]
    return lookup


# Tracking symbols that differ from their Yahoo Finance quote symbol.
YAHOO_SYMBOL_ALIASES = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}


def fetch_current_prices(tickers: set) -> dict:
    if not tickers:
        return {}
    prices = {}
    # Primary: Yahoo Finance chart API via requests. Uses the standard
    # proxy/CA env vars (REQUESTS_CA_BUNDLE etc.), which yfinance's curl_cffi
    # backend does not honor — behind a re-terminating proxy that TLS handshake
    # fails, so requests is the portable path here.
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        headers = {"User-Agent": "Mozilla/5.0"}
        for ticker in tickers:
            try:
                symbol = YAHOO_SYMBOL_ALIASES.get(ticker, ticker)
                url = (
                    "https://query1.finance.yahoo.com/v8/finance/chart/"
                    f"{symbol}?range=1d&interval=1d"
                )
                r = requests.get(url, headers=headers, timeout=20)
                if r.status_code != 200:
                    continue
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                if price:
                    prices[ticker] = float(price)
            except Exception:
                pass
    # Fallback for any ticker still missing: yfinance (works where curl_cffi
    # can reach Yahoo directly, e.g. off-proxy environments).
    missing = tickers - prices.keys()
    if missing:
        try:
            import yfinance as yf
        except ImportError:
            yf = None
        if yf is not None:
            for ticker in missing:
                try:
                    info = yf.Ticker(ticker).info
                    price = info.get("currentPrice") or info.get("regularMarketPrice")
                    if price:
                        prices[ticker] = float(price)
                except Exception:
                    pass
    return prices


PRICE_CACHE_PATH = TRACKING_DIR / "price-cache.json"


def _load_price_cache() -> dict:
    cache = load_json(PRICE_CACHE_PATH, {})
    return cache if isinstance(cache, dict) else {}


def _save_price_cache(cache: dict) -> None:
    try:
        PRICE_CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass  # cache is an optimization; never fail the run over it


def fetch_price_history(tickers: set, start_date: str) -> dict:
    """Daily closes per ticker from Yahoo's chart API, start_date → today.

    Returns {ticker: {"YYYY-MM-DD": close}}. Same requests-based path (and
    proxy behavior) as fetch_current_prices; any ticker that fails is simply
    absent and the P&L chart degrades to a note.

    A per-ticker cache (tracking/price-cache.json, gitignored) skips the fetch
    when the ticker was already pulled today over a wide-enough range. Cache
    hits are all-or-nothing per ticker — Yahoo returns split/dividend-adjusted
    closes, so appending to a stale series would silently mix adjustment bases;
    a stale ticker is refetched whole instead.
    """
    if not tickers or not start_date:
        return {}
    today = date.today().isoformat()
    cache = _load_price_cache()
    history = {}
    to_fetch = []
    for ticker in tickers:
        entry = cache.get(ticker)
        if (isinstance(entry, dict) and entry.get("fetched_at") == today
                and entry.get("start", "9999") <= start_date and entry.get("closes")):
            history[ticker] = entry["closes"]
        else:
            to_fetch.append(ticker)
    if not to_fetch:
        return history
    try:
        import requests
    except ImportError:
        return history
    try:
        # small buffer before the first trade so the first point has a close
        p1 = int(datetime.fromisoformat(start_date).timestamp()) - 7 * 86400
    except ValueError:
        return history
    p2 = int(time.time()) + 86400
    headers = {"User-Agent": "Mozilla/5.0"}
    for ticker in to_fetch:
        symbol = YAHOO_SYMBOL_ALIASES.get(ticker, ticker)
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?period1={p1}&period2={p2}&interval=1d"
        )
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                continue
            result = r.json()["chart"]["result"][0]
            stamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
            series = {}
            for ts, close in zip(stamps, closes):
                if close is not None:
                    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    series[day] = float(close)
            if series:
                history[ticker] = series
                cache[ticker] = {"fetched_at": today, "start": start_date,
                                 "closes": series}
        except Exception:
            pass
    _save_price_cache(cache)
    return history


# ---------------------------------------------------------------------------
# Data transformation
# ---------------------------------------------------------------------------

def build_timeline(runs: list, trades: list, journal: list | None = None, verdict_lookup: dict | None = None) -> list:
    events = []
    for r in runs:
        events.append({"type": "research", **r})
    for j in (journal or []):
        events.append({"type": "journal", **j})
    for t in trades:
        ticker = t["ticker"]
        enriched_links = []
        for link in t.get("linked_research", []):
            verdict = link.get("market_verdict")
            if verdict is None and verdict_lookup:
                verdict = verdict_lookup.get((ticker, link.get("run")))
            enriched_links.append({**link, "market_verdict": verdict})
        events.append({"type": "trade", **t, "linked_research": enriched_links})
    events.sort(key=lambda e: e["date"])
    return events


def _alignment(action: str, verdict: str | None) -> str:
    if not verdict:
        return "neutral"
    action = action.lower()
    verdict = verdict.lower()
    if action in ("buy", "add") and verdict in ("buy", "hold"):
        return "aligned"
    if action in ("sell", "trim") and verdict == "avoid":
        return "aligned"
    if action in ("buy", "add") and verdict == "avoid":
        return "misaligned"
    return "neutral"


def _days_lag(trade_date: str, research_date: str) -> int | None:
    try:
        return (date.fromisoformat(trade_date) - date.fromisoformat(research_date)).days
    except Exception:
        return None


def build_alignment(trades: list, verdict_lookup: dict | None = None, prices: dict | None = None) -> list:
    prices = prices or {}
    rows = []
    for trade in trades:
        links = trade.get("linked_research", [])
        ticker = trade["ticker"]

        # Per-trade P&L for buy/add lots with price data
        pnl_usd = None
        pnl_pct = None
        if trade["action"] in ("buy", "add"):
            shares = trade.get("shares")
            entry = trade.get("price_per_share")
            current = prices.get(ticker)
            if shares and entry and current:
                pnl_usd = round(shares * (current - entry), 2)
                pnl_pct = round((current - entry) / entry * 100, 2)

        if not links:
            rows.append({
                "trade_date": trade["date"],
                "ticker": ticker,
                "action": trade["action"],
                "amount_usd": trade.get("amount_usd"),
                "research_links": [],
                "alignment": "unlinked",
                "trade_id": trade["id"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
            })
        else:
            link_details = []
            for link in links:
                verdict = link.get("market_verdict")
                if verdict is None and verdict_lookup:
                    verdict = verdict_lookup.get((ticker, link.get("run")))
                link_details.append({
                    "run": link.get("run"),
                    "date": link.get("date"),
                    "verdict": verdict,
                    "alignment": _alignment(trade["action"], verdict),
                    "days_lag": _days_lag(trade["date"], link.get("date", "")),
                })
            alignments = [l["alignment"] for l in link_details]
            if "misaligned" in alignments:
                overall = "misaligned"
            elif "aligned" in alignments:
                overall = "aligned"
            else:
                overall = "neutral"
            rows.append({
                "trade_date": trade["date"],
                "ticker": trade["ticker"],
                "action": trade["action"],
                "amount_usd": trade.get("amount_usd"),
                "research_links": link_details,
                "alignment": overall,
                "trade_id": trade["id"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
            })
    rows.sort(key=lambda r: (r["trade_date"], r["ticker"]))
    return rows


def build_pnl_by_alignment(trades: list, alignment_rows: list, prices: dict) -> dict:
    """Unrealized P&L on buy/add lots, bucketed by each trade's research alignment.

    Each lot is judged by its own entry price vs. the current price (not pooled
    avg cost), so a trade's P&L reflects the decision it represents. Sells/trims
    are excluded — this is an open-lot unrealized view.
    """
    align_by_id = {r["trade_id"]: r["alignment"] for r in alignment_rows}
    buckets = {k: {"trades": 0, "cost": 0.0, "value": 0.0}
               for k in ("aligned", "misaligned", "neutral", "unlinked")}
    for t in trades:
        if t["action"] not in ("buy", "add"):
            continue
        shares, price = t.get("shares"), t.get("price_per_share")
        cp = prices.get(t["ticker"])
        if not (shares and price and cp):
            continue
        b = buckets[align_by_id.get(t["id"], "unlinked")]
        b["trades"] += 1
        b["cost"] += shares * price
        b["value"] += shares * cp

    def summarize(d: dict) -> dict:
        pnl = d["value"] - d["cost"]
        return {
            "trades": d["trades"],
            "cost": round(d["cost"], 2),
            "value": round(d["value"], 2),
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / d["cost"] * 100, 2) if d["cost"] else None,
        }

    result = {k: summarize(v) for k, v in buckets.items()}
    total = {"trades": sum(v["trades"] for v in buckets.values()),
             "cost": sum(v["cost"] for v in buckets.values()),
             "value": sum(v["value"] for v in buckets.values())}
    result["all"] = summarize(total)
    return result


def build_value_series(trades: list, history: dict) -> dict:
    """Daily portfolio market value vs. cumulative net invested.

    Value = shares held (cumulative buys − sells) × that day's close, summed
    across tickers. Net invested = cumulative buy cost − sell proceeds, so the
    gap between the two lines is total P&L, realized + unrealized — no lot
    matching needed at this level. A ticker whose trades lack shares/price, or
    with no price history, can't be valued; it is excluded entirely (both
    lines) and reported in "excluded" so the chart can say so.
    """
    dated = sorted((t for t in trades if t.get("date")), key=lambda t: t["date"])
    if not dated:
        return {"series": [], "excluded": []}

    def complete(t):
        return bool(t.get("shares") and t.get("price_per_share"))

    all_tickers = {t["ticker"] for t in dated}
    bad = {}
    for t in dated:
        if not complete(t):
            bad.setdefault(t["ticker"], "missing shares/price on some trades")
    for tk in all_tickers:
        if tk not in bad and tk not in history:
            bad[tk] = "no price history"
    # a ticker whose recorded sells exceed its recorded buys held shares from
    # before the trade log began — its proceeds would fake the net-invested
    # line (return of unrecorded principal reads as P&L), so drop it whole
    running = {}
    for t in dated:
        tk = t["ticker"]
        if tk in bad:
            continue
        sign = 1 if t["action"] in ("buy", "add") else -1
        running[tk] = running.get(tk, 0) + sign * t["shares"]
        if running[tk] < -1e-9:
            bad[tk] = "sells exceed recorded buys (position predates trade log)"
    usable = [t for t in dated if t["ticker"] not in bad]
    excluded = [{"ticker": k, "reason": v} for k, v in sorted(bad.items())]
    if not usable:
        return {"series": [], "excluded": excluded}

    first_date = usable[0]["date"]
    calendar = sorted({d for tk in {t["ticker"] for t in usable}
                       for d in history[tk]})
    # forward-fill closes onto the union calendar (crypto trades weekends,
    # equities don't — every ticker needs a close on every calendar day)
    filled = {}
    for tk in {t["ticker"] for t in usable}:
        last = None
        col = {}
        for d in calendar:
            last = history[tk].get(d, last)
            col[d] = last
        filled[tk] = col

    series = []
    shares_held = {}
    invested = 0.0
    idx = 0
    for d in calendar:
        if d < first_date:
            continue
        while idx < len(usable) and usable[idx]["date"] <= d:
            t = usable[idx]
            sign = 1 if t["action"] in ("buy", "add") else -1
            shares_held[t["ticker"]] = shares_held.get(t["ticker"], 0) + sign * t["shares"]
            invested += sign * t["shares"] * t["price_per_share"]
            idx += 1
        value = 0.0
        ok = True
        for tk, sh in shares_held.items():
            if sh <= 1e-9:
                continue
            close = filled[tk].get(d)
            if close is None:
                ok = False  # held before its first close (pre-IPO gap) — skip day
                break
            value += sh * close
        if ok:
            series.append({"date": d, "value": round(value, 2),
                           "invested": round(invested, 2)})
    return {"series": series, "excluded": excluded}


# ---------------------------------------------------------------------------
# What-if scenarios (tracking/hypotheticals.json, written by /what-if)
# ---------------------------------------------------------------------------

# Categorical slots for scenario lines, validated against the dark surface
# alongside the actual-portfolio blue. Green/red stay reserved for gain/loss.
SCENARIO_COLORS = ["#c98500", "#9085e9", "#d95926", "#d55181"]


def close_on_or_after(series: dict, day: str) -> float | None:
    """Execution-price proxy for a hypothetical trade dated `day`: the first
    close on or after it (a closed market can't fill), else the last close."""
    days = sorted(series)
    for d in days:
        if d >= day:
            return series[d]
    return series[days[-1]] if days else None


def scenario_needed_tickers(scenarios: list) -> set:
    """Tickers whose price history the scenarios need beyond the actual set."""
    need = set()
    for sc in scenarios:
        stype = sc.get("type")
        if stype == "benchmark" and sc.get("benchmark_ticker"):
            need.add(sc["benchmark_ticker"])
        elif stype == "standalone":
            need.update(t["ticker"] for t in sc.get("trades", []) if t.get("ticker"))
        elif stype == "substitute":
            for o in sc.get("overrides", []):
                tk = o.get("ticker") or o.get("to")
                if tk:
                    need.add(tk)
    return need


def resolve_scenario_trades(scenario: dict, actual_trades: list, history: dict) -> tuple:
    """Expand a scenario into a synthetic trade list build_value_series accepts.

    Returns (trades, warnings). A swapped buy converts the same dollars into
    the target ticker at its close on/after the trade date. A swapped sell
    sells the same *fraction* of the hypothetical position as the original
    sell took of the real one — dollar amounts aren't comparable once the two
    positions have diverged.
    """
    warnings = []
    stype = scenario.get("type")

    if stype == "standalone":
        out = []
        for i, t in enumerate(sorted(scenario.get("trades", []), key=lambda t: t.get("date", ""))):
            tk, day = t.get("ticker"), t.get("date")
            if not tk or not day:
                warnings.append(f"trade #{i + 1}: missing ticker/date — skipped")
                continue
            shares, price = t.get("shares"), t.get("price_per_share")
            if not (shares and price):
                close = close_on_or_after(history.get(tk, {}), day)
                if close is None:
                    warnings.append(f"{tk} {day}: no price history — skipped")
                    continue
                amount = t.get("amount_usd")
                if not amount:
                    warnings.append(f"{tk} {day}: no amount/shares — skipped")
                    continue
                price, shares = close, amount / close
            out.append({"ticker": tk, "date": day,
                        "action": t.get("action", "buy"),
                        "shares": shares, "price_per_share": price,
                        "amount_usd": t.get("amount_usd") or round(shares * price, 2)})
        return out, warnings

    # substitute / benchmark: derived from the actual trade log
    if stype == "benchmark":
        bench = scenario.get("benchmark_ticker")
        if not bench:
            return [], ["benchmark scenario has no benchmark_ticker"]
        def target(t):
            return bench
    elif stype == "substitute":
        by_id, by_ticker = {}, {}
        for o in scenario.get("overrides", []):
            if o.get("trade_id"):
                by_id[o["trade_id"]] = o.get("ticker") or o.get("to")
            elif o.get("from") and o.get("to"):
                by_ticker[o["from"]] = o["to"]
        def target(t):
            return by_id.get(t.get("id")) or by_ticker.get(t["ticker"]) or t["ticker"]
    else:
        return [], [f"unknown scenario type {stype!r}"]

    out = []
    orig_shares = {}  # running shares per (original, target) swap chain
    hyp_shares = {}
    dated = sorted((t for t in actual_trades if t.get("date")), key=lambda t: t["date"])
    for t in dated:
        tgt = target(t)
        if not tgt or tgt == t["ticker"]:
            out.append(t)
            continue
        label = t.get("id") or f"{t['ticker']} {t['date']}"
        if not (t.get("shares") and t.get("price_per_share")):
            warnings.append(f"{label}: missing shares/price — kept unswapped")
            out.append(t)
            continue
        close = close_on_or_after(history.get(tgt, {}), t["date"])
        if close is None:
            warnings.append(f"{tgt} {t['date']}: no price history — trade skipped")
            continue
        key = (t["ticker"], tgt)
        if t["action"] in ("buy", "add"):
            amount = t.get("amount_usd") or t["shares"] * t["price_per_share"]
            sh = amount / close
            orig_shares[key] = orig_shares.get(key, 0) + t["shares"]
            hyp_shares[key] = hyp_shares.get(key, 0) + sh
        else:
            held = orig_shares.get(key, 0)
            if held <= 1e-9 or hyp_shares.get(key, 0) <= 1e-9:
                warnings.append(f"{label}: sell precedes any swapped buy — skipped")
                continue
            frac = min(t["shares"] / held, 1.0)
            sh = frac * hyp_shares[key]
            orig_shares[key] = held - t["shares"]
            hyp_shares[key] -= sh
        out.append({"ticker": tgt, "date": t["date"], "action": t["action"],
                    "shares": sh, "price_per_share": close,
                    "amount_usd": round(sh * close, 2)})
    return out, warnings


def build_whatif_series(scenarios: list, actual_trades: list, history: dict) -> list:
    """Value series per scenario, in the shape the chart overlays expect."""
    out = []
    for sc in scenarios:
        trades, warnings = resolve_scenario_trades(sc, actual_trades, history)
        vs = build_value_series(trades, history)
        warnings += [f"{e['ticker']}: {e['reason']}" for e in vs["excluded"]]
        ci = sc.get("color_index")
        if ci is None:
            ci = len(out)
        out.append({
            "id": sc.get("id"),
            "name": sc.get("name") or sc.get("id") or "unnamed scenario",
            "type": sc.get("type"),
            # same cashflows as the actual portfolio → end values compare directly
            "comparable": sc.get("type") in ("substitute", "benchmark"),
            "color": SCENARIO_COLORS[ci % len(SCENARIO_COLORS)],
            "series": vs["series"],
            "warnings": warnings,
        })
    return out


def build_pnl_drivers(trades: list, prices: dict, alignment_rows: list) -> list:
    """Per-ticker total P&L (realized + unrealized), biggest movers first.

    total = (shares still held × current price) + sell proceeds − buy cost.
    Ticker-level netting needs no lot matching. Alignment stays a per-trade
    attribute, surfaced here only as counts; per-trade detail lives in the
    Research → Trade tab.
    """
    align_by_id = {r["trade_id"]: r for r in alignment_rows}
    out = []
    for ticker in sorted({t["ticker"] for t in trades}):
        tts = sorted((t for t in trades if t["ticker"] == ticker),
                     key=lambda t: t.get("date", ""))
        counts = {"aligned": 0, "misaligned": 0, "neutral": 0, "unlinked": 0}
        for t in tts:
            alignment = align_by_id.get(t["id"], {}).get("alignment", "unlinked")
            counts[alignment] = counts.get(alignment, 0) + 1

        pnl_usd = pnl_pct = None
        reason = None
        if any(not (t.get("shares") and t.get("price_per_share")) for t in tts):
            reason = "missing shares/price on some trades"
        else:
            cost = sum(t["shares"] * t["price_per_share"] for t in tts
                       if t["action"] in ("buy", "add"))
            proceeds = sum(t["shares"] * t["price_per_share"] for t in tts
                           if t["action"] in ("sell", "trim"))
            held = sum(t["shares"] if t["action"] in ("buy", "add") else -t["shares"]
                       for t in tts)
            current = prices.get(ticker)
            if held < -1e-9:
                # proceeds of unrecorded shares would read as pure profit
                reason = "sells exceed recorded buys (position predates trade log)"
            elif not cost:
                reason = "no recorded buys"
            elif held > 1e-9 and not current:
                reason = "current price unavailable"
            else:
                value = held * current if held > 1e-9 else 0.0
                pnl_usd = round(value + proceeds - cost, 2)
                pnl_pct = round(pnl_usd / cost * 100, 2)

        out.append({"ticker": ticker, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
                    "reason": reason, "alignment_counts": counts,
                    "n_trades": len(tts)})
    out.sort(key=lambda d: (d["pnl_usd"] is None, -(d["pnl_usd"] or 0)))
    return out


def build_per_ticker(
    portfolio: dict,
    candidates: dict,
    trades: list,
    prices: dict,
) -> dict:
    tickers = set(t["ticker"] for t in trades)
    result = {}
    for ticker in sorted(tickers):
        ticker_trades = [t for t in trades if t["ticker"] == ticker]
        holding = portfolio.get(ticker) or candidates.get(ticker) or {}
        research_history = holding.get("reports", [])
        events = holding.get("events", [])[:3]

        # P&L calculation
        pnl_pct = None
        current_price = prices.get(ticker)
        if current_price:
            buys = [t for t in ticker_trades if t["action"] in ("buy", "add") and t.get("shares") and t.get("price_per_share")]
            sells = [t for t in ticker_trades if t["action"] in ("sell", "trim") and t.get("shares") and t.get("price_per_share")]
            shares_bought = sum(t["shares"] for t in buys)
            shares_sold = sum(t["shares"] for t in sells)
            shares_held = shares_bought - shares_sold
            if shares_held > 0 and buys:
                total_cost = sum(t["shares"] * t["price_per_share"] for t in buys)
                avg_cost = total_cost / shares_bought
                current_val = shares_held * current_price
                cost_remaining = avg_cost * shares_held
                pnl_pct = round((current_val - cost_remaining) / cost_remaining * 100, 2) if cost_remaining else None

        result[ticker] = {
            "ticker": ticker,
            "trades": ticker_trades,
            "research_history": research_history,
            "events": events,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "in_portfolio": ticker in portfolio,
        }
    return result


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; font-size: 14px; }
nav { display: flex; gap: 4px; padding: 12px 16px; background: #1a1d2e;
      border-bottom: 1px solid #2d3148; position: sticky; top: 0; z-index: 10; }
nav button { padding: 6px 16px; border: 1px solid #3d4166; border-radius: 6px;
             background: transparent; color: #94a3b8; cursor: pointer; font-size: 13px; }
nav button.active { background: #3b4fd8; color: #fff; border-color: #3b4fd8; }
.view { display: none; padding: 20px 24px; max-width: 1200px; margin: 0 auto; }
.view.active { display: block; }
h2 { font-size: 18px; font-weight: 600; margin-bottom: 16px; color: #f1f5f9; }
h3 { font-size: 14px; font-weight: 600; color: #cbd5e1; margin-bottom: 8px; }

/* Timeline */
.timeline { position: relative; padding-left: 28px; }
.timeline::before { content: ''; position: absolute; left: 10px; top: 0; bottom: 0;
                    width: 2px; background: #2d3148; }
.tl-item { position: relative; margin-bottom: 12px; }
.tl-dot { position: absolute; left: -22px; top: 8px; width: 10px; height: 10px;
          border-radius: 50%; border: 2px solid #3d4166; background: #1a1d2e; }
.tl-item.research .tl-dot { background: #3b4fd8; border-color: #3b4fd8; }
.tl-item.trade .tl-dot { background: #10b981; border-color: #10b981; }
.tl-item.journal .tl-dot { background: #a78bfa; border-color: #a78bfa; }
.tl-card { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px;
           padding: 10px 14px; }
.tl-item.journal .tl-card { border-left: 3px solid #a78bfa; }
.tl-card .date { font-size: 11px; color: #64748b; margin-bottom: 4px; }
.tl-card .title { font-size: 13px; color: #e2e8f0; }
.tl-card .reflection { font-size: 13px; color: #cbd5e1; line-height: 1.5;
                       white-space: pre-wrap; font-style: italic; }
.tl-card .meta { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
.badge.buy { background: #064e3b; color: #34d399; }
.badge.sell { background: #4c0519; color: #fb7185; }
.badge.trim { background: #3b2800; color: #fb923c; }
.badge.add { background: #064e3b; color: #34d399; }
.badge.research { background: #1e3a5f; color: #60a5fa; }
.badge.journal { background: #2e1065; color: #c4b5fd; }
.badge.ticker { background: #1e293b; color: #94a3b8; }
.badge.investable { background: #312e81; color: #a5b4fc; }
.badge.aligned { background: #064e3b; color: #34d399; }
.badge.misaligned { background: #4c0519; color: #fb7185; }
.badge.neutral { background: #3b2800; color: #fbbf24; }
.badge.unlinked { background: #1e293b; color: #64748b; }
.badge.hold { background: #3b2800; color: #fbbf24; }
.badge.avoid { background: #4c0519; color: #fb7185; }

/* Summary bar */
.summary-bar { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px;
               padding: 12px 16px; margin-bottom: 16px; display: flex; gap: 24px; }
.summary-stat { text-align: center; }
.summary-stat .num { font-size: 24px; font-weight: 700; color: #f1f5f9; }
.summary-stat .label { font-size: 11px; color: #64748b; margin-top: 2px; }

/* Table */
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { padding: 8px 12px; text-align: left; font-size: 12px; color: #64748b;
     border-bottom: 1px solid #2d3148; cursor: pointer; white-space: nowrap; }
th:hover { color: #94a3b8; }
td { padding: 8px 12px; border-bottom: 1px solid #1e2538; font-size: 13px; }
tr:hover td { background: #1e2538; }
tr.aligned td:first-child { border-left: 3px solid #10b981; }
tr.misaligned td:first-child { border-left: 3px solid #fb7185; }
tr.neutral td:first-child { border-left: 3px solid #fbbf24; }
tr.unlinked td:first-child { border-left: 3px solid #334155; }

/* Ticker drilldown */
.ticker-select { padding: 8px 12px; background: #1a1d2e; border: 1px solid #3d4166;
                 border-radius: 6px; color: #e2e8f0; font-size: 14px; margin-bottom: 20px;
                 width: 200px; }
.ticker-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 700px) { .ticker-panel { grid-template-columns: 1fr; } }
.panel-card { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px; padding: 14px; }
.pnl-positive { color: #34d399; font-weight: 600; }
.pnl-negative { color: #fb7185; font-weight: 600; }
.price-row { display: flex; justify-content: space-between; margin-bottom: 8px; }
.price-label { color: #64748b; }
.event-item { padding: 6px 0; border-bottom: 1px solid #2d3148; font-size: 12px; }
.event-item:last-child { border-bottom: none; }
.event-type { font-size: 10px; color: #64748b; text-transform: uppercase; margin-bottom: 2px; }

/* P&L tab */
.kpi-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.stat-tile { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px;
             padding: 12px 16px; flex: 1; min-width: 160px; }
.stat-tile .label { font-size: 11px; color: #64748b; margin-bottom: 4px; }
.stat-tile .value { font-size: 24px; font-weight: 600; color: #f1f5f9; }
.stat-tile .delta { font-size: 12px; margin-top: 2px; }
.chart-card { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px;
              padding: 14px; margin-bottom: 16px; position: relative; }
.chart-legend { display: flex; gap: 16px; margin-bottom: 8px; font-size: 12px;
                color: #94a3b8; }
.chart-legend .key { display: inline-block; width: 14px; height: 2px;
                     vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.chart-tooltip { position: absolute; pointer-events: none; background: #232842;
                 border: 1px solid #3d4166; border-radius: 6px; padding: 8px 10px;
                 font-size: 12px; display: none; z-index: 5; min-width: 150px;
                 box-shadow: 0 4px 12px rgba(0,0,0,.4); }
.chart-tooltip .tt-date { color: #64748b; font-size: 11px; margin-bottom: 4px; }
.chart-tooltip .tt-row { display: flex; justify-content: space-between; gap: 12px;
                         padding: 1px 0; }
.chart-tooltip .tt-label { color: #94a3b8; }
.chart-tooltip .tt-val { font-weight: 600; color: #f1f5f9;
                         font-variant-numeric: tabular-nums; }
.muted-note { color: #64748b; font-size: 12px; padding: 8px 0; }
svg text { font-family: inherit; }
.axis-tick { font-size: 10px; fill: #64748b; font-variant-numeric: tabular-nums; }
.bar-label { font-size: 11px; fill: #e2e8f0; font-variant-numeric: tabular-nums; }
.bar-ticker { font-size: 11px; fill: #94a3b8; font-weight: 600; }

"""

JS = """
let pnlRendered = false;
function showTab(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="' + id + '"]').classList.add('active');
  if (id === 'view-pnl' && !pnlRendered) { renderPnlTab(); pnlRendered = true; }
}

// ---------------------------------------------------------------------------
// P&L tab
// ---------------------------------------------------------------------------
const C_VALUE = '#3987e5', C_CONTEXT = '#64748b', C_GAIN = '#199e70', C_LOSS = '#e5484d';
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmtUsd = (v, compact) => {
  const sign = v < 0 ? '-' : '';
  const a = Math.abs(v);
  if (compact && a >= 1e6) return sign + '$' + (a/1e6).toFixed(1) + 'M';
  if (compact && a >= 1e3) return sign + '$' + (a/1e3).toFixed(1) + 'K';
  return sign + '$' + a.toLocaleString(undefined, {maximumFractionDigits: 0});
};
const fmtSigned = v => (v >= 0 ? '+' : '') + fmtUsd(v, true).replace(/^-/, '-');

function niceTicks(min, max, n) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  // floor/ceil so the tick range fully covers the data — ticks double as the domain
  const ticks = [];
  for (let v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + step * 1e-9; v += step) ticks.push(v);
  return ticks;
}

function renderPnlTab() {
  renderValueChart();
  renderDriversChart();
}

function renderValueChart() {
  const card = document.getElementById('pnl-value-card');
  const s = DATA.pnl_series.series || [];
  const excluded = DATA.pnl_series.excluded || [];
  const hasActual = s.length >= 2;
  const scen = (DATA.whatif || []).filter(w => w.series && w.series.length >= 2);
  const scenWarn = scen.flatMap(w => (w.warnings || []).map(m => esc(w.name) + ': ' + esc(m)));
  let note = excluded.length
    ? `<div class="muted-note">Excluded: ${excluded.map(e => esc(e.ticker) + ' (' + esc(e.reason) + ')').join(', ')}</div>` : '';
  if (scenWarn.length) note += `<div class="muted-note">What-if notes: ${scenWarn.join(' · ')}</div>`;
  if (!hasActual && !scen.length) {
    card.insertAdjacentHTML('beforeend',
      '<div class="muted-note">Not enough data for a value series — needs trades with shares + price and reachable price history.</div>' + note);
    return;
  }
  // x-domain: the actual portfolio's calendar; hypotheticals-only fallback
  const base = hasActual ? s : scen.reduce((a, w) => (w.series.length > a.length ? w.series : a), scen[0].series);
  const idx = {};
  base.forEach((d, i) => { idx[d.date] = i; });
  // scenario lines drawn only where they share the x-domain calendar
  const scenView = scen
    .map(w => ({...w, pts: w.series.filter(p => idx[p.date] != null)}))
    .filter(v => v.pts.length >= 2);

  const W = Math.max(card.clientWidth - 28, 320), H = 260;
  const M = {top: 14, right: 74, bottom: 26, left: 56};
  const pw = W - M.left - M.right, ph = H - M.top - M.bottom;
  const vals = [];
  if (hasActual) s.forEach(d => vals.push(d.value, d.invested));
  scenView.forEach(v => v.pts.forEach(p => vals.push(p.value)));
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals), 5);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const x = i => M.left + (base.length === 1 ? pw / 2 : i / (base.length - 1) * pw);
  const y = v => M.top + ph - (v - y0) / (y1 - y0) * ph;

  const grid = ticks.map(t =>
    `<line x1="${M.left}" y1="${y(t)}" x2="${M.left + pw}" y2="${y(t)}" stroke="#262b40" stroke-width="1"/>
     <text class="axis-tick" x="${M.left - 8}" y="${y(t) + 3}" text-anchor="end">${fmtUsd(t, true)}</text>`).join('');
  const nx = Math.min(5, base.length);
  const xlabels = Array.from({length: nx}, (_, k) => {
    const i = Math.round(k * (base.length - 1) / Math.max(nx - 1, 1));
    return `<text class="axis-tick" x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(base[i].date.slice(5))}</text>`;
  }).join('');

  const path = key => s.map((d, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(d[key]).toFixed(1)).join(' ');
  const last = hasActual ? s[s.length - 1] : null;
  let actualSvg = '', endLabels = '';
  if (hasActual) {
    const area = path('value') + ` L ${x(s.length - 1).toFixed(1)} ${M.top + ph} L ${M.left} ${M.top + ph} Z`;
    actualSvg = `
      <path d="${area}" fill="${C_VALUE}" opacity="0.1"/>
      <path d="${path('invested')}" fill="none" stroke="${C_CONTEXT}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <path d="${path('value')}" fill="none" stroke="${C_VALUE}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${x(s.length-1)}" cy="${y(last.invested)}" r="4" fill="${C_CONTEXT}" stroke="#1a1d2e" stroke-width="2"/>
      <circle cx="${x(s.length-1)}" cy="${y(last.value)}" r="4" fill="${C_VALUE}" stroke="#1a1d2e" stroke-width="2"/>`;
    // direct end-labels — only when the two endpoints separate enough to read
    const sep = Math.abs(y(last.value) - y(last.invested)) >= 14;
    const endLabel = (v, txt) => sep
      ? `<text x="${x(s.length-1) + 8}" y="${y(v) + 3}" font-size="11" fill="#e2e8f0">${txt}</text>` : '';
    endLabels = endLabel(last.value, fmtUsd(last.value, true)) + endLabel(last.invested, fmtUsd(last.invested, true));
  }
  // hypothetical lines: dashed (shape distinguishes them from the actual, not
  // just hue), each with an end dot at its last in-domain point
  const scenSvg = scenView.map(v => {
    const p = v.pts.map((pt, k) => (k ? 'L' : 'M') + x(idx[pt.date]).toFixed(1) + ' ' + y(pt.value).toFixed(1)).join(' ');
    const lp = v.pts[v.pts.length - 1];
    return `<path d="${p}" fill="none" stroke="${v.color}" stroke-width="2" stroke-dasharray="5 3" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${x(idx[lp.date])}" cy="${y(lp.value)}" r="4" fill="${v.color}" stroke="#1a1d2e" stroke-width="2"/>`;
  }).join('');

  const legend = [
    ...(hasActual ? [
      `<span><span class="key" style="background:${C_VALUE}"></span>Portfolio value</span>`,
      `<span><span class="key" style="background:${C_CONTEXT}"></span>Net invested</span>`] : []),
    ...scenView.map(v => `<span><span class="key" style="background:repeating-linear-gradient(90deg,${v.color} 0 5px,transparent 5px 8px)"></span>${esc(v.name)}</span>`),
  ].join('');

  // per-scenario summary: end value, own P&L, and — when the cashflows match
  // the actual portfolio's (substitute/benchmark) — the P&L delta vs actual
  // (P&L, not end value: sale proceeds differ between the two, and the
  // invested line already nets them out on both sides)
  const summary = scenView.map(v => {
    const lp = v.series[v.series.length - 1];
    const pnl = lp.value - lp.invested;
    const vsActual = (v.comparable && last) ? ` · vs actual P&amp;L ${fmtSigned(pnl - (last.value - last.invested))}` : '';
    return `<div class="muted-note"><span class="key" style="background:${v.color};display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:6px"></span>${esc(v.name)}: ${fmtUsd(lp.value, true)} (P&amp;L ${fmtSigned(pnl)}${vsActual})</div>`;
  }).join('');

  card.insertAdjacentHTML('beforeend', `
    <div class="chart-legend">${legend}</div>
    <svg width="${W}" height="${H}" role="img" aria-label="Portfolio value vs net invested over time, with what-if scenario overlays">
      ${grid}${xlabels}
      ${actualSvg}
      ${scenSvg}
      ${endLabels}
      <line id="pnl-crosshair" y1="${M.top}" y2="${M.top + ph}" stroke="#3d4166" stroke-width="1" visibility="hidden"/>
      <rect x="${M.left}" y="${M.top}" width="${pw}" height="${ph}" fill="transparent" id="pnl-hover"/>
    </svg>
    <div class="chart-tooltip" id="pnl-tt"></div>` + summary + note);

  const scenMaps = scenView.map(v => {
    const m = {};
    v.pts.forEach(p => { m[p.date] = p.value; });
    return m;
  });
  const ttKey = c => `<span class="key" style="background:${c};display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:5px"></span>`;
  const svgEl = card.querySelector('svg');
  const hover = document.getElementById('pnl-hover');
  const cross = document.getElementById('pnl-crosshair');
  const tt = document.getElementById('pnl-tt');
  hover.addEventListener('pointermove', ev => {
    const rect = svgEl.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const i = Math.max(0, Math.min(base.length - 1, Math.round((px - M.left) / pw * (base.length - 1))));
    const d = base[i];
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
    cross.setAttribute('visibility', 'visible');
    let rows = '';
    if (hasActual) {
      const a = s[i];
      const pnl = a.value - a.invested;
      rows += `<div class="tt-row"><span class="tt-label">${ttKey(C_VALUE)}Value</span><span class="tt-val">${fmtUsd(a.value)}</span></div>
        <div class="tt-row"><span class="tt-label">${ttKey(C_CONTEXT)}Invested</span><span class="tt-val">${fmtUsd(a.invested)}</span></div>
        <div class="tt-row"><span class="tt-label">P&amp;L</span><span class="tt-val ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${(pnl >= 0 ? '+' : '') + fmtUsd(pnl)}</span></div>`;
    }
    scenView.forEach((v, k) => {
      const val = scenMaps[k][d.date];
      if (val != null) rows += `<div class="tt-row"><span class="tt-label">${ttKey(v.color)}${esc(v.name)}</span><span class="tt-val">${fmtUsd(val)}</span></div>`;
    });
    tt.innerHTML = `<div class="tt-date">${esc(d.date)}</div>` + rows;
    tt.style.display = 'block';
    const cardRect = card.getBoundingClientRect();
    const ttx = Math.min(ev.clientX - cardRect.left + 14, card.clientWidth - tt.offsetWidth - 8);
    tt.style.left = ttx + 'px';
    tt.style.top = (ev.clientY - cardRect.top + 14) + 'px';
  });
  hover.addEventListener('pointerleave', () => {
    cross.setAttribute('visibility', 'hidden'); tt.style.display = 'none';
  });

  // KPI tiles from the latest actual point
  if (!hasActual) return;
  const pnl = last.value - last.invested;
  const pct = last.invested ? pnl / last.invested * 100 : null;
  document.getElementById('kpi-value').textContent = fmtUsd(last.value);
  document.getElementById('kpi-invested').textContent = fmtUsd(last.invested);
  const kp = document.getElementById('kpi-pnl');
  kp.textContent = (pnl >= 0 ? '+' : '') + fmtUsd(pnl);
  kp.className = 'value ' + (pnl >= 0 ? 'pnl-positive' : 'pnl-negative');
  if (pct != null) {
    const kd = document.getElementById('kpi-pnl-delta');
    kd.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '% on net invested';
    kd.className = 'delta ' + (pct >= 0 ? 'pnl-positive' : 'pnl-negative');
  }
}

function renderDriversChart() {
  const card = document.getElementById('pnl-drivers-card');
  const all = DATA.pnl_drivers || [];
  const rows = all.filter(d => d.pnl_usd != null);
  const skipped = all.filter(d => d.pnl_usd == null);
  const note = skipped.length
    ? `<div class="muted-note">No P&amp;L computed for: ${skipped.map(d => esc(d.ticker) + ' (' + esc(d.reason || '?') + ')').join(', ')}</div>` : '';
  if (!rows.length) {
    card.insertAdjacentHTML('beforeend', '<div class="muted-note">No tickers with computable P&amp;L yet.</div>' + note);
    return;
  }
  const W = Math.max(card.clientWidth - 28, 320);
  const band = 30, barH = 18;
  const vmin = Math.min(0, ...rows.map(d => d.pnl_usd));
  const vmax = Math.max(0, ...rows.map(d => d.pnl_usd));
  // extra left padding so a negative bar's outside-end label clears the ticker gutter
  const M = {top: 6, right: 72, bottom: 22, left: 64 + (vmin < 0 ? 64 : 0)};
  const H = M.top + rows.length * band + M.bottom;
  const pw = W - M.left - M.right;
  const x = v => M.left + (v - vmin) / ((vmax - vmin) || 1) * pw;
  const zero = x(0);

  const bars = rows.map((d, i) => {
    const yTop = M.top + i * band + (band - barH) / 2;
    const pos = d.pnl_usd >= 0;
    const x0 = pos ? zero : x(d.pnl_usd), x1 = pos ? x(d.pnl_usd) : zero;
    const w = Math.max(x1 - x0, 1);
    const r = Math.min(4, w);
    // 4px rounded data-end, square at the zero baseline
    const path = pos
      ? `M ${x0} ${yTop} L ${x1 - r} ${yTop} Q ${x1} ${yTop} ${x1} ${yTop + r} L ${x1} ${yTop + barH - r} Q ${x1} ${yTop + barH} ${x1 - r} ${yTop + barH} L ${x0} ${yTop + barH} Z`
      : `M ${x1} ${yTop} L ${x0 + r} ${yTop} Q ${x0} ${yTop} ${x0} ${yTop + r} L ${x0} ${yTop + barH - r} Q ${x0} ${yTop + barH} ${x0 + r} ${yTop + barH} L ${x1} ${yTop + barH} Z`;
    const labelX = pos ? x1 + 6 : x0 - 6;
    const anchor = pos ? 'start' : 'end';
    return `<g class="driver-row" data-i="${i}">
      <text class="bar-ticker" x="56" y="${yTop + barH / 2 + 4}" text-anchor="end">${esc(d.ticker)}</text>
      <path d="${path}" fill="${pos ? C_GAIN : C_LOSS}"/>
      <text class="bar-label" x="${labelX}" y="${yTop + barH / 2 + 4}" text-anchor="${anchor}">${fmtSigned(d.pnl_usd)}</text>
      <rect x="0" y="${M.top + i * band}" width="${W}" height="${band}" fill="transparent"/>
    </g>`;
  }).join('');

  card.insertAdjacentHTML('beforeend', `
    <svg width="${W}" height="${H}" role="img" aria-label="P&L by ticker">
      <line x1="${zero}" y1="${M.top}" x2="${zero}" y2="${H - M.bottom}" stroke="#3d4166" stroke-width="1"/>
      <text class="axis-tick" x="${zero}" y="${H - 6}" text-anchor="middle">$0</text>
      ${bars}
    </svg>
    <div class="chart-tooltip" id="drivers-tt"></div>` + note);

  const tt = document.getElementById('drivers-tt');
  card.querySelectorAll('.driver-row').forEach(g => {
    const d = rows[+g.dataset.i];
    g.addEventListener('pointermove', ev => {
      g.querySelector('path').setAttribute('opacity', '0.8');
      const c = d.alignment_counts;
      const parts = ['aligned', 'misaligned', 'neutral', 'unlinked']
        .filter(k => c[k]).map(k => c[k] + ' ' + k).join(' · ');
      tt.innerHTML = `<div class="tt-date">${esc(d.ticker)} — ${d.n_trades} trade${d.n_trades > 1 ? 's' : ''}</div>
        <div class="tt-row"><span class="tt-label">Total P&amp;L</span><span class="tt-val ${d.pnl_usd >= 0 ? 'pnl-positive' : 'pnl-negative'}">${(d.pnl_usd >= 0 ? '+' : '') + fmtUsd(d.pnl_usd)}${d.pnl_pct != null ? ' (' + (d.pnl_pct >= 0 ? '+' : '') + d.pnl_pct + '%)' : ''}</span></div>
        <div class="tt-row"><span class="tt-label">Alignment</span><span class="tt-val" style="font-weight:400">${esc(parts) || '—'}</span></div>`;
      tt.style.display = 'block';
      const cardRect = card.getBoundingClientRect();
      const ttx = Math.min(ev.clientX - cardRect.left + 14, card.clientWidth - tt.offsetWidth - 8);
      tt.style.left = ttx + 'px';
      tt.style.top = (ev.clientY - cardRect.top + 14) + 'px';
    });
    g.addEventListener('pointerleave', () => {
      g.querySelector('path').removeAttribute('opacity');
      tt.style.display = 'none';
    });
  });
}


// Alignment table sort — default: trade_date asc (col 0), then ticker asc (col 1)
let sortCol = 0, sortAsc = true;
function sortTable(col) {
  if (sortCol === col) sortAsc = !sortAsc; else { sortCol = col; sortAsc = true; }
  const keys = ['trade_date','ticker','action','amount_usd','alignment','pnl_pct'];
  const rows = DATA.alignment_rows.slice().sort((a, b) => {
    const va = a[keys[col]] ?? '', vb = b[keys[col]] ?? '';
    if (va === vb && col === 0) { return a.ticker < b.ticker ? -1 : 1; }
    return sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1);
  });
  renderAlignmentRows(rows);
}

function renderAlignmentRows(rows) {
  const tbody = document.getElementById('align-tbody');
  tbody.innerHTML = rows.map(r => {
    const researchHtml = r.research_links.length === 0
      ? '—'
      : r.research_links.map(l => {
          const slug = l.run ? l.run.replace(/^\\d{4}-\\d{2}-\\d{2}-/, '') : '?';
          const lag = l.days_lag != null ? ` <span style="color:#64748b">${l.days_lag}d</span>` : '';
          const verdict = l.verdict ? ` <span class="badge ${l.verdict.toLowerCase()}" style="font-size:10px">${l.verdict}</span>` : '';
          return `<div style="white-space:nowrap" title="${l.run||''}">${slug}${verdict}${lag}</div>`;
        }).join('');
    let pnlHtml = '—';
    if (r.pnl_pct != null) {
      const pos = r.pnl_pct >= 0;
      const cls = pos ? 'pnl-positive' : 'pnl-negative';
      const sign = pos ? '+' : '';
      const usd = r.pnl_usd != null ? ` <span style="color:#64748b;font-size:11px">(${r.pnl_usd >= 0 ? '+' : '-'}$${Math.abs(r.pnl_usd).toLocaleString(undefined,{maximumFractionDigits:0})})</span>` : '';
      pnlHtml = `<span class="${cls}">${sign}${r.pnl_pct}%</span>${usd}`;
    }
    return `
    <tr class="${r.alignment}">
      <td>${r.trade_date}</td>
      <td><b>${r.ticker}</b></td>
      <td><span class="badge ${r.action}">${r.action}</span></td>
      <td>${r.amount_usd != null ? '$' + r.amount_usd.toLocaleString() : '—'}</td>
      <td style="max-width:260px">${researchHtml}</td>
      <td><span class="badge ${r.alignment}">${r.alignment}</span></td>
      <td style="white-space:nowrap">${pnlHtml}</td>
    </tr>`;
  }).join('');
}

function renderTimeline() {
  const container = document.getElementById('timeline-container');
  container.innerHTML = DATA.timeline.map(e => {
    if (e.type === 'research') {
      const inv = e.investable ? '<span class="badge investable">investable</span>' : '';
      return `<div class="tl-item research"><div class="tl-dot"></div>
        <div class="tl-card">
          <div class="date">${e.date} &mdash; research</div>
          <div class="title">${e.question}</div>
          <div class="meta"><span class="badge research">${e.slug.replace(/^\\d{4}-\\d{2}-\\d{2}-/, '')}</span>${inv}</div>
        </div></div>`;
    } else if (e.type === 'journal') {
      const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const runBadges = (e.linked_research||[]).map(l =>
        `<span class="badge research" title="${l.run}">${l.run.replace(/^\\d{4}-\\d{2}-\\d{2}-/, '')}</span>`).join(' ');
      const tickerBadges = (e.linked_tickers||[]).map(t =>
        `<span class="badge ticker">${t}</span>`).join(' ');
      const links = (runBadges || tickerBadges)
        ? `<div class="meta"><span class="badge journal">journal</span>${tickerBadges}${runBadges}</div>`
        : `<div class="meta"><span class="badge journal">journal</span></div>`;
      return `<div class="tl-item journal"><div class="tl-dot"></div>
        <div class="tl-card">
          <div class="date">${e.date} &mdash; journal</div>
          <div class="reflection">${esc(e.text)}</div>
          ${links}
        </div></div>`;
    } else {
      const links = (e.linked_research||[]).map(l => {
        const v = l.market_verdict;
        const vBadge = v ? ` <span class="badge ${v.toLowerCase()}" style="font-size:10px">${v}</span>` : ' <span style="color:#64748b;font-size:10px">?</span>';
        return `<span class="badge research" title="${l.run}">${l.run.replace(/^\\d{4}-\\d{2}-\\d{2}-/, '')}</span>${vBadge}`;
      }).join(' ');
      const price = e.price_per_share ? ` @ $${e.price_per_share}` : '';
      return `<div class="tl-item trade"><div class="tl-dot"></div>
        <div class="tl-card">
          <div class="date">${e.date} &mdash; trade</div>
          <div class="title"><b>${e.ticker}</b> &mdash; $${(e.amount_usd||0).toLocaleString()}${price}</div>
          <div class="meta"><span class="badge ${e.action}">${e.action}</span>${links}</div>
        </div></div>`;
    }
  }).join('');
}

function renderTicker(ticker) {
  if (!ticker || !DATA.per_ticker[ticker]) { document.getElementById('ticker-panel').innerHTML = ''; return; }
  const d = DATA.per_ticker[ticker];
  const priceBlock = d.current_price ? `
    <div class="price-row"><span class="price-label">Current price</span><span>$${d.current_price.toLocaleString()}</span></div>
    ${d.pnl_pct != null ? `<div class="price-row"><span class="price-label">Unrealized P&L</span><span class="${d.pnl_pct >= 0 ? 'pnl-positive' : 'pnl-negative'}">${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct}%</span></div>` : ''}
  ` : '<div style="color:#64748b;font-size:12px">Price unavailable (run with yfinance installed)</div>';

  const tradesHtml = d.trades.map(t =>
    `<tr><td>${t.date}</td><td><span class="badge ${t.action}">${t.action}</span></td>
     <td>$${(t.amount_usd||0).toLocaleString()}</td>
     <td>${t.shares != null ? t.shares : '—'}</td>
     <td>${t.price_per_share != null ? '$' + t.price_per_share : '—'}</td></tr>`).join('');

  const researchHtml = d.research_history.map(r =>
    `<tr><td>${r.date}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.run}">${r.run.replace(/^\\d{4}-\\d{2}-\\d{2}-/, '')}</td>
     <td><span class="badge ${(r.verdict||'').toLowerCase()}">${r.verdict||'—'}</span></td></tr>`).join('');

  const eventsHtml = d.events.map(ev =>
    `<div class="event-item"><div class="event-type">${ev.type||''}</div><div>${ev.condition||''}</div></div>`).join('') || '<div style="color:#64748b;font-size:12px">No active events</div>';

  document.getElementById('ticker-panel').innerHTML = `
    <div class="ticker-panel">
      <div>
        <div class="panel-card" style="margin-bottom:12px">
          <h3>Position</h3>
          ${priceBlock}
          <div style="margin-top:8px;color:#64748b;font-size:12px">${d.in_portfolio ? 'In portfolio' : 'Not currently held'}</div>
        </div>
        <div class="panel-card">
          <h3>Trade History</h3>
          <div class="tbl-wrap"><table>
            <thead><tr><th>Date</th><th>Action</th><th>Amount</th><th>Shares</th><th>Price</th></tr></thead>
            <tbody>${tradesHtml}</tbody>
          </table></div>
        </div>
      </div>
      <div>
        <div class="panel-card" style="margin-bottom:12px">
          <h3>Research History</h3>
          <div class="tbl-wrap"><table>
            <thead><tr><th>Date</th><th>Run</th><th>Verdict</th></tr></thead>
            <tbody>${researchHtml || '<tr><td colspan="3" style="color:#64748b">No research history</td></tr>'}</tbody>
          </table></div>
        </div>
        <div class="panel-card">
          <h3>Active Events / Falsifiers</h3>
          ${eventsHtml}
        </div>
      </div>
    </div>`;
}


document.addEventListener('DOMContentLoaded', () => {
  renderTimeline();
  renderAlignmentRows(DATA.alignment_rows);

  // Summary bar
  const total = DATA.alignment_rows.length;
  const linked = DATA.alignment_rows.filter(r => r.research_links && r.research_links.length > 0).length;
  const aligned = DATA.alignment_rows.filter(r => r.alignment === 'aligned').length;
  const alignedPct = linked ? Math.round(aligned / linked * 100) : 0;
  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-linked').textContent = linked + ' / ' + total;
  document.getElementById('stat-aligned').textContent = alignedPct + '%';

  // Total P&L across all buy/add lots with price data
  const pnlRows = DATA.alignment_rows.filter(r => r.pnl_usd != null);
  if (pnlRows.length > 0) {
    const totalPnlUsd = pnlRows.reduce((s, r) => s + r.pnl_usd, 0);
    // Weighted average P&L%: sum(pnl_usd) / sum(cost) — derive cost from amount_usd
    // Use pnl_usd and pnl_pct to back-calculate cost per lot
    let totalCost = 0;
    pnlRows.forEach(r => {
      if (r.pnl_pct != null && r.pnl_pct !== 0) {
        totalCost += r.pnl_usd / (r.pnl_pct / 100);
      } else if (r.amount_usd) {
        totalCost += r.amount_usd;
      }
    });
    const totalPnlPct = totalCost ? totalPnlUsd / totalCost * 100 : null;
    const usdSign = totalPnlUsd >= 0 ? '+' : '-';
    const cls = totalPnlUsd >= 0 ? 'pnl-positive' : 'pnl-negative';
    const usdEl = document.getElementById('stat-pnl-usd');
    const pctEl = document.getElementById('stat-pnl-pct');
    usdEl.textContent = usdSign + '$' + Math.abs(totalPnlUsd).toLocaleString(undefined, {maximumFractionDigits: 0});
    usdEl.className = 'num ' + cls;
    if (totalPnlPct != null) {
      const pctSign = totalPnlPct >= 0 ? '+' : '';
      pctEl.textContent = pctSign + totalPnlPct.toFixed(1) + '%';
      pctEl.className = 'num ' + cls;
    }
  }

  // Ticker dropdown
  const sel = document.getElementById('ticker-select');
  Object.keys(DATA.per_ticker).forEach(t => {
    const opt = document.createElement('option');
    opt.value = t; opt.textContent = t;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', () => renderTicker(sel.value));
  if (sel.options.length > 1) { sel.selectedIndex = 1; renderTicker(sel.value); }
});
"""


def render_html(timeline: list, alignment_rows: list, per_ticker: dict,
                pnl_series: dict, pnl_drivers: list, whatif: list) -> str:
    data_json = json.dumps({
        "timeline": timeline,
        "alignment_rows": alignment_rows,
        "per_ticker": per_ticker,
        "pnl_series": pnl_series,
        "pnl_drivers": pnl_drivers,
        "whatif": whatif,
    }, default=str)

    total = len(alignment_rows)
    linked = sum(1 for r in alignment_rows if r["alignment"] != "unlinked")
    aligned = sum(1 for r in alignment_rows if r["alignment"] == "aligned")
    aligned_pct = f"{round(aligned / linked * 100)}%" if linked else "—"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prism Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<nav>
  <span style="color:#64748b;font-size:13px;margin-right:8px;align-self:center">Prism</span>
  <button class="active" data-tab="view-timeline" onclick="showTab('view-timeline')">Timeline</button>
  <button data-tab="view-alignment" onclick="showTab('view-alignment')">Research → Trade</button>
  <button data-tab="view-pnl" onclick="showTab('view-pnl')">P&amp;L</button>
  <button data-tab="view-ticker" onclick="showTab('view-ticker')">Per Ticker</button>
  <span style="margin-left:auto;color:#334155;font-size:11px;align-self:center">Generated {generated}</span>
</nav>

<div id="view-timeline" class="view active">
  <h2>Research & Trade Timeline</h2>
  <div class="timeline" id="timeline-container"></div>
</div>

<div id="view-alignment" class="view">
  <h2>Research → Trade Alignment</h2>
  <div class="summary-bar">
    <div class="summary-stat"><div class="num" id="stat-total">{total}</div><div class="label">Trades</div></div>
    <div class="summary-stat"><div class="num" id="stat-linked">{linked} / {total}</div><div class="label">With research linked</div></div>
    <div class="summary-stat"><div class="num" id="stat-aligned">{aligned_pct}</div><div class="label">Aligned with verdict</div></div>
    <div class="summary-stat"><div class="num" id="stat-pnl-usd">—</div><div class="label">Total unrealized P&amp;L</div></div>
    <div class="summary-stat"><div class="num" id="stat-pnl-pct">—</div><div class="label">Total P&amp;L %</div></div>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead>
        <tr>
          <th onclick="sortTable(0)">Trade Date</th>
          <th onclick="sortTable(1)">Ticker</th>
          <th onclick="sortTable(2)">Action</th>
          <th onclick="sortTable(3)">Amount</th>
          <th>Research (verdict · lag)</th>
          <th onclick="sortTable(4)">Alignment</th>
          <th onclick="sortTable(5)">P&amp;L</th>
        </tr>
      </thead>
      <tbody id="align-tbody"></tbody>
    </table>
  </div>
</div>

<div id="view-pnl" class="view">
  <h2>P&amp;L</h2>
  <div class="kpi-row">
    <div class="stat-tile"><div class="label">Portfolio value</div><div class="value" id="kpi-value">—</div></div>
    <div class="stat-tile"><div class="label">Net invested</div><div class="value" id="kpi-invested">—</div></div>
    <div class="stat-tile"><div class="label">Total P&amp;L</div><div class="value" id="kpi-pnl">—</div><div class="delta" id="kpi-pnl-delta"></div></div>
  </div>
  <div class="chart-card" id="pnl-value-card">
    <h3>Portfolio value over time</h3>
  </div>
  <div class="chart-card" id="pnl-drivers-card">
    <h3>P&amp;L drivers by ticker</h3>
    <div class="muted-note">Per-trade detail (alignment, lot P&amp;L) lives in the Research → Trade tab.</div>
  </div>
</div>

<div id="view-ticker" class="view">
  <h2>Per-Ticker Drilldown</h2>
  <select class="ticker-select" id="ticker-select">
    <option value="">Select ticker…</option>
  </select>
  <div id="ticker-panel"></div>
</div>

<script>const DATA = {data_json};</script>
<script>{JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_whatif_preview(whatif: list, pnl_series: dict) -> None:
    actual_last = pnl_series["series"][-1] if pnl_series["series"] else None
    for w in whatif:
        print(f"\n{w['name']} ({w['id']})")
        for warn in w["warnings"]:
            print(f"  ! {warn}")
        if not w["series"]:
            print("  no computable value series")
            continue
        last = w["series"][-1]
        pnl = last["value"] - last["invested"]
        print(f"  {w['series'][0]['date']} → {last['date']}: "
              f"value ${last['value']:,.0f} | invested ${last['invested']:,.0f} | "
              f"P&L {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}")
        if actual_last and w["comparable"]:
            # compare P&L, not end value — after a sell the proceeds sit in
            # cash, and the invested line already nets them out on both sides
            actual_pnl = actual_last["value"] - actual_last["invested"]
            d = pnl - actual_pnl
            print(f"  vs actual P&L ({'+' if actual_pnl >= 0 else '-'}${abs(actual_pnl):,.0f}): "
                  f"{'+' if d >= 0 else '-'}${abs(d):,.0f}")
    print()


def main():
    open_browser = "--open" in sys.argv
    preview = None
    if "--whatif-preview" in sys.argv:
        i = sys.argv.index("--whatif-preview")
        if i + 1 >= len(sys.argv):
            print("usage: generate_dashboard.py --whatif-preview <scenario id or name>")
            sys.exit(2)
        preview = sys.argv[i + 1]

    print("Loading data…")
    trades = load_trades()
    portfolio = load_portfolio()
    candidates = load_candidates()
    runs = load_research_runs()
    journal = load_journal()
    hypotheticals = load_hypotheticals()

    if preview:
        # preview targets one scenario, active or not — no HTML is written
        scenarios = [s for s in hypotheticals
                     if s.get("id") == preview
                     or preview.lower() in (s.get("name") or "").lower()]
        if not scenarios:
            print(f"No scenario matching {preview!r} in tracking/hypotheticals.json")
            sys.exit(1)
    else:
        scenarios = [s for s in hypotheticals if s.get("status", "active") == "active"]

    # Daily price history for the P&L value chart (only fully-priced tickers),
    # plus whatever the active scenarios need on top of the actual set
    first_trade_date = min((t["date"] for t in trades if t.get("date")), default=None)
    history_tickers = {t["ticker"] for t in trades if t.get("shares") and t.get("price_per_share")}
    scen_starts = [t["date"] for sc in scenarios if sc.get("type") == "standalone"
                   for t in sc.get("trades", []) if t.get("date")]
    hist_start = min([d for d in [first_trade_date] + scen_starts if d], default=None)
    hist_tickers = history_tickers | scenario_needed_tickers(scenarios)
    if hist_tickers and hist_start:
        print(f"Fetching price history since {hist_start} for: {', '.join(sorted(hist_tickers))}")
    history = fetch_price_history(hist_tickers, hist_start) if hist_start else {}

    pnl_series = build_value_series(trades, history)
    whatif = build_whatif_series(scenarios, trades, history)

    if preview:
        print_whatif_preview(whatif, pnl_series)
        return

    # Fetch current prices for tickers with entry price data (enables P&L)
    priced_tickers = {t["ticker"] for t in trades if t.get("price_per_share")}
    if priced_tickers:
        print(f"Fetching current prices for: {', '.join(sorted(priced_tickers))}")
    prices = fetch_current_prices(priced_tickers)

    verdict_lookup = build_verdict_lookup(portfolio, candidates)
    timeline = build_timeline(runs, trades, journal, verdict_lookup)
    alignment = build_alignment(trades, verdict_lookup, prices)
    per_ticker = build_per_ticker(portfolio, candidates, trades, prices)
    pnl_drivers = build_pnl_drivers(trades, prices, alignment)

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    out = DASHBOARD_DIR / "index.html"
    out.write_text(render_html(timeline, alignment, per_ticker, pnl_series, pnl_drivers, whatif))

    print(f"Dashboard written to {out}")
    print(f"  {len(runs)} research runs  |  {len(trades)} trades  |  {len(journal)} journal entries  |  "
          f"{len(per_ticker)} tickers  |  {len(whatif)} active what-if scenarios")

    if open_browser:
        webbrowser.open(out.as_uri())
    else:
        print(f"Open with: open {out}")


if __name__ == "__main__":
    main()
