---
description: Create and manage hypothetical "what-if" portfolio scenarios — counterfactual trade substitutions, standalone hypothetical portfolios, and single-ticker benchmarks — stored in tracking/hypotheticals.json and overlaid on the dashboard P&L chart. Also handles archive / reactivate / rename by natural language. Lands on its own whatif/<YYYY-MM-DD>-<NNN> branch + PR.
---

Create or manage a **what-if scenario** — a hypothetical portfolio computed from daily closes and overlaid on the dashboard's P&L value chart next to the actual portfolio. This is counterfactual comparison of concrete trade histories ("what if I'd bought AMD instead of NVDA?", "what if I'd just DCA'd into SPY?"), **not** strategy backtesting — no rebalancing rules, no dividends, no intraday fills. Directional sanity-checking, not precise P&L.

A what-if run does **no** research and writes **no** Notion. It edits `tracking/hypotheticals.json` (schema in `tracking/README.md`), optionally regenerates the dashboard (user-gated at commit time), and lands on its own branch + PR (same git flow as `/journal`).

Everything the user typed after `/what-if` is either a **new scenario description** (the default) or a **management request** — archive, reactivate, rename, or delete-flavored phrasings ("get rid of", "bring back", "call it X instead"). Detect by intent, not keywords. If nothing was typed, ask what scenario they'd like to explore.

## Steps

1. **Sync with main and resolve today's date** — do this before anything else:
   - **Clean-tree guard**: run `git status --porcelain`. If it shows any changes, stop and report; never stash or sweep them in.
   - Run `git switch main && git pull --ff-only origin main`. On any failure, report and stop — never stash, reset, or force.
   - **Resolve today's date from the system clock** (`date +%Y-%m-%d`) — never rely on a date carried in context. Use it everywhere below (entry `id`, `created`, branch name, `last_updated`).

2. **Classify the request** — creation or management.

   **Management** (archive / reactivate / rename): match the referenced scenario against `tracking/hypotheticals.json` by **name substring first, then exact `id`** (case-insensitive). If nothing matches, list the existing scenarios and ask. If more than one matches, ask which via `AskUserQuestion`. Then skip to step 6 with the single edit:
   - **archive** — set `status: "archived"`. Everything else (including `color_index`) stays.
   - **reactivate** — set `status: "active"`, keeping the original `color_index` so the line comes back in its old color.
   - **rename** — change `name` only. The `id` is immutable (same stability rule as event-entry IDs).
   - If the user asks to **delete** a scenario, explain that archiving keeps the record and is preferred; hard-delete only if they insist after that.

   **Creation**: continue with step 3.

3. **Parse the scenario into one of three types** (schema details in `tracking/README.md`):

   - **`substitute`** — "what if instead of buying X I'd bought Y". Read `tracking/trades.json` and identify the referenced trades. **Prefer a ticker-level override** `{ "from": "NVDA", "to": "AMD" }`, which swaps the whole chain (buys *and* later sells stay consistent). Use a per-trade override `{ "trade_id": "...", "ticker": "..." }` only when the user singles out one specific trade — and warn them that any later sell of the original ticker will then exceed its remaining buys and drop that ticker from the chart. If the referenced trade/ticker isn't in `trades.json`, say so and stop.
   - **`benchmark`** — "what if I'd put the same money into SPY/QQQ/…" → `benchmark_ticker`. Every actual trade is re-executed into that one ticker, same dates and amounts.
   - **`standalone`** — an explicit hypothetical trade list with no reference to the actual log. Expand recurring phrasings ("$500/month into QQQ since March") into explicit dated trades — one per period on the same day-of-month (clamped to month end), absolute dates, each `{ date, ticker, action, amount_usd }`. Do **not** compute shares or prices — the dashboard script derives them from daily closes.

   **Derive a short display name** from the scenario (e.g. `"AMD instead of NVDA (2026-03-02)"`, `"SPY benchmark"`, `"QQQ DCA $500/mo"`) and confirm it with the user along with a one-line restatement of what will be simulated. This name is what the chart legend shows — keep it under ~40 chars.

4. **Compute the ID, color, and branch — then cut the branch** (per the **Git convention** in `CLAUDE.md`):
   - Read `tracking/hypotheticals.json` (if absent, treat as zero scenarios). `NNN` = count of scenarios with `created == <today>` + 1, zero-padded (`001`). For **management** runs, or if branch `whatif/<YYYY-MM-DD>-<NNN>` already exists, bump `NNN` until the branch name is free (`git branch --list` + `git ls-remote --heads origin`).
   - Entry `id` = `whatif-<YYYYMMDD>-<NNN>`; branch = `whatif/<YYYY-MM-DD>-<NNN>`.
   - `color_index` = the lowest value in 0–3 not used by any currently **active** scenario (fall back to `count of active % 4`). Once written it never changes — colors follow the scenario, not its position.

   ```bash
   git switch -c whatif/<YYYY-MM-DD>-<NNN>
   ```
   If the branch step fails, report and stop. Everything below happens on this branch.

5. **Write and preview** — append the scenario to `tracking/hypotheticals.json` (initialize the file as `{ "last_updated": "<today>", "scenarios": [] }` if missing), set `status: "active"`, update `last_updated`. Then run the preview:

   ```bash
   python3 scripts/generate_dashboard.py --whatif-preview <id>
   ```

   This fetches only the needed price history (cached per day in the gitignored `tracking/price-cache.json`), prints the scenario's value / invested / P&L — and, for `substitute`/`benchmark`, the P&L delta vs the actual portfolio — and writes **no** HTML. **Show the user the output.**
   - If it prints warnings (e.g. `no price history` — likely a bad or delisted ticker), surface them and ask whether to fix the scenario (edit the JSON and re-preview) or abandon (take the decline path in step 6).
   - If the count of active scenarios is now above 4, note that the chart palette has 4 slots so colors will repeat, and suggest archiving an old scenario.

6. **Commit & PR** — show the user the file(s) to be committed (`tracking/hypotheticals.json`; `dashboard/index.html` only if regenerated). Ask via `AskUserQuestion` with three questions:
   - "Commit and push this what-if scenario?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - "Regenerate the dashboard? (fetches live prices — slow on a cold cache)" (Yes / No)

   **If the user opted to regenerate**, run `python3 scripts/generate_dashboard.py` now, before staging (the scenario renders as a dashed overlay line on the P&L chart). **If the user declines the commit**, return to `main` and drop the branch: `git switch main && git branch -D whatif/<YYYY-MM-DD>-<NNN>` (the edit stays in the working tree; nothing was pushed). Report and stop.

   If approved:
   ```bash
   git add tracking/hypotheticals.json   # + dashboard/index.html if regenerated
   git commit -m "whatif: <name or action> (<id>)"     # e.g. "whatif: AMD instead of NVDA (whatif-20260711-001)" or "whatif: archive SPY benchmark (whatif-20260705-001)"
   git push -u origin whatif/<YYYY-MM-DD>-<NNN>
   ```
   If the PR was approved:
   ```bash
   gh pr create --base main --head whatif/<YYYY-MM-DD>-<NNN> --title "whatif: <name or action>" --body "<one line: what the scenario simulates, or what changed>"
   ```
   After the PR is open, ask via `AskUserQuestion`: "Merge this PR now?" (Yes / No). If yes, `gh pr merge --squash --delete-branch`; if no, leave it open. Either way finish on `main` (`git switch main`). Any git/`gh` failure: report and stop, no destructive retry.

7. **Report back** — the scenario `id` and name (or the management action), the preview numbers, the PR URL if opened, and a reminder that the scenario appears on the dashboard P&L chart after the next regeneration if they skipped it.

## Examples

`/what-if I'd bought AMD instead of NVDA back in March`
→ reads trades.json, finds the NVDA trades → `substitute` with `{from: NVDA, to: AMD}` → name `"AMD instead of NVDA (2026-03-02)"` confirmed → branch `whatif/2026-07-11-001` → write + preview (`value $5,281 | P&L +$3,887 | vs actual P&L +$2,447`) → commit/PR questions.

`/what-if just track what putting the same money into SPY would have done`
→ `benchmark` with `benchmark_ticker: "SPY"` → same flow.

`/what-if a portfolio that DCAs $500 into QQQ on the 1st of every month since March`
→ `standalone` with four explicit dated trades expanded from the DCA phrasing.

`/what-if archive the SPY one` · `/what-if rename whatif-20260711-001 to "AMD swap"` · `/what-if bring back the QQQ DCA scenario`
→ management: match by name/id, single-field edit, same branch + PR flow.

## Handling missing input

- Empty `/what-if`: ask what scenario to explore before doing anything.
- A substitute referencing trades that don't exist in `trades.json`: report what *is* in the log and stop — never invent trades.
- A hypothetical ticker with no price history at preview time: surface the warning and let the user fix or abandon; never silently commit a scenario that can't render.
