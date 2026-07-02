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
git status --porcelain                               # unrelated changes → stop & report
git switch main && git pull --ff-only origin main
git switch -c research/<id>
```

If the switch, pull, or branch fails, report and stop — never stash, reset, or force.

Then call the `research-director` agent via the `Agent` tool with a prompt that includes:

- The user's question, verbatim.
- The sources list (or "none provided").
- The effort level.
- Today's date.
- **The run directory to use, verbatim: `reports/<id>/`.** The director must write into exactly this dir — it does not invent its own slug or path.

The director handles everything else: planning, allocation, parallel dispatch, adaptive critique loop, and final synthesis — all on the branch you just created.

After the director reports `final-report.md` is written:

1. **Dashboard regeneration is optional** — the run added a report and may have appended to `tracking/`, both of which the dashboard reads, but regenerating runs `python3 scripts/generate_dashboard.py`, which fetches live prices via yfinance (slow / network-heavy). So it is **not** run automatically — it is gated on the user's choice in Step 3. Don't run it here.

2. **Show the user** the list of files that would be committed (`reports/<id>/` and any changed `tracking/` files), using `git status --short` or a plain file listing. Note that `dashboard/index.html` is included only if the user opts to regenerate it (Step 3).

3. **Ask for permission** using `AskUserQuestion` with three questions:
   - "Commit and push these changes?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - "Regenerate the dashboard? (fetches live prices via yfinance — slow)" (Yes / No)

   Wait for the user's answers. **If the user opted to regenerate**, run `python3 scripts/generate_dashboard.py` from the repo root now, before staging. Report any error output but don't block the commit on it. **If the user declines the commit**, return to `main` and drop the unused branch: `git switch main && git branch -D research/<id>` (the run's files remain in the working tree; nothing was pushed). Report and stop.

4. If the user approves the commit, run (the branch already exists — just stage, commit, push). Include `dashboard/index.html` in the `git add` **only if it was regenerated** in Step 3:

```bash
cd <repo-root>
git add reports/<id> tracking/ .claude/cusip-map.json   # + dashboard/index.html if regenerated in Step 3
# (.claude/cusip-map.json: the 13F script may cache a newly resolved CUSIP during the run —
#  stage it with the run so the tree is clean after merge; a no-op when unchanged)
git commit -m "research: <slug> (<date>)"
git push -u origin research/<id>
```

5. If the user also approved the PR, run:

```bash
gh pr create --base main --head research/<id> --title "research: <slug>" --body "<one-line: the question + effort>"
```

6. After the PR is open, **ask the user** via `AskUserQuestion`: "Merge this PR now?" (Yes / No). If yes, run `gh pr merge --squash --delete-branch`. If no, leave the PR open for the user to merge on GitHub. Either way, finish on `main`:

```bash
git switch main
```

The remote is named `origin`; the branch `research/<id>` matches its run dir (e.g. `research/2026-05-28-edge-computing-agentic-ai`). The clean-tree guard already ran before branching; by commit time the uncommitted changes are this run's own files plus — only if the user opted to regenerate — `dashboard/index.html` (tracked, committed with the run). If any git/`gh` step fails, report the error and stop — don't retry destructively.

If the director's reply included a **verdict table** (investable runs), relay it to the user verbatim in your final message alongside the report path and PR URL — it's the at-a-glance digest the user always wants. Do not paste any other report contents.

## Examples

`/research How is the AI capex cycle affecting electric utility load growth?`
→ effort=medium, no sources

`/research Which companies benefit most from on-shoring of advanced packaging? effort:high`
→ implies tickers; 4 revision rounds allowed

`/research What's the state of solid-state battery commercialization? sources:https://example.com/report.pdf,notes/ssb.md effort:low`
→ seed sources provided; 1 revision round

`/research Is the GLP-1 supply chain still capacity-constrained? effort:quick`
→ fast scan: 2–3 bundles, no critique loop, single combined ticker scan, high-level theses + verdict table
