# Troubleshooting

Never `cat` or `echo` `.kiro/langfuse.local.env`. Never `cp` the example over a working 0600 file.

| Symptom | Safe check | Expected |
|---------|------------|----------|
| Hooks skip | `python3 scripts/doctor.py` | `TRACE_TO_LANGFUSE=true`; key lines filled |
| 401 from APIs | `npx langfuse-cli api projects list --json` | HTTP 200 with **your** project list. `/health` is not auth. |
| No traces | Confirm native workflow ran in Kiro; `TRACE_TO_LANGFUSE`; `LANGFUSE_BASE_URL=http://127.0.0.1:3000` | Local state under `.kiro/langfuse-state-local/` (gitignored) |
| `trace_id=null` on finalize | Pass `--trace-id` of the **summarize** 32-hex id, or keep local session files; observations v2 needs `fields=metadata` | 32-hex, never `wf_*` |
| Provenance gate | `git rev-parse HEAD` vs observation `git.sha` | Must match. A later commit cannot score an older ingest. |
| Dashboard duplicates | GET-by-id `definition.widgets[].widgetId`; second apply adds 0 | LIST `placements` is a **count**, not tiles |
| `build_pass` not_configured | Copy `.kiro/objective-checks.json.template` (keys are `build_pass` / `test_pass` / `lint_clean`) | Commands are argv arrays |
| Compose won't start | `./scripts/langfuse-compose.sh check` with `LANGFUSE_COMPOSE_DIR` | This repo does not install Docker |
| Port 3000 in a Cloud Agent | Agent browser policy | **Not** a Langfuse limit. On your laptop, open localhost normally. |

## Known limits (not bugs)

See [limitations.md](limitations.md). Observation widgets are not workflow-run KPIs. `build_pass` is syntax compile, not a package build. Tool spans with start==end are missing duration.
