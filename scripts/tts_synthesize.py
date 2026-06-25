#!/usr/bin/env python3
"""
Synthesize a Prism podcast script via OpenAI TTS.

Usage:
  tts_synthesize.py <report-dir>

Reads:
  <report-dir>/podcast/script.md
  <repo-root>/.claude/podcast-cast.json   (cast voices + per-role style instructions)

Writes:
  <report-dir>/podcast/segments/NNN-<role>.mp3   (one file per [ROLE] line, in script order)

Requires:
  OPENAI_API_KEY in env.
  Python 3.8+. No third-party packages (uses stdlib urllib).
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.openai.com/v1/audio/speech"
LINE_RE = re.compile(r"^\[(MODERATOR|LEAD|SKEPTIC)\]\s+(.+)$")


def die(msg: str, code: int = 1) -> None:
    print(f"tts_synthesize: {msg}", file=sys.stderr)
    sys.exit(code)


def load_dotenv(repo_root: Path) -> None:
    """Load KEY=VALUE pairs from <repo-root>/.env into os.environ if not already set."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_cast(repo_root: Path) -> dict:
    cfg_path = repo_root / ".claude" / "podcast-cast.json"
    if not cfg_path.exists():
        die(f"cast config not found at {cfg_path} — producer must create it before synthesis")
    with open(cfg_path) as f:
        cfg = json.load(f)
    for role in ("moderator", "lead", "skeptic"):
        if role not in cfg or "voice" not in cfg[role]:
            die(f"cast config missing role '{role}' or its 'voice' field")
    if "model" not in cfg:
        die("cast config missing 'model' field")
    return cfg


def parse_script(script_path: Path) -> list[tuple[str, str]]:
    if not script_path.exists():
        die(f"script not found at {script_path}")
    turns: list[tuple[str, str]] = []
    with open(script_path) as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = LINE_RE.match(line)
            if not m:
                die(f"script line {lineno} does not match '[ROLE] text': {line[:80]}")
            turns.append((m.group(1).lower(), m.group(2).strip()))
    if not turns:
        die("script has no speaker turns")
    return turns


def synthesize(api_key: str, model: str, voice: str, text: str, instructions: str) -> bytes:
    body = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        body["instructions"] = instructions
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from None


def main() -> None:
    if len(sys.argv) != 2:
        die("usage: tts_synthesize.py <report-dir>")
    report_dir = Path(sys.argv[1]).resolve()
    if not report_dir.is_dir():
        die(f"report-dir not a directory: {report_dir}")

    # Repo root = walk up from report-dir until we find a `.claude/` sibling.
    repo_root = report_dir
    while repo_root != repo_root.parent and not (repo_root / ".claude").is_dir():
        repo_root = repo_root.parent
    if not (repo_root / ".claude").is_dir():
        die(f"could not locate repo root (no .claude/ found above {report_dir})")

    load_dotenv(repo_root)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        die(f"OPENAI_API_KEY not set (checked env and {repo_root}/.env)")

    cast = load_cast(repo_root)
    script_path = report_dir / "podcast" / "script.md"
    turns = parse_script(script_path)

    segments_dir = report_dir / "podcast" / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    model = cast["model"]
    total = len(turns)
    print(f"synthesizing {total} turns to {segments_dir}", file=sys.stderr)

    for idx, (role, text) in enumerate(turns, start=1):
        role_cfg = cast[role]
        voice = role_cfg["voice"]
        instructions = role_cfg.get("instructions", "")
        out_path = segments_dir / f"{idx:03d}-{role}.mp3"
        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"[{idx:03d}/{total}] skip {role} (exists)", file=sys.stderr)
            continue
        try:
            audio = synthesize(api_key, model, voice, text, instructions)
        except RuntimeError as e:
            die(f"line {idx} ({role}) failed: {e}")
        out_path.write_bytes(audio)
        print(f"[{idx:03d}/{total}] {role} -> {out_path.name} ({len(audio)} bytes)", file=sys.stderr)

    print(str(segments_dir))


if __name__ == "__main__":
    main()
