#!/usr/bin/env python3
"""Print native Kiro workflow ids for THIS machine's workspace. No Langfuse credentials."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".kiro" / "hooks"))

from finalize_workflow import (  # noqa: E402
    WorkflowState,
    find_latest_workflow,
    find_workflow_state,
    native_wall_ms,
    workspace_hash,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover native workflow-state.json files")
    parser.add_argument("--workspace", default=str(Path.cwd()), help="Workspace path (default: cwd)")
    parser.add_argument("--workflow-id", help="Look up one workflow id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    workspace = str(Path(args.workspace).resolve())

    if args.workflow_id:
        path = find_workflow_state(args.workflow_id, workspace)
        if not path:
            print(f"ERROR: workflow not found: {args.workflow_id}", file=sys.stderr)
            return 1
        wf = WorkflowState.from_file(path)
        payload = {
            "workflow_id": wf.workflow_id,
            "workflow_name": wf.workflow_name,
            "status": wf.status.value,
            "is_terminal": wf.is_terminal,
            "native_wall_ms": native_wall_ms(wf),
            "steps": [{"node_id": s.node_id, "status": s.status, "session_id": s.session_id} for s in wf.steps],
            "state_path": str(path),
        }
        print(json.dumps(payload, indent=2) if args.json else json.dumps(payload, indent=2))
        return 0

    wf_id, path = find_latest_workflow(workspace)
    out = {
        "workspace": workspace,
        "workspace_hash": workspace_hash(workspace),
        "latest_workflow_id": wf_id,
        "state_path": str(path) if path else None,
        "sessions_root": str(Path.home() / ".kiro" / "sessions"),
        "note": "Run two-step-demo in Kiro IDE first. This is not a CLI substitute for the workflow.",
    }
    if path:
        wf = WorkflowState.from_file(path)
        out.update({
            "workflow_name": wf.workflow_name,
            "status": wf.status.value,
            "is_terminal": wf.is_terminal,
            "native_wall_ms": native_wall_ms(wf),
            "step_count": len(wf.steps),
            "steps": [{"node_id": s.node_id, "status": s.status} for s in wf.steps],
        })
    print(json.dumps(out, indent=2))
    return 0 if wf_id else 2


if __name__ == "__main__":
    sys.exit(main())
