#!/usr/bin/env python3
"""
Research coverage + Market verdicts, read straight from reports/ — the one
implementation every consumer shares (dashboard, /sync-portfolio, /log-trade,
/journal). There is no tracking file for research; this reads the reports.

Usage:
  research_index.py <TICKER> [<TICKER> ...] [--json] [--all]

  For each ticker: the runs that covered it (newest first) with that run's
  Market verdict. Default output is one line per ticker with the latest run;
  --all lists every covering run; --json prints the full structure.

Coverage rule: a run covered a ticker if one of its tickers/<SYMBOL>.md files
is that ticker, or its final-report.md mentions $<SYMBOL>. Symbols are
compared as economic_group(canonical(...)) on both sides, so legacy file
spellings (000660-KS.md) and share classes of one holding (GOOGL/GOOG) match.

Verdict sources, in precedence order (formats accumulated over time): the
final report's verdict table (a row whose first cell is a ticker and some
cell carries a verdict word — the "Market" column when the header names one),
per-ticker headings ("### $NVDA — Buy"), then the per-ticker deep dives'
"Market verdict" line.

Stdlib only.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from symbols import canonical, economic_group  # noqa: E402

ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "reports"

VERDICT_WORD = re.compile(r"\b(Buy|Hold|Avoid|Accumulate|Sell|Trim|Watch)\b", re.I)
TABLE_TICKER = re.compile(r"[*$`]*([A-Z0-9][A-Z0-9.\-]{0,7})\b")
HEADING_VERDICT = re.compile(
    r"^#{2,4} \$?([A-Z0-9][A-Z0-9.\-]{0,7})\b[^\n]*?[—\-:]\s*(?:Market verdict:?\s*)?\**(Buy|Hold|Avoid)\b", re.M)
MARKET_VERDICT = re.compile(r"Market verdict\W{0,6}(Buy|Hold|Avoid)\b", re.I)
DOLLAR_TICKER = re.compile(r"\$([A-Z0-9][A-Z0-9]*(?:[.\-][A-Z0-9]+)?)")
NON_TICKER_FILES = {"ticker-scan"}


def _parse_frontmatter(text: str) -> dict:
    """Parse simple YAML frontmatter (key: value lines only)."""
    result = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip().strip('"')
    return result


def load_research_runs(reports_dir: Path = REPORTS_DIR) -> list:
    """Every run with a final-report.md: slug, date (from the dir prefix), question, investable."""
    runs = []
    if not reports_dir.exists():
        return runs
    for run_dir in sorted(reports_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        fr = run_dir / "final-report.md"
        if not fr.exists():
            continue
        fm = _parse_frontmatter(fr.read_text())
        slug = run_dir.name
        m = re.match(r"(\d{4}-\d{2}-\d{2})", slug)
        runs.append({
            "slug": slug,
            "date": m.group(1) if m else fm.get("date", ""),
            "question": fm.get("question", slug),
            "investable": fm.get("investable", "false").lower() == "true",
        })
    return runs


def parse_run_verdicts(run_dir: Path, key=economic_group) -> dict:
    """{key(ticker): Market verdict} for one research run (first source wins)."""
    out = {}
    fr = run_dir / "final-report.md"
    text = fr.read_text() if fr.exists() else ""
    market_col = None   # index of the header's "Market" column, when the table has one
    for line in text.splitlines():
        if not line.startswith("|"):
            market_col = None
            continue
        if set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0].strip("*$` ").lower() == "ticker":     # header row (any case)
            market_col = next((i for i, c in enumerate(cells) if c.strip("* ").lower().startswith("market")), None)
            continue
        m = TABLE_TICKER.match(cells[0])
        if not m or not re.search("[A-Z]", m.group(1)):
            continue
        cols = [cells[market_col]] if market_col is not None and market_col < len(cells) else cells[1:]
        v = next((VERDICT_WORD.search(c) for c in cols if VERDICT_WORD.search(c)), None)
        if v:
            out.setdefault(key(m.group(1)), v.group(1).title())
    for m in HEADING_VERDICT.finditer(text):
        out.setdefault(key(m.group(1)), m.group(2).title())
    for tf in sorted((run_dir / "tickers").glob("*.md")) if (run_dir / "tickers").is_dir() else []:
        if tf.stem in NON_TICKER_FILES:
            continue
        m = MARKET_VERDICT.search(tf.read_text())
        if m:
            out.setdefault(key(tf.stem), m.group(1).title())
    return out


def build_verdict_lookup(runs: list, reports_dir: Path = REPORTS_DIR, key=economic_group) -> dict:
    """{(key(ticker), run slug): Market verdict} across every research run."""
    lookup = {}
    for r in runs:
        for ticker, verdict in parse_run_verdicts(reports_dir / r["slug"], key).items():
            lookup[(ticker, r["slug"])] = verdict
    return lookup


def resolve_run(run, slugs: set):
    """A linked run's report dir. Some older links dropped the date prefix."""
    if not run or run in slugs:
        return run
    dated = sorted(s for s in slugs if s[11:] == run)
    return dated[-1] if dated else run


def run_tickers(run_dir: Path, key=economic_group) -> dict:
    """{key(ticker): report_path} for every ticker a run covered. report_path is the
    ticker file when one exists, else the final report."""
    covered = {}
    tdir = run_dir / "tickers"
    if tdir.is_dir():
        for tf in sorted(tdir.glob("*.md")):
            if tf.stem not in NON_TICKER_FILES:
                covered.setdefault(key(tf.stem), tf)
    fr = run_dir / "final-report.md"
    if fr.exists():
        for m in DOLLAR_TICKER.finditer(fr.read_text()):
            covered.setdefault(key(m.group(1)), fr)
    return covered


def coverage(tickers, reports_dir: Path = REPORTS_DIR) -> dict:
    """{canonical ticker: [{run, date, verdict, report_path}, ...] newest first}."""
    want = {canonical(t): economic_group(t) for t in tickers}
    runs = load_research_runs(reports_dir)
    out = {t: [] for t in want}
    for r in runs:
        run_dir = reports_dir / r["slug"]
        covered = run_tickers(run_dir)
        if not any(g in covered for g in want.values()):
            continue
        verdicts = parse_run_verdicts(run_dir)
        for t, g in want.items():
            if g in covered:
                try:
                    path = str(covered[g].relative_to(reports_dir.parent))
                except ValueError:
                    path = str(covered[g])
                out[t].append({"run": r["slug"], "date": r["date"],
                               "verdict": verdicts.get(g), "report_path": path})
    for t in out:
        out[t].sort(key=lambda x: (x["date"], x["run"]), reverse=True)
    return out


def main() -> None:
    args = sys.argv[1:]
    as_json, show_all = "--json" in args, "--all" in args
    tickers = [a for a in args if not a.startswith("--")]
    if not tickers:
        sys.exit(__doc__.strip().split("\n\n")[1])
    cov = coverage(tickers)
    if as_json:
        print(json.dumps(cov, indent=2))
        return
    for t, runs in cov.items():
        if not runs:
            print(f"{t}\tno research run on file")
            continue
        for i, r in enumerate(runs if show_all else runs[:1]):
            line = f"{t if i == 0 else ''}\t{r['date']}\t{r['run']}\t{r['verdict'] or '(no verdict)'}"
            print(line + (f"\t{len(runs)} run(s)" if i == 0 else ""))


if __name__ == "__main__":
    main()
