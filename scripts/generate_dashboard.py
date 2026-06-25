#!/usr/bin/env python3
"""Generate dashboard/index.html — research-to-trade performance tracker."""

import json
import os
import re
import sys
import webbrowser
from datetime import date, datetime
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


def fetch_current_prices(tickers: set) -> dict:
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}
    prices = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price:
                prices[ticker] = float(price)
        except Exception:
            pass
    return prices


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

"""

JS = """
function showTab(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="' + id + '"]').classList.add('active');
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
      const usd = r.pnl_usd != null ? ` <span style="color:#64748b;font-size:11px">(${r.pnl_usd >= 0 ? '+' : ''}$${Math.abs(r.pnl_usd).toLocaleString(undefined,{maximumFractionDigits:0})})</span>` : '';
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
    const usdSign = totalPnlUsd >= 0 ? '+' : '';
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


def render_html(timeline: list, alignment_rows: list, per_ticker: dict) -> str:
    data_json = json.dumps({
        "timeline": timeline,
        "alignment_rows": alignment_rows,
        "per_ticker": per_ticker,
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

def main():
    open_browser = "--open" in sys.argv

    print("Loading data…")
    trades = load_trades()
    portfolio = load_portfolio()
    candidates = load_candidates()
    runs = load_research_runs()
    journal = load_journal()

    # Fetch current prices for tickers with entry price data (enables P&L)
    priced_tickers = {t["ticker"] for t in trades if t.get("price_per_share")}
    if priced_tickers:
        print(f"Fetching current prices for: {', '.join(sorted(priced_tickers))}")
    prices = fetch_current_prices(priced_tickers)

    verdict_lookup = build_verdict_lookup(portfolio, candidates)
    timeline = build_timeline(runs, trades, journal, verdict_lookup)
    alignment = build_alignment(trades, verdict_lookup, prices)
    per_ticker = build_per_ticker(portfolio, candidates, trades, prices)

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    out = DASHBOARD_DIR / "index.html"
    out.write_text(render_html(timeline, alignment, per_ticker))

    print(f"Dashboard written to {out}")
    print(f"  {len(runs)} research runs  |  {len(trades)} trades  |  {len(journal)} journal entries  |  {len(per_ticker)} tickers")

    if open_browser:
        webbrowser.open(out.as_uri())
    else:
        print(f"Open with: open {out}")


if __name__ == "__main__":
    main()
