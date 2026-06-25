#!/usr/bin/env bash
# Prism session setup — run once before starting a Claude Code session.
# Installs required Python packages and checks for system tools and env vars.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

ok()   { echo "  [ok] $*"; ((PASS++)) || true; }
warn() { echo "  [!!] $*"; ((FAIL++)) || true; }

# Pick a pip-capable Python interpreter.
# Prefer an interpreter where pip can install packages (not an externally-managed
# Homebrew Python). Try 'python' first (resolves to conda/system on many setups
# where 'python3' may be Homebrew-managed), then 'python3'.
pick_python() {
  for cmd in python python3; do
    if ! command -v "$cmd" &>/dev/null; then continue; fi
    # Check pip works without the externally-managed-environment error
    if $cmd -m pip install --dry-run pip &>/dev/null 2>&1; then
      echo "$cmd"; return
    fi
  done
  # Fallback: use whatever Python exists, even if pip is restricted
  for cmd in python python3; do
    if command -v "$cmd" &>/dev/null; then echo "$cmd"; return; fi
  done
  echo "ERROR: no python or python3 found in PATH" >&2
  exit 1
}
PYTHON="$(pick_python)"

echo ""
echo "=== Prism setup ==="
echo ""

# --- Python packages -------------------------------------------------------

echo "Python packages:"
echo "  using: $($PYTHON -c 'import sys; print(sys.executable)')"
echo ""

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

if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  ok "OPENAI_API_KEY set"
else
  warn "OPENAI_API_KEY not set — /podcast TTS synthesis will fail"
  echo "       add to .env: OPENAI_API_KEY=sk-..."
fi

if [[ -n "${NOTION_TOKEN:-}" ]]; then
  ok "NOTION_TOKEN set"
else
  warn "NOTION_TOKEN not set — /log-trade will fail with 401"
  echo "       add to .env: NOTION_TOKEN=ntn_..."
  echo "       see .env.example for setup instructions"
fi

echo ""

# --- Summary ---------------------------------------------------------------

if [[ $FAIL -eq 0 ]]; then
  echo "All checks passed. Ready to start a Prism session."
else
  echo "${FAIL} issue(s) found above. Resolve before running /podcast or /research with tickers."
fi

echo ""
