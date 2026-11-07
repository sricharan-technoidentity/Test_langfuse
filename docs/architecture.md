# Architecture and directory map

Kiro IDE hooks emit OpenTelemetry traces to a **local** Langfuse instance. Dashboards and scores use Langfuse public APIs. Nothing in this repo deploys AWS or Langfuse Cloud.

## Runtime paths that must stay put

Kiro loads hooks and recipes from `.kiro/` relative to the workspace you open. Do not relocate these:

| Path | Role |
|------|------|
| `.kiro/hooks/langfuse-tracing.json` | Hook registration (Kiro 1.4+) |
| `.kiro/hooks/hook_launcher.py` | Resolves workspace from event `cwd` |
| `.kiro/hooks/langfuse_kiro_hook.py` | OTEL export |
| `.kiro/hooks/tracing_context.py` | Task 1 metadata |
| `.kiro/hooks/dashboard_provision.py` | Task 2 unstable dashboard API |
| `.kiro/hooks/finalize_workflow.py` | Task 3 end-of-workflow scores |
| `.kiro/workflows/two-step-demo.workflow.json` | Native IDE recipe |
| `.kiro/langfuse.local.env` | Gitignored keys (`chmod 600`) |
| `.kiro/tracing.context.json` | Gitignored identity (copy from example) |
| `sample/checkout_helper.py` | Default `target_file` for the demo recipe |

## Layout after clone

```
README.md                 Entry + numbered walkthrough
requirements.txt          pytest, ruff (no paid services)
docs/                     How-to, limits, official Langfuse links
docs/archive/             Host-specific historical notes (not your IDs)
examples/                 Pointers to copy-once templates
config/                   Loopback Compose override (no secrets)
scripts/                  Prereq / venv / tracing / doctor (no key echo)
tests/                    Unit tests + labeled fixtures
tests/fixtures/           Synthetic / historical fixtures (not defaults)
.kiro/                    Live Kiro config (paths above)
sample/                   Demo Python module for the native workflow
```

## Data flow

1. You run `two-step-demo` **in Kiro IDE** (not a CLI substitute).
2. Hooks POST OTEL to `LANGFUSE_BASE_URL` when `TRACE_TO_LANGFUSE=true`.
3. Langfuse `sessionId` = your `workspace_key`. Kiro step sessions stay in metadata.
4. `dashboard_provision.py` creates observation widgets (not workflow-run KPIs).
5. After native `root.status=completed`, `finalize_workflow.py` attaches boolean checks to the **final step** 32-hex trace, only if `git.sha` matches this checkout.

## What is not in this tree

- No committed API keys, env files, or project ids
- No AWS / CloudFormation
- No automatic `docker compose down -v`
- Host-specific dashboard placement UUIDs live only as a **fixture** under `tests/fixtures/`
