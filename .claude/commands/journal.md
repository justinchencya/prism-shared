---
description: Capture a free-form reflection, hesitation, imagined scenario, or road-not-taken decision into the Prism timeline. Always asks whether to link related research runs and tickers (portfolio / candidate / add-to-candidate), suggesting matches when possible. Appends to tracking/journal.json, optionally regenerates the dashboard (user-gated at commit time), and lands the entry on its own journal/<id> branch + PR.
---

Capture a **journal entry** — a reflection, hesitation, imagined scenario, or a decision you could have made but didn't — into the Prism timeline so you can look back and reflect on your thinking process. This is the user's own thinking log; not advice, no boilerplate.

A journal entry does **no** research and writes **no** Notion. It appends to `tracking/journal.json`, optionally linking research runs and tickers, optionally regenerates the dashboard (only if the user opts in at commit time), and lands on its own branch + PR (same git flow as `/scout`).

Everything the user typed after `/journal` is the **raw reflection** — a seed to refine, not the final entry. You help the user turn their rough thought into clearer prose before it's stored (e.g. `/journal hesitated on adding to NVDA before earnings — worried the AI-capex narrative was already fully priced`). If the user typed nothing after `/journal`, ask them what they want to record before proceeding — never write an empty entry.

## Steps

1. **Sync with main and resolve today's date** — do this before anything else:
   - **Clean-tree guard**: run `git status --porcelain`. Nothing has been written yet, so the tree should be clean — if it shows any changes, stop and report; never stash or sweep them in.
   - Run `git switch main && git pull --ff-only origin main` so `tracking/*.json` and `reports/` reflect the latest merged runs. If the switch or pull fails (uncommitted changes in the way, network error, non-fast-forward), report the error and stop — never stash, reset, or force anything.
   - **Resolve today's date from the system clock** by running `date +%Y-%m-%d` — never rely on a date carried in context, which may be stale. Use this value as "today" everywhere below (the entry `date`, `linked_at`, the entry `id` date segment, the branch name, `added_date` for any new candidate, and `last_updated`).

2. **Refine the reflection, then cut the branch** — take everything after `/journal` as the **raw reflection** (if empty, ask `"What would you like to record?"` and use the reply). Treat it as raw material, not the final entry text.

   **Refine it into clearer prose:**
   - Strip transcription artifacts and filler ("um", "like", "you know", "I mean"), false starts, and repetition.
   - Tighten rambling into clear, readable sentences with a natural flow.
   - **Preserve the user's voice, meaning, and stance.** Keep it first-person; keep the hesitations, open questions, uncertainty, and conviction exactly as felt. This is a thinking log, not a polished essay — do **not** add analysis, claims, framing, or conclusions the user didn't make, and do **not** sand off genuine doubt or sharpen a tentative thought into a firm one. When unsure, stay closer to what they said.

   **Show the refined version and confirm** before storing anything:
   ```
   Refined:
   "<refined text>"
   Use this, edit it, or refine again?
   ```
   If the user edits or asks for another pass, iterate until they approve. The approved text becomes the entry body (the `text` field in Step 6). Never store an entry the user hasn't seen in refined form.

   Once the text is approved, **compute the entry sequence and cut the branch off `main`** (per the **Git convention** in `CLAUDE.md`): read `tracking/journal.json` (if absent, treat as zero entries), count entries with `date == <today>`, and set `NNN` = that count + 1 (zero-padded, starting `001`). This fixes both the entry `id` (`journal-<YYYYMMDD>-<NNN>`) and the branch (`journal/<YYYY-MM-DD>-<NNN>`). Cut it now, before any tracking write:

   ```bash
   git switch -c journal/<YYYY-MM-DD>-<NNN>      # e.g. journal/2026-06-22-001
   ```

   If the branch step fails, report and stop. Everything below — `journal.json`, any candidate add, dashboard regen — happens on this branch.

3. **Suggest and confirm research links** — find research runs that might relate, and ask which (if any) to link. Always ask, even if nothing obvious matches.

   **Finding candidate runs**:
   - Scan the reflection text for ticker symbols and topic keywords.
   - Read `tracking/portfolio.json` and `tracking/candidates.json`: any ticker mentioned in the text that has a `reports[]` array contributes those runs.
   - Scan `reports/` directories (most recent first): surface runs whose slug or `question` frontmatter matches a keyword from the text, plus the 5 most recent runs regardless of match (recent thinking is often what a reflection is reacting to).
   - Deduplicate, sort by date descending, cap at 10.

   Present a compact numbered list (mark keyword/ticker matches with `*`):
   ```
   Research runs you might link:
     1. 2026-06-22 | msft-concentration        * MSFT
     2. 2026-06-22 | ai-bottleneck-materials
     3. 2026-06-20 | ai-memory-bottleneck-mu
   Link research (e.g. "1,3"), "all", or "none":
   ```
   - If no runs exist at all, display `"No research runs found."` and skip to step 4.

   **Parse the response**: `"1,3"` / `"1 3"` → those runs · `"all"` → all displayed · `"none"` / `"n"` / empty → no links.

   **Build `linked_research` entries** — for each selected run:
   ```json
   {
     "run": "<run-dir-name e.g. 2026-06-22-msft-concentration>",
     "date": "<YYYY-MM-DD from the run dir name>",
     "report_path": "reports/<run-dir>/final-report.md",
     "linked_at": "<today YYYY-MM-DD>"
   }
   ```

4. **Suggest and confirm ticker links** — find tickers to associate, and ask which (if any) to link. Always ask.

   **Finding candidate tickers**:
   - Extract ticker-like tokens from the reflection text (uppercase 1–5 letter symbols; also resolve obvious company names you recognize to their ticker).
   - Tag each against tracking: `[PORTFOLIO]` if in `portfolio.json`, `[CANDIDATE]` if in `candidates.json`, `[NEW]` otherwise.
   - Also offer any tickers from the research runs the user just linked in step 3.

   Present:
   ```
   Tickers you might link:
     1. MSFT  [PORTFOLIO]
     2. NVDA  [CANDIDATE]
     3. ASTS  [NEW]
   Link tickers (e.g. "1,2"), "all", or "none":
   ```
   - If none detected, ask `"Any tickers to link? (comma-separated symbols, or 'none')"`.

   **Parse the response** the same way (numbers / `all` / `none`). The result is `linked_tickers` — a flat array of symbols, e.g. `["MSFT", "NVDA"]`.

   **Add-to-candidate prompt** — for every linked ticker tagged `[NEW]` (not in portfolio or candidates), ask per ticker:
   `"<TICKER> isn't tracked. Add it to candidates.json? (y/n):"`
   - If yes: append to `candidates.json` `entries[]`: `{ "ticker": "<TICKER>", "added_date": "<today>", "reports": [], "events": [] }`. Update that file's `last_updated`. (The ticker is still recorded in the entry's `linked_tickers` either way — adding to candidates just starts tracking it.)
   - If no: leave it untracked; it stays in `linked_tickers`.

5. **Confirm the entry** — show a one-line summary:
   ```
   Journal [today]: "<first ~80 chars of text>…" | research: [slugs or "none"] | tickers: [symbols or "none"]
   ```
   Proceed immediately (no need to wait for explicit confirmation).

6. **Write to `tracking/journal.json`** — if it does not exist, initialize it as:
   ```json
   { "last_updated": "<today>", "entries": [] }
   ```
   The entry `id` was already fixed in Step 2: `journal-<YYYYMMDD>-<NNN>` (same `NNN` as the branch).

   Append this object to `entries`:
   ```json
   {
     "id": "journal-20260622-001",
     "date": "<YYYY-MM-DD>",
     "text": "<the approved refined reflection from Step 2>",
     "linked_research": [ /* from step 3, [] if none */ ],
     "linked_tickers": [ /* from step 4, [] if none */ ]
   }
   ```
   Update `last_updated` to today. Write the file. Report: `"Logged to tracking/journal.json (ID: journal-20260622-001)."`

7. **Dashboard regeneration is optional** — regenerating runs `python3 scripts/generate_dashboard.py`, which fetches live prices via yfinance (slow / network-heavy), so it is **not** run automatically. It is gated on the user's choice in Step 8 — don't run it here. (When regenerated, the entry appears as a violet node in the Timeline view.)

8. **Commit & PR** — the work is already on the `journal/<YYYY-MM-DD>-<NNN>` branch cut in Step 2. **Show the user** the files that would be committed (`tracking/journal.json`, and `tracking/candidates.json` if a ticker was added). Note that `dashboard/index.html` is included only if the user opts to regenerate it.

   Ask for permission using `AskUserQuestion` with three questions:
   - "Commit and push this journal entry?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - "Regenerate the dashboard? (fetches live prices via yfinance — slow)" (Yes / No)

   Wait for the answers. **If the user opted to regenerate**, run `python3 scripts/generate_dashboard.py` from the repo root now, before staging. Report success or any error output. **If the user declines the commit**, return to `main` and drop the branch: `git switch main && git branch -D journal/<YYYY-MM-DD>-<NNN>` (the entry stays in the working tree; only the branch is discarded). Report and stop.

   If the user approves the commit, run (the branch already exists — just stage, commit, push). Include `dashboard/index.html` in the `git add` **only if it was regenerated**:
   ```bash
   cd <repo-root>
   git add tracking/journal.json   # + dashboard/index.html if regenerated; + tracking/candidates.json if a ticker was added
   git commit -m "journal: <date> (<id>)"
   git push -u origin journal/<YYYY-MM-DD>-<NNN>
   ```

   If the user also approved the PR, run:
   ```bash
   gh pr create --base main --head journal/<YYYY-MM-DD>-<NNN> --title "journal: <date> (<id>)" --body "<one line: first ~80 chars of the reflection + linked tickers/runs>"
   ```

   After the PR is open, **ask the user** via `AskUserQuestion`: "Merge this PR now?" (Yes / No). If yes, run `gh pr merge --squash --delete-branch`; if no, leave it open for the user to merge on GitHub. Either way, finish on `main`:
   ```bash
   git switch main
   ```

   The remote is named `origin`. `dashboard/index.html` is tracked and committed with the entry only if the user opted to regenerate it. If any git/`gh` step fails, report the error and stop — no destructive retry.

9. **Report back** — the entry `id`, the path (`tracking/journal.json`), what was linked, the PR URL (if opened), and a one-line confirmation.

## Handling missing input

- If the raw reflection is empty: ask for it before proceeding (step 2), then refine the reply.
- If the user declines all linking: write the entry with `linked_research: []` and `linked_tickers: []` — a bare reflection is valid.

## Examples

`/journal hesitated on adding to NVDA before earnings — the AI-capex narrative felt fully priced and I didn't want to chase`
→ Refine the rough thought into clearer prose → show it → user approves → cut branch `journal/2026-06-22-001` → suggest runs (NVDA-tagged + recent) and tickers (`NVDA [CANDIDATE]`) → user links a run + NVDA → confirm → append entry `journal-20260622-001` → regen dashboard → commit + PR.

`/journal so um like what if I'd just kept the whole SNAP position instead of rotating into RKLB and NOW, you know, want to revisit this in like 6 months`
→ Refine to e.g. *"What if I'd kept the full SNAP position instead of rotating into RKLB and NOW? Want to revisit in 6 months."* → user approves → tickers detected: `SNAP [NEW]`, `RKLB [PORTFOLIO]`, `NOW [PORTFOLIO]` → user links all three → asked whether to add SNAP to candidates → records the road-not-taken reflection for later review.

`/journal`
→ no text given; the command asks what to record, then refines the reply before running.
