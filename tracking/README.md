# Tracking

Seven persistent JSON files that accumulate actionable follow-ups across all Prism research runs.

- **portfolio.json** — tickers you hold: each position carries a `reports[]` array (thesis evolution across runs) and an `events[]` array (buy triggers, falsifiers, event monitors)
- **candidates.json** — tickers you're watching: user-curated, same schema as portfolio positions. You add tickers manually; research runs populate their `reports[]` and `events[]`.
- **catalysts.json** — macro/multi-ticker events: regulatory decisions, IPOs, policy changes
- **trades.json** — log of every individual trade execution written by `/log-trade` (see schema below)
- **journal.json** — free-form reflections written by `/journal`: each entry carries `text` plus optional `linked_research[]` and `linked_tickers[]`. A capture log for your own thinking (hesitations, imagined scenarios, roads not taken), surfaced in the dashboard timeline.
- **hypotheticals.json** — what-if scenarios written by `/what-if`: counterfactual trade substitutions, standalone hypothetical portfolios, and single-ticker benchmarks, overlaid on the dashboard P&L chart (see schema below)
- **brokerage-snapshot.json** — the real brokerage state (accounts, balances, positions, recent activity) pulled from SnapTrade by `/sync-portfolio` via `scripts/fetch_snaptrade.py` (see schema below). The source of truth portfolio.json/trades.json are reconciled against; consumers judge staleness from `fetched_at`. Ships as an empty template in prism-shared — real data lives only in the private repo.

(An eighth file, `price-cache.json`, may appear here — a gitignored daily-close cache written by `scripts/generate_dashboard.py`. Pure regenerable optimization, never committed.)

Entries in portfolio.json / candidates.json / catalysts.json are created and updated automatically by the research-director (Phase 8) after each `/research` run — but only for tickers already in portfolio.json or candidates.json. New tickers surfaced in a run appear as `[NEW]` in the final report; you add them to candidates.json manually if you want to track them (or via `/journal` when you link a `[NEW]` ticker to a reflection).

Scout reads portfolio.json, candidates.json, and catalysts.json for context but never writes to them. trades.json, journal.json, and hypotheticals.json are read by the dashboard only. brokerage-snapshot.json is written and read only by `/sync-portfolio` (which may propose fixes to portfolio.json/candidates.json from it — trades.json is never written outside `/log-trade`).

---

## Unified ticker entry schema

Both `portfolio.json` positions and `candidates.json` entries share the same structure:

```json
{
  "ticker": "NVDA",
  "added_date": "2026-06-06", // candidates.json only; omit in portfolio.json
  "reports": [
    {
      "run": "2026-05-29-nvda-analysis",
      "date": "2026-05-29",
      "hypothesis": "2-sentence falsifiable thesis from this run",
      "entry_condition": "Buy below $130 on pullback",
      "verdict": "Buy"
    }
  ],
  "events": [
    {
      "id": "NVDA-BUY-130",
      "type": "buy_trigger",
      "status": "active",
      "condition": "...",
      "watch": "...",
      "added": "2026-05-29",
      "reviewed": "2026-05-29",
      "history": [...]
    }
  ]
}
```

### reports[] fields

| Field | Type | Description |
|-------|------|-------------|
| `run` | string | Research run slug (matches reports/ directory name) |
| `date` | YYYY-MM-DD | Date the run completed |
| `hypothesis` | string | 2-sentence falsifiable thesis from that run |
| `entry_condition` | string \| null | Buy/add condition if verdict is Hold/Buy; null if Avoid |
| `verdict` | string | `"Buy"` · `"Hold"` · `"Avoid"` |

Reports are appended in chronological order. Reading them in sequence shows how the thesis evolved.

---

## Schema: portfolio.json

```json
{
  "last_updated": "YYYY-MM-DD",
  "positions": [ <TickerEntry>, ... ]
}
```

Each position has `ticker`, `reports[]`, `events[]`. Snapshot — presence means currently held. To exit a position, delete the entry.

---

## Schema: candidates.json

```json
{
  "last_updated": "YYYY-MM-DD",
  "entries": [ <TickerEntry>, ... ]
}
```

Each entry has `ticker`, `added_date`, `reports[]`, `events[]`. Snapshot — presence means actively watching. To stop watching, delete the entry. When you make a trade, delete the entry from candidates.json and add the ticker to portfolio.json.

---

## Schema: events[] entries

Each object in an `events[]` array is a watchlist entry:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✓ | Stable ID. Format: `TICKER-TYPE_SHORT-KEYWORD`. Never changed after creation. |
| `ticker` | string | ✓ | Uppercase symbol (e.g. `PGR`) |
| `type` | enum | ✓ | `"buy_trigger"` · `"falsifier"` · `"event_monitor"` |
| `status` | enum | ✓ | `"active"` · `"resolved"` · `"stale"` |
| `condition` | string | ✓ | Full prose description of the condition being monitored |
| `watch` | string | ✓ | What specifically to check and at what threshold |
| `added` | YYYY-MM-DD | ✓ | Date of the source research run |
| `reviewed` | YYYY-MM-DD | ✓ | Date last touched by any research run |
| `stale_warning` | string | — | Added by director when entry is >18 months old without review |
| `resolved_date` | YYYY-MM-DD | if resolved | Date the condition resolved |
| `resolving_run` | string | if resolved | Slug of the run that resolved it |
| `resolving_catalyst` | string | — | ID of the catalyst entry that triggered resolution (if applicable) |
| `history` | array | ✓ | Ordered log of events (see below) |

### History event types

| Event type | When used | Key fields |
|------------|-----------|------------|
| `created` | First extraction from a research run | `from_verdict: null`, `to_verdict` |
| `verdict_change` | A later run produces a different market verdict | `from_verdict`, `to_verdict`, `note` |
| `updated` | A later run rechecks — verdict unchanged | `note: "Rechecked — verdict unchanged."` |
| `status_change` | Entry moves to resolved or stale | `from_status`, `to_status`, `note` |

Every history event carries: `date`, `run` (slug), `source_file`, `event`, `note`.

---

## Schema: catalysts.json

```json
{
  "last_updated": "YYYY-MM-DD",
  "entries": [ <CatalystEntry>, ... ]
}
```

### CatalystEntry fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✓ | Stable ID. Format: `TICKER-EVENT-YEAR` or `MACRO_KEY-YEAR-EVENT`. Never changed. |
| `type` | enum | ✓ | `"regulatory"` · `"corporate"` · `"macro"` |
| `status` | enum | ✓ | `"active"` · `"resolved"` · `"stale"` |
| `tickers_affected` | string[] | ✓ | Tickers whose event entries this catalyst touches |
| `expected_window` | string | ✓ | Plain-language time window (e.g. `"Q4 2026–Q1 2027"`) |
| `description` | string | ✓ | What the event is, why it matters, what to watch |
| `what_fires` | string | ✓ | Concrete action to take when this resolves |
| `added` | YYYY-MM-DD | ✓ | Date of the source research run |
| `reviewed` | YYYY-MM-DD | ✓ | Date last touched by any research run |
| `resolved_date` | YYYY-MM-DD | if resolved | Date the event resolved |
| `resolving_run` | string | if resolved | Slug of the run that captured the resolution |
| `outcome` | string | if resolved | One sentence: what actually happened |
| `history` | array | ✓ | Ordered log of events |

---

## ID generation rules

**Event entries** (inside `events[]`): `TICKER-TYPE_SHORT-KEYWORD` (SCREAMING-KEBAB-CASE)
- TYPE_SHORT: `BUY` · `FAL` · `EVT`
- KEYWORD: 1–2 words from the most specific, immutable part of the condition (metric name, event type, price level). Not the company name.
- Examples: `PGR-BUY-180`, `CRM-FAL-CRPO`, `GOOGL-EVT-DOJ`, `AMZN-FAL-SILICON`

**Catalyst entries**: `TICKER-EVENT-YEAR` or `MACRO_KEY-YEAR-EVENT`
- Examples: `GOOGL-DOJ-2026`, `AMZN-ANTHROPIC-IPO`, `FED-RATE-2026`

**Stability rule**: once written, an `id` never changes — even if `condition` or `description` prose is updated.

---

## Update protocol: when a catalyst fires

When a catalyst event resolves (e.g., Anthropic IPO prices, DOJ issues a remedy ruling):

**Step 1** — run `/research` on the event. Phase 8 handles file updates automatically.

**Step 2 — What Phase 8 does to catalysts.json**: sets `status: "resolved"`, adds `resolved_date`, `resolving_run`, `outcome`, appends a `status_change` history event.

**Step 3 — What Phase 8 does to portfolio.json / candidates.json**: for each ticker in `tickers_affected`, finds active event entries and appends either a `verdict_change` (if the outcome changes the market verdict) or an `updated` event (if neutral), with `resolving_catalyst` noted in the history.

---

## Manual lifecycle

You can update entries directly in the JSON files at any time.

**To resolve an event entry manually**:
```json
"status": "resolved",
"resolved_date": "YYYY-MM-DD",
"resolving_run": "manual",
"history": [..., {
  "date": "YYYY-MM-DD",
  "run": "manual",
  "source_file": null,
  "event": "status_change",
  "from_status": "active",
  "to_status": "resolved",
  "note": "<what happened>"
}]
```

**When you make a trade on a candidate**:
1. Delete the entry from candidates.json
2. Add a new position to portfolio.json and copy over the `reports[]` and `events[]` arrays

**When you fully exit a portfolio position**:
1. Delete the entry from portfolio.json

The director never auto-deletes or auto-resolves entries. Entries >18 months old without review get a `stale_warning` field added.

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
| `market_verdict` | string \| null | Market verdict from that report (`"Buy"` / `"Hold"` / `"Avoid"`) |
| `linked_at` | YYYY-MM-DD | Date the link was made (may differ from trade date for retroactive linking) |

**Note:** `linked_research` captures only the research that *drove* this specific trade. A ticker's full research history (all runs that ever covered it) lives in `portfolio.json` or `candidates.json` under `reports[]`.

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

## Where the full extraction rules live

- `.claude/agents/research-director.md` — Phase 8 (agent-facing rules; runs automatically after each `/research`)
- `CLAUDE.md` — Tracking section (architecture overview and ID rule reference)
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
