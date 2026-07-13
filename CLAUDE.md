# Prism

Personal deep-research system. Takes a question (with optional sources), decomposes it into a layered research plan, dispatches researcher subagents in parallel, iterates adaptively against director critique, then synthesizes a final report that thinks as a graph rather than as a stack of summaries.

This is for the user's own thinking. Not advice for anyone else. Skip disclaimer boilerplate.

## Architecture

Four agents, three entry points. Plus four utility commands with no agent — `/log-trade` hits Notion directly, `/journal` and `/what-if` write only local tracking, `/sync-portfolio` hits SnapTrade directly. The commands are **independent** — none assumes another ran first. They can be chained, but nothing depends on it.

| Command | Agent(s) | Prompt file(s) |
|---|---|---|
| `/scout <focus>` | scout | `.claude/agents/scout.md` |
| `/research <question>` | research-director + researcher(s) | `.claude/agents/research-director.md`, `researcher.md` |
| `/podcast <run-dir>` | podcast-producer | `.claude/agents/podcast-producer.md` |
| `/log-trade <description>` | — (Notion MCP direct) | `.claude/commands/log-trade.md` |
| `/journal <reflection>` | — (local tracking direct) | `.claude/commands/journal.md` |
| `/what-if <scenario>` | — (local tracking direct) | `.claude/commands/what-if.md` |
| `/sync-portfolio [focus]` | — (SnapTrade direct) | `.claude/commands/sync-portfolio.md` |

The agents' prompts are the rubrics; if output quality drifts, tighten the relevant prompt rather than adding scaffolding.

## Scout flow

0. **Query spec** — the scout agent expands the user's **focus** into `scouts/<timestamp>/queries.json` (GDELT query strings, HN keywords, EDGAR full-text terms + tickers). It also copies the user's curated X follow list from `.claude/scout-x-feeds.json` into `x_handles` (standing accounts, not focus-derived; omitted if that file is absent). The focus sets direction; the agent picks the probes specific enough to spike.
1. **Signals** — `scripts/fetch_signals.py scouts/<timestamp> scouts/<timestamp>/queries.json` pulls normalized records into `scouts/<timestamp>/signals.json` from GDELT, Hacker News, SEC EDGAR (free, no auth) and — when `X_BEARER_TOKEN` is set and `x_handles` is non-empty — recent X posts from the curated follow list. No LLM; one dead or unconfigured source degrades to a warning.
2. **Cluster & score** — the scout agent clusters signals into candidate themes, checks overlap against `reports/` (a covered topic with a fresh signal resurfaces as a follow-on — overlap is not an automatic drop), scores by relevance/novelty/researchability, and writes `scouts/<timestamp>/brief.md`.
3. **Commit** — `/scout` commits the brief on its own `scout/<timestamp>` branch and opens a PR. It does **not** kick off research — the brief lists ready-to-run questions the user can pass to `/research` separately.

## Research flow

1. **Intake** — director parses question + sources + effort.
2. **Decompose** — director writes a two-layer question tree to `reports/<run>/plan.md`. Layer 1 = broad themes, Layer 2 = specific researchable items.
3. **Allocate** — director groups Layer-2 items into 3–8 bundles.
4. **Dispatch (parallel)** — one researcher per bundle, fired in parallel.
5. **Critique & iterate (adaptive)** — director reads all reports, writes per-report critiques to `critiques/round-N.md`, re-dispatches only those needing revision. Repeats until satisfied OR effort cap hit (`quick`=0 rounds, `low`=1, `medium`=2, `high`=4 — upper bounds; director may stop earlier). `quick` shrinks the whole pipeline, not just revisions: 2–3 bundles at survey depth, no critique files, and one combined ticker-scan researcher (`tickers/ticker-scan.md`) instead of per-ticker deep dives.
6. **Synthesize** — director writes `final-report.md` as a graph: connections, contradictions, cascades, emergent picture. Tickers section included only if the question implies investable output.
7. **Commit** — `/research` commits the run on its own `research/<slug>` branch and opens a PR.

## Podcast flow

1. **Verify** — `/podcast` syncs `main`, confirms the target `final-report.md` is present (the research PR must already be merged), and checks `OPENAI_API_KEY`.
2. **Script** — podcast-producer reads the report, writes an outline, then a Moderator / Lead / Skeptic dialogue script. No new research; every claim traces back to the report.
3. **Synthesize** — per-line TTS via `scripts/tts_synthesize.py` (OpenAI), then ffmpeg stitches into `episode.mp3`. ~$0.30/episode.
4. **Commit** — `/podcast` commits the podcast dir on its own `podcast/<slug>` branch and opens a PR.

## Journal flow

A lightweight capture command for the user's own thinking — reflections, hesitations, imagined scenarios, roads-not-taken — surfaced in the dashboard timeline for later review. No agent, no research, no Notion.

1. **Sync + refine** — `/journal` syncs `main`, takes everything after `/journal` as a raw reflection (asks for it if empty), and helps the user refine it into clearer prose — preserving voice, stance, and doubt — before storing the approved text.
2. **Link (always asked)** — suggests related research runs (keyword/ticker matches + recent runs) and tickers (tagged `[PORTFOLIO]`/`[CANDIDATE]`/`[NEW]`), and asks which to link. For a linked `[NEW]` ticker, offers to add it to `candidates.json`.
3. **Persist** — appends an entry (`journal-<YYYYMMDD>-<seq>`) to `tracking/journal.json` and, if the user opts in at commit time, regenerates the dashboard (the entry renders as a violet node in the Timeline view).
4. **Commit** — `/journal` commits on its own `journal/<YYYY-MM-DD>-<NNN>` branch and opens a PR (same git flow as `/scout`).

## What-if flow

Counterfactual comparison of concrete trade histories — **not** strategy backtesting (no rebalancing rules, dividends, or intraday fills; daily closes only). Scenarios live in `tracking/hypotheticals.json` (schema in `tracking/README.md`) and render as dashed overlay lines on the dashboard's P&L value chart.

1. **Classify** — `/what-if` takes everything after it as either a new scenario (default) or a management request (archive / reactivate / rename), detected by intent. Scenarios are matched by name substring first, then `id`.
2. **Parse** — a new scenario becomes one of three types: `substitute` (clone the actual trade log with ticker swaps — same dollars into the swapped ticker at its close; swapped sells sell the same fraction of the hypothetical position), `benchmark` (every actual trade re-executed into one ticker, e.g. SPY), or `standalone` (explicit hypothetical trade list; DCA phrasings expanded to dated trades). Shares/prices are never stored for derived trades — `scripts/generate_dashboard.py` derives them from daily closes.
3. **Preview** — `python3 scripts/generate_dashboard.py --whatif-preview <id>` prints the scenario's value / invested / P&L (and, for substitute/benchmark, the P&L delta vs actual) without writing HTML. Price history is cached per day in the gitignored `tracking/price-cache.json`, so iterating is cheap.
4. **Commit** — `/what-if` commits on its own `whatif/<YYYY-MM-DD>-<NNN>` branch and opens a PR (same git flow as `/journal`). IDs are `whatif-<YYYYMMDD>-<NNN>`, immutable; `status` flips between `active`/`archived` instead of deleting; each scenario keeps a fixed `color_index` so chart colors never reshuffle.

## Sync-portfolio flow

Pulls the **real brokerage state** via SnapTrade (Personal API key — HMAC-signed requests, read-only for Fidelity, no userId/userSecret; the key resolves the user) and reviews it. Two modes, chosen automatically by capability:

1. **Detect** — `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` present (env or `.env`) → sync mode; absent (e.g. a cloud sandbox without the keys) → **review-only mode**: no fetch, no writes, no git — the same review runs against the committed `tracking/brokerage-snapshot.json`, led by a staleness warning from its `fetched_at`.
2. **Fetch** (sync mode) — `scripts/fetch_snaptrade.py tracking/brokerage-snapshot.json` writes accounts, balances, positions, and lookback-window activities (stdlib-only; account numbers masked to last 4; exit 2 = creds missing → fall back to review-only).
3. **Reconcile** — snapshot vs tracking: held-but-untracked → propose adding to `portfolio.json`; held-but-candidate → propose the move; tracked-but-not-held → propose removal; unlogged trades → **suggest `/log-trade`, never write `trades.json`** (it mirrors Notion 1:1). Fixes are user-approved before any write.
4. **Review** — terse, investor-stance read of the snapshot: concentration, cash, per-position P&L, held-without-thesis (→ ready-to-run `/research` questions), active `events[]` on held names, thesis staleness.
5. **Commit** — `/sync-portfolio` commits the snapshot + approved fixes on its own `sync-portfolio/<timestamp>` branch and opens a PR. Dashboard regen offered only if `portfolio.json`/`candidates.json` changed.

## Git convention

Every run lands as its **own branch + Pull Request** — never a direct commit to `main`. The **agents never touch git** — the commands own it. Remote is named `origin`; base branch is `main`.

**Branch-first, uniform across all seven.** Every command follows the identical order: **compute a standardized branch name up front → pull `main` → cut the branch off `main` → do the work *on the branch* → commit → PR → merge.** The branch is created *before* any work, so the run happens on its own branch from the first write — `main`'s working tree is never touched. Because the name is fixed up front, the agents are told the exact run-dir to write into (they no longer invent their own).

Standardized branch names — `<type>/<identifier>`, all date-prefixed, all computable before the work:

| Command | Branch | Identifier (computed up front) | Run dir |
|---|---|---|---|
| `/scout` | `scout/<YYYY-MM-DD-HHMMSS>` | `date +%Y-%m-%d-%H%M%S` (per-second; multiple/day never collide) | `scouts/<id>/` |
| `/research` | `research/<YYYY-MM-DD>-<slug>` | today + kebab slug the command derives from the question | `reports/<id>/` |
| `/podcast` | `podcast/<YYYY-MM-DD>-<slug>` | basename of the target report dir (already `YYYY-MM-DD-<slug>`) | writes into `reports/<id>/podcast/` |
| `/log-trade` | `log-trade/<YYYY-MM-DD>-<ticker-slug>` | today + parsed ticker(s) (hyphen-joined if several) | — (tracking files) |
| `/journal` | `journal/<YYYY-MM-DD>-<NNN>` | today + next per-day sequence from `journal.json` | — (tracking file) |
| `/what-if` | `whatif/<YYYY-MM-DD>-<NNN>` | today + next per-day sequence from `hypotheticals.json` (bumped past existing branches for management runs) | — (tracking file) |
| `/sync-portfolio` | `sync-portfolio/<YYYY-MM-DD-HHMMSS>` | `date +%Y-%m-%d-%H%M%S` (per-second, like `/scout`) | — (tracking files) |

**Prompt-first for anything remote.** Local branch creation is cheap and unprompted; the run does its work on the branch, then the command shows the files and uses `AskUserQuestion` to gate the externally-visible steps: *commit & push?* / *open PR?*, then (after the PR is open) *merge?* — nothing is pushed or merged without the user's yes.

**The dashboard is committed, regeneration is opt-in.** `dashboard/index.html` is a tracked file. Any command that mutates a dashboard **input** — `/log-trade`, `/journal`, and `/what-if` (tracking files), `/research` (tracking + a new `final-report.md`), and `/sync-portfolio` (only when a reconciliation fix touches `portfolio.json`/`candidates.json`; the snapshot itself is not a dashboard input) — *can* regenerate it (`python3 scripts/generate_dashboard.py`), but regeneration is **not automatic**: the script fetches live prices via yfinance (slow / network-heavy), so each of those commands **asks the user** (a third question folded into the same commit-permission `AskUserQuestion`) whether to regenerate. Only when the user says yes does the command run the script and stage `dashboard/index.html` **with the run**; otherwise the committed dashboard is left as-is and may lag the data until the next opt-in regen. `/scout` and `/podcast` touch no dashboard input, so they neither regenerate nor stage it.

Canonical procedure (each command substitutes branch / paths / title):

```bash
# ── at the start, before any work ──
git status --porcelain                               # clean-tree guard: unrelated changes → stop & report
git switch main && git pull --ff-only origin main    # the ONE sync
git switch -c <type>/<identifier>                     # branch off fresh main, BEFORE the work
# ... the command does its work ON THE BRANCH: agent run / file writes / Notion ...
# ── at commit time, after showing files + AskUserQuestion yes ──
git add <run-paths>                                   # + dashboard/index.html if the run mutated a dashboard input
git commit -m "<type>: <slug> (<date>)"
git push -u origin <type>/<identifier>
gh pr create --base main --head <type>/<identifier> --title "<type>: <slug>" --body "<one line>"
# ── if the user approves the merge ──
gh pr merge --squash --delete-branch
git switch main                                       # always finish on main
```

- **Clean-tree guard** — before pulling/branching, if `git status --porcelain` shows changes unrelated to this run, stop and report; never stash or sweep them in.
- **Decline path** — if the user says no at *commit & push?*, return to `main` and drop the unused local branch: `git switch main && git branch -D <type>/<identifier>` (the work returns to the working tree; nothing was pushed). For `/log-trade` and `/journal`, any Notion write and tracking-file edits already made remain — only the branch is discarded.
- **Failure rule** — any git/`gh` failure: report and stop, no destructive retry.
- PRs are merged only on the user's explicit *merge?* yes; otherwise left open to merge on GitHub. `/podcast` additionally expects its target report to already be merged into `main` (it verifies the report is present right after the sync, before cutting the branch).

## Two-repo sync

Prism lives in two sibling repos:

- **prism-shared** (public) — the engine: `scripts/`, `.claude/agents/`, `.claude/commands/`, `CLAUDE.md`, `README.md`, `setup.sh`. Canonical home for all feature work. Tracking JSONs are empty templates; `scout-x-feeds.json` holds placeholder handles.
- **prism** (private) — the daily driver: the same engine **plus** personal data (`reports/`, `scouts/`, `memos/`, real `tracking/*.json`, generated `dashboard/index.html`, real X follows, `.env`).

**Rules:**

- Engine changes belong in **prism-shared first**, then sync to prism. When a change lands in prism first (it happens — that's where daily work runs), port it back to prism-shared promptly.
- Engine files should be **byte-identical** across the two repos. The only expected diffs are personal data: `tracking/*.json`, `dashboard/index.html`, `.claude/scout-x-feeds.json`, `.claude/settings.local.json`, `.claude/podcast-cast.json`, `.env`, `reports/`, `scouts/`, `memos/`.
- **Nothing personal ever enters prism-shared** — no real handles, Notion IDs, keys, personal paths, or tracking data. prism → prism-shared is a port with sanitization, not a copy.
- Sync commits reference the source repo's PR (`sync: <what> from prism (#N)`), so direction and provenance are recoverable from history.
- `/sync` (`.claude/commands/sync.md`) automates the whole flow: diffs the engine set, infers direction from git history, sanitizes, and lands a PR in the receiving repo per the Git convention.

## Output layout

```
scouts/                        # only if /scout was run
  YYYY-MM-DD-HHMMSS/           # one dir per scout run (timestamped — multiple/day OK)
    queries.json               # focus → per-source query spec the agent built
    signals.json               # raw normalized signals from GDELT/HN/EDGAR
    brief.md                   # ranked candidate themes
reports/
  YYYY-MM-DD-<slug>/
    plan.md
    individual/
      01-<topic-slug>.md
      02-<topic-slug>.md
      ...
    critiques/
      round-1.md
      round-2.md
    final-report.md
    podcast/                   # only if /podcast was run
      outline.md
      script.md
      segments/                # NNN-<role>.mp3 per turn
      episode.mp3
      episode.json
tracking/
  portfolio.json               # held positions with reports[] + events[] per ticker
  candidates.json              # user-curated watchlist with reports[] + events[] per ticker
  catalysts.json               # macro/multi-ticker events
  trades.json                  # individual trade log written by /log-trade
  journal.json                 # free-form reflections written by /journal
  hypotheticals.json           # what-if scenarios written by /what-if
  brokerage-snapshot.json      # real brokerage state fetched from SnapTrade by /sync-portfolio
dashboard/
  index.html                   # generated by scripts/generate_dashboard.py; tracked — regenerated + committed by the command that mutates a dashboard input (log-trade / journal / research) only when the user opts in at commit time (regen is slow — fetches live prices)
```

Cast voices live in `.claude/podcast-cast.json` (created on first `/podcast` run, reused after). Helper at `scripts/tts_synthesize.py` does the OpenAI HTTP calls. The scout's curated X follow list lives in `.claude/scout-x-feeds.json` (hand-edited; `handles[]` with optional `topics`/`note`).

## Tracking

Seven persistent JSON files accumulate across all research runs. They live in `tracking/` and are committed alongside each research/trade/journal/what-if/sync-portfolio PR (via `git add tracking/`).

```
tracking/
  portfolio.json     # held positions: ticker list with reports[] + events[] per ticker
  candidates.json    # user-curated watchlist: tickers under consideration, same schema
  catalysts.json     # macro/multi-ticker: regulatory events, IPOs, policy decisions
  trades.json        # individual trade log: every /log-trade execution with linked research
  journal.json       # free-form reflections: every /journal entry with linked runs/tickers
  hypotheticals.json # what-if scenarios: substitutions / standalone portfolios / benchmarks
  brokerage-snapshot.json # real brokerage state (accounts/positions/activity) from SnapTrade via /sync-portfolio
```

(`tracking/price-cache.json` may also exist — a gitignored daily-close cache written by `scripts/generate_dashboard.py`, refetched whole per ticker per day. Never committed.)

**What feeds in**: research-director (Phase 8) appends `reports` and `events` entries to `portfolio.json` (for held tickers) and `candidates.json` (for candidates) after each run. New tickers (`[NEW]`) are not auto-written — at the end of the run the director lists them and asks which to add to `candidates.json`. `/log-trade` writes each trade execution to `trades.json` (date, ticker, action, amount, shares, price, and `linked_research[]` linking to the specific runs that motivated the trade) and mirrors it as a row in the **Investment Log** Notion database (columns: Ticker, date, Action, Amount, Shares, Price, Comment — Notion's `Amount` is **signed**: positive for `buy`/`add`, negative for `sell`/`trim`, since the log tracks net money invested; `Action` also carries the direction). `trades.json` mirrors the Notion log 1:1 **except** that its `amount_usd` is the unsigned positive magnitude (direction lives in the `action` field, which `scripts/generate_dashboard.py` relies on for the sign) — only Notion's `Amount` column carries the sign. `/journal` writes each reflection to `journal.json` (date, text, `linked_research[]`, `linked_tickers[]`) and may add a `[NEW]` linked ticker to `candidates.json` on request — it does not touch Notion. `/what-if` writes scenarios to `hypotheticals.json` (create + archive/reactivate/rename; schema in `tracking/README.md`) — local only. `/sync-portfolio` overwrites `brokerage-snapshot.json` with the fetched brokerage state and, with per-fix user approval, applies reconciliation fixes to `portfolio.json`/`candidates.json` (add held-but-untracked, move candidate → portfolio, remove exited) — it never writes `trades.json` (owned by `/log-trade`, mirrors Notion 1:1). Podcast and scout do not write to any tracking files.

**What consumes them**: scout (Phase 3) reads `portfolio.json`, `candidates.json`, and `catalysts.json` to tag signals as `[PORTFOLIO]`, `[CANDIDATE]`, or `[NEW]` and to surface watchlist alerts when signals match active event entries. `scripts/generate_dashboard.py` reads `portfolio.json`, `candidates.json`, `trades.json`, `journal.json`, and `hypotheticals.json` to generate `dashboard/index.html` (timeline with research/trade/journal nodes, research-to-trade alignment, per-ticker drilldown with P&L, and what-if scenario overlays on the P&L value chart). `/sync-portfolio` reads `brokerage-snapshot.json` (its review-only fallback when SnapTrade credentials are absent) plus `portfolio.json`, `candidates.json`, and `trades.json` for reconciliation.

**journal.json structure**: `{ last_updated, entries: [{ id, date, text, linked_research: [{ run, date, report_path, linked_at }], linked_tickers: [<symbol>] }] }`. Append-only log — entries are not snapshots and are never auto-pruned. Entry `id` is `journal-<YYYYMMDD>-<seq>` (per-day sequence, mirrors the `trades.json` id scheme).

**portfolio.json structure**: `{ positions: [{ ticker, reports: [], events: [] }] }`. Snapshot — presence means currently held. To exit a position, delete the entry. Research runs append to `reports[]` and `events[]` automatically.

**candidates.json structure**: `{ entries: [{ ticker, added_date, reports: [], events: [] }] }`. Snapshot — presence means actively watching. To stop watching, delete the entry. When a trade is made, delete from candidates and add to portfolio. Research runs append to `reports[]` and `events[]`.

**reports[] entry** (inside each ticker): `{ run, date, hypothesis, entry_condition, verdict }` — one entry per research run that touched that ticker.

### Event entry ID format (SCREAMING-KEBAB-CASE, deterministic)

**Ticker-level events** (inside portfolio.json and candidates.json `events[]` arrays): `<TICKER>-<TYPE_SHORT>-<KEYWORD>`
- TYPE_SHORT: `BUY` (buy_trigger) · `FAL` (falsifier) · `EVT` (event_monitor)
- KEYWORD: 1–2 words from the most specific, immutable part of the condition — the metric name, event type, or price level. Not the company name. Not a generic word.
- Examples: `PGR-BUY-180`, `CRM-FAL-CRPO`, `GOOGL-EVT-DOJ`, `AMZN-FAL-SILICON`

**Catalyst entries**: `<MACRO_KEY>-<YEAR>-<EVENT>` or `<TICKER>-<EVENT>-<YEAR>`
- Examples: `GOOGL-DOJ-2026`, `AMZN-ANTHROPIC-IPO`, `FED-RATE-2026`

**Stability rule**: once an `id` is written, never change it. Match existing entries by `id` first, then fall back to `ticker + type + keyword substring` to catch minor ID drift. Update in place; never duplicate.

### History tracking

Every event entry carries a `history` array. Each element records one event:
- `created` — initial extraction
- `verdict_change` — a later research run produced a different verdict on the same condition
- `updated` — rechecked by a later run, verdict unchanged
- `status_change` — active → resolved or stale

### Manual lifecycle

- **Resolved**: change `status` to `"resolved"`, add `resolved_date` + `resolving_run` to the entry, note what happened.
- **Stale**: change `status` to `"stale"` and note why (condition expired, no longer relevant).
- The director flags entries >18 months old with a `stale_warning` field but never auto-deletes or auto-resolves.

## Investor stance

The user is a long-term investor (multi-year holds), not a short-term arbitrageur — a quality compounder at full valuation can still be the right buy if the multi-year runway holds, while a name pricing in assumptions even base-case multi-year execution can't justify is not, even when the thesis is directionally right.

## Voice

- Terse. Falsifiable. No hype, no hedging-just-to-hedge.
- Every empirical claim links to a source (URL or filing path).
- Reasoning steps explicit. Never "obviously" or "clearly" to paper over a jump.
- If something is unknown, say "unknown" — don't write fluently past it.
- State conviction explicitly when claims are uncertain.

## Data sources

- **Web search + WebFetch** for news, transcripts, IR pages, reporting, social signal.
- **Scout signal sources** (used by `scripts/fetch_signals.py` for discovery, not deep research):
  - **GDELT DOC 2.0** — `https://api.gdeltproject.org/api/v2/doc/doc` (free, no key). `mode=timelinevolraw` for coverage-volume spike detection; `mode=artlist` for representative articles. Rate-limits hard — the helper retries with backoff.
  - **Hacker News (Algolia)** — `https://hn.algolia.com/api/v1/search_by_date` + `.../search?tags=front_page` (free, no auth). Points/comment velocity.
  - **X API v2** — `https://api.x.com/2/users/by/username/:handle` then `.../2/users/:id/tweets` (bearer auth via `X_BEARER_TOKEN`; needs a tier with user-timeline read). Recent original posts (excludes reposts/replies) from the curated follow list in `.claude/scout-x-feeds.json`, scored by `engagement_velocity` (likes+reposts+quotes per hour). The only paid/keyed scout source — unset token degrades to a warning and the source is skipped.
- **SEC EDGAR** (free, no auth, requires `User-Agent` header with a contact email — set `EDGAR_CONTACT_EMAIL` in `.env`; the fetch scripts read it, falling back to a generic placeholder if unset):
  - Full-text search: `https://efts.sec.gov/LATEST/search-index?q=...&forms=...` (used by the scout for focus-term filing hits)
  - Ticker → CIK: `https://www.sec.gov/files/company_tickers.json`
  - Filings: `https://data.sec.gov/submissions/CIK{10-digit-padded}.json`
  - Company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit-padded}.json`
  - **13F institutional breadth** — `scripts/fetch_13f_breadth.py <TICKER> <out.json>` pages full-text search over 13F-HR filings and writes per-quarter counts of distinct institutions holding the name (deduped by filer CIK) plus new/exited holders vs the prior quarter. 13F info tables key on CUSIP, not ticker — the script keeps a lazily-grown cache at `.claude/cusip-map.json`; for an uncached ticker the researcher looks the CUSIP up once and passes `--cusip`. A **confirmation lens** for ticker deep dives (researcher Step 3d): what matters is direction across 2–3+ consecutive quarters of holder count and new-position velocity — never a discovery signal (filings lag up to 45 days) and never a verdict driver on its own. **On by default only at effort=high** (it takes minutes per name); other efforts skip it unless the user explicitly asks for the 13F/institutional lens — the director passes `include_13f: yes/no` in each ticker dispatch.
- **Yahoo Finance via yfinance** (`pip install yfinance`) — used by `scripts/fetch_ticker_stats.py` for per-ticker financial stats (price, PE ratios, margins, quarterly financials, price history) plus a `technicals` block: 50/200-day MAs, RSI(14), trailing returns, relative strength vs SPY (the free capital-flow proxy), and a volume trend — all computed in pandas (no ta-lib). Technicals are an **entry-timing lens** for the multi-year holder, never a buy/sell signal. Free, no auth. Researchers call this script at the start of Phase 3 in ticker sub-mode; do not call Yahoo Finance ad hoc when this script is available.
- **SnapTrade** (`https://api.snaptrade.com/api/v1`, Personal API key — free tier) — used only by `scripts/fetch_snaptrade.py` for `/sync-portfolio`: accounts, balances, positions, recent activities from the user's connected brokerage (Fidelity — read-only, no trade placement). Auth is `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY` in `.env`: every request carries `clientId` + `timestamp` query params and a `Signature` header (base64 HMAC-SHA256 of `{content, path, query}` signed with the consumerKey); no userId/userSecret — a Personal key resolves the user directly, and Register User is never called. The consumerKey has full read access to the connected brokerage: never print or commit it. Unset credentials degrade `/sync-portfolio` to review-only over the committed snapshot.
- The research pipeline uses no paid data APIs (web search, WebFetch, EDGAR, yfinance are all free). If a free path doesn't exist for a question, say so in the report. The only keyed sources anywhere in Prism are scout's optional X feed (`X_BEARER_TOKEN`) and `/sync-portfolio`'s SnapTrade credentials — both degrade gracefully when unset.

## Dates

Today's anchor for relative date phrases is whatever the system reports. Always write absolute dates in saved files.

## Historical artifacts

The `memos/` directory contains output from the prior version of Prism (closed-loop investment-thesis pipeline). Kept for reference; new runs go to `reports/`.
