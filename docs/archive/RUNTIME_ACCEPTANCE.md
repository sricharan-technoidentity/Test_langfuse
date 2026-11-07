# Runtime acceptance (local Langfuse 4.76.0) — parent box only

This VM cannot reach the parent's `127.0.0.1:3000`. Do **not** treat unit tests as live ingest/score/dashboard proof. Do **not** call observation/trace counts native workflow-run KPIs.

Existing native run `wf_7482ba1888446d73` stays `user=chomu-local` / no team / `git.sha=5706b066…`. Do **not** rewrite it. Do **not** attach checks run at a later SHA. Future GUI runs use `kiro-test-runner` + `demo-engineering` + `identity_type=automation` + tag `kiro-automation` (not a human engineer, not a real company team).

Never `cat` or `echo` `.kiro/langfuse.local.env` (keys). Never `cp` the template over a working 0600 env. To enable tracing only:

```bash
# If TRACE_TO_LANGFUSE is missing, append — do not overwrite keys
grep -q '^TRACE_TO_LANGFUSE=' .kiro/langfuse.local.env \
  || echo 'TRACE_TO_LANGFUSE=true' >> .kiro/langfuse.local.env
# If the key exists but is false, edit that one line in place.
```

Credential check (not `/health`):

```bash
npx langfuse-cli api projects list --json
```

Browser `:3000` remains platform-blocked. No bypass, exposure, or reset. Local machine GUI execution is after these fixes.

## After pulling this commit

```bash
cd /path/to/Test_langfuse-local-validation   # or this repo
python3 -m pytest tests/ -q
```

Unit tests are not live proof.

---

## Task2 — documented GET shape + placement idempotence

**9d5390b live fail (evidence_d8fc.json):** LIST `/api/public/unstable/dashboards` reports `placements` as an **integer count** (management 6→12→18, engineering 13→19→25). GET-by-id 200 is `{definition: {widgets: [{type, id, widgetId, x, y, width, height}]}}`. Placement `id` is the tile UUID; `widgetId` is the widget id (same field as POST addPlacement). The 9d5390b provisioner looked for a top-level `placements` array / `definition.placements`, guessed `widget.id`, and swallowed GET errors as “nothing placed” — pass2 POSTed 6+6 more tiles. Original 6/13 tiles survived.

Public schema citations:

- GET `/api/public/unstable/dashboards/{dashboardId}` → `definition.widgets[]`
- `unstableDashboardPlacement.widgetId`
- POST `/api/public/unstable/dashboards/{id}/placements` body `{type, widgetId, x, y, width, height}`
- DELETE `/api/public/unstable/dashboards/{id}/placements/{placementId}` (tile only, widget object remains)

### 0. Inspect current GET shape (no mutations)

```bash
python3 .kiro/hooks/dashboard_provision.py --inspect-placements --json \
  --from-file .kiro/hooks/failed-acceptance-placements-9d5390b.json
```

Expect `placement_source=definition.widgets`, integer `list_placements_field_type` if a count leaked onto GET (must still parse widgets), and `placement_count` 18 / 25 until cleanup.

### 1. Cleanup failed-acceptance tiles only (original 6/13 must survive)

This VM must **not** call these against the parent box. Parent only. Dry-run first.

```bash
python3 .kiro/hooks/dashboard_provision.py --cleanup-placements --json \
  --from-file .kiro/hooks/failed-acceptance-placements-9d5390b.json
# expect would_delete: 12 management + 12 engineering, remaining_after 6 and 13

python3 .kiro/hooks/dashboard_provision.py --cleanup-placements --execute-cleanup --json \
  --from-file .kiro/hooks/failed-acceptance-placements-9d5390b.json
```

Deletes **only** the 24 placement UUIDs from pass1+pass2 of 9d5390b (see the JSON file). Safety gate refuses if remaining would drop below 6 / 13. Does not delete widget objects or original tiles.

Then:

```bash
npx langfuse-cli api unstable-dashboards list --json
# expect placements: 6 management, 13 engineering
```

### 2. Provision (idempotent against definition.widgets / widgetId)

```bash
python3 .kiro/hooks/dashboard_provision.py --dry-run --json
python3 .kiro/hooks/dashboard_provision.py --json
python3 .kiro/hooks/dashboard_provision.py --json   # second apply: placements added = 0
```

Expect:

- `Boolean Score Rates` chartType `PIVOT_TABLE` (not VERTICAL_BAR)
- Dashboards reused by name (`dashboard_action: exists`)
- GET parse `definition.widgets widgetId`; fail-closed (no new tiles) if GET is a count or missing array
- **After cleanup:** first apply may add 6+6 placements for the 9d5390b widget objects (names did not collide with the original 6/13). That is not a duplicate of the originals.
- **Second apply:** `placements: []`, `placements_skipped` 6+6
- Widget copy says **observation** counts, not workflow-run counts

Do not claim dashboard **workflow** KPIs complete from observation proxies. Unsupported in native widgets: distinct `workflow.run_id`, distinct human engineers, native completion rate, native duration (~26.048s from root timestamps, not hung parent ~133s), true per-tool latency (start==end → 0), team/repo unique-run adoption.

```bash
npx langfuse-cli api unstable-dashboards list --json
npx langfuse-cli api unstable-dashboard-widgets list --json
```

---

## Task3 — associate via observations v2 + provenance gate

**9d5390b live fail:** `root.children` parse and native wall **26048ms** worked. Finalizer still set `trace_id=null` for `wf_7482ba1888446d73` and rejected explicit `--trace-id 8bf382ae53767f200a24212647e28d3d` (6 real observations) because association used only local `.kiro/langfuse-state-local/*.json`. Langfuse `sessionId` is `workspace_key`, not the Kiro step session. No scores written. Historical `git.sha=5706b066…` must not receive checks from `9d5390b` / this commit.

Scores are **workflow-objective-checks attached to the final completed step trace** with run+SHA provenance. They are **not** a workflow-root trace (`wf_*` is not an OTEL trace; the summarize trace is a step).

Copy checks if missing (gitignored): `cp .kiro/objective-checks.json.template .kiro/objective-checks.json`

### Prove association on the historical run (must still refuse submit)

```bash
# Dry-run: must associate summarize 8bf382ae… via observations v2 metadata
# (fields=core,basic,metadata + expandMetadata workflow.run_id,step_id,step_session_id,git.sha)
# and MUST report provenance.ok=false vs workspace HEAD.
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --workflow-id wf_7482ba1888446d73 \
  --trace-id 8bf382ae53767f200a24212647e28d3d --dry-run --json
```

Expect:

- `trace_id=8bf382ae53767f200a24212647e28d3d`
- `trace_association.observation_count=6`
- `trace_association.step_id=summarize`
- `trace_association.score_scope=workflow-objective-checks`
- `trace_association.attached_to=final-completed-step-trace`
- `trace_association.not_workflow_root_trace=true`
- `native_wall_ms=26048`
- `provenance.ok=false` (trace `git.sha=5706b066…` ≠ this HEAD)
- `scores_submitted=[]`

Without `--trace-id`, auto-discovery may use observations v2 `filter` on `metadata.workflow.run_id` if the API accepts that filter; if metadata is omitted from `fields`, association must fail closed (not guess).

A submit against 7482 **must fail** the provenance gate:

```bash
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --workflow-id wf_7482ba1888446d73 \
  --trace-id 8bf382ae53767f200a24212647e28d3d --json
# expect errors: provenance/code mismatch; scores_submitted []
```

Readback (still empty for this historical trace unless an older unauthorized write happened — do not add one):

```bash
npx langfuse-cli api scores list --trace-id 8bf382ae53767f200a24212647e28d3d --json
# NOT GET /api/public/scores (404 on v4 events_only)
# NOT GET /api/public/traces (404)
npx langfuse-cli api observations list --trace-id 8bf382ae53767f200a24212647e28d3d \
  --fields core,basic,metadata --expand-metadata workflow.run_id,workflow.step_id,workflow.step_session_id,git.sha --json
```

### Real score acceptance — fresh native GUI workflow at THIS commit

Reload Kiro after hook/config pull. Identity for **new** runs only: `kiro-test-runner` / `demo-engineering` / `identity_type=automation` / tag `kiro-automation`. Run `two-step-demo` to completion (Stop is per-turn; wait for native `root.status=completed` and summarize `completionSignal`).

Then:

```bash
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --latest --verify-scores --json
```

Expect:

- `trace_id` is 32-hex of the **summarize** step, not `wf_*`, not the hung parent chat
- `provenance.ok=true` (observation `git.sha` matches `git rev-parse HEAD`)
- `scores_submitted` build_pass / test_pass / lint_clean
- `scores_readback` via GET `/api/public/v3/scores?traceId=...` non-empty
- score comment/metadata: `score_scope=workflow-objective-checks`, `attached_to=final-completed-step-trace`, `not_workflow_root_trace`, `workflow.run_id`, `check.git.sha`

Dedup: second call exits 0 without re-running checks unless `--force` (force still cannot bypass the SHA gate).

Do not claim score write complete until v3 list is non-empty **and** provenance.ok was true for that submit.

---

## Explicit limitations

- Observation volume/latency widgets are **not** workflow-run KPIs. wf_7482: 14 observations / 3 traces / 1 native workflow / 26.048s native wall.
- Hung parent-chat latency is not workflow duration.
- Tool spans with start==end are missing duration, not fast.
- Historical `wf_7482` / `5706b066` cannot truthfully receive this commit’s checks.
- Browser `:3000` blocked; AWS excluded; secrets preserved; this VM does not mutate the parent Langfuse box.
- Cleanup `--execute-cleanup` is parent-only and scoped to the 24 failed-acceptance placement UUIDs.
