---
description: Mine free signals (GDELT, Hacker News, SEC EDGAR) for emerging themes within a focus you give it, and write a ranked brief. Discovery only — it never kicks off research.
---

Run the **scout**: discover candidate research themes from cheap, no-auth signal sources and produce a ranked brief. The brief is the only deliverable — the scout never researches anything. If a candidate looks worth pursuing, you run `/research` on it yourself, separately.

Everything the user typed after `/scout` is the **focus** — the direction for this run (e.g. `/scout semiconductors and datacenter power`). The scout translates it into the concrete GDELT/HN/EDGAR queries it fetches.

**A focus is required.** If the user typed nothing after `/scout`, ask them what direction to scout before dispatching — do not run a directionless pass.

**Branch off `main` before any work** (per the **Git convention** in `CLAUDE.md`). Compute the run timestamp up front so the branch and run dir are fixed before the agent runs:

1. Compute the run timestamp: `date +%Y-%m-%d-%H%M%S`. The run dir is `scouts/<timestamp>/` and the branch is `scout/<timestamp>`.
2. Clean-tree guard, sync, and cut the branch:

```bash
cd <repo-root>
test -e .prism-engine && echo "engine repo"                 # marker present → stop: run this in the private repo
git status --porcelain                               # unrelated changes → stop & report
git switch main && git pull --ff-only origin main
git switch -c scout/<timestamp>
```

If the switch, pull, or branch fails, report and stop — never stash, reset, or force.

Call the `scout` agent via the `Agent` tool with a prompt that includes:

- The **focus**, verbatim.
- Today's date.
- **The run directory to use, verbatim: `scouts/<timestamp>/`.** The agent writes into exactly this dir — it does not compute its own timestamp.

The agent owns the pipeline (all on the branch you just created): expand the focus into `scouts/<timestamp>/queries.json` → `scripts/fetch_signals.py scouts/<timestamp> scouts/<timestamp>/queries.json` → overlap-check against `reports/` → cluster + score → write `scouts/<timestamp>/brief.md`. It returns a terse ranked candidate list (rank, headline, why-now, ready-to-run question, suggested effort, investable y/n).

If the agent reports that all sources came back empty (e.g. GDELT fully rate-limited and HN/EDGAR thin), tell the user, point them at the brief, and stop — don't fabricate picks. (The branch already exists; if you stop without committing, drop it: `git switch main && git branch -D scout/<timestamp>`.)

The scout run is a self-contained artifact. To commit it:

1. **Show the results first, then the files** — before any prompt, output as plain text the path to `brief.md` and the agent's terse ranked candidate list (with the `/research "<question>"` reminder below), so the user can read it while answering. Text emitted mid-turn between tool calls is not reliably visible, so the list is also carried inside the Step 2 prompt and repeated in the final report-back. Then show the file that would be committed (`scouts/<timestamp>/brief.md` and supporting files).

2. **Ask for permission** using a single `AskUserQuestion` call with three questions:
   - "Commit and push the scout brief?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)
   - "Merge the PR right after opening it? (squash; ignored if no PR)" (Yes / No)

   **Put the results in the prompt itself**: on the first question, set the `preview` field of **both** options to the `brief.md` path followed by the ranked candidate list — this is what guarantees the list is visible at decision time.

   Wait for the user's answers. **If the user declines the commit**, return to `main` and drop the branch: `git switch main && git branch -D scout/<timestamp>`. Report and stop.

3. If the user approves the commit, run (the branch already exists — just stage, commit, push):

```bash
cd <repo-root>
git add scouts/<timestamp>
git commit -m "scout: brief <timestamp>"
git push -u origin scout/<timestamp>
```

4. If the user also approved the PR, run:

```bash
gh pr create --base main --head scout/<timestamp> --title "scout: brief <timestamp>" --body "<one-line: focus + candidate count>"
```

5. No second prompt — if the user approved the PR **and** the merge, run `gh pr merge --squash --delete-branch`; otherwise leave the PR open for the user to merge on GitHub. Either way, finish on `main`:

```bash
git switch main
```

The remote is named `origin`. The clean-tree guard already ran before branching; by commit time the only uncommitted content is this run's own `scouts/<timestamp>/` files. If any git/`gh` step fails, report the error and stop — don't retry destructively.

Report back briefly (always, including the decline path): the path to `brief.md`, the terse ranked candidate list, the PR URL (if opened), and the merge outcome. Repeat the list here even though Step 1 and the Step 2 preview already showed it — the final message is the only output guaranteed to stay visible. Remind the user that to pursue a candidate they run `/research "<the suggested question from the brief>"` — the scout does not start research itself.

## Examples

`/scout semiconductors and advanced packaging`
→ scouts that direction; writes a brief; opens a `scout/<ts>` PR; reports candidates.

`/scout datacenter power and the electric-utility load cycle`
→ same, focused on that sub-area.

`/scout`
→ no focus given; the command asks what direction to scout before running.
