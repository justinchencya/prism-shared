#!/usr/bin/env python3
"""
Read-only consistency check across Prism's data. Reports; never fixes.

Usage:
  check_consistency.py                 full scan — lists every issue, exit 0
  check_consistency.py --paths P ...   only issues these files / run dirs gained since git
                                       HEAD (the run's own writes) — exit 1 if any
  check_consistency.py --json          machine-readable output (either mode)

Full scans are informational: legacy artifacts written before a rule existed
are reported, not failed — readers tolerate them (see scripts/symbols.py).
Commands that write data run it with --paths on what they just wrote, before
the ship gate. --paths subtracts the issues the same paths already had at git
HEAD, so legacy entries in an appended-to file never block — only the run's
own new inconsistencies do. Commands show those to the user instead of
patching them.

Checks:
  symbol       a ticker that isn't canonical (BRKB, BRK.B, 000660-KS)
  not-a-quote  a ticker that isn't a quote symbol at all (company names,
               pair notes like SPCX-SPY) — tracking files and report
               tickers/<SYMBOL>.md names
  link         trades/journal linked_research report_path that doesn't exist
  raw-symbol   brokerage snapshot rows missing raw_symbol (pre-normalization
               snapshot)
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from symbols import canonical, is_quote_symbol  # noqa: E402

ROOT = Path(__file__).parent.parent
TRACKING = ROOT / "tracking"
REPORTS = ROOT / "reports"
NON_TICKER_FILES = {"ticker-scan"}


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(p)


def _sym_issues(where: str, sym) -> list:
    if not sym:
        return []
    c = canonical(sym)
    if sym != c:
        return [{"check": "symbol", "where": where, "detail": f"{sym!r} is not canonical — use {c!r}"}]
    if not is_quote_symbol(sym):
        return [{"check": "not-a-quote", "where": where, "detail": f"{sym!r} is not a quote symbol"}]
    return []


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def check_trades(path: Path) -> list:
    out, data = [], _load(path) or {}
    for t in data.get("trades", []):
        where = f"{_rel(path)}:{t.get('id')}"
        out += _sym_issues(where, t.get("ticker"))
        for link in t.get("linked_research") or []:
            rp = link.get("report_path")
            if rp and not (ROOT / rp).exists():
                out.append({"check": "link", "where": where, "detail": f"report_path {rp} does not exist"})
    return out


def check_journal(path: Path) -> list:
    out, data = [], _load(path) or {}
    for e in data.get("entries", []):
        where = f"{_rel(path)}:{e.get('id')}"
        for s in e.get("linked_tickers") or []:
            out += _sym_issues(where, s)
        for link in e.get("linked_research") or []:
            rp = link.get("report_path")
            if rp and not (ROOT / rp).exists():
                out.append({"check": "link", "where": where, "detail": f"report_path {rp} does not exist"})
    return out


def check_hypotheticals(path: Path) -> list:
    out, data = [], _load(path) or {}
    for sc in data.get("scenarios", []):
        where = f"{_rel(path)}:{sc.get('id')}"
        out += _sym_issues(where, sc.get("benchmark_ticker"))
        for o in sc.get("overrides") or []:
            for k in ("from", "to", "ticker"):
                out += _sym_issues(where, o.get(k))
        for t in sc.get("trades") or []:
            out += _sym_issues(where, t.get("ticker"))
    return out


def check_snapshot(path: Path) -> list:
    out, data = [], _load(path) or {}
    for a in data.get("accounts", []):
        rows = [("position", p, "symbol") for p in a.get("positions", [])] + \
               [("activity", x, "symbol") for x in a.get("activities", [])] + \
               [("option", o, "underlying") for o in a.get("options", [])]
        missing = 0
        for kind, row, key in rows:
            sym = row.get(key)
            if sym and sym != canonical(sym):
                out.append({"check": "symbol", "where": f"{_rel(path)}:{kind}",
                            "detail": f"{sym!r} is not canonical — use {canonical(sym)!r}"})
            if sym and "raw_symbol" not in row and "raw_underlying" not in row:
                missing += 1
        if missing:
            out.append({"check": "raw-symbol", "where": _rel(path),
                        "detail": f"{missing} row(s) without raw_symbol — snapshot predates symbol normalization"})
    return out


def check_run(run_dir: Path) -> list:
    out = []
    tdir = run_dir / "tickers"
    for tf in sorted(tdir.glob("*.md")) if tdir.is_dir() else []:
        if tf.stem not in NON_TICKER_FILES:
            out += _sym_issues(_rel(tf), tf.stem)
    return out


CHECKERS = {
    "trades.json": check_trades,
    "journal.json": check_journal,
    "hypotheticals.json": check_hypotheticals,
    "brokerage-snapshot.json": check_snapshot,
}


def check_path(p: Path) -> list:
    p = p if p.is_absolute() else ROOT / p
    if p.is_dir() and p.parent.resolve() == REPORTS.resolve():
        return check_run(p)
    if p.is_file() and p.parent.name == "tickers":
        return check_run(p.parent.parent)
    if p.name in CHECKERS:
        return CHECKERS[p.name](p)
    if p.is_dir() and p.resolve() == REPORTS.resolve():
        return [i for d in sorted(p.iterdir()) if d.is_dir() for i in check_run(d)]
    if p.is_file() and p.parent.parent.resolve() == REPORTS.resolve():
        return check_run(p.parent)               # any file inside a run dir → that run
    print(f"consistency: not checked (no rule for this path): {p}", file=sys.stderr)
    return []


def _git(*args) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def _key(issue: dict) -> tuple:
    return issue["check"], issue["where"], issue["detail"]


def head_issues(p: Path) -> set:
    """Issue keys the same path already had at git HEAD (legacy, not this run's)."""
    p = p if p.is_absolute() else ROOT / p
    rel = _rel(p)
    if p.name in CHECKERS:
        blob = _git("show", f"HEAD:{rel}")
        if not blob:
            return set()
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t) / p.name
            tmp.write_text(blob)
            return {(c, w.replace(_rel(tmp), rel, 1), d) for c, w, d in map(_key, CHECKERS[p.name](tmp))}
    tracked = set(_git("ls-tree", "-r", "--name-only", "HEAD", "--", rel).split())
    return {_key(i) for i in check_path(p) if i["where"].split(":")[0] in tracked}


def main() -> None:
    args = sys.argv[1:]
    as_json = "--json" in args
    scoped = "--paths" in args
    paths = [Path(a) for a in args if not a.startswith("--")]
    if not scoped:
        paths = [TRACKING / n for n in CHECKERS] + [REPORTS]
    issues = []
    for p in paths:
        found = check_path(p)
        if scoped:
            legacy = head_issues(p)
            found = [i for i in found if _key(i) not in legacy]
        issues += found
    if as_json:
        print(json.dumps({"scoped": scoped, "issues": issues}, indent=2))
    elif not issues:
        print("consistency: no issues" + (" in scope" if scoped else ""))
    else:
        by = {}
        for i in issues:
            by.setdefault(i["check"], []).append(i)
        for check, rows in by.items():
            print(f"[{check}] {len(rows)}")
            for r in rows:
                print(f"  {r['where']}: {r['detail']}")
    if scoped and issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
