---
description: Sync engine files between the two Prism repos (prism-shared = public engine, prism = private daily driver). Diffs the engine file set, determines direction per file from git history, and lands the changes as a PR in the receiving repo. Sanitizes anything flowing into the public repo.
---

Sync engine changes between **prism-shared** (public engine, canonical) and **prism** (private daily driver). See **Two-repo sync** in `CLAUDE.md` for the relationship and the engine/personal file split.

## Steps

1. **Locate both repos** — identify which repo you are in via `git remote get-url origin` (`prism-shared.git` → engine repo; `prism.git` → personal repo). The sibling is expected at `../prism` or `../prism-shared` relative to the repo root. If the sibling is missing, report and stop.

2. **Guard both repos** — in each repo: `git status --porcelain` must be clean (unrelated changes → stop and report; never stash), then `git switch main && git pull --ff-only origin main`. Any failure: report and stop.

3. **Diff the engine file set** — compare the two working trees over engine files only:

   ```bash
   diff -rq <shared> <prism> \
     --exclude=.git --exclude=reports --exclude=scouts --exclude=memos \
     --exclude=.env --exclude=.DS_Store
   ```

   Then drop the **expected diffs** (personal data that must never converge):
   - `tracking/*.json` — real data in prism, empty templates in prism-shared
   - `dashboard/index.html` — generated output; template changes live in `scripts/generate_dashboard.py`
   - `.claude/scout-x-feeds.json` — real follows in prism, placeholder examples in prism-shared
   - `.claude/settings.local.json`, `.claude/podcast-cast.json` — local/personal config

   What remains is the **unsynced engine delta**. If empty, report "in sync" and stop.

4. **Determine direction per file** — for each differing file, compare last-touch dates: `git log -1 --format='%cI %h %s' -- <file>` in each repo. The newer side is the presumed source. Read the newer commit's message — sync commits reference the other repo's PR number (e.g. "Ports prism#168", "from prism-shared (#5)"), which tells you whether the change already flowed and only a cleanup remains.

5. **Confirm the plan** — show a table: file · direction · source commit · one-line rationale. Use `AskUserQuestion` to confirm which files to sync (per-file opt-out). Never mix directions in one PR — if both directions have deltas, run two passes.

6. **Sanitize anything flowing into prism-shared** — before copying a file prism → prism-shared, scan it for personal data: real account handles, Notion/database IDs, API keys or tokens, absolute personal paths (e.g. `/Users/...`, `/root/...`), personal email addresses, hardcoded env values. Generalize or strip them (read from env, use placeholders). If sanitization changes the file meaningfully, note it in the commit message ("cleaned up on port: ..."). prism → prism-shared is a **port**, not a blind copy.

7. **Land as a PR in the receiving repo** — follow the **Git convention**: in the receiving repo, cut `sync/<YYYY-MM-DD>-<slug>` off fresh `main`, copy the confirmed files in, sanity-check (`python3 -c "import ast; ast.parse(open('<file>').read())"` for any `.py`), commit as `sync: <what> from <source repo> (#<source PR>)`, then gate *commit & push?* / *open PR?* / *merge?* with `AskUserQuestion` exactly as the other commands do. Finish on `main` in both repos.

8. **Report** — list what was synced in which direction, what was skipped as expected-diff, and any sanitization applied.
