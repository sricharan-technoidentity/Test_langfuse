# Runtime acceptance (local Langfuse 4.76.0) — parent box only

This VM cannot reach the parent's `127.0.0.1:3000`. Do **not** treat unit tests as live ingest/score/dashboard proof.

Existing native run `wf_7482ba1888446d73` stays `user=chomu-local` / no team. Do **not** rewrite it. Future GUI runs use `kiro-test-runner` + `demo-engineering` + `identity_type=automation` (not a human engineer, not a real company team).

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

## After pulling this commit

```bash
cd /path/to/Test_langfuse-local-validation   # or this repo
# unit tests (not live proof)
python3 -m pytest tests/ -q
```

### Task2 — provision (idempotent)

```bash
python3 .kiro/hooks/dashboard_provision.py --dry-run --json
python3 .kiro/hooks/dashboard_provision.py --json
python3 .kiro/hooks/dashboard_provision.py --json   # second apply: 0 new placements
```

Expect:

- `Boolean Score Rates` chartType `PIVOT_TABLE` (not VERTICAL_BAR) — no HTTP 400
- Management/engineering dashboards reused by name
- Second apply: widgets `updated`, placements list empty (no duplicates)
- Widget titles/descriptions say **observation** counts, not workflow-run counts

Live query honesty (observations ≠ workflows). For wf_7482 there were **14 observations / 3 traces / 1 native workflow**. Parent hung ~133s is **not** the ~26s native root wall.

```bash
npx langfuse-cli api unstable-dashboards list --json
npx langfuse-cli api unstable-dashboard-widgets list --json
# Metrics API v2 counts observations, not uniq(workflow.run_id) — that dimension is unsupported
npx langfuse-cli api scores list --trace-id 8bf382ae53767f200a24212647e28d3d --json
```

**Unsupported in native Langfuse widgets (do not claim shipped):** distinct `workflow.run_id`, distinct human engineers, native completion rate, native duration, true per-tool latency (start==end), team/repo unique-run adoption. Filter `kiro-automation` in the UI if the tag is present on *future* traces.

### Task3 — finalize + score submit + readback + dedup

Copy checks if missing (gitignored): `cp .kiro/objective-checks.json.template .kiro/objective-checks.json`

```bash
# Dry-run: must NOT use trace_id=wf_...
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --workflow-id wf_7482ba1888446d73 --dry-run --json

# Submit to canonical last completed STEP trace (summarize 32-hex), then v3 readback
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --workflow-id wf_7482ba1888446d73 --verify-scores --json

# Dedup: second call exits 0 without re-running checks unless --force
python3 .kiro/hooks/finalize_workflow.py \
  --workspace "$PWD" --workflow-id wf_7482ba1888446d73 --json
```

Expected canonical trace for wf_7482 (if local session state still maps sessions):

- analyze `189269f11c8175150f4e8f017cf0849a`
- summarize `8bf382ae53767f200a24212647e28d3d` ← scores
- parent `b5ff64627d58a8d18bb15f2b0675a1ff` ← must not be used (hung chat)

If session state is gone, pass `--trace-id 8bf382ae53767f200a24212647e28d3d` only when that id is still a step of this run. A random 32-hex or `wf_...` is rejected.

Readback:

```bash
npx langfuse-cli api scores list --trace-id 8bf382ae53767f200a24212647e28d3d --json
# NOT GET /api/public/scores (404 on v4 events_only)
# NOT GET /api/public/traces (404)
```

Do not claim score write complete until this list is non-empty.

### Future native GUI run (new identity)

Reload Kiro after hook/config pull. Run `two-step-demo`. Expect tags `kiro-automation` (not human adoption), team `demo-engineering` (demo label), same `workflow.run_id` on both steps, distinct step session ids, real git SHA/repo from discovery.

Then finalize `--latest` against that new `wf_*` (not 7482).
