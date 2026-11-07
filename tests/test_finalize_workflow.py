"""Tests for Task3 finalize_workflow command."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent / ".kiro" / "hooks"))
from finalize_workflow import (
    WorkflowState,
    WorkflowStep,
    WorkflowStatus,
    ScoreOutcome,
    CheckConfig,
    CheckResult,
    load_check_configs,
    run_check,
    redact_output,
    result_to_score_value,
    find_workflow_state,
    workspace_hash,
    discover_trace_id_for_score,
    is_valid_otel_trace_id,
    incomplete_steps,
    native_wall_ms,
)


class TestWorkflowState:
    """Test workflow state parsing and validation."""

    def test_parse_completed_workflow_native_schema(self, tmp_path: Path):
        """Parse a completed two-step workflow using native Kiro root.children schema."""
        # This is the ACTUAL native Kiro structure (root.children, not top-level steps[])
        state = {
            "workflowId": "wf_test123",
            "workflowName": "two-step-demo",
            "parentSessionId": "sess_parent",
            "workspacePath": str(tmp_path),
            "createdAt": "2026-11-06T15:00:00Z",
            "root": {
                "status": "completed",
                "startedAt": "2026-11-06T15:00:00.100Z",
                "endedAt": "2026-11-06T15:00:30.500Z",
                "children": [
                    {
                        "nodeId": "analyze",
                        "sessionId": "sess_analyze_123",
                        "status": "completed",
                        "startedAt": "2026-11-06T15:00:00.200Z",
                        "endedAt": "2026-11-06T15:00:15.000Z",
                        "completionSignal": None,
                        "modelId": "claude-haiku-4.5",
                    },
                    {
                        "nodeId": "summarize",
                        "sessionId": "sess_summarize_456",
                        "status": "completed",
                        "startedAt": "2026-11-06T15:00:15.100Z",
                        "endedAt": "2026-11-06T15:00:30.400Z",
                        "completionSignal": "success",
                        "modelId": "claude-haiku-4.5",
                    },
                ],
            },
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        assert wf.workflow_id == "wf_test123"
        assert wf.workflow_name == "two-step-demo"
        assert wf.status == WorkflowStatus.COMPLETED
        assert wf.is_terminal is True
        assert wf.completion_signal == "success"
        assert len(wf.steps) == 2
        # Verify step parsing
        assert wf.steps[0].node_id == "analyze"
        assert wf.steps[0].session_id == "sess_analyze_123"
        assert wf.steps[0].status == "completed"
        assert wf.steps[1].node_id == "summarize"
        assert wf.steps[1].completion_signal == "success"
        # Verify timestamps from root
        assert wf.started_at == "2026-11-06T15:00:00.100Z"
        assert wf.ended_at == "2026-11-06T15:00:30.500Z"

    def test_parse_legacy_steps_schema(self, tmp_path: Path):
        """Fallback to top-level steps[] for older format."""
        state = {
            "workflowId": "wf_legacy",
            "workflowName": "legacy-workflow",
            "status": "completed",
            "steps": [
                {"nodeId": "step1", "sessionId": "s1", "status": "completed"},
            ],
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        assert wf.workflow_id == "wf_legacy"
        assert len(wf.steps) == 1
        assert wf.steps[0].node_id == "step1"

    def test_running_workflow_not_terminal(self, tmp_path: Path):
        """Running workflow should not be terminal."""
        state = {
            "workflowId": "wf_running",
            "root": {
                "status": "running",
                "children": [
                    {"nodeId": "step1", "sessionId": "s1", "status": "completed"},
                    {"nodeId": "step2", "sessionId": "s2", "status": "running"},
                ],
            },
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        assert wf.status == WorkflowStatus.RUNNING
        assert wf.is_terminal is False

    def test_error_workflow_is_terminal(self, tmp_path: Path):
        """Error workflow should be terminal."""
        state = {
            "workflowId": "wf_error",
            "root": {"status": "error", "children": []},
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        assert wf.status == WorkflowStatus.ERROR
        assert wf.is_terminal is True


class TestPrematureStopRejection:
    """Test that finalization is rejected when workflow not complete."""

    def test_reject_when_analyze_done_but_summarize_pending(self, tmp_path: Path):
        """Simulate Stop after analyze but before summarize starts.
        
        This is the critical case: Stop fires after analyze step completes,
        but summarize hasn't even started yet. We must reject scoring.
        """
        state = {
            "workflowId": "wf_premature",
            "workflowName": "two-step-demo",
            "status": "running",  # Still running overall
            "steps": [
                {"nodeId": "analyze", "status": "completed", "completionSignal": None},
                {"nodeId": "summarize", "status": "pending"},  # Not started!
            ],
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        # Workflow is not terminal
        assert wf.is_terminal is False
        
        # Would have pending steps (WorkflowStep objects, native or legacy fixture)
        from finalize_workflow import incomplete_steps
        unfinished = incomplete_steps(wf)
        assert len(unfinished) == 1
        assert unfinished[0].node_id == "summarize"

    def test_reject_when_step_still_running(self, tmp_path: Path):
        """Reject when any step is still running."""
        state = {
            "workflowId": "wf_running_step",
            "status": "running",
            "steps": [
                {"nodeId": "step1", "status": "running"},
            ],
        }
        state_file = tmp_path / "workflow-state.json"
        state_file.write_text(json.dumps(state))
        
        wf = WorkflowState.from_file(state_file)
        
        assert wf.is_terminal is False
        from finalize_workflow import incomplete_steps
        running = incomplete_steps(wf)
        assert len(running) == 1
        assert running[0].node_id == "step1"


class TestCheckExecution:
    """Test check command execution."""

    def test_successful_command(self, tmp_path: Path):
        """Successful command returns PASS."""
        config = CheckConfig(
            name="test_pass",
            command=["python3", "-c", "print('hello')"],
            timeout_seconds=10,
            enabled=True,
        )
        
        result = run_check(config, str(tmp_path))
        
        assert result.outcome == ScoreOutcome.PASS
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_failing_command(self, tmp_path: Path):
        """Failing command returns FAIL."""
        config = CheckConfig(
            name="test_fail",
            command=["python3", "-c", "import sys; sys.exit(1)"],
            timeout_seconds=10,
            enabled=True,
        )
        
        result = run_check(config, str(tmp_path))
        
        assert result.outcome == ScoreOutcome.FAIL
        assert result.exit_code == 1

    def test_timeout_command(self, tmp_path: Path):
        """Command that exceeds timeout returns TIMEOUT."""
        config = CheckConfig(
            name="test_timeout",
            command=["python3", "-c", "import time; time.sleep(10)"],
            timeout_seconds=1,  # Very short timeout
            enabled=True,
        )
        
        result = run_check(config, str(tmp_path))
        
        assert result.outcome == ScoreOutcome.TIMEOUT
        assert "TIMEOUT" in result.stderr

    def test_command_not_found(self, tmp_path: Path):
        """Non-existent command returns ERROR."""
        config = CheckConfig(
            name="test_notfound",
            command=["nonexistent_command_xyz123"],
            timeout_seconds=10,
            enabled=True,
        )
        
        result = run_check(config, str(tmp_path))
        
        assert result.outcome == ScoreOutcome.ERROR
        assert "not found" in result.stderr.lower()

    def test_disabled_command(self, tmp_path: Path):
        """Disabled command returns SKIPPED."""
        config = CheckConfig(
            name="test_disabled",
            command=["echo", "should not run"],
            timeout_seconds=10,
            enabled=False,
        )
        
        result = run_check(config, str(tmp_path))
        
        assert result.outcome == ScoreOutcome.SKIPPED


class TestCheckConfigLoading:
    """Test loading check configurations."""

    def test_load_valid_config(self, tmp_path: Path):
        """Load valid configuration file."""
        config = {
            "build_pass": {
                "command": ["python3", "-m", "py_compile", "test.py"],
                "timeout_seconds": 30,
                "enabled": True,
            },
            "test_pass": {
                "command": ["python3", "-m", "pytest"],
                "timeout_seconds": 120,
                "enabled": True,
            },
            "lint_clean": {
                "command": ["python3", "-m", "ruff", "check", "."],
                "enabled": False,
            },
        }
        config_dir = tmp_path / ".kiro"
        config_dir.mkdir()
        config_file = config_dir / "objective-checks.json"
        config_file.write_text(json.dumps(config))
        
        configs = load_check_configs(str(tmp_path))
        
        assert "build_pass" in configs
        assert configs["build_pass"].command == ["python3", "-m", "py_compile", "test.py"]
        assert configs["build_pass"].timeout_seconds == 30
        
        assert "test_pass" in configs
        assert configs["test_pass"].enabled is True
        
        assert "lint_clean" in configs
        assert configs["lint_clean"].enabled is False

    def test_load_string_command(self, tmp_path: Path):
        """String command should be split into array."""
        config = {
            "build_pass": {
                "command": "python3 -m py_compile test.py",
                "enabled": True,
            },
        }
        config_dir = tmp_path / ".kiro"
        config_dir.mkdir()
        config_file = config_dir / "objective-checks.json"
        config_file.write_text(json.dumps(config))
        
        configs = load_check_configs(str(tmp_path))
        
        assert configs["build_pass"].command == ["python3", "-m", "py_compile", "test.py"]

    def test_missing_config_file(self, tmp_path: Path):
        """Missing config file returns empty dict."""
        configs = load_check_configs(str(tmp_path))
        assert configs == {}


class TestIdempotency:
    """Test duplicate/retry handling."""

    def test_already_scored_workflow_tracked(self, tmp_path: Path):
        """Finalization state tracks scored workflows."""
        from finalize_workflow import load_finalization_state, save_finalization_state
        
        workspace = str(tmp_path)
        
        # Initially empty
        state = load_finalization_state(workspace)
        assert state.get("scored_workflows", {}) == {}
        
        # Save scored workflow
        state["scored_workflows"] = {
            "wf_test123": {
                "scored_at": "2026-11-06T12:00:00Z",
                "outcomes": {"build_pass": "pass", "test_pass": "pass"},
            }
        }
        save_finalization_state(workspace, state)
        
        # Reload and verify
        state2 = load_finalization_state(workspace)
        assert "wf_test123" in state2["scored_workflows"]


class TestOutputRedaction:
    """Test sensitive output redaction."""

    def test_redact_api_keys(self):
        """API keys should be redacted."""
        output = "Using key sk-abc123def456ghi789jkl"
        redacted = redact_output(output)
        assert "sk-abc123def456ghi789jkl" not in redacted
        assert "[REDACTED" in redacted

    def test_redact_bearer_token(self):
        """Bearer tokens should be redacted."""
        output = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        redacted = redact_output(output)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "[REDACTED" in redacted

    def test_preserve_normal_output(self):
        """Normal output should be preserved."""
        output = "Build succeeded in 5 seconds"
        redacted = redact_output(output)
        assert redacted == output

    def test_truncate_long_output(self):
        """Long output should be truncated."""
        output = "x" * 20000
        redacted = redact_output(output, max_len=1000)
        assert len(redacted) < 1500
        assert "truncated" in redacted


class TestScoreValueConversion:
    """Test outcome to score value conversion."""

    def test_pass_is_one(self):
        assert result_to_score_value(ScoreOutcome.PASS) == 1.0

    def test_fail_is_zero(self):
        assert result_to_score_value(ScoreOutcome.FAIL) == 0.0

    def test_not_configured_is_none(self):
        assert result_to_score_value(ScoreOutcome.NOT_CONFIGURED) is None

    def test_skipped_is_none(self):
        assert result_to_score_value(ScoreOutcome.SKIPPED) is None

    def test_timeout_is_none(self):
        assert result_to_score_value(ScoreOutcome.TIMEOUT) is None

    def test_error_is_none(self):
        assert result_to_score_value(ScoreOutcome.ERROR) is None


class TestCLIEntrypoint:
    """Test finalize_workflow.py CLI."""

    def test_missing_workflow_id_and_latest(self, tmp_path: Path):
        """Should error without --workflow-id or --latest."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / ".kiro" / "hooks" / "finalize_workflow.py"),
             "--workspace", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        
        assert result.returncode != 0
        assert "workflow-id" in result.stderr.lower() or "latest" in result.stderr.lower()

    def test_workspace_not_found(self, tmp_path: Path):
        """Should error when workspace doesn't exist."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / ".kiro" / "hooks" / "finalize_workflow.py"),
             "--workspace", str(tmp_path / "nonexistent"),
             "--workflow-id", "wf_test"],
            capture_output=True,
            text=True,
        )
        
        assert result.returncode != 0
        assert "not found" in result.stderr.lower()

    def test_dry_run_does_not_submit_scores(self, tmp_path: Path):
        """Dry run should not submit scores."""
        # Create minimal workflow state
        kiro_sessions = Path.home() / ".kiro" / "sessions"
        ws_hash = workspace_hash(str(tmp_path))
        wf_dir = kiro_sessions / ws_hash / "workflows" / "wf_dryrun"
        wf_dir.mkdir(parents=True, exist_ok=True)
        
        state = {
            "workflowId": "wf_dryrun",
            "workflowName": "test",
            "status": "completed",
            "steps": [{"nodeId": "step1", "status": "completed", "completionSignal": "success"}],
        }
        (wf_dir / "workflow-state.json").write_text(json.dumps(state))
        
        # Create check config that passes
        config_dir = tmp_path / ".kiro"
        config_dir.mkdir()
        config = {
            "build_pass": {"command": ["python3", "-c", "pass"], "enabled": True},
            "test_pass": {"command": ["python3", "-c", "pass"], "enabled": True},
            "lint_clean": {"enabled": False},
        }
        (config_dir / "objective-checks.json").write_text(json.dumps(config))
        
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / ".kiro" / "hooks" / "finalize_workflow.py"),
             "--workspace", str(tmp_path),
             "--workflow-id", "wf_dryrun",
             "--dry-run",
             "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""},
        )
        
        # Dry run should succeed (exit 0)
        assert result.returncode == 0
        
        output = json.loads(result.stdout)
        assert output["checks"]["build_pass"]["outcome"] == "pass"
        assert output["scores_submitted"] == []  # No scores in dry run
        assert output.get("trace_id") in (None, "")
        # Must never default to the workflow id
        assert output.get("trace_id") != "wf_dryrun"


class TestUnknownStepStatus:
    """Unknown child statuses are incomplete, even if root says completed."""

    def test_unknown_child_status_is_incomplete(self, tmp_path: Path):
        # SYNTHETIC fixture (not a captured native payload)
        state = {
            "workflowId": "wf_unknown",
            "workflowName": "two-step-demo",
            "root": {
                "status": "completed",
                "children": [
                    {"nodeId": "analyze", "sessionId": "s1", "status": "completed"},
                    {"nodeId": "summarize", "sessionId": "s2", "status": "mystery"},
                ],
            },
        }
        p = tmp_path / "workflow-state.json"
        p.write_text(json.dumps(state))
        wf = WorkflowState.from_file(p)
        unfinished = incomplete_steps(wf)
        assert len(unfinished) == 1
        assert unfinished[0].node_id == "summarize"


class TestNativeDuration:
    def test_wall_ms_from_root_timestamps(self, tmp_path: Path):
        # SYNTHETIC fixture
        state = {
            "workflowId": "wf_dur",
            "root": {
                "status": "completed",
                "startedAt": "2026-11-06T16:07:40.409Z",
                "endedAt": "2026-11-06T16:08:06.457Z",
                "children": [{"nodeId": "a", "sessionId": "s", "status": "completed"}],
            },
        }
        p = tmp_path / "workflow-state.json"
        p.write_text(json.dumps(state))
        wf = WorkflowState.from_file(p)
        wall = native_wall_ms(wf)
        assert wall is not None
        assert 25000 <= wall <= 27000  # ~26s native wall, not hung parent 133s


class TestTraceIdDiscovery:
    """Scores attach to ingested 32-hex step traces, never wf_*."""

    def _wf(self, tmp_path: Path) -> WorkflowState:
        # SYNTHETIC fixture modeled on native root.children
        state = {
            "workflowId": "wf_7482ba1888446d73",
            "parentSessionId": "sess_parent",
            "workspacePath": str(tmp_path),
            "root": {
                "status": "completed",
                "startedAt": "2026-11-06T16:07:40.409Z",
                "endedAt": "2026-11-06T16:08:06.457Z",
                "children": [
                    {
                        "nodeId": "analyze",
                        "sessionId": "sess_analyze",
                        "status": "completed",
                    },
                    {
                        "nodeId": "summarize",
                        "sessionId": "sess_summarize",
                        "status": "completed",
                        "completionSignal": "success",
                    },
                ],
            },
        }
        p = tmp_path / "workflow-state.json"
        p.write_text(json.dumps(state))
        return WorkflowState.from_file(p)

    def _write_session_state(self, tmp_path: Path, session_id: str, trace_id: str) -> None:
        d = tmp_path / ".kiro" / "langfuse-state-local"
        d.mkdir(parents=True)
        (d / f"{session_id}.json").write_text(json.dumps({
            "session_id": session_id,
            "trace_id": trace_id,
        }))

    def test_wf_id_is_not_a_valid_trace(self):
        assert is_valid_otel_trace_id("wf_7482ba1888446d73") is False
        assert is_valid_otel_trace_id("189269f11c8175150f4e8f017cf0849a") is True

    def test_prefers_last_completed_step_not_parent(self, tmp_path: Path):
        wf = self._wf(tmp_path)
        state_dir = tmp_path / ".kiro" / "langfuse-state-local"
        state_dir.mkdir(parents=True)
        (state_dir / "sess_analyze.json").write_text(json.dumps({
            "session_id": "sess_analyze",
            "trace_id": "189269f11c8175150f4e8f017cf0849a",
        }))
        (state_dir / "sess_summarize.json").write_text(json.dumps({
            "session_id": "sess_summarize",
            "trace_id": "8bf382ae53767f200a24212647e28d3d",
        }))
        (state_dir / "sess_parent.json").write_text(json.dumps({
            "session_id": "sess_parent",
            "trace_id": "b5ff64627d58a8d18bb15f2b0675a1ff",
        }))
        tid, source = discover_trace_id_for_score(str(tmp_path), wf.workflow_id, wf)
        assert tid == "8bf382ae53767f200a24212647e28d3d"
        assert "summarize" in source
        assert tid != wf.workflow_id
        assert tid != "b5ff64627d58a8d18bb15f2b0675a1ff"

    def test_refuses_missing_state(self, tmp_path: Path):
        wf = self._wf(tmp_path)
        tid, source = discover_trace_id_for_score(str(tmp_path), wf.workflow_id, wf)
        assert tid is None
        assert "wf_" not in (tid or "")
        assert "No ingested" in source or "No langfuse-state" in source or "step" in source.lower()

    def test_explicit_wf_id_rejected(self, tmp_path: Path):
        wf = self._wf(tmp_path)
        tid, source = discover_trace_id_for_score(
            str(tmp_path), wf.workflow_id, wf, explicit_trace_id="wf_7482ba1888446d73"
        )
        assert tid is None
        assert "32-hex" in source or "Invalid" in source

    def test_explicit_unassociated_hex_rejected(self, tmp_path: Path):
        wf = self._wf(tmp_path)
        self._write_session_state(tmp_path, "sess_summarize", "8bf382ae53767f200a24212647e28d3d")
        tid, source = discover_trace_id_for_score(
            str(tmp_path), wf.workflow_id, wf,
            explicit_trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        assert tid is None
        assert "not associated" in source

    def test_explicit_parent_trace_rejected(self, tmp_path: Path):
        wf = self._wf(tmp_path)
        state_dir = tmp_path / ".kiro" / "langfuse-state-local"
        state_dir.mkdir(parents=True)
        (state_dir / "sess_parent.json").write_text(json.dumps({
            "session_id": "sess_parent",
            "trace_id": "b5ff64627d58a8d18bb15f2b0675a1ff",
        }))
        (state_dir / "sess_summarize.json").write_text(json.dumps({
            "session_id": "sess_summarize",
            "trace_id": "8bf382ae53767f200a24212647e28d3d",
        }))
        tid, source = discover_trace_id_for_score(
            str(tmp_path), wf.workflow_id, wf,
            explicit_trace_id="b5ff64627d58a8d18bb15f2b0675a1ff",
        )
        assert tid is None
        assert "not associated" in source or "parent" in source.lower()
