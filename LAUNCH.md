# Validation Checklist for Tasks 1-3

## Prerequisites

1. **Langfuse self-hosted running** at `http://127.0.0.1:3000`
2. **API keys configured** in `.kiro/langfuse.local.env`
3. **Kiro IDE 1.4.x+** installed

## Task 1: Tracing Metadata Validation

### Setup
```bash
# Configure tracing context
cp .kiro/tracing.context.example.json .kiro/tracing.context.json
# Edit: set user_id, workspace_key

# Configure credentials
cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env
chmod 600 .kiro/langfuse.local.env
# Edit: set your Langfuse keys
```

### Launch & Run
```bash
kiro --new-window .
```

In Kiro:
```
/workflow-run two-step-demo target_file="sample/checkout_helper.py"
```

### Verify in Langfuse

After workflow completes, check traces:

| Field | Expected |
|-------|----------|
| `sessionId` | Your configured `workspace_key` |
| `userId` | Your configured `user_id` |
| `metadata.workflow.name` | `two-step-demo` |
| `metadata.workflow.version` | `sha256:...` |
| `metadata.workflow.run_id` | `wf_...` (same for both steps) |
| `metadata.workflow.step_session_id` | Different for each step |
| `metadata.workflow.step_id` | `analyze` or `summarize` |
| `tags` | Contains `kiro-workflow-step` |
| `metadata.missing_fields` | Lists any missing (team, repo, etc.) |

### Verify via CLI
```bash
# Using Langfuse CLI
npx @langfuse/cli@1.3.1 api traces list --limit 5 \
  --base-url http://localhost:3000 \
  --public-key pk-lf-... \
  --secret-key sk-lf-...
```

---

## Task 2: Dashboard Validation

### Check API Availability
```bash
curl -u "pk-lf-...":"sk-lf-..." \
  "http://localhost:3000/api/public/unstable/dashboards?limit=1"
```

**If 200**: API available, can provision programmatically
**If 404**: Use JSON import via UI

### Import Dashboards (UI Method)
1. Open Langfuse UI → Dashboards
2. Drag-drop `.kiro/dashboards/management-dashboard.json`
3. Drag-drop `.kiro/dashboards/engineering-dashboard.json`

### Verify Dashboard Contents

**Management Dashboard should show:**
- [ ] Weekly Active Engineers (excludes automation)
- [ ] Workflow Runs Per Week (deduplicated)
- [ ] Completion Rate by Status
- [ ] Adoption by Team
- [ ] Adoption by Repository
- [ ] Top Workflows by Usage
- [ ] Average Duration by Workflow

**Engineering Dashboard should show:**
- [ ] Completion by Workflow Version
- [ ] Tool Error Rate by Tool
- [ ] Tool Latency p50 (may show 0)
- [ ] Tool Latency p95 (may show 0)
- [ ] Tool Calls Per Workflow Run
- [ ] Failed Traces
- [ ] Objective Check Pass Rate (Task3)

---

## Task 3: Objective Scores Validation

### Setup
```bash
# Configure objective checks
cp .kiro/objective-checks.example.json .kiro/objective-checks.json
# Edit: customize commands for your project
```

### Run Workflow
```
/workflow-run sample-file-review target_file="sample/checkout_helper.py"
```

### Verify Scores in Langfuse

After workflow completes, check scores:

```bash
# Via CLI
npx @langfuse/cli@1.3.1 api scores list --limit 10 \
  --base-url http://localhost:3000 \
  --public-key pk-lf-... \
  --secret-key sk-lf-...
```

**Expected scores:**
| Name | Value | Notes |
|------|-------|-------|
| `build_pass` | `pass`/`fail`/`not_configured` | |
| `test_pass` | `pass`/`fail`/`not_configured` | |
| `lint_clean` | `pass`/`fail`/`not_configured` | |

### Score Metadata
Each score should have:
- `metadata.outcome`: Detailed outcome
- `metadata.exit_code`: Command exit code (if run)
- `metadata.duration_ms`: Execution time
- `metadata.workflow_run_id`: For correlation

### Deduplication Test
Run the same workflow again - scores should NOT be duplicated.

---

## Troubleshooting

### No traces appearing
1. Check `TRACE_TO_LANGFUSE=true` in env file
2. Verify `LANGFUSE_BASE_URL` points to correct instance
3. Check `.kiro/langfuse-state-local/errors.log`

### Auth errors (401)
1. Verify API keys match your Langfuse project
2. Ensure keys are in `.kiro/langfuse.local.env`, not global

### Missing workflow metadata
1. Ensure session is a workflow step (`agentMode` starts with `wf-`)
2. Check recipe exists at `.kiro/workflows/<name>.workflow.json`

### Scores not appearing
1. Check `.kiro/objective-checks.json` exists and is valid
2. Verify commands work manually in workspace
3. Check state dir for dedup: `.kiro/langfuse-state-local/`

### Dashboard API 404
The unstable API may not be available in your Langfuse version.
Use UI import with the JSON files instead.

---

## What NOT to Count as Proof

- Connectivity-check traces
- Failed lookup traces
- Synthetic/mock traces from unit tests
- Traces without `kiro-workflow-step` tag

---

## SDK Versions Used

```
langfuse==5.2.0
@langfuse/cli@1.3.1
```

Langfuse server: 4.76.0 (self-hosted)
