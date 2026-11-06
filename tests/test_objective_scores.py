#!/usr/bin/env python3
"""Tests for Task3 objective scores (build/test/lint pass status)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

HOOKS = Path(__file__).resolve().parents[1] / ".kiro" / "hooks"
sys.path.insert(0, str(HOOKS))

from objective_scores import (
    CheckCommand,
    CheckResult,
    ObjectiveConfig,
    ScoreOutcome,
    ScoreState,
    run_check,
    run_all_checks,
    result_to_langfuse_score,
    results_to_boolean_scores,
    redact_output,
    generate_example_config,
    score_key,
)


class TestScoreOutcome(unittest.TestCase):
    """Test score outcome enum values."""
    
    def test_all_outcomes_defined(self):
        outcomes = [o.value for o in ScoreOutcome]
        self.assertIn("pass", outcomes)
        self.assertIn("fail", outcomes)
        self.assertIn("not_configured", outcomes)
        self.assertIn("skipped", outcomes)
        self.assertIn("timeout", outcomes)
        self.assertIn("error", outcomes)


class TestCheckCommand(unittest.TestCase):
    """Test check command parsing."""
    
    def test_simple_command(self):
        cmd = CheckCommand(
            name="test",
            command=["python", "-m", "pytest"],
            timeout=60,
        )
        self.assertEqual(cmd.name, "test")
        self.assertEqual(cmd.command, ["python", "-m", "pytest"])
        self.assertTrue(cmd.enabled)
    
    def test_disabled_command(self):
        cmd = CheckCommand(
            name="lint",
            command=["ruff", "check"],
            timeout=30,
            enabled=False,
        )
        self.assertFalse(cmd.enabled)


class TestObjectiveConfig(unittest.TestCase):
    """Test configuration loading."""
    
    def test_load_missing_config(self):
        with tempfile.TemporaryDirectory() as td:
            config = ObjectiveConfig.load(td)
            self.assertIsNone(config.build)
            self.assertIsNone(config.test)
            self.assertIsNone(config.lint)
            self.assertFalse(config.has_any_configured())
    
    def test_load_string_commands(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / ".kiro" / "objective-checks.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({
                "build": "python -m py_compile main.py",
                "test": "pytest tests/",
                "lint": "ruff check .",
            }))
            
            config = ObjectiveConfig.load(td)
            self.assertIsNotNone(config.build)
            self.assertEqual(config.build.command, ["python", "-m", "py_compile", "main.py"])
            self.assertTrue(config.has_any_configured())
    
    def test_load_dict_commands(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / ".kiro" / "objective-checks.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({
                "build": {
                    "command": "make build",
                    "timeout": 180,
                    "enabled": True,
                },
                "test": {
                    "command": ["pytest", "-v"],
                    "timeout": 600,
                },
                "lint": {
                    "enabled": False,
                    "command": "ruff",
                },
            }))
            
            config = ObjectiveConfig.load(td)
            self.assertEqual(config.build.timeout, 180)
            self.assertEqual(config.test.command, ["pytest", "-v"])
            self.assertFalse(config.lint.enabled)
    
    def test_load_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / ".kiro" / "objective-checks.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("not valid json {")
            
            config = ObjectiveConfig.load(td)
            self.assertIsNone(config.build)
    
    def test_load_none_cwd(self):
        config = ObjectiveConfig.load(None)
        self.assertFalse(config.has_any_configured())


class TestRunCheck(unittest.TestCase):
    """Test running individual checks."""
    
    def test_successful_command(self):
        cmd = CheckCommand(
            name="test",
            command=["python3", "-c", "print('hello')"],
            timeout=10,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertEqual(result.outcome, ScoreOutcome.PASS)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello", result.stdout)
    
    def test_failing_command(self):
        cmd = CheckCommand(
            name="test",
            command=["python3", "-c", "import sys; sys.exit(1)"],
            timeout=10,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertEqual(result.outcome, ScoreOutcome.FAIL)
            self.assertEqual(result.exit_code, 1)
    
    def test_timeout_command(self):
        cmd = CheckCommand(
            name="test",
            command=["python3", "-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertEqual(result.outcome, ScoreOutcome.TIMEOUT)
            self.assertIn("timed out", result.error_message.lower())
    
    def test_command_not_found(self):
        cmd = CheckCommand(
            name="test",
            command=["nonexistent_command_xyz_123"],
            timeout=10,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertEqual(result.outcome, ScoreOutcome.ERROR)
            self.assertIn("not found", result.error_message.lower())
    
    def test_disabled_command(self):
        cmd = CheckCommand(
            name="test",
            command=["echo", "should not run"],
            timeout=10,
            enabled=False,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertEqual(result.outcome, ScoreOutcome.SKIPPED)
    
    def test_missing_working_dir(self):
        cmd = CheckCommand(
            name="test",
            command=["echo", "test"],
            timeout=10,
            working_dir="/nonexistent/path/xyz",
        )
        result = run_check(cmd, None)
        self.assertEqual(result.outcome, ScoreOutcome.ERROR)
    
    def test_duration_captured(self):
        cmd = CheckCommand(
            name="test",
            command=["python3", "-c", "import time; time.sleep(0.1)"],
            timeout=10,
        )
        with tempfile.TemporaryDirectory() as td:
            result = run_check(cmd, td)
            self.assertIsNotNone(result.duration_ms)
            self.assertGreater(result.duration_ms, 50)


class TestRunAllChecks(unittest.TestCase):
    """Test running all checks."""
    
    def test_all_not_configured(self):
        config = ObjectiveConfig()
        results = run_all_checks(config, "/tmp")
        
        for name in ["build_pass", "test_pass", "lint_clean"]:
            self.assertIn(name, results)
            self.assertEqual(results[name].outcome, ScoreOutcome.NOT_CONFIGURED)
    
    def test_mixed_results(self):
        with tempfile.TemporaryDirectory() as td:
            config = ObjectiveConfig(
                build=CheckCommand("build", ["python3", "-c", "pass"], 10),
                test=CheckCommand("test", ["python3", "-c", "import sys; sys.exit(1)"], 10),
                lint=None,
            )
            results = run_all_checks(config, td)
            
            self.assertEqual(results["build_pass"].outcome, ScoreOutcome.PASS)
            self.assertEqual(results["test_pass"].outcome, ScoreOutcome.FAIL)
            self.assertEqual(results["lint_clean"].outcome, ScoreOutcome.NOT_CONFIGURED)


class TestRedactOutput(unittest.TestCase):
    """Test output redaction."""
    
    def test_truncate_long_output(self):
        output = "x" * 10000
        redacted = redact_output(output, max_bytes=100)
        self.assertLess(len(redacted), 200)
        self.assertIn("[truncated]", redacted)
    
    def test_redact_secrets(self):
        output = "key: sk-lf-abc123secret password=mysecret token=xyz"
        redacted = redact_output(output)
        self.assertNotIn("abc123secret", redacted)
        self.assertNotIn("mysecret", redacted)
        self.assertIn("[REDACTED]", redacted)
    
    def test_preserve_normal_output(self):
        output = "Build successful\nTests passed: 42"
        redacted = redact_output(output)
        self.assertEqual(output, redacted)


class TestResultToScore(unittest.TestCase):
    """Test converting results to Langfuse scores."""
    
    def test_pass_score(self):
        result = CheckResult(
            name="build pass",
            outcome=ScoreOutcome.PASS,
            exit_code=0,
            duration_ms=1234,
        )
        score = result_to_langfuse_score(result, "trace123", "wf_run_456")
        
        self.assertEqual(score["traceId"], "trace123")
        self.assertEqual(score["name"], "build_pass")
        self.assertEqual(score["value"], "pass")
        self.assertEqual(score["dataType"], "CATEGORICAL")
        self.assertEqual(score["metadata"]["workflow_run_id"], "wf_run_456")
        self.assertEqual(score["metadata"]["exit_code"], 0)
        self.assertEqual(score["metadata"]["duration_ms"], 1234)
    
    def test_fail_score(self):
        result = CheckResult(
            name="test pass",
            outcome=ScoreOutcome.FAIL,
            exit_code=1,
            stderr="Error: test failed",
        )
        score = result_to_langfuse_score(result, "trace123")
        
        self.assertEqual(score["name"], "test_pass")
        self.assertEqual(score["value"], "fail")
        self.assertIn("stderr_preview", score["metadata"])
    
    def test_not_configured_score(self):
        result = CheckResult(
            name="lint clean",
            outcome=ScoreOutcome.NOT_CONFIGURED,
        )
        score = result_to_langfuse_score(result, "trace123")
        
        self.assertEqual(score["value"], "not_configured")
    
    def test_error_score(self):
        result = CheckResult(
            name="build pass",
            outcome=ScoreOutcome.ERROR,
            error_message="Command not found",
        )
        score = result_to_langfuse_score(result, "trace123")
        
        self.assertEqual(score["value"], "error")
        self.assertIn("error_message", score["metadata"])


class TestBooleanScores(unittest.TestCase):
    """Test boolean score generation."""
    
    def test_pass_fail_only(self):
        results = {
            "build_pass": CheckResult("build", ScoreOutcome.PASS, exit_code=0),
            "test_pass": CheckResult("test", ScoreOutcome.FAIL, exit_code=1),
            "lint_clean": CheckResult("lint", ScoreOutcome.NOT_CONFIGURED),
        }
        scores = results_to_boolean_scores(results, "trace123")
        
        # Only pass/fail should be included
        self.assertEqual(len(scores), 2)
        
        build_score = next(s for s in scores if s["name"] == "build_pass_boolean")
        self.assertEqual(build_score["value"], 1.0)
        self.assertEqual(build_score["dataType"], "BOOLEAN")
        
        test_score = next(s for s in scores if s["name"] == "test_pass_boolean")
        self.assertEqual(test_score["value"], 0.0)
    
    def test_no_definitive_results(self):
        results = {
            "build_pass": CheckResult("build", ScoreOutcome.NOT_CONFIGURED),
            "test_pass": CheckResult("test", ScoreOutcome.SKIPPED),
            "lint_clean": CheckResult("lint", ScoreOutcome.TIMEOUT),
        }
        scores = results_to_boolean_scores(results, "trace123")
        self.assertEqual(len(scores), 0)


class TestScoreState(unittest.TestCase):
    """Test score deduplication state."""
    
    def test_load_missing_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = ScoreState.load(Path(td))
            self.assertEqual(state.scored_runs, {})
    
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            state = ScoreState()
            state.mark_scored("wf_123", ["build_pass", "test_pass"])
            state.save(Path(td))
            
            loaded = ScoreState.load(Path(td))
            self.assertTrue(loaded.is_scored("wf_123"))
            self.assertFalse(loaded.is_scored("wf_456"))
    
    def test_deduplication(self):
        state = ScoreState()
        self.assertFalse(state.is_scored("wf_abc"))
        state.mark_scored("wf_abc", ["build_pass"])
        self.assertTrue(state.is_scored("wf_abc"))


class TestScoreKey(unittest.TestCase):
    """Test score key generation."""
    
    def test_deterministic(self):
        key1 = score_key("wf_123", "build_pass")
        key2 = score_key("wf_123", "build_pass")
        self.assertEqual(key1, key2)
    
    def test_different_inputs(self):
        key1 = score_key("wf_123", "build_pass")
        key2 = score_key("wf_123", "test_pass")
        key3 = score_key("wf_456", "build_pass")
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)


class TestGenerateConfig(unittest.TestCase):
    """Test example config generation."""
    
    def test_valid_json(self):
        config = generate_example_config()
        parsed = json.loads(config)
        self.assertIn("build", parsed)
        self.assertIn("test", parsed)
        self.assertIn("lint", parsed)


if __name__ == "__main__":
    unittest.main()
