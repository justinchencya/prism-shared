---
description: Pull the live brokerage state (accounts, balances, positions, open option contracts, recent activity) from SnapTrade into tracking/brokerage-snapshot.json, check it against trades.json, and review the portfolio (concentration, cash, P&L, options, held names with no or stale research). Degrades to a review-only pass over the committed snapshot when SnapTrade credentials are unavailable (e.g. a cloud sandbox without the keys). Lands on its own sync-portfolio/<timestamp> branch + PR.
---

Sync the user's **actual brokerage state** into Prism and review it. The brokerage snapshot is the **source of truth for holdings** — what is really held, how much, at what cost. `tracking/trades.json` is the intentional, research-linked trade log, and `reports/` holds every research run. This command refreshes the holdings truth, checks the trade log against it, and then reads the portfolio critically.

Two modes, chosen automatically by capability, never by asking:

- **Sync mode** — `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` are available (environment or `<repo-root>/.env`): fetch live, check trades, review, commit on a branch + PR.
- **Review-only mode** — credentials absent (typical for a cloud sandbox that doesn't carry the keys): no fetch, no writes, no git. Read the committed `tracking/brokerage-snapshot.json`, lead with a staleness warning derived from its `fetched_at`, and run the same trade check + review (Steps 4–5) against it. If the snapshot is still the empty template (`fetched_at: null`), stop: `"No brokerage snapshot yet and SnapTrade credentials are unavailable here — run /sync-portfolio from an environment with SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY configured."`

The SnapTrade Fidelity integration is **read-only** (no trade placement); this command never mutates anything at the brokerage. Everything after `/sync-portfolio` is an optional focus hint (e.g. `/sync-portfolio concentration` or `/sync-portfolio just the options`) — honor it in the review; with no hint, run the full review.

## Steps

1. **Detect mode** — check for the credentials *without* printing them: look for non-empty `SNAPTRADE_CLIENT_ID` and `SNAPTRADE_CONSUMER_KEY` in the environment, else as assignments in `<repo-root>/.env` (e.g. `grep -c '^SNAPTRADE_CLIENT_ID=..*' .env`). Never echo, cat, or log the key values. Both present → sync mode (continue with Step 2). Either missing → review-only mode (skip to Step 5, prefixing the review with the staleness warning).

2. **Sync with main and cut the branch** — per the **Git convention** in `CLAUDE.md`, before any work:
   - **Engine-repo guard**: if `.prism-engine` exists at the repo root, stop — this is the public engine repo; tell the user to run `/sync-portfolio` in their private daily-driver repo.
   - **Clean-tree guard**: `git status --porcelain` — any unrelated changes → stop and report; never stash or sweep them in.
   - `git switch main && git pull --ff-only origin main`. On failure: report and stop.
   - Compute the identifier and cut the branch:
     ```bash
     git switch -c sync-portfolio/$(date +%Y-%m-%d-%H%M%S)
     ```
   - Also resolve today's date (`date +%Y-%m-%d`) — use it everywhere below; never a date carried in context.

3. **Fetch the snapshot** — on the branch, run:
   ```bash
   python3 scripts/fetch_snaptrade.py tracking/brokerage-snapshot.json
   ```
   - Exit 0 → snapshot written (accounts, positions, open option contracts, activities); surface any warnings the script printed (a degraded sub-source is worth the user knowing about, not worth stopping for).
   - Exit 2 (credentials missing after all — e.g. empty values) → drop back cleanly (`git switch main && git branch -D sync-portfolio/<id>`) and continue in review-only mode from the committed snapshot.
   - Exit 1 (credentials rejected / network / no accounts) → report the script's stderr, clean up the branch the same way, and stop.

4. **Check the trade log against the snapshot** (read-only — this step never writes):

   - **Unlogged trades** — snapshot activities of type buy/sell with no matching `trades.json` entry (match on ticker + date ±1 day + direction). **Never write `trades.json` here** — it mirrors the Notion Investment Log 1:1 and is owned by `/log-trade`; list the misses and suggest `/log-trade` per trade instead.
   - **Share-count drift** — a name in both the snapshot and `trades.json`, but snapshot units disagree materially with what `trades.json` history implies → report the delta (informational; the snapshot is the truth).

   If both lists are empty, say `"Trade log is consistent with the brokerage."` and move on.

5. **Review** — the chat-facing product. Read the snapshot (plus the `reports/` runs covering held names) and write a terse, falsifiable review — Prism voice, investor stance (mostly multi-year holds plus a small speculative sleeve — don't judge an evidently short-term position by a multi-year bar), no disclaimers:

   - **Shape** — total value, cash %, position count, top-3 concentration (% of equity value). Call out single-position concentration explicitly when one name dominates.
   - **Per-position** — units, market value, open P&L vs average cost. Order by weight.
   - **Open options** — read `accounts[].options[]`. Group by underlying and report: direction (`Short`/`Long`), type, strike, expiry, contracts, and current market value (signed — a short contract is a liability). Then the two things the raw list doesn't say:
     - **Share encumbrance** — for each short call, `contracts x multiplier` shares of the underlying are committed. Compare against the held share count and say what fraction is encumbered; a short call on a name held below the contract size is **naked**, flag it explicitly.
     - **Expiry proximity** — contracts expiring within ~2 weeks, and any whose expiry has already passed relative to today (a stale line the brokerage hasn't cleared yet). Note assignment risk where the underlying's current price is through the strike.
     State "no open option contracts" when the array is empty, and say so plainly if `options[]` is absent from an older snapshot rather than inferring none.
   - **Held without research** — held positions (excluding index funds and the cash sweep) that no run in `reports/` covered (no `tickers/<TICKER>.md`, no `$<TICKER>` in any `final-report.md`): name them plainly ("held with no research run on file") and list them as ready-to-run `/research` questions. The snapshot knows *what* is held; this flags *what lacks a why*.
   - **Stale research** — held names whose most recent covering run is >6 months old (run date = the report dir's date prefix). Give the run, its date, and its Market verdict for the name, and suggest a refresh `/research` question.
   - **Recent activity** — notable buys/sells/dividends/deposits from the lookback window, cross-linked to `trades.json`/journal entries where they exist.
   - If the user gave a focus hint, weight the review toward it. State "unknown" where the snapshot doesn't answer something; never fill gaps fluently.

   In review-only mode Steps 4–5 run against the committed snapshot; nothing is written.

6. **Ship gate** (sync mode only) — **show the user** the file to be committed: `tracking/brokerage-snapshot.json`. The snapshot is not a dashboard input, so there's no dashboard check or regen.

   Ask the **ship gate** (per **Git convention** in `CLAUDE.md`): a single `AskUserQuestion` call with **one** question, "Ship this portfolio sync?", and three options in this order:
   - "Ship it — commit, push, open PR, merge" (Recommended)
   - "Commit, push & open PR — leave it open"
   - "Don't commit"

   Set the `preview` of both shipping options to the review's headline lines (concentration, cash, unlogged-trade count, held-without-research count).

   **Decline path** ("Don't commit"): `git switch main && git branch -D sync-portfolio/<id>` (work returns to the working tree; nothing was pushed); report and stop. Otherwise:
   ```bash
   git add tracking/brokerage-snapshot.json
   git commit -m "sync-portfolio: <YYYY-MM-DD>"
   git push -u origin sync-portfolio/<id>
   gh pr create --base main --head sync-portfolio/<id> --title "sync-portfolio: <YYYY-MM-DD>" --body "<one line: N accounts, M positions>"
   ```
   No second prompt — "Ship it" → `gh pr merge --squash --delete-branch`; otherwise leave it open. Either way finish on `main` (`git switch main`). Any git/`gh` failure: report and stop, no destructive retry.

7. **Report back** — mode used, snapshot age, accounts/positions synced, unlogged trades suggested for `/log-trade`, and the PR URL if opened.

## Security notes

- The `consumerKey` is a Personal API key with full read access to the connected brokerage. Never print, echo, or commit it; never copy it out of `.env`/env. The script signs requests locally with it.
- The snapshot itself contains personal financial data — it belongs in the private prism repo only. In prism-shared, `tracking/brokerage-snapshot.json` stays the empty template (same rule as every other tracking file).

## Examples

`/sync-portfolio`
→ creds found → branch `sync-portfolio/2026-07-12-091500` → fetch → 1 unlogged VRT buy (→ suggest `/log-trade`) → review: 61% top-3 concentration, 4% cash, 3 names held with no research run on file (→ ready-to-run `/research`), NOW's last research 14 months old → ship gate → commit + PR.

`/sync-portfolio concentration`
→ same flow; the review leads with concentration and position sizing.

`/sync-portfolio` (on a phone / cloud sandbox without the keys)
→ review-only: "Snapshot is 6 days old (fetched 2026-07-06)." → same trade check + review off the committed snapshot; no branch, no PR.
