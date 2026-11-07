# Kiro IDE → Langfuse Tracing (Tasks 1-3)

Local-first Langfuse integration for Kiro IDE workflow tracing, dashboards, and objective scores.

## Overview

| Task | Description | Status |
|------|-------------|--------|
| Task 1 | Enrich tracing metadata | Code complete; live ingest proven on parent for wf_7482 (old identity). Future runs: `kiro-test-runner` / `demo-engineering` / `identity_type=automation` |
| Task 2 | Native Langfuse dashboards | Provisioner uses GET `definition.widgets` + `widgetId` (LIST `placements` is a count). Observation widgets are **not** workflow-run counts. Failed 9d5390b apply duplicated tiles; cleanup plan is parent-only (original 6/13 survive). Several requested metrics are **blocked** (see RUNTIME_ACCEPTANCE.md) |
| Task 3 | Objective scores | Explicit `finalize_workflow.py` after native terminal state. Scores attach to a 32-hex **final-step** trace (not a workflow-root). Association uses observations v2 metadata. Historical `5706b066` cannot receive later-SHA checks. Fresh native run at this HEAD required for score acceptance. |

## Quick Start

### 1. Configure Langfuse (Self-Hosted)

**Headless initialization** (recommended for reproducible dev/test):

```bash
# Set environment variables before starting Langfuse
export LANGFUSE_INIT_ORG_ID=my-org
export LANGFUSE_INIT_PROJECT_ID=my-project
export LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-your-key-here
export LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-your-key-here
# User is optional for API-only access
```

See: https://langfuse.com/self-hosting/administration/headless-initialization.md

### 2. Configure Workspace

Copy and edit configuration files:

```bash
# Tracing context (user, team, workspace grouping)
cp .kiro/tracing.context.example.json .kiro/tracing.context.json
# Edit: set user_id, team, workspace_key

# Langfuse credentials (mode 0600, never commit)
cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env
chmod 600 .kiro/langfuse.local.env
# Edit: set LANGFUSE_BASE_URL, keys

# Task3 objective checks (optional)
cp .kiro/objective-checks.example.json .kiro/objective-checks.json
# Edit: configure build/test/lint commands
```

### 3. Run Workflows

```bash
kiro --new-window .
```

In Kiro:
```
/workflow-run sample-file-review target_file="sample/checkout_helper.py"
/workflow-run two-step-demo target_file="sample/checkout_helper.py"
```

---

## Task 1: Tracing Metadata

### Metadata Schema

| Field | Source | Notes |
|-------|--------|-------|
| `sessionId` | `workspace_key` config | Stable cross-machine grouping |
| `userId` | `user_id` config only | **Never** inferred from OS/email |
| `workflow.name` | Kiro session + recipe | Only on workflow steps |
| `workflow.version` | Recipe `version` or `sha256:...` | Content hash provenance |
| `workflow.run_id` | Native Kiro `workflowId` | Execution scope (spans steps) |
| `workflow.step_session_id` | Kiro step session | Per-step, not the run id |
| `team` | Config only | Not invented |
| `repo` | Config or git remote | Credentials sanitized |
| `git.sha` | Config or `git rev-parse HEAD` | Real HEAD SHA |

### Endpoint Selection

**Important**: Default endpoint is `http://127.0.0.1:3000` (local self-hosted).

```bash
# In .kiro/langfuse.local.env
LANGFUSE_BASE_URL=http://127.0.0.1:3000  # Self-hosted (default)
# LANGFUSE_BASE_URL=https://us.cloud.langfuse.com  # US Cloud (explicit only)
# LANGFUSE_BASE_URL=https://cloud.langfuse.com     # EU Cloud (explicit only)
```

No silent fallback to cloud - data stays where you configure it.

### Missing Fields

When required fields are absent, traces are:
- Tagged `metadata-incomplete`
- `missing_fields` metadata lists what's missing
- Never silently invented

---

## Task 2: Langfuse Dashboards

Provision via documented unstable API (not drag-drop of guessed JSON):

```bash
python3 .kiro/hooks/dashboard_provision.py --dry-run --json
python3 .kiro/hooks/dashboard_provision.py --json
```

Schema citations: `npx langfuse-cli api unstable-dashboard-widgets create --help` and
https://langfuse.com/docs/metrics/features/metrics-api.md — views are `observations`,
`scores-numeric`, `scores-boolean`, `scores-categorical`. Legacy `traces` view is not
supported for new widgets. Only `PIVOT_TABLE` may use multiple dimensions.

**Shipped widgets count observations (or boolean scores), not uniq(workflow.run_id).**
One native two-step run produced 14 observations / 3 traces. Hung parent latency is
not workflow duration.

**Blocked (not faked):** distinct workflow runs, distinct human engineers, native
completion rate, native duration, true per-tool latency, team/repo unique-run adoption.

Automation exclusion for *future* traces is the `kiro-automation` tag from
`identity_type` / known runner ids (`kiro-test-runner`, `chomu-local`) — not a
hardcoded single username in the widget API.

See `RUNTIME_ACCEPTANCE.md`.

---

## Task 3: Objective Scores

### Configuration

Create `.kiro/objective-checks.json`:

```json
{
  "build": {
    "command": "python3 -m py_compile your_module.py",
    "timeout": 120,
    "enabled": true
  },
  "test": {
    "command": "python3 -m pytest tests/ -v",
    "timeout": 300,
    "enabled": true
  },
  "lint": {
    "command": "python3 -m ruff check .",
    "timeout": 60,
    "enabled": true
  }
}
```

### Score Outcomes

| Outcome | Description |
|---------|-------------|
| `pass` | Command exited 0 |
| `fail` | Command exited non-zero |
| `not_configured` | No command configured |
| `skipped` | Command disabled |
| `timeout` | Command exceeded timeout |
| `error` | Execution error (not found, permission, etc.) |

### Safety Design

- **Not automatic**: Never runs on workspace open
- **Workflow end only**: Triggers at Stop hook for workflow steps
- **Idempotent**: Dedup by workflow_run_id + score_name
- **Bounded output**: Max 4KB, secrets redacted
- **Bounded timeout**: Configurable per-check
- **No payload inference**: Commands from config only, never from LLM text
- **Fail-open**: Score failure doesn't block IDE

### Score Correlation

Scores are attached to the workflow step trace with:
- `traceId`: The step's trace ID
- `metadata.workflow_run_id`: For correlation across steps
- `metadata.exit_code`, `metadata.duration_ms`: Execution details

---

## SDK/CLI Versions

Tested compatibility:

| Component | Version | Notes |
|-----------|---------|-------|
| Langfuse Server | 4.76.0 | Self-hosted |
| Langfuse Python SDK | 5.2.0 | For programmatic access |
| Langfuse CLI | 1.3.1 | For dashboard/score management |
| Python | 3.10+ | Required |

Pin versions in your requirements:
```
langfuse==5.2.0
```

---

## Tests

```bash
# All tests
python3 tests/test_tracing_context.py -v      # 43 tests
python3 tests/test_objective_scores.py -v     # 32 tests
python3 tests/test_dashboard_config.py -v     # 26 tests
python3 tests/test_langfuse_scores.py -v      # 12 tests

# Total: 113 tests
```

---

## File Structure

```
.kiro/
├── hooks/
│   ├── langfuse_kiro_hook.py      # Main hook (OTLP/HTTP)
│   ├── tracing_context.py         # Task1 metadata enrichment
│   ├── objective_scores.py        # Task3 check execution
│   ├── langfuse_scores.py         # Task3 score submission
│   ├── dashboard_config.py        # Task2 dashboard definitions
│   └── langfuse-tracing.json      # Hook triggers
├── dashboards/
│   ├── management-dashboard.json  # Exported dashboard config
│   └── engineering-dashboard.json # Exported dashboard config
├── workflows/
│   ├── sample-file-review.workflow.json
│   └── two-step-demo.workflow.json
├── tracing.context.json           # Workspace config
├── tracing.context.example.json
├── langfuse.local.env.example
├── objective-checks.example.json
└── workflow-output/
```

---

## Acceptance Checklist

### Task 1: Tracing Metadata
- [x] Workflow name/version tagged on traces
- [x] workflow.run_id distinct from step_session_id
- [x] Version uses content hash when no declared version
- [x] User/team from config only, not OS/email
- [x] Git SHA from real HEAD, credentials sanitized
- [x] Missing fields flagged honestly
- [x] Default endpoint is self-hosted (no silent cloud fallback)
- [x] Parent chat correlation only when evidenced

### Task 2: Dashboards
- [x] Management dashboard defined with 7 widgets
- [x] Engineering dashboard defined with 7 widgets
- [x] Workflow runs deduplicated by run_id
- [x] Automation users excluded from adoption
- [x] Tool latency caveats documented
- [x] JSON configs exported for UI import
- [x] API client for programmatic provisioning (when available)

### Task 3: Objective Scores
- [x] build_pass, test_pass, lint_clean scores
- [x] Outcomes: pass/fail/not_configured/skipped/timeout/error
- [x] Commands from config only (never from payload)
- [x] Bounded timeout and output
- [x] Idempotent (dedup by run_id)
- [x] Correlated to workflow trace
- [x] Not automatic on workspace open

---

## Limitations

- **Team validation**: Cross-machine grouping requires shared workspace_key by convention
- **No AWS deployment**: This is local-first only
- **Dashboard API unstable**: May require UI import instead
- **Tool latency**: Often 0 (Kiro doesn't provide genuine timing)
- **Token usage**: Not available (Kiro credits ≠ tokens)
- **Time saved**: Requires external baseline, not measurable

---

## Security

- Credentials in `.kiro/langfuse.local.env` with mode 0600
- Git remote URLs with embedded credentials are sanitized
- Enrichment payloads never contain secrets
- Score output is truncated and redacted
- No OS user or email inference

---

## License

See repository license.
