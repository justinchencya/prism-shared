#!/usr/bin/env python3
"""
Ticker identity for Prism — the one place symbol spellings are decided.

Usage:
  symbols.py <SYMBOL> [<SYMBOL> ...]   print each symbol's canonical form, one per line
  symbols.py --check <SYMBOL> ...      same, but exit 1 if any isn't a well-formed quote symbol

Canonical form = the Yahoo Finance quote spelling (yfinance is Prism's price
source): US share classes take a hyphen (BRK-B), foreign listings keep their
exchange suffix after a dot (000660.KS, 2344.TW, 7741.T). Crypto stays as
tracked (BTC) — quote_symbol() maps it to the Yahoo pair (BTC-USD) only when
fetching prices.

Rules:
  - Normalize at write boundaries (fetchers, /log-trade, /journal, /what-if,
    research dispatch); readers canonicalize both sides before comparing, so
    legacy spellings already on disk keep working — nothing is backfilled.
  - canonical() fixes *spelling* only — it never merges two real securities.
    economic_group() is the separate, deliberate merge of share classes that
    are one holding for research/dashboard purposes (GOOGL → GOOG). Trade
    reconciliation never uses it: GOOGL and GOOG are different securities.

Stdlib only.
"""

import re
import sys

# Broker spellings a structural rule can't split (no separator). Extend here.
BROKER_ALIASES = {
    "BRKB": "BRK-B",
    "BRKA": "BRK-A",
    "BFB": "BF-B",
    "BFA": "BF-A",
}

# Yahoo exchange suffixes (listing venue, not share class). Kept after a dot.
EXCHANGE_SUFFIXES = {
    "T", "KS", "KQ", "HK", "TW", "TWO", "SS", "SZ", "SI", "KL", "JK", "BK",
    "NS", "BO", "AX", "NZ", "L", "IL", "DE", "F", "PA", "AS", "BR", "LS", "MC",
    "MI", "SW", "VI", "ST", "CO", "OL", "HE", "IR", "TO", "V", "NE", "SA",
    "MX", "TA", "JO", "IS",
}

# Share classes that are the same economic holding (research/dashboard view).
ECONOMIC_GROUPS = {
    "GOOGL": "GOOG",
}

# Tracking symbols whose Yahoo quote symbol differs.
QUOTE_ALIASES = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}

_SUFFIXED = re.compile(r"^([A-Z0-9]{1,10})[.\-/ ]([A-Z]{1,3})$")
_US = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
_FOREIGN = re.compile(r"^[A-Z0-9]{1,10}\.([A-Z]{1,3})$")


def canonical(raw) -> str:
    """The canonical spelling of a ticker. Idempotent; unknown shapes pass through
    uppercased (is_quote_symbol() is what flags them)."""
    s = str(raw or "").strip().strip("`*").lstrip("$").strip().upper()
    if s in BROKER_ALIASES:
        return BROKER_ALIASES[s]
    m = _SUFFIXED.match(s)
    if m:
        base, suffix = m.groups()
        if suffix in EXCHANGE_SUFFIXES:
            return f"{base}.{suffix}"
        if len(suffix) == 1 and base.isalpha():
            return f"{base}-{suffix}"          # US share class: BRK.B / BRK/B → BRK-B
    return s


def economic_group(raw) -> str:
    """Canonical symbol, with share classes of one holding merged (GOOGL → GOOG)."""
    c = canonical(raw)
    return ECONOMIC_GROUPS.get(c, c)


def quote_symbol(raw) -> str:
    """The symbol to request from Yahoo Finance."""
    c = canonical(raw)
    return QUOTE_ALIASES.get(c, c)


def is_quote_symbol(raw) -> bool:
    """True if `raw` is already canonical and shaped like a real quote symbol
    (catches company names, pair notes like SPCX-SPY, typos like SHA0)."""
    s = str(raw or "")
    if s != canonical(s):
        return False
    if s in QUOTE_ALIASES or s in QUOTE_ALIASES.values():
        return True
    if _US.match(s):
        return True
    m = _FOREIGN.match(s)
    return bool(m and m.group(1) in EXCHANGE_SUFFIXES)


def main() -> None:
    args = sys.argv[1:]
    check = "--check" in args
    syms = [a for a in args if a != "--check"]
    if not syms:
        sys.exit(__doc__.strip().split("\n\n")[1])
    bad = []
    for s in syms:
        c = canonical(s)
        print(c)
        if not is_quote_symbol(c):
            bad.append(s)
    if bad:
        print(f"symbols: not a well-formed quote symbol: {', '.join(bad)}", file=sys.stderr)
        if check:
            sys.exit(1)


if __name__ == "__main__":
    main()
