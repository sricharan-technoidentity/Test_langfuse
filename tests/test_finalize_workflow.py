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
)


class TestWorkflowState:
    """Test workflow state parsing and validation."""

    def test_parse_completed_workflow(self, tmp_path: Path):
        """Parse a completed two-step workflow."""
        state = {
            "workflowId": "wf_test123",
            "workflowName": "two-step-demo",
            "status": "completed",
            "parentSessionId": "sess_parent",
            "workspacePath": str(tmp_path),
            "createdAt": "2026-11-06T15:00:00Z",
            "steps": [
                {"nodeId": "analyze", "status": "completed", "completionSignal": None},
                {"nodeId": "summarize", "status": "completed", "completionSignal": "success"},
            ],
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

    def test_running_workflow_not_terminal(self, tmp_path: Path):
        """Running workflow should not be terminal."""
        state = {
            "workflowId": "wf_running",
            "status": "running",
            "steps": [
                {"nodeId": "step1", "status": "completed"},
                {"nodeId": "step2", "status": "running"},
            ],
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
            "status": "error",
            "steps": [],
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
        
        # Would have pending steps
        pending = [s for s in wf.steps if s.get("status") == "pending"]
        assert len(pending) == 1
        assert pending[0]["nodeId"] == "summarize"

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
        running = [s for s in wf.steps if s.get("status") == "running"]
        assert len(running) == 1


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
