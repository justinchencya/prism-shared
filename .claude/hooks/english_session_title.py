#!/usr/bin/env python3
"""UserPromptSubmit hook: keep session titles English.

Claude Code titles a session from the first prompt with its own model call,
which never reads CLAUDE.md, so a mixed English/Chinese prompt can yield a
Chinese title. This hook runs on the first prompt of a session only and, when
that prompt contains CJK text, sets an English title built from the slash
command plus the prompt's English words / tickers. English prompts are left
to the default titler. Stdlib only; any failure exits 0 silently.
"""
import json
import os
import re
import sys
import tempfile
from datetime import date

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿＀-￯]")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9.&'\-]*")
FLAG = re.compile(r"\b(?:effort|sources):\S*", re.I)
MAX_WORDS = 8


def title_for(prompt: str) -> str:
    text = prompt.strip()
    command = ""
    m = re.match(r"/([a-z][a-z0-9\-]*)\s*", text)
    if m:
        command, text = m.group(1), text[m.end():]
    text = FLAG.sub(" ", text)
    text = re.sub(r"https?://\S+", " ", text)
    words = [w.strip(".-'") for w in WORD.findall(text)]
    words = [w for w in words if w][:MAX_WORDS]
    if command and words:
        return f"{command}: {' '.join(words)}"
    if command:
        return f"{command} ({date.today().isoformat()})"
    if words:
        return " ".join(words)
    return f"Prism session ({date.today().isoformat()})"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    session_id = str(payload.get("session_id") or "")
    prompt = payload.get("prompt") or ""
    if not session_id or not isinstance(prompt, str):
        return

    # First prompt of the session only — later prompts never retitle.
    marker_dir = os.path.join(tempfile.gettempdir(), "prism-session-titles")
    marker = os.path.join(marker_dir, re.sub(r"[^A-Za-z0-9_\-]", "_", session_id))
    try:
        os.makedirs(marker_dir, exist_ok=True)
        if os.path.exists(marker):
            return
        open(marker, "w").close()
    except OSError:
        return

    if not CJK.search(prompt):
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "sessionTitle": title_for(prompt)[:80],
        }
    }))


if __name__ == "__main__":
    main()
