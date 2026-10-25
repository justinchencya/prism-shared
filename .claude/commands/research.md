---
description: Run a deep research run. Decomposes the question, dispatches researchers in parallel, iterates adaptively, then synthesizes a final report.
---

Dispatch the **research-director** agent to run a research pipeline.

Parse the user's invocation for:

- **question** (required): everything that isn't an explicit `sources:` or `effort:` flag is the question.
- **sources** (optional): comma-separated URLs and/or file paths after `sources:`.
- **effort** (optional, **default `quick`**): one of `quick`, `low`, `medium`, `high` after `effort:` — or stated in plain words anywhere in the invocation ("high effort", "medium depth", "deep dive" → `high`, "thorough" → `medium`). Controls the upper bound on revision rounds (quick=0, low=1, medium=2, high=4). `quick` also shrinks the whole pipeline: single pass, no critique loop, survey-depth researchers, one combined ticker scan instead of per-ticker deep dives — high-level theses + verdict table, fast.

If no question is present, ask the user for one before dispatching.

If no effort was given, use `quick` — **do not ask**. State the level in one line when starting (e.g. `effort: quick (default — pass effort:high for a deeper run)`) so the user can interrupt and rerun deeper if they meant to.

**Branch off `main` before any work** (per the **Git convention** in `CLAUDE.md`). Compute the run identifier up front so the branch and run dir are fixed before the director runs:

1. Resolve today's date (`date +%Y-%m-%d`) and derive a short **English** kebab-case slug from the question (3–6 words capturing its core, e.g. `ai-capex-utility-load` — translate if the question isn't in English). The run identifier is `<YYYY-MM-DD>-<slug>`; the run dir is `reports/<id>/` and the branch is `research/<id>`.
2. Clean-tree guard, sync, and cut the branch:

```bash
cd <repo-root>
test ! -e .prism-engine || { echo "engine repo — run this in your private repo"; exit 1; }   # engine-repo guard
git status --porcelain                               # unrelated changes → stop & report
git switch main && git pull --ff-only origin main
git switch -c research/<id>
```

If the switch, pull, or branch fails, report and stop — never stash, reset, or force. If the question had non-English text and a `set_session_title` tool is available, rename the session to `research: <slug>` now (see **Voice** in `CLAUDE.md`).

### Orchestrating the director (you dispatch; the director only plans/critiques/synthesizes)

The `research-director` agent runs as a subagent and the harness disables nested `Agent` dispatch, so **it has no way to spawn researchers** — only this top-level session can. The director's job (`.claude/agents/research-director.md`) is planning, allocation, critique, and synthesis; it signals every point where researchers are needed by writing a dispatch manifest to `reports/<id>/dispatch-manifest.json` and stopping with the exact line `DISPATCH_REQUIRED: reports/<id>/dispatch-manifest.json`. You read that manifest, dispatch the listed researchers yourself via the `Agent` tool, then resume the director. Loop this until the director finishes Phase 8 (no more manifests) instead of stopping.

1. **Spawn the director** via the `Agent` tool (`subagent_type: research-director`, foreground — your next action depends on its result). The prompt includes:
   - The user's question, verbatim.
   - The sources list (or "none provided").
   - The effort level.
   - Today's date.
   - **The run directory to use, verbatim: `reports/<id>/`.** The director must write into exactly this dir — it does not invent its own slug or path.

   Note the agent's name/id from the result (or `ListAgents` if it's not directly in the result) — you need it to resume the director below.

2. **If the `Agent` tool is unavailable or the spawn fails**: stop and tell the user plainly — "the research pipeline requires the `Agent` tool to dispatch researchers in parallel; it isn't available in this session, so I'm not running the director inline." Do **not** fall back to doing the research yourself or letting the director do it inline. This is the one guard the whole redesign exists to enforce — never degrade silently.

3. **Dispatch loop** — repeat until the director's reply is the Phase 8 report-back (final report path + verdict table, etc.) rather than a `DISPATCH_REQUIRED` line:
   a. Read `reports/<id>/dispatch-manifest.json`. It has `{ "phase": "...", "dispatches": [{ "id", "output_path", "prompt" }, ...] }`.
   b. Dispatch every entry **in parallel** — one message, one `Agent` tool call per entry, `subagent_type: researcher`, `prompt` taken verbatim from the manifest entry.
   c. **Gate check**: after all return, confirm every entry's `output_path` exists and is non-empty. For any that don't, redispatch that single entry once (same prompt). If it's still missing/empty after the retry, proceed without it and note the failure in the resume message below — do not write the file yourself.
   d. **Resume the director**: `SendMessage` to the director's name/id with a message naming the phase just completed, the manifest's dispatch ids, their output paths, and any that failed the gate check after retry. Tell it to continue the workflow from exactly where it left off.
   e. The director's reply is either another `DISPATCH_REQUIRED` line (go to 3a for the new manifest) or the final Phase 8 report-back.

4. **Record keeping**: nothing else to do here — the director already writes `dispatch_mode: subagent` into `plan.md`, and each manifest is overwritten per phase so `reports/<id>/dispatch-manifest.json` on disk at the end just reflects the last phase (harmless; it's inside the run dir and gets committed with everything else).

After the director's Phase 8 report-back (final report path, verdict table, etc.):

0. **Show results first** — before any git step or prompt, output as plain text: the final report path (`reports/<id>/final-report.md`) and, if the director's reply included a **verdict table** (investable runs), that table verbatim. Do not paste any other report contents. Text emitted mid-turn between tool calls is not reliably visible to the user, so this alone is not enough — the table is also carried *inside* the Step 3 prompt (as the option preview) and repeated in the final message.

   **Then show the charts** (investable runs): for each `reports/<id>/tickers/chart_<TICKER>.png`, send the image to the user with whatever file-sharing tool the session has (e.g. `SendUserFile`, rendered inline), captioned with the ticker, its Market verdict, and its key levels line from the verdict table — one call for all charts. If no such tool exists, list the chart paths instead.

1. **Dashboard decision** — the run added a report, a dashboard input. Run `python3 scripts/dashboard_due.py` from the repo root and keep its one-line output (`regenerate: …` / `skip: …`); don't regenerate yet. (Rule in `CLAUDE.md` → **Git convention**: once per trading day, after the 13:00 PT close.)

2. **Show the user** the list of files that would be committed (`reports/<id>/`), using `git status --short` or a plain file listing, plus the dashboard decision line.

3. **Ship gate** — a single `AskUserQuestion` call with **one** question, "Ship this research run?", and three options in this order:
   - "Ship it — commit, push, open PR, merge" (Recommended)
   - "Commit, push & open PR — leave it open"
   - "Don't commit"

   **Put the results in the prompt itself**: set the `preview` of both shipping options to the report path, the verdict table verbatim (or, for non-investable runs, the final report's executive answer), and the dashboard decision line. The preview renders beside the options while the user answers — this is what guarantees the verdicts are visible at decision time.

   Wait for the answer. **If the user picks "Don't commit"**, return to `main` and drop the unused branch: `git switch main && git branch -D research/<id>` (the run's files remain in the working tree; nothing was pushed). Report and stop. Otherwise, **if the decision was `regenerate`** (or the user's answer explicitly asked for a regen), run `python3 scripts/generate_dashboard.py` from the repo root now, before staging; report any error output but don't block the commit on it. If the user's answer explicitly asked to skip the dashboard, skip it.

4. Stage, commit, push (the branch already exists). Include `dashboard/index.html` in the `git add` **only if it was regenerated** in Step 3:

```bash
cd <repo-root>
git add reports/<id>   # + dashboard/index.html if regenerated in Step 3
git commit -m "research: <slug> (<date>)"
git push -u origin research/<id>
```

5. Open the PR (both shipping options include it):

```bash
gh pr create --base main --head research/<id> --title "research: <slug>" --body "<one-line: the question + effort>"
```

6. No second prompt — if the user picked "Ship it", run `gh pr merge --squash --delete-branch`; otherwise leave the PR open for the user to merge on GitHub. Either way, finish on `main`:

```bash
git switch main
```

The remote is named `origin`; the branch `research/<id>` matches its run dir (e.g. `research/2026-05-28-edge-computing-agentic-ai`). The clean-tree guard already ran before branching; by commit time the uncommitted changes are this run's own files plus — only if it was regenerated — `dashboard/index.html` (tracked, committed with the run). If any git/`gh` step fails, report the error and stop — don't retry destructively.

**Final message (always, including the decline path)**: the report path, the **verdict table verbatim** (investable runs — it's the at-a-glance digest the user always wants), the PR URL if opened, the merge outcome, and whether the dashboard was regenerated (with the decision line). Repeat the table here even though Step 0 and the Step 3 preview already showed it — the final message is the only output guaranteed to stay visible. Do not paste any other report contents.

## Examples

`/research How is the AI capex cycle affecting electric utility load growth?`
→ no effort given → runs at `quick` without asking; no sources

`/research Which companies benefit most from on-shoring of advanced packaging? effort:high`
→ implies tickers; 4 revision rounds allowed

`/research What's the state of solid-state battery commercialization? sources:https://example.com/report.pdf,notes/ssb.md effort:low`
→ seed sources provided; 1 revision round

`/research Is the GLP-1 supply chain still capacity-constrained? effort:quick`
→ fast scan: 2–3 bundles, no critique loop, single combined ticker scan, high-level theses + verdict table
