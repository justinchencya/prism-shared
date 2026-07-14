---
description: Log an investment action to the Notion Investment Log database. Inserts a new row with date, ticker, action, amount, shares, price, and optional comment. Moves an existing thesis between tracking/candidates.json and tracking/positions-thesis.json when a watched name is bought or a held name is fully exited (it never writes a bare holding stub — holdings live in the brokerage snapshot). Links the trade to related research runs, writes the link as a hyperlink to the Notion Comment field, and records everything in tracking/trades.json.
---

Log a new row to the **Investment Log** Notion database, then update the local tracking files and link to related research.

## Database config (from environment)

The Investment Log identifiers are read from the environment so the engine carries no personal IDs. Resolve them once, up front, by reading the env (e.g. `printenv NOTION_INVESTMENT_LOG_DATA_SOURCE_ID NOTION_INVESTMENT_LOG_DATABASE_ID`):

- **`data_source_id`** (use this for query / create / schema calls): from `NOTION_INVESTMENT_LOG_DATA_SOURCE_ID`
- **`database_id`** (parent container, rarely needed): from `NOTION_INVESTMENT_LOG_DATABASE_ID`

**Resolution / graceful degradation**, in order:
1. If `NOTION_INVESTMENT_LOG_DATA_SOURCE_ID` is set, use it.
2. If it is unset but `NOTION_TOKEN` is configured, resolve the data source by searching Notion for one named "Investment Log" and use that.
3. If neither resolves (no env ID, no token, or the search finds nothing), **Notion is unconfigured** — skip the Notion insert in Step 5 entirely, record the trade in the local tracking files only (Steps 7–8) with `notion_page_id`/`notion_url` set to `null`, and note in the final reply that Notion mirroring was skipped. Never abort the trade log over a missing Notion config.

- **Schema**:
  - `Ticker` — title (required)
  - `date` — date (default: today)
  - `Action` — select: `buy` / `add` / `trim` / `sell` (required)
  - `Amount` — number in USD, **signed by direction** (required): **positive for `buy`/`add`** (cash out), **negative for `sell`/`trim`** (cash back). This log tracks net money invested, so cash-back actions are negative. The parsed `amount` (Step 2) is always the absolute value; the sign is applied only when writing this field — write `+amount` for buys/adds, `-amount` for sells/trims. `Shares` and `Price` stay positive regardless of direction.
  - `Shares` — number: share/unit count (coin quantity for crypto). Optional.
  - `Price` — number in USD: price per share / per coin. Optional.
  - `Comment` — rich text (optional)

## Steps

1. **Sync with main and resolve today's date** — do this before anything else:
   - Run a clean-tree guard: `git status --porcelain` — if it shows changes unrelated to this run, stop and report; never stash or sweep them in.
   - Run `git switch main && git pull --ff-only origin main` so the tracking files (`tracking/*.json`) and `reports/` reflect the latest merged research runs. If the switch or pull fails (uncommitted changes in the way, network error, non-fast-forward), report the error and stop — never stash, reset, or force anything.
   - **Resolve today's date from the system clock** by running `date +%Y-%m-%d` — never rely on a date carried in context, which may be stale. Use this value as "today" everywhere below (the `date` field default, `linked_at`, the trade `id` date segment, and `last_updated`).

2. **Parse the input** — everything after `/log-trade` is the user's natural-language description. Extract:
   - `ticker`: the stock ticker symbol (e.g. NVDA, AMZN)
   - `amount`: the dollar amount as an absolute value (e.g. 1317.70)
   - `action_type`: parse from the description verb — `"buy"` / `"add"` / `"trim"` / `"sell"`
   - `date`: if mentioned, parse to ISO 8601 (YYYY-MM-DD); otherwise use today's date
   - `comment`: only populated when the user explicitly requests a note — e.g. "add to comment...", "note: ...", "comment: ...". The comment text is whatever the user stated after that explicit signal. If the user did not explicitly call out a comment, `comment` is absent. Never extract action words, price details, or any other part of the trade description as a comment.
   - `price_per_share`: if the user mentions a per-share price (e.g. "at $135", "@ 135", "$135/share"), extract it as a float. Otherwise `null`.
   - `shares`: if `price_per_share` is extracted and `amount` is known, compute `shares = amount / price_per_share` (round to 4 decimal places). If the user mentions share count directly (e.g. "10 shares"), extract that instead. Otherwise `null`.

   **Then cut the branch off `main`** — the ticker is now known, so the branch name is fixed. You are on freshly-synced `main` from Step 1 with a clean tree; cut the branch here, **before** any Notion or tracking write, so the whole run lands on its own branch (per the **Git convention** in `CLAUDE.md`):

   ```bash
   git switch -c log-trade/<date>-<slug>      # <date>=today; <slug>=ticker, or hyphen-joined tickers when several were logged (e.g. log-trade/2026-06-22-SIVEF)
   ```

   If the branch step fails, report and stop. Everything below — Notion insert, tracking edits, dashboard regen — happens on this branch.

3. **Confirm before proceeding** — display the parsed fields as a one-line summary:
   ```
   Logging: [date] | [TICKER] | $[amount] | action: [action_type] | price: $[price_per_share or "(none)"] | comment: [value or "(none)"]
   ```
   Then proceed immediately (no need to wait for explicit confirmation unless a required field is missing).

4. **Link to research** — find research runs that covered this ticker and ask the user to select which informed this trade. Do this before inserting to Notion so the link can be included in a single write.

   **Finding relevant runs**:
   - Read `tracking/positions-thesis.json` and `tracking/candidates.json`. If the ticker entry exists, its `reports[]` array lists past research runs with `run`, `date`, and `verdict` fields — use these as the primary source.
   - Additionally scan `reports/` directories: for each subdirectory, check if `reports/<run-dir>/tickers/<TICKER>.md` exists. This catches tickers researched (e.g. with an Avoid verdict) that were never added to the thesis overlay or candidates.
   - Deduplicate (positions-thesis.json and the file scan may overlap). Sort by date descending. Cap at 10 most recent.

   **If no runs found**:
   Display: `"No research runs found for [TICKER]."`
   Ask: `"Log trade without a research link? (y/n):"`
   - If yes: set `linked_research: []` and continue.
   - If no: stop here. Suggest the user run `/research` first.

   **If runs found**, display a compact numbered list:
   ```
   Research runs mentioning [TICKER]:
     1. 2026-06-07 | ai-semi-dip-fed-spacex             | verdict: Buy
     2. 2026-06-07 | whitehouse-ai-semiconductor-invest  | verdict: Buy
     3. 2026-05-31 | mobile-agentic-cloud-stack          | verdict: Hold
   Link to research (e.g. "1,3"), "all", or "none":
   ```
   - `verdict` comes from the `reports[]` entry in positions-thesis.json/candidates.json. For runs found only via file scan (not in tracking), show `verdict: (untracked)`.
   - If more than 10 runs exist, note `"...and N older runs (enter 'all' to include all)"`.

   **Parse the response**:
   - Numbers like `"1,3"` or `"1 3"` → link those runs.
   - `"all"` → link all displayed runs.
   - `"none"` or `"n"` → `linked_research: []`.

   **Build `linked_research` entries**: for each selected run, construct:
   ```json
   {
     "run": "<run-dir-name e.g. 2026-06-07-ai-semi-dip-fed-spacex>",
     "date": "<YYYY-MM-DD>",
     "report_path": "reports/<run-dir>/tickers/<TICKER>.md",  // ticker symbol exactly as given, preserving hyphens (e.g. BRK-B.md)
     "market_verdict": "<verdict from reports[] or null if untracked>",
     "linked_at": "<today YYYY-MM-DD>"
   }
   ```

5. **Insert the row to Notion** — **if Notion is unconfigured** (per *Database config* above), skip this entire step: set `notion_page_id` and `notion_url` to `null` and continue to Step 7. Otherwise create a page in the data source with these properties:
   - `Ticker` (title), `date`, `Action` (select — the `action_type`), `Amount` (**signed**: `+amount` for `buy`/`add`, `-amount` for `sell`/`trim` — the log tracks net money invested, so cash-back actions are negative), `Shares` (number, or omit if null), `Price` (number = `price_per_share`, or omit if null).

   Build the `Comment` rich text from what is now known:

   - If no research was linked and no free-text comment: omit `Comment` entirely.
   - If free-text comment only: set `Comment` to a plain text block with that text.
   - If research linked: build `Comment` as rich text — start with the free-text comment block + `" · "` separator if a comment exists, then one `"Research"` hyperlink block per linked run (link URL: `"<repo-url>/tree/main/reports/<run-slug>"`), with `", "` plain text blocks separating multiple links. Derive `<repo-url>` from the repo's own remote — run `git remote get-url origin` and normalize to its `https://github.com/<owner>/<repo>` form (strip any trailing `.git`, convert an `git@github.com:` SSH URL to https) — so links point at whichever fork is in use, not a hardcoded account.

   Example `Comment` for one research link (no free-text comment):
   ```json
   [
     { "type": "text", "text": { "content": "Research", "link": { "url": "<repo-url>/tree/main/reports/2026-06-07-ai-semi-dip-fed-spacex" } } }
   ]
   ```

   Example for two research links:
   ```json
   [
     { "type": "text", "text": { "content": "Research", "link": { "url": "https://github.com/.../reports/2026-06-07-ai-semi-dip-fed-spacex" } } },
     { "type": "text", "text": { "content": ", " } },
     { "type": "text", "text": { "content": "Research", "link": { "url": "https://github.com/.../reports/2026-06-07-whitehouse-ai-semiconductor-invest" } } }
   ]
   ```

   Save the `notion_page_id` and `notion_url` from the API response.

6. **Confirm success** — reply with the Notion page URL.

7. **Update the thesis overlay** — `tracking/positions-thesis.json` is the thesis overlay (held names' `reports[]`/`events[]`), **not** a holdings ledger — the actual holding is captured by the next `/sync-portfolio` snapshot, so a buy never needs a bare membership stub. Only *move an existing thesis* when one exists. Read `tracking/positions-thesis.json` and `tracking/candidates.json`, then apply based on `action_type`:

   **If `action_type` is `"buy"` or `"add"`** (initiating or adding to a position):
   - Check if ticker already has an entry in `positions-thesis.json`.
   - **If not in positions-thesis**: check if it exists in `candidates.json`.
     - If in candidates: remove the entry from `candidates.json`; add it to `positions-thesis.json` copying over the candidate's `reports[]` and `events[]` arrays (a real thesis moves from the watch overlay to the held overlay). Report: "Moved [TICKER] thesis from candidates → positions-thesis."
     - If not in candidates: **do not create an entry** — there's no thesis yet, and an empty stub is exactly what the overlay design drops. Report: "Logged [TICKER] buy — no thesis on file; run /research to build one (it'll also surface in /sync-portfolio's held-without-thesis review)."
   - **If already in positions-thesis**: no change needed. Report: "Added to existing [TICKER] position (thesis on file)."

   **If `action_type` is `"sell"`** (full exit — use only when completely exiting):
   - If the ticker has a `positions-thesis.json` entry, offer to prune it (its thesis is now on an exited name) or keep it as a closed-position record. Report the choice made.
   - If not found in positions-thesis, report it and skip.

   **If `action_type` is `"trim"`**: no overlay changes needed — partial sales don't change thesis coverage.

   Always update `last_updated` on any file modified.

8. **Write to `tracking/trades.json`** — persist the trade record with research linkage.

   If `tracking/trades.json` does not exist, initialize it as:
   ```json
   { "last_updated": "<today>", "trades": [] }
   ```

   Generate a trade `id`: scan existing trades for entries where `ticker == <TICKER>` and `date == <today>`. Count them; the new id is `trade-<TICKER>-<YYYYMMDD>-<zero-padded-seq starting at 001>`.

   Append this object to `trades`:
   ```json
   {
     "id": "trade-AVGO-20260607-001",
     "date": "<YYYY-MM-DD>",
     "ticker": "<TICKER>",
     "action": "<action_type>",
     "amount_usd": <amount>,
     "shares": <shares or null>,
     "price_per_share": <price_per_share or null>,
     "comment": "<comment or null>",
     "notion_page_id": "<id from Step 5>",
     "notion_url": "<url from Step 5>",
     "linked_research": [ /* from Step 4 */ ]
   }
   ```

   `amount_usd` is the **positive magnitude** (unsigned) regardless of `action` — direction lives in the `action` field, and `scripts/generate_dashboard.py` derives the sign from it. Only the Notion `Amount` (Step 5) is signed. Never write a negative `amount_usd`.

   Update `last_updated` to today. Write the file. Report: `"Logged to tracking/trades.json (ID: trade-AVGO-20260607-001)."`

9. **Dashboard regeneration is optional** — regenerating runs `python3 scripts/generate_dashboard.py`, which fetches live prices via yfinance (slow / network-heavy), so it is **not** run automatically. It is gated on the user's choice in Step 10 — don't run it here.

10. **Commit & PR** — the work is already on the `log-trade/<date>-<slug>` branch cut in Step 2. **Show the user** the files that would be committed (`tracking/trades.json`, plus `tracking/positions-thesis.json` and/or `tracking/candidates.json` if Step 7 moved a thesis). Note that `dashboard/index.html` is included only if the user opts to regenerate it.

    Ask for permission using `AskUserQuestion` with three questions:
    - "Commit and push this trade log?" (Yes / No)
    - "Open a pull request on GitHub?" (Yes / No)
    - "Regenerate the dashboard? (fetches live prices via yfinance — slow)" (Yes / No)

    Wait for the answers. **If the user opted to regenerate**, run `python3 scripts/generate_dashboard.py` from the repo root now, before staging. Report success or any error output. **If the user declines the commit**, return to `main` and drop the branch: `git switch main && git branch -D log-trade/<date>-<slug>` (the tracking edits remain in the working tree; the Notion row written in Step 5 stays — only the branch is discarded). Report and stop.

    If the user approves the commit, run (the branch already exists — just stage, commit, push). Include `dashboard/index.html` in the `git add` **only if it was regenerated** in Step 10:
    ```bash
    cd <repo-root>
    git add tracking/trades.json   # + dashboard/index.html if regenerated; + tracking/positions-thesis.json and/or tracking/candidates.json if modified in Step 7
    git commit -m "log-trade: <ticker> <action> <date> (<id>)"
    git push -u origin log-trade/<date>-<slug>
    ```

    If the user also approved the PR, run:
    ```bash
    gh pr create --base main --head log-trade/<date>-<slug> --title "log-trade: <ticker> <action> <date>" --body "<one line: ticker, action, amount, linked runs>"
    ```

    After the PR is open, **ask the user** via `AskUserQuestion`: "Merge this PR now?" (Yes / No). If yes, run `gh pr merge --squash --delete-branch`; if no, leave it open. Either way, finish on `main`:
    ```bash
    git switch main
    ```

    The remote is named `origin`. `dashboard/index.html` is tracked and committed with the trade only if the user opted to regenerate it. If any git/`gh` step fails, report the error and stop — no destructive retry.

## Handling missing fields

- If `ticker` is missing: ask for it before proceeding.
- If `amount` is missing: ask for it before proceeding.
- If `action_type` is ambiguous: default to `"buy"`, apply no tracking change and note the ambiguity.
- If `date` is ambiguous: default to today.

## Examples

`/log-trade bought NVDA at $135, $2700 total`
→ Parse: ticker=NVDA, action=buy, amount=2700, price_per_share=135, shares=20
→ Research linking prompt shown; user picks a run
→ Notion row inserted with Comment="Research" hyperlinked to the run; trades.json appended
→ NVDA already in portfolio — no tracking change

`/log-trade initiated PGR $3600`
→ Parse: ticker=PGR, action=buy, amount=3600
→ Research linking prompt shown; user picks or skips
→ Notion row inserted (with or without Comment depending on selection)
→ PGR found in candidates.json → moved to portfolio; trades.json appended

`/log-trade sold all VSAT $3200`
→ Parse: ticker=VSAT, action=sell, amount=3200
→ Research linking prompt shown (exits can also be linked)
→ Notion row inserted with Action=sell, Amount=-3200 (negative — cash back); VSAT's thesis in positions-thesis.json offered for pruning (exited name); trades.json appended with amount_usd=3200 (unsigned magnitude)

`/log-trade May 30 — trimmed CRM $500`
→ Parse: ticker=CRM, action=trim, amount=500, date=2026-05-30
→ Research linking prompt shown; trim: no portfolio tracking change; Notion Action=trim, Amount=-500 (negative — partial cash back); trades.json amount_usd=500 (unsigned magnitude)

`/log-trade bought AVGO at $385, $3850 total`
→ Parse: ticker=AVGO, action=buy, amount=3850, price_per_share=385, shares=10
→ Research runs shown for AVGO; user selects "1,2"
→ Notion inserted with Comment: "Research, Research" (each hyperlinked to its run)
→ trades.json: trade-AVGO-20260607-001 with linked_research array (2 entries)
