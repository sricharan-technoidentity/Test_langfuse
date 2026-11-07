#!/usr/bin/env python3
"""Task3: Explicit workflow finalization with objective scoring.

This command is invoked AFTER a workflow completes to:
1. Validate the workflow reached a terminal state (completed/error/cancelled)
2. Verify correlation (workflow/session/workspace)
3. Execute configured objective checks (build/test/lint)
4. Submit scores to Langfuse for the canonical workflow trace

CRITICAL: This is NOT triggered by Stop hook (fires per-turn, not at workflow end).
This command must be explicitly invoked after workflow completion.

Usage:
    python3 finalize_workflow.py --workflow-id wf_xxx --workspace /path/to/workspace
    
    # Or find latest workflow automatically:
    python3 finalize_workflow.py --workspace /path/to/workspace --latest

The command will:
1. Read native workflow state from ~/.kiro/sessions/<hash>/workflows/<wf_id>/workflow-state.json
2. Verify all steps completed (status: completed, completionSignal present on final step)
3. Execute checks from .kiro/objective-checks.json
4. Submit build_pass/test_pass/lint_clean scores to Langfuse

Security:
- Never reads/logs credential values
- Redacts sensitive output from check commands
- Bounded timeouts and output limits
- Kills descendant processes on timeout
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

OTEL_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STEP_STATUSES = frozenset({"completed", "error", "cancelled", "canceled"})


class WorkflowStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    RUNNING = "running"
    PENDING = "pending"


class ScoreOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_CONFIGURED = "not_configured"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class CheckConfig:
    """Configuration for an objective check."""
    name: str
    command: list[str]  # Parsed command array (no shell)
    timeout_seconds: int = 60
    enabled: bool = True
    working_dir: str | None = None


@dataclass
class CheckResult:
    """Result of running a check."""
    name: str
    outcome: ScoreOutcome
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str


@dataclass
class WorkflowStep:
    """Parsed native Kiro workflow step."""
    node_id: str
    session_id: str
    status: str  # completed, running, pending, error
    started_at: str | None
    ended_at: str | None
    completion_signal: str | None
    model_id: str | None


@dataclass
class WorkflowState:
    """Parsed native Kiro workflow state.
    
    Native Kiro workflow-state.json structure:
    {
      "workflowId": "wf_xxx",
      "workflowName": "...",
      "status": "completed",
      "root": {
        "status": "completed",
        "startedAt": "...",
        "endedAt": "...",
        "children": [
          {"nodeId": "analyze", "sessionId": "...", "status": "completed", ...},
          {"nodeId": "summarize", "sessionId": "...", "status": "completed", "completionSignal": "success", ...}
        ]
      }
    }
    
    NOTE: Steps are in root.children, NOT top-level steps[].
    """
    workflow_id: str
    workflow_name: str
    status: WorkflowStatus
    parent_session_id: str
    workspace_path: str
    steps: list[WorkflowStep]
    created_at: str
    started_at: str | None
    ended_at: str | None
    completion_signal: str | None
    is_terminal: bool
    step_trace_ids: dict[str, str]  # step_node_id -> trace_id from exported state

    @classmethod
    def from_file(cls, path: Path) -> "WorkflowState":
        """Load from workflow-state.json.
        
        Handles native Kiro structure where steps are in root.children.
        """
        data = json.loads(path.read_text())
        
        # Native Kiro uses root.children for steps, not top-level steps[]
        root = data.get("root") if isinstance(data.get("root"), dict) else {}
        children = _flatten_step_nodes(root.get("children") or [])

        # Last-resort fallback for older/synthetic fixtures with top-level steps[]
        if not children and isinstance(data.get("steps"), list):
            children = _flatten_step_nodes(data.get("steps") or [])

        steps = [_parse_step_node(child) for child in children]
        
        final_step = steps[-1] if steps else None

        # Determine terminal state from root.status or top-level status
        status_str = root.get("status") or data.get("status", "pending")
        try:
            status = WorkflowStatus(status_str)
        except ValueError:
            status = WorkflowStatus.PENDING

        is_terminal = status in (WorkflowStatus.COMPLETED, WorkflowStatus.ERROR, WorkflowStatus.CANCELLED)

        # Get completion signal from final step
        completion_signal = final_step.completion_signal if final_step else None
        
        # Get timestamps from root
        started_at = root.get("startedAt") or data.get("createdAt")
        ended_at = root.get("endedAt")

        return cls(
            workflow_id=data.get("workflowId", ""),
            workflow_name=data.get("workflowName", ""),
            status=status,
            parent_session_id=data.get("parentSessionId", ""),
            workspace_path=data.get("workspacePath", ""),
            steps=steps,
            created_at=data.get("createdAt", ""),
            started_at=started_at,
            ended_at=ended_at,
            completion_signal=completion_signal,
            is_terminal=is_terminal,
            step_trace_ids={},  # Populated separately from langfuse state
        )


def _flatten_step_nodes(nodes: list) -> list[dict]:
    """Flatten native root.children, including nested children.

    SYNTHETIC fixtures and native captures both use dict nodes with nodeId/sessionId.
    Nested containers are walked; non-dict entries are ignored.
    """
    out: list[dict] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        nested = node.get("children")
        looks_like_step = bool(node.get("nodeId") or node.get("sessionId"))
        if looks_like_step:
            out.append(node)
        if isinstance(nested, list):
            out.extend(_flatten_step_nodes(nested))
    return out


def _parse_step_node(child: dict) -> WorkflowStep:
    return WorkflowStep(
        node_id=child.get("nodeId") or child.get("id") or "",
        session_id=child.get("sessionId") or child.get("session_id") or "",
        status=(child.get("status") or "pending"),
        started_at=child.get("startedAt") or child.get("started_at"),
        ended_at=child.get("endedAt") or child.get("ended_at"),
        completion_signal=child.get("completionSignal") or child.get("completion_signal"),
        model_id=child.get("modelId") or child.get("model_id"),
    )


def is_valid_otel_trace_id(value: str | None) -> bool:
    """True only for a 32-char lowercase/mixed hex OTEL trace id. wf_* is never valid."""
    if not value or not isinstance(value, str):
        return False
    if value.startswith("wf_"):
        return False
    return bool(OTEL_TRACE_ID_RE.match(value.lower()))


def parse_iso_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


def native_wall_ms(wf: WorkflowState) -> int | None:
    """Wall time from native root startedAt/endedAt — not parent-chat agent latency."""
    start = parse_iso_ms(wf.started_at)
    end = parse_iso_ms(wf.ended_at)
    if start is None or end is None or end < start:
        return None
    return end - start


def incomplete_steps(wf: WorkflowState) -> list[WorkflowStep]:
    """Steps that are pending, running, or any unknown/non-terminal status."""
    out = []
    for step in wf.steps:
        status = (step.status or "").strip().lower()
        if status not in TERMINAL_STEP_STATUSES:
            out.append(step)
    return out


def paths_equivalent(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True  # missing native path is not a hard mismatch
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.normpath(a) == os.path.normpath(b)


def workspace_hash(cwd: str) -> str:
    """Hash workspace path for session directory lookup."""
    return hashlib.sha256(cwd.encode()).hexdigest()[:16]


def find_workflow_state(workflow_id: str, workspace: str) -> Path | None:
    """Find workflow-state.json for given workflow ID."""
    kiro_sessions = Path.home() / ".kiro" / "sessions"
    ws_hash = workspace_hash(workspace)

    # Try workspace-specific session first
    ws_path = kiro_sessions / ws_hash / "workflows" / workflow_id / "workflow-state.json"
    if ws_path.exists():
        return ws_path

    # Fallback: scan all session directories
    try:
        for session_dir in kiro_sessions.iterdir():
            candidate = session_dir / "workflows" / workflow_id / "workflow-state.json"
            if candidate.exists():
                return candidate
    except OSError:
        pass

    return None


def find_latest_workflow(workspace: str) -> tuple[str | None, Path | None]:
    """Find the most recently completed workflow for workspace."""
    kiro_sessions = Path.home() / ".kiro" / "sessions"
    ws_hash = workspace_hash(workspace)

    workflows_dir = kiro_sessions / ws_hash / "workflows"
    if not workflows_dir.exists():
        return None, None

    latest_time = None
    latest_id = None
    latest_path = None

    try:
        for wf_dir in workflows_dir.iterdir():
            state_file = wf_dir / "workflow-state.json"
            if state_file.exists():
                try:
                    data = json.loads(state_file.read_text())
                    created = data.get("createdAt", "")
                    if latest_time is None or created > latest_time:
                        latest_time = created
                        latest_id = wf_dir.name
                        latest_path = state_file
                except Exception:
                    continue
    except OSError:
        pass

    return latest_id, latest_path


def load_check_configs(workspace: str) -> dict[str, CheckConfig]:
    """Load check configurations from .kiro/objective-checks.json."""
    config_path = Path(workspace) / ".kiro" / "objective-checks.json"

    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return {}

    configs = {}
    for name in ["build_pass", "test_pass", "lint_clean"]:
        check_data = data.get(name, {})
        if not check_data:
            continue

        command_raw = check_data.get("command")
        if not command_raw:
            continue

        # Parse command: support string (split on spaces) or array
        if isinstance(command_raw, str):
            # Simple split - for shell-free execution
            # NOTE: This doesn't handle quoted arguments properly
            # For complex commands, use array format in config
            command = command_raw.split()
        elif isinstance(command_raw, list):
            command = command_raw
        else:
            continue

        configs[name] = CheckConfig(
            name=name,
            command=command,
            timeout_seconds=check_data.get("timeout_seconds", 60),
            enabled=check_data.get("enabled", True),
            working_dir=check_data.get("working_dir"),
        )

    return configs


def kill_process_tree(pid: int) -> None:
    """Kill process and all descendants."""
    try:
        # Try to kill process group
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    # Fallback: kill just the process
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def redact_output(text: str, max_len: int = 10000) -> str:
    """Redact potential secrets and truncate output."""
    # Patterns that might contain secrets
    patterns = [
        (r'(password|passwd|pwd|secret|token|key|api[_-]?key|auth)["\']?\s*[:=]\s*["\']?[^\s"\']+', r'\1=[REDACTED]'),
        (r'(Bearer|Basic)\s+[A-Za-z0-9+/=_-]+', r'\1 [REDACTED]'),
        (r'sk-[a-zA-Z0-9]{20,}', '[REDACTED_KEY]'),
        (r'pk-[a-zA-Z0-9]{20,}', '[REDACTED_KEY]'),
    ]

    result = text
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    if len(result) > max_len:
        result = result[:max_len] + f"\n... [truncated {len(text) - max_len} chars]"

    return result


def run_check(config: CheckConfig, workspace: str) -> CheckResult:
    """Execute a check command with timeout and output capture."""
    if not config.enabled:
        return CheckResult(
            name=config.name,
            outcome=ScoreOutcome.SKIPPED,
            exit_code=None,
            duration_ms=0,
            stdout="",
            stderr="Check disabled in config",
        )

    working_dir = config.working_dir or workspace
    if not Path(working_dir).is_dir():
        return CheckResult(
            name=config.name,
            outcome=ScoreOutcome.ERROR,
            exit_code=None,
            duration_ms=0,
            stdout="",
            stderr=f"Working directory not found: {working_dir}",
        )

    start = time.time()
    try:
        proc = subprocess.Popen(
            config.command,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # Create new process group for cleanup
        )

        try:
            stdout, stderr = proc.communicate(timeout=config.timeout_seconds)
            exit_code = proc.returncode
            outcome = ScoreOutcome.PASS if exit_code == 0 else ScoreOutcome.FAIL
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            proc.kill()
            stdout, stderr = proc.communicate()
            exit_code = None
            outcome = ScoreOutcome.TIMEOUT
            stderr = (stderr or "") + f"\n[TIMEOUT after {config.timeout_seconds}s]"

    except FileNotFoundError:
        return CheckResult(
            name=config.name,
            outcome=ScoreOutcome.ERROR,
            exit_code=None,
            duration_ms=int((time.time() - start) * 1000),
            stdout="",
            stderr=f"Command not found: {config.command[0]}",
        )
    except Exception as e:
        return CheckResult(
            name=config.name,
            outcome=ScoreOutcome.ERROR,
            exit_code=None,
            duration_ms=int((time.time() - start) * 1000),
            stdout="",
            stderr=f"Execution error: {type(e).__name__}: {e}",
        )

    duration_ms = int((time.time() - start) * 1000)

    return CheckResult(
        name=config.name,
        outcome=outcome,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=redact_output(stdout or ""),
        stderr=redact_output(stderr or ""),
    )


def submit_score(
    base_url: str,
    public_key: str,
    secret_key: str,
    trace_id: str,
    name: str,
    value: float,
    comment: str | None = None,
    data_type: str = "BOOLEAN",
) -> dict:
    """Submit a score to Langfuse."""
    url = f"{base_url.rstrip('/')}/api/public/scores"
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    payload = {
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": data_type,
    }
    if comment:
        payload["comment"] = comment[:1000]  # Langfuse limit

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def result_to_score_value(outcome: ScoreOutcome) -> float | None:
    """Convert outcome to boolean score value (1.0=pass, 0.0=fail, None=skip)."""
    if outcome == ScoreOutcome.PASS:
        return 1.0
    elif outcome == ScoreOutcome.FAIL:
        return 0.0
    else:
        # not_configured, skipped, timeout, error - don't submit score
        return None


def load_finalization_state(workspace: str) -> dict:
    """Load finalization state (tracks which workflows already scored)."""
    state_path = Path(workspace) / ".kiro" / "langfuse-state-local" / "finalization_state.json"
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {"scored_workflows": {}}


def load_session_trace_map(workspace: str) -> dict[str, str]:
    """Map Kiro session_id -> 32-hex OTEL trace_id from local hook state files."""
    state_dir = Path(workspace) / ".kiro" / "langfuse-state-local"
    skip = {
        "finalization_state.json",
        "objective_scores_state.json",
        "submitted_scores.json",
        "pending_scores.jsonl",
    }
    session_trace_map: dict[str, str] = {}
    if not state_dir.exists():
        return session_trace_map
    try:
        for state_file in state_dir.glob("*.json"):
            if state_file.name in skip:
                continue
            try:
                data = json.loads(state_file.read_text())
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            session_id = data.get("session_id") or state_file.stem
            trace_id = data.get("trace_id")
            if session_id and is_valid_otel_trace_id(trace_id):
                session_trace_map[session_id] = trace_id.lower()
    except OSError:
        pass
    return session_trace_map


def discover_step_traces(workspace: str, wf_state: WorkflowState) -> list[dict]:
    """Associate native step sessions with ingested 32-hex traces.

    Parent chat session is excluded unless it is also a step in root.children.
    Never invents a workflow-level trace id. Never uses wf_* as a trace id.
    """
    session_trace_map = load_session_trace_map(workspace)
    step_traces = []
    parent_id = wf_state.parent_session_id
    for step in wf_state.steps:
        tid = session_trace_map.get(step.session_id)
        if not tid:
            continue
        step_traces.append({
            "node_id": step.node_id,
            "session_id": step.session_id,
            "trace_id": tid,
            "status": step.status,
            "is_parent_chat": bool(parent_id) and step.session_id == parent_id,
        })
    return step_traces


def discover_trace_id_for_score(
    workspace: str,
    workflow_id: str,
    wf_state: WorkflowState,
    explicit_trace_id: str | None = None,
) -> tuple[str | None, str]:
    """Canonical score target: last completed *step* 32-hex OTEL trace.

    Never defaults to workflow_id (wf_* is not an OTEL trace).
    Never uses hung parent-chat traces.
    Explicit --trace-id must be 32-hex AND belong to a step of this run.
    """
    if workflow_id and is_valid_otel_trace_id(workflow_id):
        pass  # theoretically possible; still must match a step
    elif workflow_id and not explicit_trace_id:
        # wf_* is expected and is NOT a trace id
        pass

    step_traces = discover_step_traces(workspace, wf_state)
    step_ids = {t["trace_id"] for t in step_traces if not t.get("is_parent_chat")}
    # If a step happens to be the parent session (unusual), still allow it as a step
    if not step_ids:
        step_ids = {t["trace_id"] for t in step_traces}

    if explicit_trace_id:
        candidate = explicit_trace_id.lower()
        if not is_valid_otel_trace_id(candidate):
            return None, (
                f"Invalid --trace-id {explicit_trace_id!r}: must be a 32-hex OTEL trace id, "
                "not a workflow id (wf_...)"
            )
        if candidate not in {t["trace_id"] for t in step_traces}:
            return None, (
                f"--trace-id {candidate} is not associated with any step of {workflow_id}. "
                "Refusing dangling score submit."
            )
        if candidate not in step_ids:
            return None, (
                f"--trace-id {candidate} is the parent chat, not a workflow step of {workflow_id}"
            )
        matched = next(t for t in step_traces if t["trace_id"] == candidate)
        return candidate, f"explicit --trace-id (step {matched['node_id']})"

    completed_steps = [
        t for t in step_traces
        if (t["status"] or "").lower() == "completed" and not t.get("is_parent_chat")
    ]
    if completed_steps:
        chosen = completed_steps[-1]
        return chosen["trace_id"], f"canonical last completed step '{chosen['node_id']}'"

    return None, (
        f"No ingested 32-hex step trace for {workflow_id}. "
        "Do not submit scores to workflow_id. Re-run after hooks export step traces, "
        "or pass --trace-id of a verified step trace."
    )


def list_scores_v3(
    base_url: str,
    public_key: str,
    secret_key: str,
    trace_id: str,
) -> list[dict]:
    """Read back scores via documented GET /api/public/v3/scores (legacy GET /scores is 404 on v4 events_only)."""
    qs = urllib.parse.urlencode({"traceId": trace_id, "limit": 50})
    url = f"{base_url.rstrip('/')}/api/public/v3/scores?{qs}"
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if isinstance(body, dict):
        return body.get("data") or body.get("scores") or []
    return body if isinstance(body, list) else []


def save_finalization_state(workspace: str, state: dict) -> None:
    """Save finalization state."""
    state_dir = Path(workspace) / ".kiro" / "langfuse-state-local"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "finalization_state.json"
    state_path.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Finalize workflow and submit objective scores to Langfuse"
    )
    parser.add_argument("--workflow-id", "-w", help="Workflow ID (e.g., wf_xxx)")
    parser.add_argument("--workspace", required=True, help="Workspace path")
    parser.add_argument("--latest", action="store_true", help="Use most recent workflow")
    parser.add_argument("--trace-id", help="Override trace ID for score submission")
    parser.add_argument("--dry-run", action="store_true", help="Run checks but don't submit scores")
    parser.add_argument("--force", action="store_true", help="Re-run even if already scored")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "--verify-scores",
        action="store_true",
        help="After submit, GET /api/public/v3/scores?traceId=... (not legacy /api/public/scores)",
    )

    # Credentials from environment (never from args - security)
    args = parser.parse_args()

    workspace = os.path.abspath(args.workspace)
    if not Path(workspace).is_dir():
        print(f"ERROR: Workspace not found: {workspace}", file=sys.stderr)
        sys.exit(1)

    # Find workflow
    if args.latest:
        workflow_id, state_path = find_latest_workflow(workspace)
        if not workflow_id:
            print("ERROR: No workflows found for workspace", file=sys.stderr)
            sys.exit(1)
    elif args.workflow_id:
        workflow_id = args.workflow_id
        state_path = find_workflow_state(workflow_id, workspace)
        if not state_path:
            print(f"ERROR: Workflow not found: {workflow_id}", file=sys.stderr)
            sys.exit(1)
    else:
        print("ERROR: Specify --workflow-id or --latest", file=sys.stderr)
        sys.exit(1)

    # Load and validate workflow state
    try:
        wf_state = WorkflowState.from_file(state_path)
    except Exception as e:
        print(f"ERROR: Failed to load workflow state: {e}", file=sys.stderr)
        sys.exit(1)

    # Verify terminal state
    if not wf_state.is_terminal:
        print(f"ERROR: Workflow {workflow_id} is not terminal (status={wf_state.status.value})", file=sys.stderr)
        print("Cannot score a running workflow. Wait for completion.", file=sys.stderr)
        sys.exit(1)

    # Check for premature finalization (analyze completed but summarize not started)
    # Also reject unknown/non-terminal child statuses even if root.status looks completed.
    unfinished = incomplete_steps(wf_state)
    if unfinished:
        detail = ", ".join(f"{s.node_id}={s.status or 'unknown'}" for s in unfinished)
        print(
            f"ERROR: Workflow has incomplete steps ({detail}). "
            "Cannot score until every root.children step is terminal.",
            file=sys.stderr,
        )
        sys.exit(1)

    if wf_state.workspace_path and not paths_equivalent(wf_state.workspace_path, workspace):
        print(
            f"ERROR: Native workspacePath {wf_state.workspace_path} does not match --workspace {workspace}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check idempotency
    fin_state = load_finalization_state(workspace)
    if workflow_id in fin_state.get("scored_workflows", {}) and not args.force:
        prev = fin_state["scored_workflows"][workflow_id]
        print(f"Workflow {workflow_id} already scored at {prev.get('scored_at')}", file=sys.stderr)
        print("Use --force to re-run", file=sys.stderr)
        sys.exit(0)

    # Load check configs
    configs = load_check_configs(workspace)

    # Execute checks
    results = {}
    for name in ["build_pass", "test_pass", "lint_clean"]:
        config = configs.get(name)
        if config:
            results[name] = run_check(config, workspace)
        else:
            results[name] = CheckResult(
                name=name,
                outcome=ScoreOutcome.NOT_CONFIGURED,
                exit_code=None,
                duration_ms=0,
                stdout="",
                stderr="No configuration in .kiro/objective-checks.json",
            )

    # Output results
    output = {
        "workflow_id": workflow_id,
        "workflow_name": wf_state.workflow_name,
        "workflow_status": wf_state.status.value,
        "workspace": workspace,
        "native_started_at": wf_state.started_at,
        "native_ended_at": wf_state.ended_at,
        "native_wall_ms": native_wall_ms(wf_state),
        "native_wall_note": "Duration from native root startedAt/endedAt, not parent-chat agent latency",
        "completion_signal": wf_state.completion_signal,
        "step_count": len(wf_state.steps),
        "checks": {},
        "scores_submitted": [],
        "errors": [],
    }

    for name, result in results.items():
        output["checks"][name] = {
            "outcome": result.outcome.value,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        }

    # Discover trace ID for score submission
    # CRITICAL: workflow_id is NOT a valid OTEL trace ID. Must use actual ingested trace.
    trace_id, trace_source = discover_trace_id_for_score(
        workspace, workflow_id, wf_state, args.trace_id
    )
    
    output["trace_id"] = trace_id
    output["trace_source"] = trace_source

    if not trace_id:
        msg = f"Cannot determine trace ID: {trace_source}"
        if args.dry_run:
            output["trace_warning"] = msg
        else:
            output["errors"].append(msg)
    
    # Submit scores to Langfuse (if not dry run and credentials available)
    if not args.dry_run:
        base_url = os.environ.get("LANGFUSE_BASE_URL", "http://127.0.0.1:3000")
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

        if not trace_id:
            output["errors"].append("Score submission skipped: no valid trace ID")
        elif public_key and secret_key:
            for name, result in results.items():
                score_value = result_to_score_value(result.outcome)
                if score_value is not None:
                    try:
                        comment = f"{result.outcome.value} (exit={result.exit_code}, {result.duration_ms}ms) workflow={workflow_id}"
                        submit_score(
                            base_url, public_key, secret_key,
                            trace_id=trace_id,
                            name=name,
                            value=score_value,
                            comment=comment,
                        )
                        output["scores_submitted"].append({
                            "name": name,
                            "value": score_value,
                            "trace_id": trace_id,
                        })
                    except Exception as e:
                        output["errors"].append(f"Score submission failed for {name}: {e}")
        else:
            output["errors"].append("Scores not submitted: LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set")

        if args.verify_scores and trace_id and public_key and secret_key:
            try:
                output["scores_readback"] = list_scores_v3(base_url, public_key, secret_key, trace_id)
            except Exception as e:
                output["errors"].append(f"v3 scores readback failed: {e}")

    # Save finalization state only after a real submit with a valid trace
    if not args.dry_run and trace_id and output["scores_submitted"] and not any(
        "Score submission failed" in e for e in output["errors"]
    ):
        fin_state.setdefault("scored_workflows", {})[workflow_id] = {
            "scored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcomes": {n: r.outcome.value for n, r in results.items()},
            "trace_id": trace_id,
            "trace_source": trace_source,
        }
        save_finalization_state(workspace, fin_state)

    # Output
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("\n=== Workflow Finalization ===")
        print(f"Workflow: {workflow_id} ({wf_state.workflow_name})")
        print(f"Status: {wf_state.status.value}")
        print(f"Workspace: {workspace}")
        print()
        print("Check Results:")
        for name, result in results.items():
            status_icon = "✓" if result.outcome == ScoreOutcome.PASS else "✗" if result.outcome == ScoreOutcome.FAIL else "○"
            print(f"  {status_icon} {name}: {result.outcome.value}", end="")
            if result.exit_code is not None:
                print(f" (exit={result.exit_code}, {result.duration_ms}ms)")
            else:
                print()

        if output["scores_submitted"]:
            print(f"\nScores submitted to Langfuse (trace={trace_id}):")
            for s in output["scores_submitted"]:
                print(f"  - {s['name']}: {s['value']}")
        elif args.dry_run:
            print("\n[DRY RUN - scores not submitted]")

        if output["errors"]:
            print("\nErrors:")
            for e in output["errors"]:
                print(f"  ! {e}")

    # Exit code based on check outcomes
    has_failures = any(r.outcome == ScoreOutcome.FAIL for r in results.values())
    has_errors = bool(output["errors"]) or any(r.outcome == ScoreOutcome.ERROR for r in results.values())

    if has_errors:
        sys.exit(2)
    elif has_failures:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
