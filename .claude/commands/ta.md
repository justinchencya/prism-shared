---
description: Technical analysis on demand — for each ticker, is it buyable right now and where? Reads trend, levels, chips and bottoming patterns (止跌形态: hammer, engulfing, morning star, capitulation, double bottom, base breakout), the SPY market gate and buy spacing vs your last fill, then gives one call per name (Add now / Wait — with the trigger / No) with annotated charts. In a falling market it says "add" only on a confirmed bottoming pattern at a real level. Read-only — no files, no git.
---

Answer **"should I add this, and where?"** for every ticker the user names, from the chart. Decision support only: places nothing, writes nothing to the repo, cuts no branch. This is the same engine `/research` uses for its Market verdicts (`scripts/technical_structure.py` → `structure.setup`), so a `/ta` call and a research verdict on the same day agree on the setup.

Everything after `/ta` is free text: tickers (`/ta NVDA TSM AMD`), optionally `held` (every non-cash holding in `tracking/brokerage-snapshot.json`), optionally a price the user has in mind ("NVDA around 175?") — judge that price explicitly. No tickers and no `held` → ask which names.

## The rule that matters most

**In a falling regime, never call "Add now" without a confirmed bottoming pattern at a confluent level.** The engine's `setup.signal` enforces it: `add` in a falling regime only comes from a pattern whose `status` is `confirmed` (a close above its trigger), formed within one ATR of a level with ≥2 kinds of evidence (or the 200-day), still fresh (≤10 sessions) and not given back. Pending patterns are a *trigger to wait for*, never a buy. You may **downgrade** the engine's call (event risk, plan rules, what the chart shows), never upgrade it — a falling-regime "add" without a confirmed pattern does not exist.

## Steps

1. **Context (one line each, only what applies)**
   - `tracking/brokerage-snapshot.json` `fetched_at` — if >~1 trading day old, say so (held shares and the last-fill check depend on it).
   - The latest plan in `tracking/journal.json` (the most recent entries with a target portfolio / rules): per-name targets, "no more buys" names, dip rules for index funds, buy-spacing rules. A plan rule beats a chart signal: if the plan says no buys on a name, the call is **No (plan)** whatever the chart says — say what would have to change in the plan.

2. **Run the engine** (one call, all names; SPY's gate is computed once):
   ```bash
   CHARTS=$(mktemp -d)
   python3 scripts/technical_structure.py <TICKER> [<TICKER> ...] --chart-dir "$CHARTS" --ledger tracking/ledger.json
   ```
   Per name it prints the trend, levels, every bottoming pattern in the last 20 sessions (level, trigger, stop, status: `confirmed` / `pending` / `expired` / `failed`, volume vs 50-day), the **SETUP** line (`regime → signal`), the **GATE (SPY)** line and the **SPACING** line (minimum gap between buys = 2.5 × the 1-year average daily range, clamped 3–20%, and the next buy price vs the last fill in the ledger). Add `--json` if you need fields the summary omits. A name with <120 daily bars (recent IPO) has no structure — say so and fall back to its range and IPO price.

3. **Events** — next earnings per name (`python3 -c "import yfinance as yf; print(yf.Ticker('<T>').calendar)"`) and anything the user mentioned. An add zone inside a print window is **Wait (event)** unless the user explicitly wants to own it through the print.

4. **Research overlay** — `python3 scripts/research_index.py <TICKER...>`: latest Market verdict + run date, or "no research on file". A recent Avoid is worth one clause; it doesn't override the chart call (the chart is about *when*, research about *whether*).

5. **Look at every chart** (Read tool on `$CHARTS/chart_<TICKER>.png`). Patterns are marked under their candle with their status, the trigger as a dotted line, and the setup in the title. Check that the pattern is real to the eye and sits at the level the engine says. If the eye disagrees, say which you trust and why — and only ever in the cautious direction.

6. **Call per name** — map the engine to one of three calls, then apply the overrides in order:

   | `setup.signal` | Call |
   |---|---|
   | `add` | **Add now** — in `entry_zone`, stop = confirmed close below `stop` |
   | `wait_confirmation` | **Wait** — buy on a close above `trigger` (or half size above `early_trigger`), stop below `stop` |
   | `wait_pullback` | **Wait** — limit order in `entry_zone` |
   | `wait_gate` | **Wait (gate)** — the pattern is there but SPY's gate is closed |
   | `no_add` | **No** — falling with no bottoming pattern at a level; name the `watch` levels where one could form |

   Overrides (each can only make the call more cautious): plan rule → **No (plan)**; spacing `too close` → **Wait** for the `next buy ≤` price; earnings inside the zone → **Wait (event)**; a live news shock on the name (search once if the move is unexplained) → say it and downgrade. **Index ETFs** (SPY, QQQ, …) ignore the gate — they *are* the dip vehicle — but the user's plan dip triggers decide size; still never "Add now" in a falling regime without a confirmed pattern unless the plan's dip trigger itself fired (then say "plan dip rule, not a chart signal").

7. **Report** — terse, Prism voice, English. Open with one line: as-of (the engine's `asof` — intraday bars are not final), **gate state** and why, snapshot age. Then one table:

   | Call | Ticker | Now | Regime | Bottoming evidence | Buy at / trigger | Stop (close below) | Spacing | Note |
   |---|---|---|---|---|---|---|---|---|

   - **Bottoming evidence**: the pattern + date + level + status + volume (e.g. "morning star 11/09 at $424 ×2, confirmed, 2.1× vol"), or "none at a level".
   - **Note**: earnings, plan target / held shares, research verdict, user's price judged ("your $175: below the trigger — wait").
   - Then ≤2 lines per **Add now** and per **Wait** with a trigger: what to do and what voids it.
   - Send all charts in one file-tool call (e.g. `SendUserFile`, rendered), captioned `TICKER — call — trigger/zone`. No such tool → list the paths.

Data reality (state once if relevant): yfinance daily bars, ~15-min delayed intraday; chip and volume-profile levels are models of holder cost, not holder data; a pattern's confirmation is a close, so an intraday move above a trigger is not a confirmation yet.
