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
from datetime import datetime, timezone
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
    metadata: dict | None = None,
) -> dict:
    """Submit a score to Langfuse POST /api/public/scores."""
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
    if metadata:
        payload["metadata"] = metadata

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
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


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


SCORE_SCOPE = "workflow-objective-checks"
SCORE_ATTACHED_TO = "final-completed-step-trace"
OBS_METADATA_KEYS = (
    "workflow.run_id,workflow.step_id,workflow.step_session_id,"
    "kiro.session_id,git.sha"
)


@dataclass
class TraceAssociation:
    """Score target: a real ingested step trace, never a workflow-root stand-in."""
    trace_id: str | None
    source: str
    step_id: str | None = None
    step_session_id: str | None = None
    observation_count: int | None = None
    git_sha: str | None = None
    workflow_run_id: str | None = None
    association_method: str | None = None
    score_scope: str = SCORE_SCOPE
    attached_to: str = SCORE_ATTACHED_TO
    not_workflow_root_trace: bool = True


@dataclass
class ProvenanceGate:
    ok: bool
    workspace_head: str | None
    trace_git_sha: str | None
    reason: str


def flatten_obs_metadata(meta: object) -> dict[str, str]:
    """Flatten observations-v2 metadata (dict, nested dict, or [{key,value}])."""
    out: dict[str, str] = {}
    if isinstance(meta, dict):
        for k, v in meta.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if v2 is None or isinstance(v2, (dict, list)):
                        continue
                    out[f"{k}.{k2}"] = str(v2)
            elif v is None or isinstance(v, (dict, list)):
                continue
            else:
                out[str(k)] = str(v)
    elif isinstance(meta, list):
        for item in meta:
            if isinstance(item, dict) and item.get("key") is not None:
                val = item.get("value")
                if val is None or isinstance(val, (dict, list)):
                    continue
                out[str(item["key"])] = str(val)
    return out


def git_sha_equivalent(a: str | None, b: str | None) -> bool:
    """True when a and b name the same commit (full or unambiguous abbreviation)."""
    if not a or not b:
        return False
    x, y = a.lower().strip(), b.lower().strip()
    if x == y:
        return True
    shorter, longer = (x, y) if len(x) < len(y) else (y, x)
    return len(shorter) >= 7 and longer.startswith(shorter)


def workspace_git_sha(workspace: str) -> str | None:
    """HEAD of the workspace worktree. Never invents a SHA."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def evaluate_provenance_gate(
    workspace_head: str | None,
    trace_git_sha: str | None,
) -> ProvenanceGate:
    """Refuse scoring a historical ingest with checks run at a later SHA.

    Live gate (evidence_d8fc.json): wf_7482 traces carry
    git.sha=5706b0662db599dce44e04daf890161785c4192c; checks at 9d5390b must not
    be attributed to that code. Missing git.sha also fails — cannot prove match.
    """
    if not workspace_head:
        return ProvenanceGate(
            False, workspace_head, trace_git_sha,
            "Cannot read workspace git HEAD; refuse score submit without check provenance.",
        )
    if not trace_git_sha:
        return ProvenanceGate(
            False, workspace_head, trace_git_sha,
            "Trace observations have no metadata.git.sha; cannot prove the ingested "
            "run matches this commit. Fresh native workflow at this HEAD is required.",
        )
    if git_sha_equivalent(workspace_head, trace_git_sha):
        return ProvenanceGate(
            True, workspace_head, trace_git_sha,
            f"trace git.sha matches workspace HEAD {workspace_head}",
        )
    return ProvenanceGate(
        False, workspace_head, trace_git_sha,
        f"Provenance mismatch: trace git.sha={trace_git_sha} is not workspace HEAD "
        f"{workspace_head}. Historical run cannot receive checks from today's commit. "
        "Run a fresh native workflow at this HEAD.",
    )


def _iso_window(wf_state: WorkflowState) -> tuple[str | None, str | None]:
    """Bounded observations-v2 window from native timestamps (+/- 1 day)."""
    start = wf_state.started_at or wf_state.created_at
    end = wf_state.ended_at or wf_state.started_at or wf_state.created_at
    if not start:
        return None, None

    def _shift(iso: str, seconds: int) -> str:
        ms = parse_iso_ms(iso)
        if ms is None:
            return iso
        dt = datetime.fromtimestamp(ms / 1000.0 + seconds, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return _shift(start, -86400), _shift(end or start, 86400)


def list_observations_v2(
    base_url: str,
    public_key: str,
    secret_key: str,
    *,
    trace_id: str | None = None,
    workflow_run_id: str | None = None,
    from_start: str | None = None,
    to_start: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """GET /api/public/v2/observations with fields including metadata.

    Docs: if `fields` omits `metadata`, the group is absent (parent saw null).
    Request core,basic,metadata,trace_context and expandMetadata for workflow/git keys.
    Always bound with fromStartTime/toStartTime when timestamps are known.
    """
    params: dict[str, str] = {
        "fields": "core,basic,metadata,trace_context",
        "expandMetadata": OBS_METADATA_KEYS,
        "limit": str(limit),
    }
    if trace_id:
        params["traceId"] = trace_id
    if from_start:
        params["fromStartTime"] = from_start
    if to_start:
        params["toStartTime"] = to_start
    if workflow_run_id:
        params["filter"] = json.dumps([{
            "type": "stringObject",
            "column": "metadata",
            "key": "workflow.run_id",
            "operator": "=",
            "value": workflow_run_id,
        }])

    qs = urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}/api/public/v2/observations?{qs}"
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if isinstance(body, dict):
        data = body.get("data") or body.get("observations") or []
        return data if isinstance(data, list) else []
    return body if isinstance(body, list) else []


def observations_link_to_workflow(
    observations: list[dict],
    wf_state: WorkflowState,
) -> tuple[bool, str, dict]:
    """True when observations-v2 metadata ties this trace to a native step.

    Langfuse sessionId is workspace_key, not the Kiro step session. Association
    must use metadata.workflow.run_id plus step_session_id / step_id / kiro.session_id.
    Empty metadata (fields group not requested) cannot associate.
    """
    if not observations:
        return False, "no observations returned for this trace", {}

    native_sessions = {s.session_id for s in wf_state.steps if s.session_id}
    native_ids = {s.node_id for s in wf_state.steps if s.node_id}
    parent_id = wf_state.parent_session_id
    metas = [flatten_obs_metadata(o.get("metadata")) for o in observations if isinstance(o, dict)]
    if not any(metas):
        return False, (
            "observations v2 returned no metadata (need fields=core,basic,metadata). "
            "Cannot associate this trace with the native workflow."
        ), {"observation_count": len(observations)}

    matched_step_id = None
    matched_session = None
    matched_run = None
    git_sha = None
    for meta in metas:
        run_id = meta.get("workflow.run_id")
        step_session = meta.get("workflow.step_session_id") or meta.get("kiro.session_id")
        step_id = meta.get("workflow.step_id")
        if meta.get("git.sha") and not git_sha:
            git_sha = meta.get("git.sha")
        if run_id and run_id != wf_state.workflow_id:
            continue
        if step_session and parent_id and step_session == parent_id and step_session not in native_sessions:
            continue
        step_ok = bool(step_session and step_session in native_sessions)
        id_ok = bool(step_id and step_id in native_ids)
        run_ok = bool(run_id and run_id == wf_state.workflow_id)
        if run_ok and (step_ok or id_ok):
            matched_run = run_id
            matched_step_id = step_id if id_ok else matched_step_id
            matched_session = step_session if step_ok else matched_session
            if step_ok and not matched_step_id:
                for s in wf_state.steps:
                    if s.session_id == step_session:
                        matched_step_id = s.node_id
                        break

    if not matched_run and not matched_session:
        return False, (
            f"observations metadata does not associate this trace with {wf_state.workflow_id} "
            "(need workflow.run_id and a native step session/id). Not a workflow-root trace."
        ), {"observation_count": len(observations), "git.sha": git_sha}

    details = {
        "observation_count": len(observations),
        "workflow.run_id": matched_run or wf_state.workflow_id,
        "workflow.step_id": matched_step_id,
        "workflow.step_session_id": matched_session,
        "git.sha": git_sha,
    }
    return True, (
        f"observations-v2 metadata matches workflow {wf_state.workflow_id} "
        f"step={matched_step_id or matched_session} obs={len(observations)}"
    ), details


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


def _association_from_local(
    chosen: dict,
    source: str,
    method: str,
) -> TraceAssociation:
    return TraceAssociation(
        trace_id=chosen["trace_id"],
        source=source,
        step_id=chosen.get("node_id"),
        step_session_id=chosen.get("session_id"),
        association_method=method,
        workflow_run_id=None,
    )


def discover_trace_id_for_score(
    workspace: str,
    workflow_id: str,
    wf_state: WorkflowState,
    explicit_trace_id: str | None = None,
    observations_for_trace=None,
    observations_for_run=None,
) -> TraceAssociation:
    """Canonical score target: last completed *step* 32-hex OTEL trace.

    Never defaults to workflow_id (wf_* is not an OTEL trace).
    Never uses hung parent-chat traces.
    Never pretends the final step is a workflow-root trace.
    Explicit --trace-id must be 32-hex AND belong to a step of this run,
    proven either by local session state OR observations-v2 metadata.
    """
    empty = TraceAssociation(trace_id=None, source="", workflow_run_id=workflow_id)
    step_traces = discover_step_traces(workspace, wf_state)
    local_ids = {t["trace_id"] for t in step_traces}
    non_parent = {t["trace_id"] for t in step_traces if not t.get("is_parent_chat")}

    def _from_obs(trace_id: str, method: str) -> TraceAssociation | None:
        if observations_for_trace is None:
            return None
        try:
            obs = observations_for_trace(trace_id)
        except Exception as e:
            return TraceAssociation(
                trace_id=None,
                source=f"observations-v2 lookup failed for {trace_id}: {e}",
                workflow_run_id=workflow_id,
                association_method="observations-v2-error",
            )
        ok, reason, details = observations_link_to_workflow(obs, wf_state)
        if not ok:
            return TraceAssociation(
                trace_id=None,
                source=reason,
                observation_count=details.get("observation_count"),
                git_sha=details.get("git.sha"),
                workflow_run_id=workflow_id,
                association_method=method,
            )
        return TraceAssociation(
            trace_id=trace_id,
            source=reason,
            step_id=details.get("workflow.step_id"),
            step_session_id=details.get("workflow.step_session_id"),
            observation_count=details.get("observation_count"),
            git_sha=details.get("git.sha"),
            workflow_run_id=details.get("workflow.run_id") or workflow_id,
            association_method=method,
        )

    if explicit_trace_id:
        candidate = explicit_trace_id.lower()
        if not is_valid_otel_trace_id(candidate):
            empty.source = (
                f"Invalid --trace-id {explicit_trace_id!r}: must be a 32-hex OTEL trace id, "
                "not a workflow id (wf_...)"
            )
            return empty
        if candidate in local_ids:
            if non_parent and candidate not in non_parent:
                empty.source = (
                    f"--trace-id {candidate} is the parent chat, not a workflow step of {workflow_id}"
                )
                return empty
            matched = next(t for t in step_traces if t["trace_id"] == candidate)
            assoc = _association_from_local(
                matched,
                f"explicit --trace-id (step {matched['node_id']})",
                "explicit-local-state",
            )
            obs_assoc = _from_obs(candidate, "explicit-observations-v2")
            if obs_assoc and obs_assoc.trace_id:
                assoc.git_sha = obs_assoc.git_sha
                assoc.observation_count = obs_assoc.observation_count
                assoc.workflow_run_id = obs_assoc.workflow_run_id or workflow_id
                assoc.source = (
                    f"explicit --trace-id (step {matched['node_id']}); {obs_assoc.source}"
                )
                assoc.association_method = "explicit-local-state+observations-v2"
            elif obs_assoc and obs_assoc.association_method == "observations-v2-error":
                assoc.source = f"{assoc.source}; {obs_assoc.source}"
            return assoc

        obs_assoc = _from_obs(candidate, "explicit-observations-v2")
        if obs_assoc is None:
            empty.source = (
                f"--trace-id {candidate} is not associated with any step of {workflow_id}. "
                "No local session map and observations-v2 was not queried. "
                "Refusing dangling score submit."
            )
            return empty
        if obs_assoc.trace_id:
            parent_sessions = {wf_state.parent_session_id} if wf_state.parent_session_id else set()
            if obs_assoc.step_session_id and obs_assoc.step_session_id in parent_sessions:
                if obs_assoc.step_session_id not in {s.session_id for s in wf_state.steps}:
                    obs_assoc.trace_id = None
                    obs_assoc.source = (
                        f"--trace-id {candidate} is the parent chat, not a workflow step of {workflow_id}"
                    )
            return obs_assoc
        obs_assoc.source = (
            f"--trace-id {candidate} is not associated with any step of {workflow_id}. "
            f"{obs_assoc.source} Refusing dangling score submit."
        )
        return obs_assoc

    completed_steps = [
        t for t in step_traces
        if (t["status"] or "").lower() == "completed" and not t.get("is_parent_chat")
    ]
    if completed_steps:
        chosen = completed_steps[-1]
        assoc = _association_from_local(
            chosen,
            f"canonical last completed step '{chosen['node_id']}'",
            "local-state",
        )
        obs_assoc = _from_obs(chosen["trace_id"], "observations-v2")
        if obs_assoc and obs_assoc.trace_id:
            assoc.git_sha = obs_assoc.git_sha
            assoc.observation_count = obs_assoc.observation_count
            assoc.workflow_run_id = obs_assoc.workflow_run_id or workflow_id
            assoc.association_method = "local-state+observations-v2"
        return assoc

    if observations_for_run is not None:
        try:
            run_obs = observations_for_run(workflow_id)
        except Exception as e:
            empty.source = f"observations-v2 workflow.run_id lookup failed: {e}"
            empty.association_method = "observations-v2-error"
            return empty
        by_trace: dict[str, list[dict]] = {}
        for o in run_obs or []:
            if not isinstance(o, dict):
                continue
            tid = o.get("traceId") or o.get("trace_id")
            if isinstance(tid, str) and is_valid_otel_trace_id(tid):
                by_trace.setdefault(tid.lower(), []).append(o)
        last_step = None
        for step in wf_state.steps:
            if (step.status or "").lower() != "completed":
                continue
            if wf_state.parent_session_id and step.session_id == wf_state.parent_session_id:
                continue
            last_step = step
        candidates = []
        for tid, obs in by_trace.items():
            ok, reason, details = observations_link_to_workflow(obs, wf_state)
            if not ok:
                continue
            if last_step and details.get("workflow.step_id") and details.get("workflow.step_id") != last_step.node_id:
                continue
            if last_step and details.get("workflow.step_session_id") and details.get("workflow.step_session_id") != last_step.session_id:
                if details.get("workflow.step_id") != last_step.node_id:
                    continue
            candidates.append((tid, reason, details))
        if len(candidates) == 1:
            tid, reason, details = candidates[0]
            return TraceAssociation(
                trace_id=tid,
                source=f"canonical last completed step via observations-v2: {reason}",
                step_id=details.get("workflow.step_id"),
                step_session_id=details.get("workflow.step_session_id"),
                observation_count=details.get("observation_count"),
                git_sha=details.get("git.sha"),
                workflow_run_id=details.get("workflow.run_id") or workflow_id,
                association_method="observations-v2-run-id",
            )
        if len(candidates) > 1 and last_step:
            for tid, reason, details in candidates:
                if details.get("workflow.step_id") == last_step.node_id or details.get("workflow.step_session_id") == last_step.session_id:
                    return TraceAssociation(
                        trace_id=tid,
                        source=f"canonical last completed step via observations-v2: {reason}",
                        step_id=details.get("workflow.step_id"),
                        step_session_id=details.get("workflow.step_session_id"),
                        observation_count=details.get("observation_count"),
                        git_sha=details.get("git.sha"),
                        workflow_run_id=details.get("workflow.run_id") or workflow_id,
                        association_method="observations-v2-run-id",
                    )

    empty.source = (
        f"No ingested 32-hex step trace for {workflow_id}. "
        "Do not submit scores to workflow_id. Re-run after hooks export step traces, "
        "query observations v2 with fields=metadata, or pass --trace-id of a verified step trace."
    )
    return empty


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
    parser.add_argument(
        "--trace-id",
        help="32-hex final-step OTEL trace. Must match this workflow via local state or observations v2 metadata (not a workflow-root id).",
    )
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

    # Discover trace ID for score submission.
    # CRITICAL: workflow_id is NOT a valid OTEL trace ID. Scores attach to a
    # final-step 32-hex trace with run+SHA provenance, not a workflow-root trace.
    base_url = os.environ.get("LANGFUSE_BASE_URL", "http://127.0.0.1:3000")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    from_start, to_start = _iso_window(wf_state)

    observations_for_trace = None
    observations_for_run = None
    if public_key and secret_key:
        def observations_for_trace(trace_id: str) -> list:
            return list_observations_v2(
                base_url, public_key, secret_key,
                trace_id=trace_id,
                from_start=from_start,
                to_start=to_start,
            )

        def observations_for_run(run_id: str) -> list:
            return list_observations_v2(
                base_url, public_key, secret_key,
                workflow_run_id=run_id,
                from_start=from_start,
                to_start=to_start,
            )

    assoc = discover_trace_id_for_score(
        workspace, workflow_id, wf_state, args.trace_id,
        observations_for_trace=observations_for_trace,
        observations_for_run=observations_for_run,
    )
    trace_id = assoc.trace_id
    trace_source = assoc.source

    output["trace_id"] = trace_id
    output["trace_source"] = trace_source
    output["trace_association"] = {
        "method": assoc.association_method,
        "step_id": assoc.step_id,
        "step_session_id": assoc.step_session_id,
        "observation_count": assoc.observation_count,
        "git.sha": assoc.git_sha,
        "workflow.run_id": assoc.workflow_run_id or workflow_id,
        "score_scope": assoc.score_scope,
        "attached_to": assoc.attached_to,
        "not_workflow_root_trace": assoc.not_workflow_root_trace,
    }
    output["native_wall_note"] = (
        "Duration from native root startedAt/endedAt, not parent-chat agent latency. "
        "Observation/trace counts are not workflow-run KPIs."
    )

    workspace_head = workspace_git_sha(workspace)
    gate = evaluate_provenance_gate(workspace_head, assoc.git_sha)
    output["provenance"] = {
        "ok": gate.ok,
        "workspace_head": gate.workspace_head,
        "trace_git_sha": gate.trace_git_sha,
        "reason": gate.reason,
    }

    if not trace_id:
        msg = f"Cannot determine trace ID: {trace_source}"
        if args.dry_run:
            output["trace_warning"] = msg
        else:
            output["errors"].append(msg)
    elif not gate.ok:
        msg = f"Provenance gate blocked score submit: {gate.reason}"
        if args.dry_run:
            output["provenance_warning"] = msg
        else:
            output["errors"].append(msg)

    # Submit scores only when associated AND provenance matches this HEAD.
    if not args.dry_run:
        if not trace_id:
            output["errors"].append("Score submission skipped: no valid trace ID")
        elif not gate.ok:
            output["errors"].append(
                "Score submission skipped: provenance/code mismatch. "
                "Do not attach today's checks to a historical ingest."
            )
        elif public_key and secret_key:
            score_metadata = {
                "score_scope": SCORE_SCOPE,
                "attached_to": SCORE_ATTACHED_TO,
                "not_workflow_root_trace": "true",
                "workflow.run_id": workflow_id,
                "workflow.step_id": assoc.step_id or "",
                "workflow.step_session_id": assoc.step_session_id or "",
                "git.sha": assoc.git_sha or "",
                "check.git.sha": workspace_head or "",
            }
            for name, result in results.items():
                score_value = result_to_score_value(result.outcome)
                if score_value is not None:
                    try:
                        comment = (
                            f"{result.outcome.value} (exit={result.exit_code}, {result.duration_ms}ms) "
                            f"workflow-objective-checks on final completed step trace "
                            f"(not workflow-root). workflow={workflow_id} "
                            f"step={assoc.step_id or ''} git.sha={assoc.git_sha or ''} "
                            f"check.git.sha={workspace_head or ''}"
                        )
                        submit_score(
                            base_url, public_key, secret_key,
                            trace_id=trace_id,
                            name=name,
                            value=score_value,
                            comment=comment,
                            metadata=score_metadata,
                        )
                        output["scores_submitted"].append({
                            "name": name,
                            "value": score_value,
                            "trace_id": trace_id,
                            "score_scope": SCORE_SCOPE,
                            "attached_to": SCORE_ATTACHED_TO,
                        })
                    except Exception as e:
                        output["errors"].append(f"Score submission failed for {name}: {e}")
        else:
            output["errors"].append("Scores not submitted: LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set")

        if args.verify_scores and trace_id and public_key and secret_key and output["scores_submitted"]:
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
