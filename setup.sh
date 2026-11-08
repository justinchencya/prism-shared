#!/usr/bin/env bash
# Prism session setup — run once before starting a Claude Code session.
# Installs required Python packages and checks for system tools and env vars.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

ok()   { echo "  [ok] $*"; ((PASS++)) || true; }
warn() { echo "  [!!] $*"; ((FAIL++)) || true; }
info() { echo "  [--] $*"; }   # optional / degrades gracefully — not counted as a failure

# The interpreter every command runs: `python3` (commands invoke
# `python3 scripts/...`). Packages must land in *that* interpreter — installing
# into `python` breaks wherever `python` and `python3` resolve differently.
# Override with PRISM_PYTHON=/path/to/python3.x to pin a specific one.
PYTHON="${PRISM_PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
  echo "ERROR: $PYTHON not found in PATH (set PRISM_PYTHON to a Python 3.10+ interpreter)" >&2
  exit 1
fi
MIN_PY="3.10"   # scripts use PEP 604 annotations (str | None); stdlib-only scripts are 3.8+

echo ""
echo "=== Prism setup ==="
echo ""

# --- Python packages -------------------------------------------------------

echo "Python:"
PY_VER="$($PYTHON -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= tuple(map(int, '$MIN_PY'.split('.'))) else 1)"; then
  ok "$PYTHON is Python $PY_VER ($($PYTHON -c 'import sys; print(sys.executable)'))"
else
  warn "$PYTHON is Python $PY_VER — Prism needs $MIN_PY+; set PRISM_PYTHON to a newer interpreter"
fi
# Every engine script must at least compile on this interpreter — catches
# version-specific syntax before a scheduled run trips over it.
BAD_PY=""
for f in "$REPO_DIR"/scripts/*.py "$REPO_DIR"/.claude/hooks/*.py; do
  [[ -f "$f" ]] || continue
  $PYTHON -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null || BAD_PY="$BAD_PY ${f#$REPO_DIR/}"
done
if [[ -z "$BAD_PY" ]]; then
  ok "all scripts compile on Python $PY_VER"
else
  warn "scripts that don't compile on Python $PY_VER:$BAD_PY"
fi
echo ""

echo "Python packages:"

install_if_missing() {
  local pkg="$1"
  local import_name="${2:-$1}"
  if $PYTHON -c "import ${import_name}" 2>/dev/null; then
    ok "${pkg} already installed"
    return
  fi
  echo "  ... installing ${pkg}"
  # Try normal install first; fall back to --user if the env is externally managed (PEP 668)
  if ! $PYTHON -m pip install -q "${pkg}" 2>/dev/null; then
    $PYTHON -m pip install -q --user "${pkg}" 2>/dev/null || true
  fi
  if $PYTHON -c "import ${import_name}" 2>/dev/null; then
    ok "${pkg} installed"
  else
    warn "${pkg} install failed — try manually: ${PYTHON} -m pip install --user ${pkg}"
  fi
}

install_if_missing "yfinance"
# matplotlib needs a working Pillow. Some images ship a distro Pillow built for
# another Python that imports but can't load its C module — install a private
# copy over it when that's the case.
if ! $PYTHON -c "from PIL import Image" 2>/dev/null; then
  $PYTHON -m pip install -q --ignore-installed pillow 2>/dev/null \
    || $PYTHON -m pip install -q --user --ignore-installed pillow 2>/dev/null || true
fi
install_if_missing "matplotlib"   # annotated K-line charts (scripts/technical_structure.py)

echo ""

# --- System tools ----------------------------------------------------------

echo "System tools:"

if command -v ffmpeg &>/dev/null; then
  ok "ffmpeg found ($(ffmpeg -version 2>&1 | head -1 | awk '{print $3}'))"
else
  warn "ffmpeg not found — podcast audio stitching (/podcast) will fail"
  echo "       install: brew install ffmpeg"
fi

if command -v gh &>/dev/null; then
  ok "gh (GitHub CLI) found"
else
  warn "gh not found — git PR creation will fail"
  echo "       install: brew install gh"
fi

echo ""

# --- Environment variables -------------------------------------------------

echo "Environment variables:"

# Load .env so these checks see values configured there, not just exported shell
# vars.
if [[ -f "$REPO_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_DIR/.env"
  set +a
  echo "  (loaded values from $REPO_DIR/.env)"
fi

# Required for the feature noted; missing → warn (counts as an issue).
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  ok "OPENAI_API_KEY set"
else
  warn "OPENAI_API_KEY not set — /podcast TTS synthesis will fail"
  echo "       add to .env: OPENAI_API_KEY=sk-..."
fi

# Optional — the engine degrades gracefully without these; missing → info, not a failure.
if [[ -n "${EDGAR_CONTACT_EMAIL:-}" ]]; then
  ok "EDGAR_CONTACT_EMAIL set"
else
  info "EDGAR_CONTACT_EMAIL not set — SEC EDGAR requests use a generic placeholder User-Agent"
  echo "       recommended. add to .env: EDGAR_CONTACT_EMAIL=you@example.com"
fi

if [[ -n "${X_BEARER_TOKEN:-}" ]]; then
  ok "X_BEARER_TOKEN set"
else
  info "X_BEARER_TOKEN not set — /scout skips the X signal source (runs on GDELT/HN/EDGAR)"
  echo "       optional. add to .env: X_BEARER_TOKEN=..."
fi

if [[ -n "${SNAPTRADE_CLIENT_ID:-}" && -n "${SNAPTRADE_CONSUMER_KEY:-}" ]]; then
  ok "SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY set"
else
  info "SnapTrade credentials not set — /sync-portfolio runs review-only off the committed snapshot"
  echo "       optional. add to .env: SNAPTRADE_CLIENT_ID=... and SNAPTRADE_CONSUMER_KEY=...   (see .env.example)"
fi

echo ""

# --- Settings files --------------------------------------------------------

echo "Settings files:"

if [[ -f "$REPO_DIR/.env" ]]; then
  ok ".env present"
elif [[ -n "${OPENAI_API_KEY:-}${SNAPTRADE_CLIENT_ID:-}${EDGAR_CONTACT_EMAIL:-}${X_BEARER_TOKEN:-}" ]]; then
  info ".env not found — keys come from the environment instead (hosted sandbox); nothing to do"
else
  warn ".env not found — copy the template and fill in your keys"
  echo "       cp .env.example .env"
fi

XFEEDS="$REPO_DIR/.claude/scout-x-feeds.json"
if [[ -f "$XFEEDS" ]]; then
  if grep -q "example_account_1" "$XFEEDS" 2>/dev/null; then
    info ".claude/scout-x-feeds.json is still the example template — edit it with accounts you follow"
    echo "       only used when X_BEARER_TOKEN is set; harmless to leave as-is otherwise"
  else
    ok ".claude/scout-x-feeds.json customized"
  fi
else
  info ".claude/scout-x-feeds.json absent — /scout runs without the X source"
fi

echo ""

# --- Summary ---------------------------------------------------------------

if [[ $FAIL -eq 0 ]]; then
  echo "All checks passed. Ready to start a Prism session."
else
  echo "${FAIL} issue(s) found above. Resolve before running /podcast or /research with tickers."
fi

echo ""
