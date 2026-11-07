#!/usr/bin/env bash
# Create or reuse ./.venv and install requirements.txt.
# Never installs AWS/Langfuse Cloud. Never overwrites an existing env file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required (3.10+)." >&2
  exit 1
fi

if [[ -d .venv ]]; then
  echo "reusing existing .venv (not deleted)"
else
  if ! python3 -m venv .venv; then
    echo "ERROR: python3 -m venv failed (often missing python3-venv / ensurepip)." >&2
    echo "This script will not apt-get or brew install anything." >&2
    echo "On Debian/Ubuntu, install python3-venv yourself, or run:" >&2
    echo "  python3 -m pip install --user -r requirements.txt" >&2
    echo "  python3 -m pytest tests/ -q" >&2
    rm -rf .venv
    exit 1
  fi
  echo "created .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo "ok venv=$ROOT/.venv"
echo "activate: source .venv/bin/activate"
