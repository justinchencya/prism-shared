# Tracking

Four persistent JSON files, each owned by one command:

- **trades.json** — log of every individual trade execution written by `/log-trade` (see schema below)
- **journal.json** — free-form reflections written by `/journal`: each entry carries `text` plus optional `linked_research[]` and `linked_tickers[]`. A capture log for your own thinking (hesitations, imagined scenarios, roads not taken), surfaced in the dashboard timeline.
- **hypotheticals.json** — what-if scenarios written by `/what-if`: counterfactual trade substitutions, standalone hypothetical portfolios, and single-ticker benchmarks, overlaid on the dashboard P&L chart (see schema below)
- **brokerage-snapshot.json** — the real brokerage state (accounts, balances, positions, recent activity) pulled from SnapTrade by `/sync-portfolio` via `scripts/fetch_snaptrade.py` (see schema below). The **source of truth for holdings**; consumers judge staleness from `fetched_at`. Ships as an empty template in prism-shared — real data lives only in the private repo.

(A fifth file, `price-cache.json`, may appear here — a gitignored daily-close cache written by `scripts/generate_dashboard.py`. Pure regenerable optimization, never committed.)

Research runs write nothing here. A ticker's research history and verdicts live in `reports/` and are read from there (see **Tracking** in `CLAUDE.md`). Scout and research-director read brokerage-snapshot.json for `[PORTFOLIO]` tagging; trades.json, journal.json, and hypotheticals.json are read by the dashboard; trades.json is never written outside `/log-trade`.

---

## Schema: trades.json

Written by `/log-trade`. Each entry is one trade execution.

```json
{
  "last_updated": "YYYY-MM-DD",
  "trades": [
    {
      "id": "trade-NVDA-20260526-001",
      "date": "2026-05-26",
      "ticker": "NVDA",
      "action": "buy",
      "amount_usd": 427.0,
      "shares": 2,
      "price_per_share": 213.5,
      "comment": "optional note",
      "notion_page_id": "...",
      "notion_url": "https://app.notion.com/p/...",
      "linked_research": [
        {
          "run": "2026-05-29-brkb-2025-drawdown",
          "date": "2026-05-29",
          "report_path": "reports/2026-05-29-brkb-2025-drawdown/tickers/NVDA.md",
          "market_verdict": "Buy",
          "linked_at": "2026-05-29"
        }
      ]
    }
  ]
}
```

### trades.json fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Stable ID. Format: `trade-<TICKER>-<YYYYMMDD>-<NNN>`. Never changed. |
| `date` | YYYY-MM-DD | Execution date |
| `ticker` | string | Uppercase symbol |
| `action` | enum | `"buy"` · `"sell"` · `"add"` · `"trim"` |
| `amount_usd` | number | Dollar value of the trade |
| `shares` | number \| null | Number of shares (backfilled from brokerage export when available) |
| `price_per_share` | number \| null | Execution price per share (backfilled from brokerage export) |
| `comment` | string \| null | Optional free-text note |
| `notion_page_id` | string | Notion page ID for the corresponding Investment Log row |
| `notion_url` | string | Direct Notion URL |
| `linked_research` | array | Research runs that motivated this specific trade (subset of all runs covering the ticker) |

### linked_research[] fields

| Field | Type | Description |
|-------|------|-------------|
| `run` | string | Research run slug |
| `date` | YYYY-MM-DD | Date the research run completed |
| `report_path` | string | Path to the ticker-level report file |
| `market_verdict` | string \| null | The run's Market verdict for this ticker when the link was made (`"Buy"` / `"Hold"` / `"Avoid"`). The dashboard uses it as stored; if null, it reads the verdict from the run's report. |
| `linked_at` | YYYY-MM-DD | Date the link was made (may differ from trade date for retroactive linking) |

**Note:** `linked_research` captures only the research that *drove* this specific trade. A ticker's full research history (all runs that ever covered it) lives in `reports/`.

---

## Schema: hypotheticals.json

Written by `/what-if`. Each scenario is a hypothetical portfolio the dashboard overlays on the P&L value chart. Shares and prices are **not** stored for derived trades — `scripts/generate_dashboard.py` computes them from daily closes at render time (execution proxy: first close on or after the trade date).

```json
{
  "last_updated": "YYYY-MM-DD",
  "scenarios": [
    {
      "id": "whatif-20260711-001",
      "name": "AMD instead of NVDA (2026-03-02)",
      "status": "active",
      "type": "substitute",
      "color_index": 0,
      "created": "2026-07-11",
      "note": "optional free text",
      "overrides": [ { "from": "NVDA", "to": "AMD" } ]
    }
  ]
}
```

### Scenario fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Stable ID: `whatif-<YYYYMMDD>-<NNN>` (per-day sequence). Never changed. |
| `name` | string | Short human label — what the dashboard legend and tooltips show. Renameable. |
| `status` | enum | `"active"` (drawn on the chart) · `"archived"` (kept, not drawn) |
| `type` | enum | `"substitute"` · `"benchmark"` · `"standalone"` |
| `color_index` | int | Fixed chart-color slot (0–3), assigned at creation, never reshuffled |
| `created` | YYYY-MM-DD | Creation date |
| `note` | string | Optional free text |

### Per-type payload

- **`substitute`** — clones the actual trade log with swaps. `overrides[]` entries are either `{ "from": "<TICKER>", "to": "<TICKER>" }` (swaps every trade in that ticker, buys and sells — preferred) or `{ "trade_id": "<trades.json id>", "ticker": "<TICKER>" }` (swaps one specific trade only). Swapped buys convert the same dollars into the target at its close; swapped sells sell the same *fraction* of the hypothetical position as the original sell took of the real one.
- **`benchmark`** — `benchmark_ticker: "<TICKER>"`: every actual trade re-executed into that one ticker, same dates and amounts ("what if I'd just bought SPY").
- **`standalone`** — `trades[]`: an explicit hypothetical trade list, each `{ date, ticker, action, amount_usd }` (or explicit `shares` + `price_per_share`). Recurring plans (DCA) are expanded into explicit dated trades by `/what-if` at creation.

`substitute` and `benchmark` share the actual portfolio's cashflows, so the dashboard and `--whatif-preview` also report the **P&L delta vs actual** (P&L, not end value — sale proceeds differ between the two sides and the invested line nets them out). `standalone` shows only its own P&L.

Everything is computed from daily closes — no intraday fills, dividends, or FX. Directional sanity-checking, not precise P&L.

---

## Schema: brokerage-snapshot.json

Written by `scripts/fetch_snaptrade.py` (invoked by `/sync-portfolio`). One whole-file snapshot per fetch — not append-only; each sync overwrites it. Requires `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` (SnapTrade Personal API key) in the env or `.env`.

```json
{
  "fetched_at": "2026-07-12T14:03:22.512Z",
  "source": "snaptrade",
  "lookback_days": 90,
  "totals": { "market_value": 51230.44, "cash": 2103.11, "currency": "USD" },
  "accounts": [
    {
      "id": "<snaptrade-account-uuid>",
      "name": "Individual",
      "number_masked": "****1234",
      "institution": "Fidelity",
      "type": "TFSA/RRSP/individual/etc",
      "sync_status": "2026-07-12T13:58:01Z",
      "balance": { "total": 51230.44, "currency": "USD", "cash": 2103.11 },
      "positions": [
        {
          "symbol": "NVDA",
          "description": "NVIDIA CORP",
          "units": 12.0,
          "price": 213.5,
          "market_value": 2562.0,
          "currency": "USD",
          "average_purchase_price": 180.2,
          "open_pnl": 399.6
        }
      ],
      "activities": [
        {
          "date": "2026-07-01",
          "type": "BUY",
          "symbol": "NVDA",
          "units": 2.0,
          "price": 213.5,
          "amount": -427.0,
          "currency": "USD",
          "description": "YOU BOUGHT NVDA ..."
        }
      ]
    }
  ],
  "warnings": []
}
```

Notes:

- `fetched_at: null` means the file is still the untouched template — `/sync-portfolio` in review-only mode stops rather than reviewing nothing.
- Account numbers are masked to the last 4 digits before hitting disk; the full number is never stored.
- `activities` covers only the trailing `lookback_days` window (default 90) — it is a reconciliation aid for `trades.json`, not a full trade history.
- Positions are sorted by market value descending; activities by date descending.
- `warnings[]` records sub-endpoint failures (e.g. one account's activities endpoint down) — the snapshot is still valid, just partial.

---
