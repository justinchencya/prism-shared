# Tracking

Four persistent JSON files (`/sync-portfolio` owns two of them):

- **ledger.json** — the brokerage's own activity history (every fill, option trade, dividend, deposit, corporate action), merged by `/sync-portfolio` via `scripts/fetch_snaptrade.py --ledger` (see schema below). The **source of truth for what traded** — nothing is logged by hand. Ships as an empty template in prism-shared.
- **journal.json** — free-form reflections written by `/journal`: each entry carries `text` plus optional `linked_research[]` and `linked_tickers[]`. A capture log for your own thinking (hesitations, imagined scenarios, roads not taken), surfaced in the dashboard timeline.
- **hypotheticals.json** — what-if scenarios written by `/what-if`: counterfactual trade substitutions, standalone hypothetical portfolios, and single-ticker benchmarks, overlaid on the dashboard P&L chart (see schema below)
- **brokerage-snapshot.json** — the real brokerage state (accounts, balances, positions, recent activity) pulled from SnapTrade by `/sync-portfolio` via `scripts/fetch_snaptrade.py` (see schema below). The **source of truth for holdings**; consumers judge staleness from `fetched_at`. Ships as an empty template in prism-shared — real data lives only in the private repo.

(A fifth file, `price-cache.json`, may appear here — a gitignored daily-close cache written by `scripts/generate_dashboard.py`. Pure regenerable optimization, never committed.)

Research runs write nothing here. A ticker's research history and verdicts live in `reports/` and are read from there (see **Tracking** in `CLAUDE.md`). Scout and research-director read brokerage-snapshot.json for `[PORTFOLIO]` tagging; ledger.json, journal.json, and hypotheticals.json are read by the dashboard (which links each fill to its research automatically); ledger.json is never written outside `/sync-portfolio`.

(Until 2026-11-08 trades were logged by hand with `/log-trade` into `trades.json`, mirrored to a Notion database. Both were retired in favor of the ledger; the old file lives on in git history.)

---

## Schema: ledger.json

Written only by `scripts/fetch_snaptrade.py --ledger` (invoked by `/sync-portfolio`). Append-only: every fetched activity is merged by SnapTrade activity `id`, so re-syncing is idempotent. A ledger with no activities for an account gets that account's **full history** (from SnapTrade's `first_transaction_date`) on the next sync; later syncs merge the `lookback_days` window. Rules live in `scripts/ledger.py`.

```json
{
  "updated_at": "2026-11-08T18:30:47Z",
  "history_start": "2024-07-30",
  "activities": [
    {
      "id": "<snaptrade activity uuid>",
      "date": "2026-11-06",
      "type": "SELL",
      "symbol": "AMD",
      "raw_symbol": "AMD",
      "units": -10.0,
      "price": 436.9,
      "amount": 4368.91,
      "fee": 0.09,
      "option": "",
      "currency": "USD",
      "description": "YOU SOLD ADVANCED MICRO DEVICES INC (AMD) (Cash)",
      "account": "****1234"
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `id` | SnapTrade activity id — the dedupe key. Never changes. |
| `date` | Broker date (for Fidelity, the posting date — up to a trading day after the trade) |
| `type` | `BUY` · `SELL` · `REI` (reinvestment) · `DIVIDEND` · `CONTRIBUTION` · `WITHDRAWAL` · `FEE` · `TAX` · `JOURNALED` · `LOAN` (share deposit, e.g. a conversion) · `ADJUSTMENT` · `SPLIT` · `SPINOFF` · `OPTIONEXPIRATION` |
| `symbol` / `raw_symbol` | Canonical symbol (`scripts/symbols.py`) / the broker's spelling. Empty for cash rows and most option rows; CUSIP-keyed for some corporate-action legs. |
| `units` | Signed share/contract count (negative = out) |
| `price`, `amount`, `fee` | Per-unit price, signed cash amount, fee |
| `option` | OCC contract symbol for option rows, else `""` |
| `account` | Masked account number |

**How consumers read it** (`scripts/ledger.py`):

- **Stock trades** = `BUY` / `SELL` / `REI` rows on quote symbols, plus share-bearing non-trade rows (`LOAN`, `ADJUSTMENT`, …) as transfers valued at the row's amount. Option rows, the cash sweep, and CUSIP-keyed legs are excluded.
- **Opening lots** — holdings that predate `history_start` get a synthesized lot: snapshot units − ledger net (at least the deepest running deficit; within a day buys apply before sells). The dashboard prices it at the first close on/after `history_start`, so P&L on those shares counts from the start of the record.
- **Cross-check** — opening lots + ledger net must equal the snapshot's shares for every symbol; a mismatch is a warning on the snapshot (`/sync-portfolio` reports it).
- **Research links** — the dashboard links each fill to the newest run covering the ticker dated on/before the fill (≤180 days old), with that run's Market verdict (`scripts/research_index.py`). Nothing is linked by hand.

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

- **`substitute`** — clones the actual trade log with swaps. `overrides[]` entries are either `{ "from": "<TICKER>", "to": "<TICKER>" }` (swaps every trade in that ticker, buys and sells — preferred) or `{ "trade_id": "<ledger activity id>", "ticker": "<TICKER>" }` (swaps one specific trade only). Swapped buys convert the same dollars into the target at its close; swapped sells sell the same *fraction* of the hypothetical position as the original sell took of the real one.
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
  "totals": {
    "market_value": 51230.44, "cash": 2103.11,
    "cash_collateral_reserved": 1500.0, "cash_free": 603.11, "cash_reported": 2140.55,
    "options_market_value": -42.0, "currency": "USD"
  },
  "accounts": [
    {
      "id": "<snaptrade-account-uuid>",
      "name": "Individual",
      "number_masked": "****1234",
      "institution": "Fidelity",
      "type": "TFSA/RRSP/individual/etc",
      "sync_status": "2026-07-12T13:58:01Z",
      "balance": {
        "total": 51230.44,
        "currency": "USD",
        "cash": 2103.11,
        "cash_breakdown": { "collateral_reserved": 1500.0, "free": 603.11 },
        "cash_reported": 2140.55,
        "cash_check": {
          "total_minus_investments": 2103.11,
          "buying_power_plus_collateral": 2103.11,
          "agree": true,
          "reported_lag": 37.44
        }
      },
      "positions": [
        {
          "symbol": "NVDA",
          "description": "NVIDIA CORP",
          "units": 12.0,
          "price": 213.5,
          "market_value": 2562.0,
          "currency": "USD",
          "average_purchase_price": 180.2,
          "open_pnl": 399.6,
          "cash_equivalent": false,
          "raw_symbol": "NVDA"
        }
      ],
      "activities": [
        {
          "date": "2026-07-01",
          "type": "BUY",
          "symbol": "NVDA",
          "raw_symbol": "NVDA",
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
- `activities` covers only the trailing `lookback_days` window (default 90). The full history lives in `ledger.json`, which every sync merges these into.
- Positions are sorted by market value descending; activities by date descending.
- `symbol` (and options' `underlying`) is canonical (`scripts/symbols.py` — Fidelity's `BRKB` is stored as `BRK-B`); the broker's own spelling is kept as `raw_symbol` / `raw_underlying`. Snapshots from before this change have no `raw_*` fields and may hold broker spellings — readers canonicalize, so they still work.
- Activity `date` is the broker's date, which for Fidelity is the posting date (up to a trading day after the trade).
- **`balance.cash` is derived, not SnapTrade's cash line.** Fidelity's core-sweep (SPAXX) units — reported both as the `cash_equivalent: true` position and as `/balances` cash — can lag the account's real cash by thousands, while `total` and security prices are current. So `cash` = `total` − non-cash-equivalent positions − net options value: the actual cash holdings (Fidelity's "Cash and credits"), **including** cash reserved as option collateral. `cash_breakdown` splits it: `collateral_reserved` = Σ short puts strike × multiplier × contracts; `free` = SnapTrade `buying_power` (settled, unreserved — Fidelity's "Available to trade"). `cash_check` cross-validates (`free + collateral_reserved` must equal `cash` within $1; `agree: false` → a snapshot warning, `cash` keeps the first method) and records `reported_lag` = `cash_reported` − `cash`. The cash-equivalent position's own `market_value` carries the lagged figure. Snapshots from before this change lack these fields — their `cash` is the raw SnapTrade line.
- `warnings[]` records sub-endpoint failures (e.g. one account's activities endpoint down) — the snapshot is still valid, just partial.

---
