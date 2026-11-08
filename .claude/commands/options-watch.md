---
description: Daily watchdog over the option contracts you already hold — flags short options that are nearly fully profitable (close early), near expiry, in the money / assignment risk, or ex-dividend-exposed, and long options that are decaying toward expiry, with a drafted roll for the short ones. Quiet when nothing needs action. Built for scheduling; read-only (no orders, no repo writes, no git).
---

Check the user's **existing** option positions and say what needs action today. This is the maintenance counterpart to `/options` (which picks *new* trades). It places nothing, writes nothing to the repo, and cuts no branch — so it is safe to run unattended on a schedule. Everything after `/options-watch` is an optional focus (e.g. `/options-watch NVDA`, `/options-watch just the shorts`) — honor it by filtering the report; default is all contracts.

## Steps

1. **Get fresh holdings** — option positions are only as good as the snapshot. Detect SnapTrade credentials *without printing them* (non-empty `SNAPTRADE_CLIENT_ID` and `SNAPTRADE_CONSUMER_KEY` in the environment, else assignments in `<repo-root>/.env`). Never echo the values.
   - **Present** → fetch to a **scratch path**, not `tracking/`, so a daily run creates no diff, branch, or PR:
     ```bash
     python3 scripts/fetch_snaptrade.py /tmp/options-watch-snapshot.json
     ```
     Exit 0 → use it. Exit 2 or 1 → say why in one line and fall back to the committed snapshot.
   - **Absent / fallback** → use the committed `tracking/brokerage-snapshot.json` and lead the report with its age from `snapshot_age_hours` ("holdings are N days old — option marks and fills since then are unknown"). A snapshot older than ~1 trading day makes every flag provisional; say so.
   - Empty template (`fetched_at: null`) → stop: "No brokerage snapshot and no SnapTrade credentials here — run `/sync-portfolio` from an environment that has them."

2. **Run the watch** once:
   ```bash
   python3 scripts/options_watch.py --snapshot <path from step 1>
   ```
   Flags it raises (thresholds are CLI args: `--capture 0.75 --expiry-days 7 --long-days 21 --delta 0.50`; change them only if the user asks):
   - **Short** — `CLOSE_EARLY` (≥75% of the premium kept), `EXPIRING` (≤7 days), `ITM` / `HIGH_DELTA` (assignment risk), `EX_DIV` (short call in the money before an ex-dividend date — early-assignment risk), `STALE` (expiry passed, broker line not cleared).
   - **Long** — `DECAY` (out of the money with ≤21 days left), `EXPIRING`, `STALE`.
   - Items needing a roll (`ITM`, `HIGH_DELTA`, `EXPIRING` shorts) carry a drafted `roll`: buy-to-close now + sell a later expiry (7–45 days past the current one), preferring a net credit and |delta| ≤ 0.30; an out-of-the-money roll never moves the strike closer to the money. `roll: null` means no workable candidate in the window — say so.
   - `ok[]` are contracts with no flag; `warnings[]` are surfaced, not fatal.

3. **Judge** — the script flags; this step decides. For each flagged contract, give an action — **close**, **roll**, **let it expire**, **accept assignment**, or **hold** — and the reason, using the user's actual situation:
   - `CLOSE_EARLY`: the remaining premium is small against the tail risk and the tied-up collateral/shares. Buying back frees the shares or cash; then point to `/options cc|csp <TICKER>` for a fresh pick rather than rolling by reflex. State dollars: cost to close, premium kept, collateral or shares freed.
   - `EXPIRING` and out of the money with a few dollars left: let it expire unless the buyback is trivially cheap and the strike is near the money.
   - `ITM` / `HIGH_DELTA` / `EX_DIV` **short call**: shares committed vs held (`covered`); would the user be content being called away at the strike? (core long-term holds usually not — check the journals/plan.) If rolling: show the `roll` — new expiry/strike, net credit or **debit** (a debit roll is paying to delay a decision; say so), and the new delta/P(ITM). If accepting assignment: state the realized price and any tax-lot concern.
   - `ITM` / `HIGH_DELTA` **short put**: would the user still want the stock at the strike (research verdict via `python3 scripts/research_index.py <TICKER> --json`)? Accept assignment (state the cost basis and cash used) or roll down-and-out (show credit/debit). For a long-dated put (> 90 days) say that time is on the user's side and the decision is not urgent unless the thesis broke.
   - **Long** `DECAY` / `EXPIRING`: sell-to-close value (`mark_per_contract`) vs what's left of the thesis. If the original reason (an event, a horizon) has passed, recommend closing; if the thesis is alive but time isn't, say what a roll costs, or point to `/options bullish|hedge` for a fresh structure. Check the journals/research for why it was bought before recommending a close.
   - `STALE`: the contract expired — say the snapshot line is stale and ignore it.

4. **Report** — terse, Prism voice, no disclaimers, **quiet by default**:
   - Line 1: `summary` + snapshot age + whether the snapshot is fresh or provisional.
   - **Nothing flagged** → stop after one line plus the count of contracts checked, e.g. "13 option contracts checked, nothing needs action. Next expiry: NVDA $260 call, 40 days." Do not pad.
   - **Flagged** → most urgent first (ITM / expiring), one block per contract, fixed shape:
     ```
     ### <UNDERLYING> $<strike> <call|put> (<short|long>), exp <date> (<N>d)  —  <ACTION IN CAPS>
     Why: <flag in plain English, with the numbers: spot vs strike, % of premium kept, delta>
     Do: <the concrete step — "buy to close 1 contract at ~$37 (you collected $499; keep $462)">
     Roll (if suggested): buy back <leg> / sell <new expiry> $<strike> call for ~$<credit> → net <credit|debit> $<amount>/contract; new delta <x> (~<y>% chance of finishing ITM)
     Risk if you do nothing: <one line, dollars>
     ```
   - One closing line of natural follow-ups (`/options …` for a fresh pick, `/journal` to record a decision). Never place or imply placing an order; the user re-checks live bid/ask at the broker first (data is ~15 minutes delayed).

## Notes

- **Scheduling** is the user's to configure. The command is stateless and idempotent: it never writes to the repo, so it can run every trading day. On a weekend or holiday it still runs (marks are the last close) — say marks are from the last session.
- `profit_captured` uses the snapshot's `average_purchase_price`, the per-contract premium at open (the broker ledger, `tracking/ledger.json`, holds the individual option fills).
- The roll draft is a screening aid under the same assumptions as `/options` (Black-Scholes IV from the mid, modeled fills at mid ∓25% of the spread, no dividends).
