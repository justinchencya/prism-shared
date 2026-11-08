---
description: Pick ONE option trade (plus a runner-up) for a stated intent — sell a covered call or cash-secured put for pocket money (you want it to expire worthless), or buy a protective put / put spread / collar, or a call / call spread on a view (you want it to gain value). Screens live Yahoo chains against chart structure, earnings, research verdicts and the brokerage snapshot, with scenario P&L. Read-only — no orders, no files, no git.
---

Pick **one** option trade for the user's stated intent, with a runner-up. Decision support for the user's own account: it places nothing, writes nothing, cuts no branch. If the user acts, option fills show up in the next `/sync-portfolio`; `/journal` captures the reasoning.

The single most important thing is **which side of the trade the user wants to win on**:

| Intent | Strategy | The user wants the option to… |
|---|---|---|
| `cc` — "sell a covered call on NVDA for pocket money" | covered call | **expire worthless** (keep premium; assignment is the unwanted outcome, so far-OTM) |
| `csp` — "sell a put on QQQ/SPY/NVDA with $20–40k, I don't want to buy" | cash-secured put | **expire worthless** (assignment is acceptable, not wanted) |
| `hedge` — "protect my NVDA over earnings" | protective put, put spread, or collar | **gain value** if the stock falls; cost/bleed if it doesn't |
| `bullish` — "GOOG got overpunished, rebound in 1–2 months" | long call or call debit spread | **gain value** — needs a target price + horizon |

Not a router for "all strategies": anything outside these four (iron condors, calendars, straddles, rolls) is out of scope — say so and offer the nearest of the four.

Everything after `/options` is free text. Classify the intent from it; if two of the four are plausible, pick the one the wording implies (pocket money / premium → income; protect / hedge → hedge; bullish / rebound / expect to rise → bullish). Never ask a question the user already answered; ask only for what's required and absent: for `bullish`, the **target price (or % move) and horizon** — if the user gave a vague view ("rebound in a month or two"), infer a defensible target from the latest research/analyst context and the chart (nearest confluent resistance or prior swing high) and say it was inferred, with the number so they can correct it. With no intent at all (e.g. bare `/options` or "what should I sell this week"), run the portfolio-wide income scan: `python3 scripts/options_screen.py` and report it as a shortlist per the old rubric (yield last, would-you-be-assigned first).

## Data reality (state once, one line)

SnapTrade returns only the contracts already held (mark, strike, expiry) — never a chain. Chains come from Yahoo via yfinance: bid/ask/OI/volume, ~15-min delayed, **no greeks, no IV history**. IV/delta are solved here from the bid/ask mid (Black-Scholes, no dividends, european). Fills are modeled at mid ±25% of the spread (buys pay up, sells give up). No IV rank is possible; `iv_rv` (implied ÷ 30d realized) is the premium-richness proxy. Scenario P&L is model output under stated assumptions, not a forecast.

## Steps

1. **Freshness** — read `tracking/brokerage-snapshot.json` `fetched_at`; if > ~1 trading day old, say so and suggest `/sync-portfolio` (share encumbrance and free cash depend on it). Cash for `csp`: use `cash_free` (cash minus collateral already reserved) unless the user gave a budget — **the user's budget wins**; never exceed it.

2. **Run the engine** (one call per candidate underlying; for "QQQ or NVDA or SPY" run each and compare):
   ```bash
   python3 scripts/options_pick.py cc      TICKER [--contracts N] [--max-delta 0.20]
   python3 scripts/options_pick.py csp     TICKER [--budget DOLLARS] [--max-delta 0.20]
   python3 scripts/options_pick.py hedge   TICKER [--hedge-shares N] [--event YYYY-MM-DD] [--max-deductible 0.08] [--no-collar]
   python3 scripts/options_pick.py bullish TICKER --target PRICE|+N% --horizon-days D [--budget DOLLARS]
   ```
   It returns `primary`, `runner_up` (a different structure when possible), the chart/earnings/portfolio context, and `warnings[]` (surface them). If it finds nothing (`primary: null`), say why — e.g. a small budget can't secure one contract on a high-priced underlying (collateral = strike × 100); suggest the nearest workable alternative (a lower-priced name or a lower-priced share class if its options are liquid enough, or a larger budget) instead of loosening filters silently.

3. **Research overlay** — `python3 scripts/research_index.py <TICKER...> --json`: each name's latest Market verdict + run date. No covering run → say "no research on file".

4. **Judge the engine's pick** — the engine is the arithmetic; this step is the judgment. Override or flag it when the rubric says so, and say why:

   **Income (`cc`, `csp`) — the option expiring worthless is the win:**
   - `score` = annualized yield × P(expires OTM) (+15% behind a confluent level, ×0.5 across earnings). It already prefers low delta; check the pick against the *user's* constraint, not just the score.
   - `cc`: would the user be content being called away at the strike? Use the journals — core long-term holds (the user's multi-year conviction names and index anchors): far-OTM and a *fraction* of the shares, never the whole position; names earmarked for rotation: closer strikes are fine. State `upside_forgone_if_called` and contracts ≤ `cc_contracts_available` (never naked). RSI < 35 at support → selling calls into a washed-out bounce is bad timing; say so.
   - `csp`: the user doesn't want the stock, but assignment must still be one they'd accept: would they buy at `breakeven` with conviction (research verdict constructive, name they'd actually hold)? State post-assignment cost basis and the position weight after assignment; the user's cash-weight target is unaffected by the collateral (still cash) but assignment converts it to stock.
   - Earnings inside the window: fat premium, real move. If every candidate spans earnings (common), say it plainly and show the strike's `sd_away` vs the implied 1σ; recommend the post-earnings expiry if one is workable.
   - Also name **the number that matters most**: probability of keeping the premium and the loss if assigned/called.

   **Value (`hedge`, `bullish`) — the option gaining value is the win:**
   - `hedge`: the pick is the best protection per unit of bleed, with the floor within `--max-deductible` of spot. Show the scenario table (stock −30% … +20%): structure P&L per share and total for N contracts, **combined with the stock** as a % of the position. State the deductible (how far down before it pays), the net cost, what you give up if the stock rallies (cost, or the collar's cap), and that the table values the day after the event with an IV-crush assumption (puts bought into earnings carry inflated IV — the main reason a put spread or collar often wins). A hedge sized to all shares is linear; ask nothing, state the share count used and that contracts scale linearly.
   - `bullish`: the pick maximizes the scenario-weighted return on premium (target 45% / halfway 20% / flat 20% / −10% 15% — state the weights; the user can reweight). Show the table, the **breakeven at the horizon and at expiry** (vs spot, in %), max loss per contract and total, and the comparator "just buy shares" return in each scenario. A call spread caps the gain at the short strike — show that cap next to the target. Check `held_options_on_name`: if the user already holds long calls on this underlying (from `held_options_on_name`), say the new trade adds to that exposure and net the two when describing total risk.
   - Chart: the long strike vs support/resistance; a target above a confluent resistance is a real obstacle — say so.

5. **Report** — terse, Prism voice, no disclaimers. Every run uses this exact structure. The engine returns `plain_english` (`headline`, `bet`, `how_you_win`, `how_you_lose`, `key_numbers`, `notes`) for the primary and runner-up, written from the actual numbers — start from it, don't re-derive it, and keep its figures. Adapt the wording to the user's situation (journals, research verdict, held-lot context) but never change a number.

   Open with one line: intent as understood · data caveat (delayed Yahoo quotes, modeled fills) · snapshot age. Then, per pick (**Primary** first, then **Runner-up**):

   ```
   ## <Primary|Runner-up>: <plain strategy name — e.g. "Sell a put", "Bull call spread", "Collar">

   | | |
   |---|---|
   | Ticker | NVDA (spot $186.64) |
   | Expiration | 2026-11-27 (19 days) |
   | Strike(s) | Sell $160 put   — or for spreads/collars, one line per leg: Buy $175 put / Sell $155 put |
   | Est. price | ~$3.12/share credit (≈ $312 for 1 contract) — modeled fill, re-check live |
   | Size | 1 contract — why (budget $30k → max 1 at $16,000 collateral / 400 free shares / hedges 500 shares) |
   | Max gain / Max loss | $312 / $15,688 (if NVDA went to zero) |
   | Break-even | $156.88 |

   **The bet, in plain English** — 2–4 sentences: what you are betting will (not) happen, by what date,
   and at what price level. Name the dates and prices ("NVDA stays above $160 through Nov 27"), say
   whether you WANT the option to expire worthless (income) or to gain value (hedge/bullish), and — for
   any multi-leg structure — explain what each leg is for ("the $155 put you sell pays for part of the
   $175 put you buy, but it also ends your protection below $155").

   **You make money if** … (price + date + dollar amount)
   **You lose money if** … (price + date + dollar amount; include the cap/gap of spreads and collars)
   **Why this strike / date** — one line each: the chart level it sits behind (support/resistance from `levels`), delta or probability, earnings placement, research verdict.
   **Watch-outs** — from `notes` (earnings in window, when to close/roll, IV crush).
   ```

   After both picks:
   - **Scenario table** (hedge/bullish only): P&L at each price move for the primary — for `hedge`, also the combined stock + hedge as % of the position; for `bullish`, with the "just buy shares" comparator.
   - **When the runner-up wins** — one line.
   - **What would make you wrong** — the one specific scenario and its dollar cost.
   - **If several underlyings were compared** (QQQ vs SPY vs NVDA): a 3-row table of each name's best pick before the detail.
   - **Conviction** — high / medium / low with the reason; name anything `unknown` or `stale_quote`.

   Never place or imply placing an order; the user executes at their broker and re-checks live bid/ask first.
