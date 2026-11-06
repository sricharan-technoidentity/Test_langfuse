# Parent GUI checklist (Task1 metadata verification)

## Setup

1. Configure `.kiro/langfuse.local.env` with your local Langfuse credentials
2. Ensure local Langfuse is running at `http://127.0.0.1:3000`

## Single-Step Workflow Test

### Launch
```bash
kiro --new-window .
```

### Run
```text
/workflow-run sample-file-review target_file="sample/checkout_helper.py"
```

### Verify in Langfuse
- `sessionId` == configured `workspace_key` (e.g., `kiro-local-workflow-demo`)
- `userId` == configured `user_id` (e.g., `chomu-local`)
- `workflow.name` == `sample-file-review`
- `workflow.version` == `sha256:...` (recipe content hash)
- `missing_fields` includes `team`, `repo`, `git.sha` (if not configured)
- Tag `metadata-incomplete` present when fields missing

## Two-Step Workflow Test

### Run
```text
/workflow-run two-step-demo target_file="sample/checkout_helper.py"
```

### Verify in Langfuse
- Both steps (`analyze`, `summarize`) have the **same** `workflow.run_id`
- Each step has a **different** `workflow.step_session_id`
- `workflow.step_id` is `analyze` for step 1, `summarize` for step 2
- Same `sessionId` (workspace grouping) across both steps

## Metadata Expectations

| Field | Expected Value |
|-------|---------------|
| `sessionId` | Your configured `workspace_key` |
| `userId` | Your configured `user_id` |
| `workflow.name` | Workflow recipe name |
| `workflow.version` | Declared version or `sha256:...` |
| `workflow.run_id` | Native Kiro `wf_...` (same across steps) |
| `workflow.step_session_id` | Kiro step session (unique per step) |
| `kiro.root_conversation_id` | Parent chat ID (when available) |

## What NOT to Count as Proof

- Connectivity-check traces
- Failed lookup traces
- Synthetic/mock traces from unit tests

## Troubleshooting

1. **No traces appearing**: Check `TRACE_TO_LANGFUSE=true` in env file
2. **Wrong endpoint**: Verify `LANGFUSE_BASE_URL` points to local instance
3. **Auth errors**: Verify API keys match your Langfuse project
4. **Missing workflow metadata**: Ensure session is a workflow step (agentMode starts with `wf-`)
