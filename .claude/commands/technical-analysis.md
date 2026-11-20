---
description: Technical analysis on demand, both directions — for each ticker: is it buyable now and where (bottoming patterns 止跌形态, SPY gate, buy spacing), is a held position topping or extended (shooting star, bearish engulfing, evening star, buying climax, double top, top breakdown), is now a good time to sell a covered call or cash-secured put and at what strike, and is there a swing trade long or short with entry / stop / target / R:R. Annotated charts. Read-only — no files, no git.
---

Read the chart for every ticker the user names and answer the question they actually asked — **add?**, **trim / take profit?**, **sell a covered call or a put?**, **swing trade?** — or, with no stated intent, all four in one line each. Decision support only: places nothing, writes nothing to the repo, cuts no branch. This is the same engine `/research` uses for the setup half of its Market verdicts (`scripts/technical_structure.py`), so a `/technical-analysis` call and a research verdict on the same day agree.

Everything after `/technical-analysis` is free text: tickers (`/technical-analysis NVDA TSM AMD`), optionally `held` (every non-cash holding in `tracking/brokerage-snapshot.json`), optionally an intent ("trim?", "covered call on NVDA?", "sell puts on QQQ?", "swing trade?") and a price the user has in mind ("NVDA around 175?") — judge that price explicitly. No tickers and no `held` → ask which names.

## The rules that matter most

- **Entry — in a falling regime, never call "Add now" without a confirmed bottoming pattern at a confluent level.** `setup.signal` enforces it: `add` in a falling regime only comes from a pattern whose `status` is `confirmed` (a close above its trigger), formed within one ATR of a level with ≥2 kinds of evidence (or the 200-day), fresh (≤10 sessions), and not given back (no heavy-volume breakdown since). Pending patterns are a trigger to wait for, never a buy.
- **Exit — tops are read the same way, mirrored.** `exit_setup.signal` `trim` needs a *confirmed* topping pattern (a close below its trigger) at a confluent level. A pending top is `watch_top` — trim only on the close below the trigger. Price already >2 ATR below the trigger → `trim_on_bounce`: don't sell the washout.
- **Core holdings are never sold on a chart alone** (Investor stance in `CLAUDE.md`). For a multi-year core name a `trim` / `broken` means: pause adds, sell covered calls into strength, check the thesis — selling shares is for the trading sleeve. Say which sleeve a held name is in when the journals make it clear.
- You may **downgrade** the engine (event risk, plan rules, what the chart shows), never upgrade it.

## Steps

1. **Context (one line each, only what applies)**
   - `tracking/brokerage-snapshot.json` `fetched_at` — if >~1 trading day old, say so (held shares, covered-call capacity and the last-fill check depend on it). Note shares held and any open option contracts on each name.
   - The latest plan in `tracking/journal.json` (most recent entries with a target portfolio / rules): per-name targets, "no more buys" names, dip rules for index funds, buy-spacing rules, which names are core vs trading. A plan rule beats a chart signal: if the plan says no buys on a name, the entry call is **No (plan)** whatever the chart says — say what would have to change.

2. **Run the engine** (one call, all names; SPY's gate is computed once):
   ```bash
   CHARTS=$(mktemp -d)
   python3 scripts/technical_structure.py <TICKER> [<TICKER> ...] --chart-dir "$CHARTS" --ledger tracking/ledger.json
   ```
   Per name it prints trend, levels, every **bottoming** pattern (`Pattern:`) and **topping** pattern (`Top:`) in the last 20 sessions with level, trigger, stop / void level, status and volume, then the decision lines:
   - **STRETCH** — overbought / oversold (RSI14, ATRs from the 20/50-day, Bollinger %b) and a volatility **squeeze** flag;
   - **SETUP** — entry: `regime → add / wait_confirmation / wait_pullback / wait_gate / no_add`;
   - **EXIT (if held)** — `trim / trim_on_bounce / watch_top / extended / at_resistance / broken / hold`, trigger, invalidation, trail stop;
   - **OPTIONS** — covered-call and cash-secured-put timing (`favorable / neutral / unfavorable`) with a strike guide (`strike ≥` the first confluent resistance ≥1 ATR up; `strike ≤` the first confluent support at/below the pattern stop or price), and the squeeze note;
   - **SWING long / short** — entry, stop, targets from the level map, reward:risk and quality (`good` ≥2, `marginal` 1–2, `poor` <1);
   - **GATE (SPY)** and **SPACING** (min gap between buys = 2.5 × 1-year avg daily range, clamped 3–20%, vs the last fill in the ledger).
   Add `--json` for fields the summary omits. A name with <120 daily bars (recent IPO) has no structure — say so and fall back to its range and IPO price.

3. **Events** — next earnings per name (`python3 -c "import yfinance as yf; print(yf.Ticker('<T>').calendar)"`) and anything the user mentioned. An add zone inside a print window is **Wait (event)**; an option whose expiry spans earnings carries the move — say so.

4. **Research overlay** — `python3 scripts/research_index.py <TICKER...>`: latest Market verdict + run date, or "no research on file". The chart is about *when*, research about *whether*: a recent Avoid is worth one clause on an entry; a Buy thesis argues against selling core shares on a top.

5. **Look at every chart** (Read tool on `$CHARTS/chart_<TICKER>.png`). Bottoming patterns are marked under their candle, topping patterns above it, with status and the trigger as a dotted line; the title carries both the entry setup and the exit signal. Check each pattern is real to the eye and sits at the level the engine says. If the eye disagrees, say which you trust and why — only ever in the cautious direction.

6. **Calls per name** — map the engine to calls, then apply overrides (each can only make a call more cautious).

   **Entry** (`setup.signal`):

   | Signal | Call |
   |---|---|
   | `add` | **Add now** — in `entry_zone`, stop = confirmed close below `stop` |
   | `wait_confirmation` | **Wait** — buy on a close above `trigger` (or half size above `early_trigger`) |
   | `wait_pullback` | **Wait** — limit in `entry_zone` |
   | `wait_gate` | **Wait (gate)** — pattern is there, SPY's gate is closed |
   | `no_add` | **No** — falling, no bottom at a level; name the `watch` levels |

   **Exit, for held names** (`exit_setup.signal`): `trim` → **Trim** (trading sleeve sells; core: covered calls / pause adds), void on a close above `invalidation`; `trim_on_bounce` → **Trim on bounce** toward `trigger`; `watch_top` → **Watch** — trim on a close below `trigger`; `extended` / `at_resistance` → **Don't chase** — covered-call window; `broken` → **Broken** — trading positions exit on the break rule, core pauses adds + thesis check; `hold` → **Hold**, trail stop at `trail_stop`.

   **Options** (`options_timing`): report the timing and strike guide; when the user wants an actual contract, hand off to `/options cc|csp <TICKER>` (it does the chain, delta and premium math) with the strike guide as the floor/ceiling. Never sell calls into an `oversold` / bottoming chart, never sell puts into `no_add` except below the lowest watch level, small, with assignment the user would welcome. A squeeze means thin premium — say so.

   **Swing** (`swing`): show the plan only when quality is `good` or `marginal`; `poor` R:R → "no swing worth taking". A `short` swing is a bearish trade (puts / put spreads, or trimming the trading sleeve) — never a suggestion to short a core holding.

   Overrides: plan rule → **No (plan)**; spacing `too close` → **Wait** for the `next buy ≤` price; earnings inside the zone or the option's window → **Wait (event)** / flag it; a live news shock on the name (search once if the move is unexplained) → say it and downgrade. **Index ETFs** (SPY, QQQ, …) ignore the gate — they *are* the dip vehicle — but the user's plan dip triggers decide size; still never "Add now" in a falling regime without a confirmed pattern unless the plan's dip trigger itself fired (then say "plan dip rule, not a chart signal").

7. **Report** — terse, Prism voice, English. Open with one line: as-of (the engine's `asof` — intraday bars are not final), **gate state** and why, snapshot age. Then one table (drop columns the user's intent doesn't need):

   | Ticker | Now | Regime / stretch | Entry call | Exit call (if held) | CC / CSP | Swing | Key levels | Note |
   |---|---|---|---|---|---|---|---|---|

   - **Entry / Exit call**: the call + the pattern behind it (type, date, level ×confluence, status, volume), or "none at a level".
   - **CC / CSP**: timing + strike guide (e.g. "CC favorable ≥ $205 · CSP unfavorable").
   - **Swing**: "long $X → $T, stop $S, 2.3R" or "—".
   - **Key levels**: buy zone / trigger, stop, overhead.
   - **Note**: earnings, plan target / held shares, research verdict, the user's price judged ("your $175: below the trigger — wait").
   - Then ≤2 lines per actionable call: what to do and what voids it.
   - Send all charts in one file-tool call (e.g. `SendUserFile`, rendered), captioned `TICKER — entry / exit — trigger`. No such tool → list the paths.

Data reality (state once if relevant): yfinance daily bars, ~15-min delayed intraday; chip and volume-profile levels are models of holder cost, not holder data; a pattern's confirmation is a close, so an intraday move through a trigger is not a confirmation yet. How well each signal has held up historically: `python3 scripts/backtest_technical.py <TICKERS> --years 3` (point-in-time replay) — cite it when the user asks how much to trust a call.
