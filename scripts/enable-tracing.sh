#!/usr/bin/env bash
# Enable TRACE_TO_LANGFUSE on an existing gitignored env file.
# Never copies the example over a working file. Never prints secret values.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.kiro/langfuse.local.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE does not exist." >&2
  echo "Copy the example first (once):" >&2
  echo "  cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env && chmod 600 .kiro/langfuse.local.env" >&2
  echo "Then put YOUR Langfuse project keys in that file. This script will not invent keys." >&2
  exit 1
fi

chmod 600 "$ENV_FILE" 2>/dev/null || true

if grep -q '^TRACE_TO_LANGFUSE=true' "$ENV_FILE"; then
  echo "ok TRACE_TO_LANGFUSE already true (file not rewritten)"
  exit 0
fi

if grep -q '^TRACE_TO_LANGFUSE=' "$ENV_FILE"; then
  echo "ERROR: TRACE_TO_LANGFUSE is set but not true. Edit that one line in place; refusing to rewrite the file." >&2
  exit 2
fi

# Ensure trailing newline before append
[[ -s "$ENV_FILE" && "$(tail -c1 "$ENV_FILE" | wc -l)" -eq 0 ]] && printf '\n' >> "$ENV_FILE"
echo 'TRACE_TO_LANGFUSE=true' >> "$ENV_FILE"
echo "ok appended TRACE_TO_LANGFUSE=true (keys untouched)"
