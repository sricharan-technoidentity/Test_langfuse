#!/usr/bin/env bash
# Report local prerequisites. Never installs packages, Docker, AWS, or paid services.
# Never wipes volumes. Never prints credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

os="$(uname -s 2>/dev/null || echo unknown)"
echo "os=$os"
echo "repo=$ROOT"
echo "platform_tested=Linux"
echo "platform_untested=macOS,Windows"

ok=0
fail=0
warn=0

have() { command -v "$1" >/dev/null 2>&1; }

check_cmd() {
  local name="$1" required="$2"
  if have "$name"; then
    echo "ok  $name=$($name --version 2>/dev/null | head -n 1 | tr '\n' ' ')"
    ok=$((ok + 1))
  elif [[ "$required" == required ]]; then
    echo "fail $name missing (required for documented non-GUI steps)"
    fail=$((fail + 1))
  else
    echo "warn $name missing (needed later: $3)"
    warn=$((warn + 1))
  fi
}

check_cmd python3 required "unit tests and hooks"
if have python3; then
  pyv="$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
  echo "ok  python3.version=$pyv (need >= 3.10)"
fi

check_cmd node optional "npx langfuse-cli"
check_cmd npm optional "npx langfuse-cli"
check_cmd docker optional "self-hosted Langfuse Compose"
if have docker && docker compose version >/dev/null 2>&1; then
  echo "ok  docker compose=$(docker compose version 2>/dev/null | head -n 1)"
  ok=$((ok + 1))
else
  echo "warn docker compose missing (self-host Langfuse later; this script will not install it)"
  warn=$((warn + 1))
fi

if have kiro; then
  echo "ok  kiro=$(kiro --version 2>/dev/null | head -n 1 || echo present)"
  ok=$((ok + 1))
else
  echo "warn kiro missing (native two-step-demo is a Kiro IDE command, not a CLI substitute)"
  warn=$((warn + 1))
fi

echo "note docker_available=$(have docker && echo yes || echo no) — this repo will not claim a Docker test unless you run Compose yourself"
echo "note never: docker compose down -v, apt-get install of paid clouds, AWS, or overwriting .kiro/langfuse.local.env"
echo "summary ok=$ok warn=$warn fail=$fail"
[[ "$fail" -eq 0 ]]
