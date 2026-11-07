# Kiro IDE → Langfuse (local, Tasks 1–3)

Self-service clone of the **unmerged** branch. Open this workspace in Kiro on **your** machine, run a native workflow, and talk to **your** loopback Langfuse. No AWS. No merge. No committed keys.

```bash
git clone --branch cursor/kiro-langfuse-task1-3aac --single-branch https://github.com/sricharan-technoidentity/Test_langfuse.git
cd Test_langfuse
```

README on GitHub: https://github.com/sricharan-technoidentity/Test_langfuse/blob/cursor/kiro-langfuse-task1-3aac/README.md

**Platform:** prereq scripts and unit tests were run on **Linux**. macOS and Windows are **untested**.

A Cloud Agent may block browser `:3000`. That is **not** a Langfuse limitation. On your laptop, open `http://127.0.0.1:3000` normally.

---

## Honest scope

| Task | What you get | What you do **not** get |
|------|----------------|-------------------------|
| 1 | Workflow name/version/`run_id`, step sessions, git SHA, sanitized repo, your configured user/team | OS/email identity inference |
| 2 | Two Langfuse dashboards of **observation** (and boolean score) widgets | Workflow-run KPIs, adoption, completion rate, native duration, tool-timing, team/repo group-bys |
| 3 | `build_pass` / `test_pass` / `lint_clean` on the **final-step 32-hex trace** after native completion | Auto-score on Stop (per-turn); treating `wf_*` as a trace; scoring a historical SHA from a later checkout |

`build_pass` is **syntax compile** (`py_compile`), not a package build.

A prior local proof at `442a511` (19 obs / 4 traces, ~24s native wall, 3 BOOLEAN scores ingested + readback, dedup 3→3) is archived in `docs/archive/validation-notes-442a511.md`. Those IDs are **not** your defaults. **Any new commit changes HEAD** — run a fresh native workflow at the SHA you checked out before scoring.

---

## Directory map

```
.kiro/hooks + .kiro/workflows   Live Kiro paths — do not move
docs/                           Architecture, local Langfuse, limits
docs/archive/                   Host-specific history (not your IDs)
examples/                       Copy-once pointers
config/                         Loopback Compose override
scripts/                        Safe helpers (no key echo, no volume wipe)
tests/ + tests/fixtures/        Unit tests; labeled fixtures
sample/                         Demo file for two-step-demo
```

Details: [docs/architecture.md](docs/architecture.md)

---

## Numbered walkthrough (clean machine)

Kiro `two-step-demo` uses **your IDE model plan**. Scripts never call that model.

### 0. Prerequisites (Linux tested)

Need: `git`, **Python 3.10+**, **Node.js** (for `npx langfuse-cli`), **Docker + Compose** (for self-hosted Langfuse), **Kiro IDE 1.4+** (native workflow).

```bash
chmod +x scripts/*.sh
./scripts/check-prereqs.sh
```

This script **does not** install anything, purchase anything, or start Docker.

### 1. Python venv + tests

```bash
./scripts/setup-venv.sh
source .venv/bin/activate
python3 -m pytest tests/ -q
```

If `python3 -m venv` fails (missing `ensurepip` / `python3-venv`), the script **exits** and will not `apt-get` for you. Fallback:

```bash
python3 -m pip install --user -r requirements.txt
python3 -m pytest tests/ -q
```

Unit tests are **not** live ingest/score/dashboard proof.

### 2. Loopback Langfuse (official Compose, your clone)

Follow [docs/local-langfuse.md](docs/local-langfuse.md). Short form:

```bash
git clone --branch v4.76.0 --depth 1 https://github.com/langfuse/langfuse.git "$HOME/src/langfuse"
# In that checkout: replace # CHANGEME secrets; bind langfuse-web to 127.0.0.1:3000
export LANGFUSE_COMPOSE_DIR="$HOME/src/langfuse"
./scripts/langfuse-compose.sh check
./scripts/langfuse-compose.sh up      # refuses down -v; keeps volumes
```

If `LANGFUSE_COMPOSE_DIR` is unset, the script prints instructions and **exits 2** (does not auto-clone).

Create **your** org/project/API keys in the UI (`http://127.0.0.1:3000`) or via official `LANGFUSE_INIT_*` headless vars. Project id is **dynamic**.

### 3. Workspace config (no overwrite of existing secrets)

```bash
cp .kiro/tracing.context.example.json .kiro/tracing.context.json
# edit user_id / team / workspace_key — local labels, not a previous run

cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env
chmod 600 .kiro/langfuse.local.env
# edit YOUR pk/sk and LANGFUSE_BASE_URL=http://127.0.0.1:3000

./scripts/enable-tracing.sh          # appends TRACE_TO_LANGFUSE=true if missing
cp .kiro/objective-checks.json.template .kiro/objective-checks.json
python3 scripts/doctor.py            # never prints key values
```

Do **not** `cp` the example over a filled env file. Do **not** commit `.kiro/langfuse.local.env`.

### 4. Auth (projects list, not `/health`)

```bash
set -a && source .kiro/langfuse.local.env && set +a
npx --yes langfuse-cli api projects list --json
python3 scripts/print-urls.py --project-id YOUR_PROJECT_ID
```

### 5. Dashboards (dry-run → apply → idempotence)

```bash
python3 .kiro/hooks/dashboard_provision.py --dry-run --json
python3 .kiro/hooks/dashboard_provision.py --json
python3 .kiro/hooks/dashboard_provision.py --json   # second apply: 0 new placements
```

These widgets are **observation proxies**. See [docs/limitations.md](docs/limitations.md).

### 6. Native two-step demo (**Kiro IDE**, not CLI)

```bash
kiro --new-window .
```

In Kiro (wait until **both** steps complete; Stop is per-turn, not workflow-end):

```
/workflow-run two-step-demo target_file="sample/checkout_helper.py"
```

Reload Kiro after the first clone so hooks register.

### 7. Discover **your** workflow id, then score at **this** SHA

```bash
python3 scripts/discover-workflow.py --json
# expect latest_workflow_id like wf_… and is_terminal true

python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --latest --dry-run --json
# expect 32-hex trace_id (not wf_*), provenance.ok true only if ingest git.sha == $(git rev-parse HEAD)

python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --latest --verify-scores --json
# expect scores_submitted build_pass/test_pass/lint_clean and v3 readback

python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --latest --json
# dedup: exits 0, does not write extra scores
```

Pass `--trace-id` only for a 32-hex **step** trace of that run. Scores are workflow checks on the **final-step** trace (`score_scope=workflow-objective-checks`), not a workflow-root.

### 8. Open **your** URLs

```bash
python3 scripts/print-urls.py \
  --base-url http://127.0.0.1:3000 \
  --project-id YOUR_PROJECT_ID \
  --dashboard-id YOUR_DASHBOARD_ID \
  --trace-id YOUR_32_HEX_TRACE
```

---

## Shortest verified non-GUI commands (this Linux VM)

Docker, Kiro GUI, and live Langfuse were **not** available here. These were run on an isolated checkout:

```bash
git clone --branch cursor/kiro-langfuse-task1-3aac --single-branch https://github.com/sricharan-technoidentity/Test_langfuse.git
cd Test_langfuse
chmod +x scripts/*.sh
./scripts/check-prereqs.sh
./scripts/setup-venv.sh && source .venv/bin/activate
python3 -m pytest tests/ -q
python3 scripts/doctor.py
python3 scripts/print-urls.py --base-url http://127.0.0.1:3000
./scripts/langfuse-compose.sh help
```

---

## More docs

- [docs/local-langfuse.md](docs/local-langfuse.md) — official Compose, loopback, keys
- [docs/troubleshooting.md](docs/troubleshooting.md)
- [docs/limitations.md](docs/limitations.md)
- [docs/archive/](docs/archive/) — historical host notes, not defaults
