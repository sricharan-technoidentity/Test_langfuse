#!/usr/bin/env bash
# Optional helper around an official Langfuse Docker Compose checkout.
# Does NOT clone Langfuse, install Docker, pull paid images beyond what you already run,
# wipe volumes, or bind services on 0.0.0.0.
#
# Official docs (version-matching):
#   https://langfuse.com/self-hosting/deployment/docker-compose.md
#   https://langfuse.com/self-hosting/administration/headless-initialization.md
# Tested server version for this repo: 4.76.0 (tag v4.76.0).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-help}"
COMPOSE_DIR="${LANGFUSE_COMPOSE_DIR:-}"

usage() {
  cat <<'EOF'
usage: scripts/langfuse-compose.sh [check|up|stop|help]

Environment:
  LANGFUSE_COMPOSE_DIR  path to a local clone of github.com/langfuse/langfuse
                        checked out at tag v4.76.0 (recommended)

This script never runs: docker compose down -v
This script never installs Docker or AWS.
Loopback bind: use config/langfuse.loopback.override.yml with the official compose file
so langfuse-web is 127.0.0.1:3000 — not a public bind.

If LANGFUSE_COMPOSE_DIR is unset, prints the official steps and exits 2 without starting anything.
EOF
}

if [[ "$ACTION" == help || "$ACTION" == -h || "$ACTION" == --help ]]; then
  usage
  exit 0
fi

if [[ "$ACTION" == down ]]; then
  echo "ERROR: refusing docker compose down (would risk removing named networks)." >&2
  echo "Use: $0 stop    # containers stop, volumes kept" >&2
  echo "This script will never run docker compose down -v." >&2
  exit 2
fi

if [[ -z "$COMPOSE_DIR" ]]; then
  cat <<'EOF'
LANGFUSE_COMPOSE_DIR is unset — refusing to auto-clone or auto-start.

On YOUR machine (loopback, no AWS):

  git clone --branch v4.76.0 --depth 1 https://github.com/langfuse/langfuse.git "$HOME/src/langfuse"
  cd "$HOME/src/langfuse"
  # Update # CHANGEME secrets in docker-compose.yml / .env as official docs require.
  # Bind the UI to loopback (official file publishes 3000:3000 on all interfaces):
  export LANGFUSE_COMPOSE_DIR="$HOME/src/langfuse"
  # from THIS repo:
  docker compose -f "$LANGFUSE_COMPOSE_DIR/docker-compose.yml" \
    -f config/langfuse.loopback.override.yml up -d

Create your own org/project/API keys in the UI at http://127.0.0.1:3000
(or LANGFUSE_INIT_* headless vars — generate YOUR keys, do not copy ours).

Never: docker compose down -v
Never: start this stack on a public IP unless you intend to.
EOF
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. Install Docker yourself if you want a local Langfuse." >&2
  echo "This repo will not install Docker or any paid service." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin not found." >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
  echo "ERROR: $COMPOSE_DIR/docker-compose.yml not found" >&2
  exit 1
fi

OVERRIDE="$ROOT/config/langfuse.loopback.override.yml"
cd "$COMPOSE_DIR"

case "$ACTION" in
  check)
    docker compose -f docker-compose.yml -f "$OVERRIDE" config >/dev/null
    echo "ok compose file parses with loopback override"
    ;;
  up)
    echo "starting official compose with loopback override (no volume wipe, no --pull always)"
    docker compose -f docker-compose.yml -f "$OVERRIDE" up -d
    echo "UI (on YOUR machine): http://127.0.0.1:3000"
    ;;
  stop)
    echo "stopping containers without deleting volumes"
    docker compose -f docker-compose.yml -f "$OVERRIDE" stop
    ;;
  *)
    usage
    exit 1
    ;;
esac
