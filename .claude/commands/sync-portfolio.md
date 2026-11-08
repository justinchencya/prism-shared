---
description: Pull the live brokerage state (accounts, balances, positions, open option contracts, recent activity) from SnapTrade into tracking/brokerage-snapshot.json, merge the brokerage's activity history into the trade ledger (tracking/ledger.json), and review the portfolio (concentration, cash, P&L, options, held names with no or stale research). Degrades to a review-only pass over the committed snapshot when SnapTrade credentials are unavailable (e.g. a cloud sandbox without the keys). Lands on its own sync-portfolio/<timestamp> branch + PR.
---

Sync the user's **actual brokerage state** into Prism and review it. The brokerage is the **source of truth** for both what is held (the snapshot) and what traded (`tracking/ledger.json` — the broker's own activity history; nothing is logged by hand). `reports/` holds every research run; the dashboard links each fill to the research behind it automatically. This command refreshes both, cross-checks them, and then reads the portfolio critically.

Two modes, chosen automatically by capability, never by asking:

- **Sync mode** — `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` are available (environment or `<repo-root>/.env`): fetch live, merge the ledger, review, commit on a branch + PR.
- **Review-only mode** — credentials absent (typical for a cloud sandbox that doesn't carry the keys): no fetch, no writes, no git. Read the committed `tracking/brokerage-snapshot.json`, lead with a staleness warning derived from its `fetched_at`, and run the same review (Steps 4–5) against it and the committed ledger. If the snapshot is still the empty template (`fetched_at: null`), stop: `"No brokerage snapshot yet and SnapTrade credentials are unavailable here — run /sync-portfolio from an environment with SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY configured."`

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

3. **Fetch the snapshot and merge the ledger** — on the branch, run:
   ```bash
   python3 scripts/fetch_snaptrade.py tracking/brokerage-snapshot.json --ledger tracking/ledger.json
   ```
   - Exit 0 → snapshot written (accounts, positions, open option contracts, activities) and every fetched activity merged into the ledger by SnapTrade activity id (idempotent; an empty ledger gets the account's full history). Surface any warnings the script printed (a degraded sub-source is worth the user knowing about, not worth stopping for). SnapTrade rate-limits; the script backs off on its own.
   - Exit 2 (credentials missing after all — e.g. empty values) → drop back cleanly (`git switch main && git branch -D sync-portfolio/<id>`) and continue in review-only mode from the committed snapshot.
   - Exit 1 (credentials rejected / network / no accounts) → report the script's stderr, clean up the branch the same way, and stop.

4. **Ledger check** (read-only): run `python3 scripts/ledger.py --json` and report:
   - **New activity** — how many activities this sync merged (the fetch's `ledger:` log line) and the notable new stock/option fills.
   - **Cross-check** — `warnings` lists symbols where the ledger's implied shares (opening lots + history) disagree with the snapshot. Empty → say `"Ledger reconciles with holdings."` Non-empty → show them; never patch the ledger by hand (it only ever holds what the broker returned).
   - **Opening lots** — holdings that predate the ledger's `history_start` (their cost basis predates the record; the dashboard prices them at the first close on/after it). One line, only if it changed since the last sync.

5. **Review** — the chat-facing product. Read the snapshot (plus the `reports/` runs covering held names) and write a terse, falsifiable review — Prism voice, investor stance (mostly multi-year holds plus a small speculative sleeve — don't judge an evidently short-term position by a multi-year bar), no disclaimers:

   - **Shape** — total value, cash, position count, top-3 concentration (% of equity value; exclude `cash_equivalent` positions). Call out single-position concentration explicitly when one name dominates.
   - **Cash** — report `balance.cash` (derived actual cash holdings, collateral included — see `tracking/README.md`) as `$ and % of total`, split into `cash_breakdown.collateral_reserved` (put collateral) and `cash_breakdown.free`, e.g. "Cash $24,300 (18.2%) — $10,000 put collateral, $14,300 free". Never quote the cash-equivalent position's (SPAXX) market value or `cash_reported` as cash — it lags. If `cash_check.agree` is `false`, lead the section with the two disagreeing figures. If `|cash_check.reported_lag|` > $1, add one line: "SnapTrade's SPAXX/cash line lags by $X." On an older snapshot without `cash_breakdown`, use `balance.cash` and say it is SnapTrade's raw (possibly lagged) figure.
   - **Per-position** — units, market value, open P&L vs average cost. Order by weight.
   - **Open options** — read `accounts[].options[]`. Group by underlying and report: direction (`Short`/`Long`), type, strike, expiry, contracts, and current market value (signed — a short contract is a liability). Then the two things the raw list doesn't say:
     - **Share encumbrance** — for each short call, `contracts x multiplier` shares of the underlying are committed. Compare against the held share count and say what fraction is encumbered; a short call on a name held below the contract size is **naked**, flag it explicitly.
     - **Expiry proximity** — contracts expiring within ~2 weeks, and any whose expiry has already passed relative to today (a stale line the brokerage hasn't cleared yet). Note assignment risk where the underlying's current price is through the strike.
     State "no open option contracts" when the array is empty, and say so plainly if `options[]` is absent from an older snapshot rather than inferring none.
   - **Research coverage** — run `python3 scripts/research_index.py <every held symbol and option underlying> --json` once; it returns each name's covering runs (newest first) with Market verdicts, matching symbols canonically. Don't grep reports by hand.
   - **Held without research** — held positions (excluding index funds and `cash_equivalent` positions) with no covering run: name them plainly ("held with no research run on file") and list them as ready-to-run `/research` questions. The snapshot knows *what* is held; this flags *what lacks a why*.
   - **Stale research** — held names whose most recent covering run is >6 months old (run date = the report dir's date prefix). Give the run, its date, and its Market verdict for the name, and suggest a refresh `/research` question.
   - **Recent activity** — notable buys/sells/dividends/deposits from the lookback window, cross-linked to `/journal` entries where they exist.
   - If the user gave a focus hint, weight the review toward it. State "unknown" where the snapshot doesn't answer something; never fill gaps fluently.

   In review-only mode Steps 4–5 run against the committed snapshot; nothing is written.

6. **Ship gate** (sync mode only) — **show the user** the files to be committed: `tracking/brokerage-snapshot.json` and `tracking/ledger.json`. The ledger is a dashboard input, so run `python3 scripts/dashboard_due.py` and keep its one-line decision (per **Git convention** in `CLAUDE.md`). Run `python3 scripts/check_consistency.py --paths tracking/brokerage-snapshot.json tracking/ledger.json`; if it exits 1, show its output with the ship gate (a fetcher bug, not something to patch in the data).

   Ask the **ship gate** (per **Git convention** in `CLAUDE.md`): a single `AskUserQuestion` call with **one** question, "Ship this portfolio sync?", and three options in this order:
   - "Ship it — commit, push, open PR, merge" (Recommended)
   - "Commit, push & open PR — leave it open"
   - "Don't commit"

   Set the `preview` of both shipping options to the review's headline lines (concentration, cash, new ledger activity + cross-check result, held-without-research count) plus the dashboard decision line.

   **Decline path** ("Don't commit"): `git switch main && git branch -D sync-portfolio/<id>` (work returns to the working tree; nothing was pushed); report and stop. Otherwise:
   ```bash
   python3 scripts/dashboard_due.py && python3 scripts/generate_dashboard.py   # regen only when due
   git add tracking/brokerage-snapshot.json tracking/ledger.json   # + dashboard/index.html if regenerated
   git commit -m "sync-portfolio: <YYYY-MM-DD>"
   git push -u origin sync-portfolio/<id>
   gh pr create --base main --head sync-portfolio/<id> --title "sync-portfolio: <YYYY-MM-DD>" --body "<one line: N accounts, M positions>"
   ```
   No second prompt — "Ship it" → `gh pr merge --squash --delete-branch`; otherwise leave it open. Either way finish on `main` (`git switch main`). Any git/`gh` failure: report and stop, no destructive retry.

7. **Report back** — mode used, snapshot age, accounts/positions synced, new ledger activity and the cross-check result, the dashboard decision, and the PR URL if opened.

## Security notes

- The `consumerKey` is a Personal API key with full read access to the connected brokerage. Never print, echo, or commit it; never copy it out of `.env`/env. The script signs requests locally with it.
- The snapshot and the ledger contain personal financial data — they belong in the private prism repo only. In prism-shared, `tracking/brokerage-snapshot.json` and `tracking/ledger.json` stay empty templates (same rule as every other tracking file).

## Examples

`/sync-portfolio`
→ creds found → branch `sync-portfolio/2026-07-12-091500` → fetch + ledger merge (14 new activities, reconciles with holdings) → review: 61% top-3 concentration, 4% cash, 3 names held with no research run on file (→ ready-to-run `/research`), NOW's last research 14 months old → ship gate → commit + PR.

`/sync-portfolio concentration`
→ same flow; the review leads with concentration and position sizing.

`/sync-portfolio` (on a phone / cloud sandbox without the keys)
→ review-only: "Snapshot is 6 days old (fetched 2026-07-06)." → same ledger check + review off the committed files; no branch, no PR.
