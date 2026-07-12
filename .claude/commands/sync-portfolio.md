---
description: Pull the live brokerage state (accounts, balances, positions, recent activity) from SnapTrade into tracking/brokerage-snapshot.json, reconcile it against portfolio.json / candidates.json / trades.json, and review the portfolio (concentration, cash, P&L, held-without-thesis, active falsifiers). Degrades to a review-only pass over the committed snapshot when SnapTrade credentials are unavailable (e.g. a cloud sandbox without the keys). Lands on its own sync-portfolio/<timestamp> branch + PR.
---

Sync the user's **actual brokerage state** into Prism and review it. SnapTrade (Personal API key) is the source of truth for what is *really* held; `tracking/portfolio.json` and `tracking/trades.json` are what Prism *believes*. This command closes the gap and then reads the portfolio critically — for the user's own thinking, no advice boilerplate.

Two modes, chosen automatically by capability, never by asking:

- **Sync mode** — `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` are available (environment or `<repo-root>/.env`): fetch live, reconcile, review, commit on a branch + PR.
- **Review-only mode** — credentials absent (typical for a cloud sandbox that doesn't carry the keys): no fetch, no writes, no git. Read the committed `tracking/brokerage-snapshot.json`, lead with a staleness warning derived from its `fetched_at`, and run the same review + reconciliation *report* (Step 5–6) against it. If the snapshot is still the empty template (`fetched_at: null`), stop: `"No brokerage snapshot yet and SnapTrade credentials are unavailable here — run /sync-portfolio from an environment with SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY configured."`

The SnapTrade Fidelity integration is **read-only** (no trade placement); this command never mutates anything at the brokerage. Everything after `/sync-portfolio` is an optional focus hint (e.g. `/sync-portfolio concentration` or `/sync-portfolio just the reconciliation`) — honor it in the review; with no hint, run the full review.

## Steps

1. **Detect mode** — check for the credentials *without* printing them: look for non-empty `SNAPTRADE_CLIENT_ID` and `SNAPTRADE_CONSUMER_KEY` in the environment, else as assignments in `<repo-root>/.env` (e.g. `grep -c '^SNAPTRADE_CLIENT_ID=..*' .env`). Never echo, cat, or log the key values. Both present → sync mode (continue with Step 2). Either missing → review-only mode (skip to Step 5, prefixing the review with the staleness warning).

2. **Sync with main and cut the branch** — per the **Git convention** in `CLAUDE.md`, before any work:
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
   - Exit 0 → snapshot written; surface any warnings the script printed (a degraded sub-source is worth the user knowing about, not worth stopping for).
   - Exit 2 (credentials missing after all — e.g. empty values) → drop back cleanly (`git switch main && git branch -D sync-portfolio/<id>`) and continue in review-only mode from the committed snapshot.
   - Exit 1 (credentials rejected / network / no accounts) → report the script's stderr, clean up the branch the same way, and stop.

4. **Reconcile** — diff the fresh snapshot against Prism's tracking. Build a numbered list of proposed fixes; **do not write anything yet**:

   - **Held but untracked** — a snapshot position whose ticker is in neither `portfolio.json` nor `candidates.json` → propose adding to `portfolio.json`: `{ "ticker": "<T>", "reports": [], "events": [] }`.
   - **Held but filed as candidate** — ticker present in `candidates.json` → propose moving the entry (with its `reports[]`/`events[]`) into `portfolio.json` and deleting it from candidates.
   - **Tracked but not held** — a `portfolio.json` position absent from the snapshot → propose removing it (position exited). Flag, don't assume: a transfer or a sub-$1 residual can look like an exit.
   - **Unlogged trades** — snapshot activities of type buy/sell with no matching `trades.json` entry (match on ticker + date ±1 day + direction). **Never write `trades.json` here** — it mirrors the Notion Investment Log 1:1 and is owned by `/log-trade`; list the misses and suggest `/log-trade` per trade instead.
   - **Share-count drift** — ticker in both, but snapshot units disagree materially with what `trades.json` history implies → report the delta (informational; the snapshot is the truth).

   Present the write-fixes (first three categories) as a numbered list and ask which to apply — `"1,3"`, `"all"`, or `"none"` — then apply the approved ones on the branch, updating each touched file's `last_updated`. If there are no proposed fixes, say `"Tracking is in sync with the brokerage."` and move on.

5. **Review** — the chat-facing product. Read the snapshot (plus `portfolio.json` reports/events and recent `reports/` runs) and write a terse, falsifiable review — Prism voice, investor stance (multi-year holder), no disclaimers:

   - **Shape** — total value, cash %, position count, top-3 concentration (% of equity value). Call out single-position concentration explicitly when one name dominates.
   - **Per-position** — units, market value, open P&L vs average cost. Order by weight.
   - **Held without a thesis** — positions whose ticker has no `reports[]` entry in `portfolio.json`: name them plainly ("held with no research run on file") and list them as ready-to-run `/research` questions.
   - **Active events on held names** — surface `events[]` with `status: "active"` (buy triggers, falsifiers, monitors) for held tickers; flag any where recent snapshot activity or price is near the condition.
   - **Thesis staleness** — held names whose latest `reports[]` entry is >6 months old.
   - **Recent activity** — notable buys/sells/dividends/deposits from the lookback window, cross-linked to `trades.json`/journal entries where they exist.
   - If the user gave a focus hint, weight the review toward it. State "unknown" where the snapshot doesn't answer something; never fill gaps fluently.

   In review-only mode this step runs against the committed snapshot and the reconciliation findings from Step 4's logic are **reported only** (no numbered apply-prompt, no writes) — end with a note that fixes require a sync-mode run.

6. **Commit & PR** (sync mode only) — **show the user** the files to be committed: `tracking/brokerage-snapshot.json`, plus any tracking files changed in Step 4. Ask via `AskUserQuestion`:
   - "Commit and push this portfolio sync?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - Only if Step 4 changed `portfolio.json` or `candidates.json` (dashboard inputs): "Regenerate the dashboard? (fetches live prices via yfinance — slow)" (Yes / No)

   If regen approved: run `python3 scripts/generate_dashboard.py` now and stage `dashboard/index.html` with the run. **Decline path**: `git switch main && git branch -D sync-portfolio/<id>` (work returns to the working tree; nothing was pushed); report and stop. Otherwise:
   ```bash
   git add tracking/brokerage-snapshot.json   # + portfolio.json / candidates.json if fixed; + dashboard/index.html if regenerated
   git commit -m "sync-portfolio: <YYYY-MM-DD>"
   git push -u origin sync-portfolio/<id>
   ```
   If the PR was approved:
   ```bash
   gh pr create --base main --head sync-portfolio/<id> --title "sync-portfolio: <YYYY-MM-DD>" --body "<one line: N accounts, M positions, fixes applied if any>"
   ```
   After the PR is open, ask via `AskUserQuestion`: "Merge this PR now?" (Yes / No). Yes → `gh pr merge --squash --delete-branch`; no → leave it open. Either way finish on `main` (`git switch main`). Any git/`gh` failure: report and stop, no destructive retry.

7. **Report back** — mode used, snapshot age, accounts/positions synced, fixes applied (or "reported only"), unlogged trades suggested for `/log-trade`, and the PR URL if opened.

## Security notes

- The `consumerKey` is a Personal API key with full read access to the connected brokerage. Never print, echo, or commit it; never copy it out of `.env`/env. The script signs requests locally with it.
- The snapshot itself contains personal financial data — it belongs in the private prism repo only. In prism-shared, `tracking/brokerage-snapshot.json` stays the empty template (same rule as every other tracking file).

## Examples

`/sync-portfolio`
→ creds found → branch `sync-portfolio/2026-07-12-091500` → fetch → 2 fixes proposed (VRT held but untracked; SNAP tracked but not held), user applies both → review: 61% top-3 concentration, 4% cash, NOW held 14 months since last thesis check → AskUserQuestion → commit + PR.

`/sync-portfolio concentration`
→ same flow; the review leads with concentration and position sizing.

`/sync-portfolio` (on a phone / cloud sandbox without the keys)
→ review-only: "Snapshot is 6 days old (fetched 2026-07-06)." → same review off the committed snapshot; reconciliation differences reported but not applied; no branch, no PR.
