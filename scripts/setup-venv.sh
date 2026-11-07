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
  python3 -m venv .venv
  echo "created .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo "ok venv=$ROOT/.venv"
echo "activate: source .venv/bin/activate"
