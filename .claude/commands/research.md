---
description: Run a deep research run. Decomposes the question, dispatches researchers in parallel, iterates adaptively, then synthesizes a final report.
---

Dispatch the **research-director** agent to run a research pipeline.

Parse the user's invocation for:

- **question** (required): everything that isn't an explicit `sources:` or `effort:` flag is the question.
- **sources** (optional): comma-separated URLs and/or file paths after `sources:`.
- **effort** (optional): one of `quick`, `low`, `medium`, `high` after `effort:`. Controls the upper bound on revision rounds (quick=0, low=1, medium=2, high=4). `quick` also shrinks the whole pipeline: single pass, no critique loop, survey-depth researchers, one combined ticker scan instead of per-ticker deep dives — high-level theses + verdict table, fast.

If no question is present, ask the user for one before dispatching.

If no `effort:` flag was supplied, use `AskUserQuestion` to ask which level the user wants. Default highlight on `quick` (Recommended) — it's the usual case. Briefly describe what each level means (quick: fast single-pass scan, high-level theses + verdicts; low: full pipeline, 1 revision round; medium: thorough default, 2 rounds; high: deepest iteration, 4 rounds). Do not silently fall back to a default — surface the choice every time so the user makes it consciously.

**Branch off `main` before any work** (per the **Git convention** in `CLAUDE.md`). Compute the run identifier up front so the branch and run dir are fixed before the director runs:

1. Resolve today's date (`date +%Y-%m-%d`) and derive a short kebab-case slug from the question (3–6 words capturing its core, e.g. `ai-capex-utility-load`). The run identifier is `<YYYY-MM-DD>-<slug>`; the run dir is `reports/<id>/` and the branch is `research/<id>`.
2. Clean-tree guard, sync, and cut the branch:

```bash
cd <repo-root>
test ! -e .prism-engine || { echo "engine repo — run this in your private repo"; exit 1; }   # engine-repo guard
git status --porcelain                               # unrelated changes → stop & report
git switch main && git pull --ff-only origin main
git switch -c research/<id>
```

If the switch, pull, or branch fails, report and stop — never stash, reset, or force.

### Orchestrating the director (you dispatch; the director only plans/critiques/synthesizes)

The `research-director` agent runs as a subagent and the harness disables nested `Agent` dispatch, so **it has no way to spawn researchers** — only this top-level session can. The director's job (`.claude/agents/research-director.md`) is planning, allocation, critique, and synthesis; it signals every point where researchers are needed by writing a dispatch manifest to `reports/<id>/dispatch-manifest.json` and stopping with the exact line `DISPATCH_REQUIRED: reports/<id>/dispatch-manifest.json`. You read that manifest, dispatch the listed researchers yourself via the `Agent` tool, then resume the director. Loop this until the director finishes Phase 9 (no more manifests) instead of stopping.

1. **Spawn the director** via the `Agent` tool (`subagent_type: research-director`, foreground — your next action depends on its result). The prompt includes:
   - The user's question, verbatim.
   - The sources list (or "none provided").
   - The effort level.
   - Today's date.
   - **The run directory to use, verbatim: `reports/<id>/`.** The director must write into exactly this dir — it does not invent its own slug or path.

   Note the agent's name/id from the result (or `ListAgents` if it's not directly in the result) — you need it to resume the director below.

2. **If the `Agent` tool is unavailable or the spawn fails**: stop and tell the user plainly — "the research pipeline requires the `Agent` tool to dispatch researchers in parallel; it isn't available in this session, so I'm not running the director inline." Do **not** fall back to doing the research yourself or letting the director do it inline. This is the one guard the whole redesign exists to enforce — never degrade silently.

3. **Dispatch loop** — repeat until the director's reply is the Phase 9 report-back (final report path + verdict table, etc.) rather than a `DISPATCH_REQUIRED` line:
   a. Read `reports/<id>/dispatch-manifest.json`. It has `{ "phase": "...", "dispatches": [{ "id", "output_path", "prompt" }, ...] }`.
   b. Dispatch every entry **in parallel** — one message, one `Agent` tool call per entry, `subagent_type: researcher`, `prompt` taken verbatim from the manifest entry.
   c. **Gate check**: after all return, confirm every entry's `output_path` exists and is non-empty. For any that don't, redispatch that single entry once (same prompt). If it's still missing/empty after the retry, proceed without it and note the failure in the resume message below — do not write the file yourself.
   d. **Resume the director**: `SendMessage` to the director's name/id with a message naming the phase just completed, the manifest's dispatch ids, their output paths, and any that failed the gate check after retry. Tell it to continue the workflow from exactly where it left off.
   e. The director's reply is either another `DISPATCH_REQUIRED` line (go to 3a for the new manifest) or the final Phase 9 report-back.

4. **Record keeping**: nothing else to do here — the director already writes `dispatch_mode: subagent` into `plan.md`, and each manifest is overwritten per phase so `reports/<id>/dispatch-manifest.json` on disk at the end just reflects the last phase (harmless; it's inside the run dir and gets committed with everything else).

After the director's Phase 9 report-back (final report path, verdict table, etc.):

0. **Show results first** — before any git step or prompt, output as plain text: the final report path (`reports/<id>/final-report.md`) and, if the director's reply included a **verdict table** (investable runs), that table verbatim. Do not paste any other report contents. Text emitted mid-turn between tool calls is not reliably visible to the user, so this alone is not enough — the table is also carried *inside* the Step 3 prompt (as the option preview) and repeated in the final message.

1. **Dashboard regeneration is optional** — the run added a report and may have appended to `tracking/`, both of which the dashboard reads, but regenerating runs `python3 scripts/generate_dashboard.py`, which fetches live prices via yfinance (slow / network-heavy). So it is **not** run automatically — it is gated on the user's choice in Step 3. Don't run it here.

2. **Show the user** the list of files that would be committed (`reports/<id>/` and any changed `tracking/` files), using `git status --short` or a plain file listing. Note that `dashboard/index.html` is included only if the user opts to regenerate it (Step 3).

3. **Ask for permission** using a single `AskUserQuestion` call with four questions:
   - "Commit and push these changes?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - "Merge the PR right after opening it? (squash; ignored if no PR)" (Yes / No)
   - "Regenerate the dashboard? (fetches live prices via yfinance — slow)" (Yes / No)

   **Put the results in the prompt itself**: on the first question ("Commit and push these changes?"), set the `preview` field of **both** options to the report path followed by the verdict table verbatim (or, for non-investable runs, the report path plus the final report's executive answer). The preview renders beside the options while the user answers — this is what guarantees the verdicts are visible at decision time.

   Wait for the user's answers. **If the user opted to regenerate**, run `python3 scripts/generate_dashboard.py` from the repo root now, before staging. Report any error output but don't block the commit on it. **If the user declines the commit**, return to `main` and drop the unused branch: `git switch main && git branch -D research/<id>` (the run's files remain in the working tree; nothing was pushed). Report and stop.

4. If the user approves the commit, run (the branch already exists — just stage, commit, push). Include `dashboard/index.html` in the `git add` **only if it was regenerated** in Step 3:

```bash
cd <repo-root>
git add reports/<id> tracking/   # + dashboard/index.html if regenerated in Step 3
git commit -m "research: <slug> (<date>)"
git push -u origin research/<id>
```

5. If the user also approved the PR, run:

```bash
gh pr create --base main --head research/<id> --title "research: <slug>" --body "<one-line: the question + effort>"
```

6. No second prompt — use the merge answer from Step 3. If the user approved the PR **and** the merge, run `gh pr merge --squash --delete-branch`; otherwise (no PR, or merge declined) leave the PR open for the user to merge on GitHub. Either way, finish on `main`:

```bash
git switch main
```

The remote is named `origin`; the branch `research/<id>` matches its run dir (e.g. `research/2026-05-28-edge-computing-agentic-ai`). The clean-tree guard already ran before branching; by commit time the uncommitted changes are this run's own files plus — only if the user opted to regenerate — `dashboard/index.html` (tracked, committed with the run). If any git/`gh` step fails, report the error and stop — don't retry destructively.

**Final message (always, including the decline path)**: the report path, the **verdict table verbatim** (investable runs — it's the at-a-glance digest the user always wants), the PR URL if opened, and the merge outcome. Repeat the table here even though Step 0 and the Step 3 preview already showed it — the final message is the only output guaranteed to stay visible. Do not paste any other report contents.

## Examples

`/research How is the AI capex cycle affecting electric utility load growth?`
→ no effort flag → asks which level (quick recommended); no sources

`/research Which companies benefit most from on-shoring of advanced packaging? effort:high`
→ implies tickers; 4 revision rounds allowed

`/research What's the state of solid-state battery commercialization? sources:https://example.com/report.pdf,notes/ssb.md effort:low`
→ seed sources provided; 1 revision round

`/research Is the GLP-1 supply chain still capacity-constrained? effort:quick`
→ fast scan: 2–3 bundles, no critique loop, single combined ticker scan, high-level theses + verdict table
