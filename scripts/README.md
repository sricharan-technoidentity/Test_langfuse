# Scripts

All scripts refuse to install paid services, wipe Docker volumes, or print secret keys.

| Script | What it does |
|--------|----------------|
| `check-prereqs.sh` | Report python/node/docker/kiro. No installs. |
| `setup-venv.sh` | Create or reuse `.venv`, `pip install -r requirements.txt` |
| `enable-tracing.sh` | Append `TRACE_TO_LANGFUSE=true` if missing |
| `langfuse-compose.sh` | Optional official Compose wrapper (`check`/`up`/`stop` only) |
| `doctor.py` | Path + tracing-flag diagnostics (no key values) |
| `discover-workflow.py` | Native `workflow-state.json` lookup on this machine |
| `print-urls.py` | URLs from **your** `--base-url` and `--project-id` |

Kiro `two-step-demo` is **not** wrapped here: it consumes your IDE plan/model usage.
