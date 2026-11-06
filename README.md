# Kiro IDE → Langfuse Tracing (Task1)

Local-first Langfuse metadata enrichment for Kiro IDE workflow tracing.

## Overview

This integration enables:
- **Workflow tracing**: Captures Kiro IDE workflow executions with rich metadata
- **Session grouping**: Configurable `workspace_key` for cross-machine team grouping
- **Workflow identity**: Native Kiro `workflowId` (`wf_...`) distinct from step sessions
- **Honest metadata**: Missing fields flagged, no OS/email inference for user identity

## Quick Start

1. **Configure tracing context** (`.kiro/tracing.context.json`):
   ```json
   {
     "user_id": "your-name",
     "team": "your-team",
     "workspace_key": "shared-project-id",
     "repo": null,
     "git_sha": null
   }
   ```

2. **Set up Langfuse credentials** (`.kiro/langfuse.local.env`, mode 600):
   ```bash
   TRACE_TO_LANGFUSE=true
   LANGFUSE_BASE_URL=http://127.0.0.1:3000
   LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
   LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
   ```

3. **Launch Kiro IDE**:
   ```bash
   kiro --new-window .
   ```

4. **Run a workflow**:
   ```
   /workflow-run sample-file-review target_file="sample/checkout_helper.py"
   ```

## Metadata Schema

| Field | Source | Notes |
|-------|--------|-------|
| `sessionId` | `workspace_key` config | Stable cross-machine grouping |
| `userId` | `user_id` config only | Never inferred from OS/email |
| `workflow.name` | Kiro session + recipe | Only on workflow steps |
| `workflow.version` | Recipe `version` or `sha256:...` | Content hash when no declared version |
| `workflow.run_id` | Native Kiro `workflowId` | Execution scope (may span steps) |
| `workflow.step_session_id` | Kiro step session | Per-step, not the run id |
| `team` | Config only | Not invented |
| `repo` | Config or git remote | Authenticated URLs sanitized |
| `git.sha` | Config or `git rev-parse HEAD` | Real HEAD, never invented |

### Missing Fields

When required fields are absent, traces are tagged `metadata-incomplete` and `missing_fields` lists what's missing. This is intentional—never silently invent values.

## Workflows

### sample-file-review (single step)
Inspects a target file and writes a review to `.kiro/workflow-output/sample-review.md`.

### two-step-demo (two steps)
1. **analyze**: Reads file, writes analysis
2. **summarize**: Reads analysis, writes summary

Both steps share the same `workflow.run_id` when the native Kiro `workflowId` is present.

## Architecture

```
.kiro/
├── hooks/
│   ├── langfuse_kiro_hook.py   # Main hook handler (OTLP/HTTP)
│   ├── tracing_context.py      # Task1 metadata enrichment
│   └── langfuse-tracing.json   # Hook triggers
├── workflows/
│   ├── sample-file-review.workflow.json
│   └── two-step-demo.workflow.json
├── tracing.context.json        # Workspace config (commit)
├── tracing.context.example.json
├── langfuse.local.env          # Credentials (gitignored)
└── workflow-output/            # Generated artifacts
```

### Key Design Decisions

- **Fail-open**: Hooks always exit 0, never block IDE
- **Per-workspace isolation**: Uses `KIRO_LF_ENV_FILE` override, not global `~/.kiro/langfuse.env`
- **No credential tracking**: Secrets never in enrichment payloads
- **Idempotent claims**: Concurrent hooks dedupe via file-based claims

## Tests

```bash
python3 tests/test_tracing_context.py -v
```

Coverage includes:
- Field normalization and nullish handling
- Missing field detection
- Workspace grouping (cross-machine readiness)
- Workflow evidence vs chat sessions
- Git metadata on real worktrees
- Remote URL credential sanitization
- Recipe hash stability
- Parent-chat correlation
- Multi-step workflow run_id consistency
- No secret/OS user leakage

## Configuration Reference

### tracing.context.json

| Field | Required | Description |
|-------|----------|-------------|
| `user_id` | Yes | Explicit user identity (never inferred) |
| `team` | No | Team identifier |
| `workspace_key` | Yes | Stable grouping key for Langfuse sessions |
| `repo` | No | Override git remote detection |
| `git_sha` | No | Override git HEAD detection |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TRACE_TO_LANGFUSE` | `false` | Enable/disable tracing |
| `LANGFUSE_BASE_URL` | `https://us.cloud.langfuse.com` | Langfuse endpoint |
| `LANGFUSE_PUBLIC_KEY` | (required) | API public key |
| `LANGFUSE_SECRET_KEY` | (required) | API secret key |
| `KIRO_LF_ENV_FILE` | `~/.kiro/langfuse.env` | Credentials file path |
| `KIRO_LF_STATE` | `~/.kiro/langfuse-state` | Hook state directory |
| `KIRO_LF_DEBUG` | (unset) | Enable debug logging |

## Limitations

- **Not team-validated**: Cross-machine workspace grouping requires shared `workspace_key` by convention
- **No AWS deployment**: This is local-first only
- **One-step proof**: Single-step workflows cannot prove cross-step correlation
- **Parent ≠ run**: Parent chat session is not the workflow execution id

## Security

- Credentials in `.kiro/langfuse.local.env` with mode 600
- Git remote URLs with embedded credentials are sanitized
- Enrichment payloads never contain secrets
- No OS user or email inference

## License

See repository license.
