# Local Langfuse (loopback Docker Compose)

This repo does **not** vendor a Langfuse server. Use the official Compose file at a version that matches what the hooks were tested against: **4.76.0**.

Official docs (source of truth):

- [Docker Compose](https://langfuse.com/self-hosting/deployment/docker-compose.md)
- [Headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization.md)
- [Configuration](https://langfuse.com/self-hosting/configuration.md)

## What this repo will not do

- Install Docker, Compose, or any paid cloud
- Clone Langfuse for you unless you set `LANGFUSE_COMPOSE_DIR`
- Run `docker compose down -v` (never wipe volumes)
- Overwrite an existing `.kiro/langfuse.local.env`
- Bind the UI on `0.0.0.0` from our override file
- Require AWS, GCP, or Langfuse Cloud

A Cloud Agent browser block on `:3000` is **not** a Langfuse product limitation. On **your** machine, open `http://127.0.0.1:3000` in a normal browser.

## Start (your machine)

```bash
git clone --branch v4.76.0 --depth 1 https://github.com/langfuse/langfuse.git "$HOME/src/langfuse"
cd "$HOME/src/langfuse"
# Replace every # CHANGEME secret in docker-compose.yml / .env (official file).
# Change langfuse-web ports from 3000:3000 to 127.0.0.1:3000:3000 in YOUR checkout.
```

From **this** repo, if you already cloned Langfuse:

```bash
export LANGFUSE_COMPOSE_DIR="$HOME/src/langfuse"
./scripts/langfuse-compose.sh check
./scripts/langfuse-compose.sh up     # no volume wipe, no --pull always
# later:
./scripts/langfuse-compose.sh stop   # keeps volumes
```

If `LANGFUSE_COMPOSE_DIR` is unset, `langfuse-compose.sh` prints these steps and exits 2 without starting anything.

Override file: `config/langfuse.loopback.override.yml`. Compose **merges** port lists; if the official file still publishes `3000:3000` on all interfaces, edit **your** Langfuse checkout so the host side is `127.0.0.1:3000`.

## Create YOUR project and keys

There is no default login. In the UI: Sign up → create organization → create project → Settings → API keys.

Optional headless init uses **your** `LANGFUSE_INIT_*` values ([docs](https://langfuse.com/self-hosting/administration/headless-initialization.md)). Generate keys locally. Do not copy keys, org ids, or project ids from this repository or from archive notes.

Then in **this** repo:

```bash
cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env
chmod 600 .kiro/langfuse.local.env
# edit: LANGFUSE_BASE_URL=http://127.0.0.1:3000 and YOUR pk/sk
./scripts/enable-tracing.sh
```

`enable-tracing.sh` only appends `TRACE_TO_LANGFUSE=true` when that key is missing. It never overwrites a working env file.

Auth check (not `/health`):

```bash
# uses env vars from your shell, not from echoing the file
set -a && source .kiro/langfuse.local.env && set +a
npx --yes langfuse-cli api projects list --json
```

Project id is **dynamic**. Pass it to URL helpers:

```bash
python3 scripts/print-urls.py --base-url http://127.0.0.1:3000 --project-id YOUR_PROJECT_ID
```
