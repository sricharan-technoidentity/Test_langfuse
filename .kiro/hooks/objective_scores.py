"""Task3: Objective end-of-workflow scores for build/test/lint pass status.

Records scores correlated to the true workflow run trace, idempotent across
hooks/retries/steps, no per-step premature duplicates.

Design:
- Scores recorded only at end of WORKFLOW run (not per-step)
- Outcome from actual configured commands with captured exit status
- Distinguishes: pass / fail / not-configured / skipped / timeout / error
- Safe project-local check commands with bounded timeouts
- Bounded/redacted output capture
- No commands inferred from LLM text/payload
- Does NOT run automatically on workspace open
- Idempotent via claim-based deduplication
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# Score outcome categories
class ScoreOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_CONFIGURED = "not_configured"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    ERROR = "error"

# Score data types for Langfuse
SCORE_DATA_TYPE_CATEGORICAL = "CATEGORICAL"
SCORE_DATA_TYPE_BOOLEAN = "BOOLEAN"

# Default timeouts (seconds)
DEFAULT_BUILD_TIMEOUT = 120
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_LINT_TIMEOUT = 60

# Max output to capture (bytes)
MAX_OUTPUT_BYTES = 4096

# Config file location
OBJECTIVE_CONFIG_FILE = ".kiro/objective-checks.json"


@dataclass
class CheckCommand:
    """A configured check command with timeout."""
    name: str
    command: list[str]
    timeout: int
    enabled: bool = True
    working_dir: str | None = None


@dataclass
class CheckResult:
    """Result of running a check command."""
    name: str
    outcome: ScoreOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    error_message: str | None = None
    command_run: list[str] | None = None


@dataclass
class ObjectiveConfig:
    """Configuration for objective checks."""
    build: CheckCommand | None = None
    test: CheckCommand | None = None
    lint: CheckCommand | None = None

    @classmethod
    def load(cls, cwd: str | None) -> "ObjectiveConfig":
        """Load config from workspace, fail gracefully."""
        if not cwd:
            return cls()
        config_path = Path(cwd) / OBJECTIVE_CONFIG_FILE
        if not config_path.is_file():
            return cls()
        try:
            data = json.loads(config_path.read_text())
            if not isinstance(data, dict):
                return cls()
            return cls(
                build=cls._parse_check(data.get("build"), "build", DEFAULT_BUILD_TIMEOUT),
                test=cls._parse_check(data.get("test"), "test", DEFAULT_TEST_TIMEOUT),
                lint=cls._parse_check(data.get("lint"), "lint", DEFAULT_LINT_TIMEOUT),
            )
        except (OSError, json.JSONDecodeError):
            return cls()

    @staticmethod
    def _parse_check(data: Any, name: str, default_timeout: int) -> CheckCommand | None:
        """Parse a check command from config data."""
        if not data:
            return None
        if isinstance(data, str):
            # Simple string command
            return CheckCommand(
                name=name,
                command=shlex.split(data),
                timeout=default_timeout,
                enabled=True,
            )
        if isinstance(data, dict):
            cmd = data.get("command")
            if not cmd:
                return None
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
            if not isinstance(cmd, list):
                return None
            return CheckCommand(
                name=name,
                command=cmd,
                timeout=int(data.get("timeout", default_timeout)),
                enabled=bool(data.get("enabled", True)),
                working_dir=data.get("working_dir"),
            )
        return None

    def has_any_configured(self) -> bool:
        """Check if any checks are configured and enabled."""
        return any([
            self.build and self.build.enabled,
            self.test and self.test.enabled,
            self.lint and self.lint.enabled,
        ])


def redact_output(output: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Truncate and redact sensitive patterns from command output."""
    if len(output.encode()) > max_bytes:
        output = output[:max_bytes] + "\n... [truncated]"
    # Redact common secret patterns
    import re
    patterns = [
        (r'(sk-lf-)[A-Za-z0-9]+', r'\1[REDACTED]'),
        (r'(pk-lf-)[A-Za-z0-9]+', r'\1[REDACTED]'),
        (r'(password[=:])\s*\S+', r'\1[REDACTED]'),
        (r'(secret[=:])\s*\S+', r'\1[REDACTED]'),
        (r'(token[=:])\s*\S+', r'\1[REDACTED]'),
    ]
    for pattern, replacement in patterns:
        output = re.sub(pattern, replacement, output, flags=re.IGNORECASE)
    return output


def run_check(check: CheckCommand, cwd: str | None) -> CheckResult:
    """Run a single check command safely with timeout."""
    if not check.enabled:
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.SKIPPED,
        )

    working_dir = check.working_dir or cwd
    if not working_dir or not Path(working_dir).is_dir():
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.ERROR,
            error_message=f"Working directory not found: {working_dir}",
        )

    # Validate command is not empty
    if not check.command:
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.ERROR,
            error_message="Empty command",
        )

    start_time = time.time()
    try:
        proc = subprocess.run(
            check.command,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=check.timeout,
            env={**os.environ, "CI": "true"},
        )
        duration_ms = int((time.time() - start_time) * 1000)

        outcome = ScoreOutcome.PASS if proc.returncode == 0 else ScoreOutcome.FAIL

        return CheckResult(
            name=check.name,
            outcome=outcome,
            exit_code=proc.returncode,
            stdout=redact_output(proc.stdout or ""),
            stderr=redact_output(proc.stderr or ""),
            duration_ms=duration_ms,
            command_run=check.command,
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.time() - start_time) * 1000)
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.TIMEOUT,
            duration_ms=duration_ms,
            error_message=f"Command timed out after {check.timeout}s",
            stdout=redact_output(e.stdout or "") if e.stdout else "",
            stderr=redact_output(e.stderr or "") if e.stderr else "",
            command_run=check.command,
        )
    except FileNotFoundError:
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.ERROR,
            error_message=f"Command not found: {check.command[0]}",
            command_run=check.command,
        )
    except PermissionError:
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.ERROR,
            error_message=f"Permission denied: {check.command[0]}",
            command_run=check.command,
        )
    except Exception as e:
        return CheckResult(
            name=check.name,
            outcome=ScoreOutcome.ERROR,
            error_message=f"{type(e).__name__}: {str(e)[:200]}",
            command_run=check.command,
        )


def run_all_checks(config: ObjectiveConfig, cwd: str | None) -> dict[str, CheckResult]:
    """Run all configured checks and return results."""
    results = {}

    for check_name, check in [("build_pass", config.build),
                               ("test_pass", config.test),
                               ("lint_clean", config.lint)]:
        if check is None:
            results[check_name] = CheckResult(
                name=check_name.replace("_", " "),
                outcome=ScoreOutcome.NOT_CONFIGURED,
            )
        else:
            results[check_name] = run_check(check, cwd)

    return results


def score_key(workflow_run_id: str, score_name: str) -> str:
    """Generate a unique key for deduplication."""
    return hashlib.sha256(f"{workflow_run_id}:{score_name}".encode()).hexdigest()[:24]


def result_to_langfuse_score(
    result: CheckResult,
    trace_id: str,
    workflow_run_id: str | None = None,
) -> dict:
    """Convert a CheckResult to a Langfuse score payload.
    
    Returns a score with:
    - name: build_pass, test_pass, or lint_clean
    - value: categorical outcome string
    - dataType: CATEGORICAL
    - metadata with full details
    """
    score_name = result.name.replace(" ", "_").lower()
    if score_name not in ("build_pass", "test_pass", "lint_clean"):
        score_name = f"{score_name}_outcome"

    metadata = {
        "outcome": result.outcome.value,
        "task3_version": "1.0.0",
    }
    if result.exit_code is not None:
        metadata["exit_code"] = result.exit_code
    if result.duration_ms is not None:
        metadata["duration_ms"] = result.duration_ms
    if result.error_message:
        metadata["error_message"] = result.error_message[:500]
    if result.command_run:
        metadata["command"] = " ".join(result.command_run)[:200]
    if workflow_run_id:
        metadata["workflow_run_id"] = workflow_run_id

    # Include truncated output for debugging
    if result.stdout:
        metadata["stdout_preview"] = result.stdout[:500]
    if result.stderr:
        metadata["stderr_preview"] = result.stderr[:500]

    return {
        "traceId": trace_id,
        "name": score_name,
        "value": result.outcome.value,
        "dataType": SCORE_DATA_TYPE_CATEGORICAL,
        "comment": f"Task3 objective check: {result.outcome.value}",
        "metadata": metadata,
    }


def results_to_boolean_scores(
    results: dict[str, CheckResult],
    trace_id: str,
    workflow_run_id: str | None = None,
) -> list[dict]:
    """Convert results to boolean scores (pass=1, fail=0, others=excluded).
    
    Only includes scores where we have a definitive pass/fail.
    """
    scores = []
    for name, result in results.items():
        if result.outcome not in (ScoreOutcome.PASS, ScoreOutcome.FAIL):
            continue

        boolean_value = 1.0 if result.outcome == ScoreOutcome.PASS else 0.0

        metadata = {
            "outcome_detail": result.outcome.value,
            "task3_version": "1.0.0",
        }
        if result.exit_code is not None:
            metadata["exit_code"] = result.exit_code
        if result.duration_ms is not None:
            metadata["duration_ms"] = result.duration_ms
        if workflow_run_id:
            metadata["workflow_run_id"] = workflow_run_id

        scores.append({
            "traceId": trace_id,
            "name": f"{name}_boolean",
            "value": boolean_value,
            "dataType": SCORE_DATA_TYPE_BOOLEAN,
            "comment": f"Task3: {'PASS' if boolean_value else 'FAIL'}",
            "metadata": metadata,
        })

    return scores


@dataclass
class ScoreState:
    """Persistent state for score deduplication."""
    scored_runs: dict[str, dict] = field(default_factory=dict)  # run_id -> {scores, timestamp}

    @classmethod
    def load(cls, state_dir: Path) -> "ScoreState":
        """Load state from file."""
        path = state_dir / "objective_scores_state.json"
        try:
            data = json.loads(path.read_text())
            return cls(scored_runs=data.get("scored_runs", {}))
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self, state_dir: Path) -> None:
        """Save state to file."""
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "objective_scores_state.json"
        # Keep only recent entries (last 100)
        recent = dict(sorted(self.scored_runs.items(), key=lambda x: x[1].get("timestamp", 0))[-100:])
        path.write_text(json.dumps({"scored_runs": recent}, indent=2))

    def is_scored(self, workflow_run_id: str) -> bool:
        """Check if a workflow run has already been scored."""
        return workflow_run_id in self.scored_runs

    def mark_scored(self, workflow_run_id: str, scores: list[str]) -> None:
        """Mark a workflow run as scored."""
        self.scored_runs[workflow_run_id] = {
            "scores": scores,
            "timestamp": time.time(),
        }


def generate_example_config() -> str:
    """Generate example objective-checks.json content."""
    return json.dumps({
        "build": {
            "command": "python -m py_compile your_module.py",
            "timeout": 120,
            "enabled": True,
            "_comment": "Set to your build command, or null/false to disable"
        },
        "test": {
            "command": "python -m pytest tests/ -v --tb=short",
            "timeout": 300,
            "enabled": True,
            "_comment": "Set to your test command, or null/false to disable"
        },
        "lint": {
            "command": "python -m ruff check .",
            "timeout": 60,
            "enabled": True,
            "_comment": "Set to your lint command, or null/false to disable"
        },
        "_documentation": {
            "purpose": "Task3 objective end-of-workflow scores",
            "outcomes": ["pass", "fail", "not_configured", "skipped", "timeout", "error"],
            "note": "Commands are run only at workflow end, never automatically on workspace open"
        }
    }, indent=2)
