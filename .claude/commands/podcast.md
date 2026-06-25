---
description: Convert a Prism research report into a 3-voice podcast episode (script + mp3) via OpenAI TTS. Local output only.
---

Dispatch the **podcast-producer** agent to turn a finished research run into an audio episode.

Parse the user's invocation for:

- **report-dir** (required): path to the run directory (the one containing `final-report.md`). Accept absolute or repo-relative paths. If the user just gives a date/slug, resolve under `reports/`.

If no report-dir is present, ask the user for one before dispatching.

`/podcast` is an independent command — the research run it points at is expected to already be merged into `main`. **Branch off `main` before any work** (per the **Git convention** in `CLAUDE.md`). The branch name is fixed up front: `podcast/<run-dir-basename>` (the target report dir's basename, already `YYYY-MM-DD-<slug>`). Sync `main`, verify the report is present, then cut the branch:

```bash
cd <repo-root>
git status --porcelain                               # unrelated changes → stop & report
git switch main && git pull --ff-only origin main
test -f reports/<run-dir>/final-report.md || echo "report not on main — merge its research PR first"
git switch -c podcast/<run-dir-basename>
```

If `final-report.md` isn't present on `main`, bail with that message **before cutting the branch** — don't proceed. (Its research PR must be merged first; otherwise the podcast files would have no report to attach to.) If the switch, pull, or branch fails, report and stop.

Verify `OPENAI_API_KEY` is available before dispatching — either in the shell env or in `<repo-root>/.env` (a line like `OPENAI_API_KEY=sk-...`):

```bash
test -n "$OPENAI_API_KEY" || grep -q '^OPENAI_API_KEY=' .env 2>/dev/null || echo "OPENAI_API_KEY not set"
```

If unset in both places, tell the user to either `export OPENAI_API_KEY=sk-...` in their shell or add it to `<repo-root>/.env` (already in `.gitignore`), then stop — do not dispatch.

Then call the `podcast-producer` agent via the `Agent` tool with a prompt that includes:

- The absolute path to the run directory.
- Today's date.
- A note that hosting is out of scope — write `episode.mp3` and `episode.json` to `<report-dir>/podcast/` and stop there.

The producer agent owns the full pipeline (all on the branch you just created): read report → write dialogue script → synthesize per-line via OpenAI TTS (`scripts/tts_synthesize.py`) → stitch via ffmpeg → save local files.

After the producer reports `episode.mp3` is written, commit the podcast and open a PR (per the **Git convention** in `CLAUDE.md`) — `reports/<run-dir>/podcast/` is the only new (untracked) content.

1. **Show the user** the files that would be committed (`episode.mp3`, `episode.json`, `script.md`, `outline.md` under `reports/<run-dir>/podcast/`), using `git status --short` or a plain file listing.

2. **Ask for permission** using `AskUserQuestion` with two questions:
   - "Commit and push this podcast episode?" (Yes / No)
   - "Open a pull request on GitHub?" (Yes / No)

   Wait for the user's answers. **If the user declines the commit**, return to `main` and drop the branch: `git switch main && git branch -D podcast/<run-dir-basename>`. Report and stop.

3. If the user approves the commit, run (the branch already exists — just stage, commit, push):

```bash
cd <repo-root>
git add reports/<run-dir>/podcast
git commit -m "podcast: <slug> (<date>)"
git push -u origin podcast/<run-dir-basename>
```

4. If the user also approved the PR, run:

```bash
gh pr create --base main --head podcast/<run-dir-basename> --title "podcast: <slug>" --body "<one-line>"
```

5. After the PR is open, **ask the user** via `AskUserQuestion`: "Merge this PR now?" (Yes / No). If yes, run `gh pr merge --squash --delete-branch`. If no, leave the PR open for the user to merge on GitHub. Either way, finish on `main`:

```bash
git switch main
```

The remote is named `origin`; `<run-dir-basename>` carries the date+slug (e.g. `podcast/2026-05-28-edge-computing-agentic-ai`). `reports/**/podcast/segments/` is gitignored, so `git add reports/<run-dir>/podcast` stages only `episode.mp3`, `episode.json`, `script.md`, and `outline.md`. The clean-tree guard already ran before branching. If any git/`gh` step fails, report the error and stop — don't retry destructively. Then report back with the path to the mp3 and the PR URL.

## Examples

`/podcast reports/2026-05-26-spacex-ipo-impact/`

`/podcast 2026-05-26-spacex-ipo-impact`

`/podcast <repo-root>/reports/2026-05-26-spacex-ipo-impact/`
