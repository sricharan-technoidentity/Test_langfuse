"""Tests for the hook_launcher.py cwd-independent invocation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


class TestHookLauncherDiagnostics:
    """Test that hook_launcher creates diagnostic output."""

    def test_launcher_creates_log_directory(self, tmp_path: Path):
        """Launcher should create state directory and write launcher.log."""
        # Create minimal workspace structure
        kiro_dir = tmp_path / ".kiro"
        hooks_dir = kiro_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        
        # Copy launcher
        launcher_src = Path(__file__).parent.parent / ".kiro" / "hooks" / "hook_launcher.py"
        launcher_dst = hooks_dir / "hook_launcher.py"
        launcher_dst.write_text(launcher_src.read_text())
        
        # Create minimal hook that just passes
        hook_dst = hooks_dir / "langfuse_kiro_hook.py"
        hook_dst.write_text('''
def handle_event(ev):
    pass
''')
        
        # Create env file
        env_file = kiro_dir / "langfuse.local.env"
        env_file.write_text("TRACE_TO_LANGFUSE=false\n")
        
        # Build test event
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test-session-123",
            "cwd": str(tmp_path),
            "prompt": "test prompt",
        }
        
        # Run launcher from a DIFFERENT directory (simulating Kiro behavior)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tf:
            json.dump(event, tf)
            tf_path = tf.name
        
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(launcher_dst)],
                stdin=open(tf_path),
                cwd="/tmp",  # Different from workspace!
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            # Should succeed
            assert result.returncode == 0
            
            # Should create diagnostic log
            state_dir = kiro_dir / "langfuse-state-local"
            launcher_log = state_dir / "launcher.log"
            assert state_dir.exists(), "State directory should be created"
            assert launcher_log.exists(), "Launcher log should be created"
            
            log_content = launcher_log.read_text()
            assert "INVOKED" in log_content
            assert "hook=UserPromptSubmit" in log_content
            assert "session=test-session-123" in log_content
            assert "SUCCESS" in log_content
        finally:
            os.unlink(tf_path)

    def test_launcher_resolves_paths_from_event_cwd(self, tmp_path: Path):
        """Launcher should use cwd from event, not shell cwd."""
        # Create workspace
        workspace = tmp_path / "my-workspace"
        kiro_dir = workspace / ".kiro"
        hooks_dir = kiro_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        
        # Copy launcher
        launcher_src = Path(__file__).parent.parent / ".kiro" / "hooks" / "hook_launcher.py"
        launcher_dst = hooks_dir / "hook_launcher.py"
        launcher_dst.write_text(launcher_src.read_text())
        
        # Create hook that records where it found env
        hook_dst = hooks_dir / "langfuse_kiro_hook.py"
        hook_dst.write_text(f'''
import os
from pathlib import Path
def handle_event(ev):
    marker = Path("{workspace}/.kiro/langfuse-state-local/env_path.txt")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(os.environ.get("KIRO_LF_ENV_FILE", "not-set"))
''')
        
        # Create env file in workspace
        env_file = kiro_dir / "langfuse.local.env"
        env_file.write_text("TRACE_TO_LANGFUSE=false\n")
        
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test-123",
            "cwd": str(workspace),  # Event says workspace is here
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tf:
            json.dump(event, tf)
            tf_path = tf.name
        
        try:
            # Run from a completely different directory
            result = subprocess.run(
                [sys.executable, "-u", str(launcher_dst)],
                stdin=open(tf_path),
                cwd=str(tmp_path),  # NOT the workspace
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            assert result.returncode == 0
            
            # Check that env was resolved to workspace path
            env_marker = workspace / ".kiro" / "langfuse-state-local" / "env_path.txt"
            assert env_marker.exists()
            recorded_env = env_marker.read_text()
            assert str(workspace) in recorded_env
            assert ".kiro/langfuse.local.env" in recorded_env
        finally:
            os.unlink(tf_path)

    def test_launcher_logs_missing_hook_script(self, tmp_path: Path):
        """Launcher should log when hook script doesn't exist."""
        # Create workspace with launcher but NO hook script
        kiro_dir = tmp_path / ".kiro"
        hooks_dir = kiro_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        
        launcher_src = Path(__file__).parent.parent / ".kiro" / "hooks" / "hook_launcher.py"
        launcher_dst = hooks_dir / "hook_launcher.py"
        launcher_dst.write_text(launcher_src.read_text())
        
        # NO langfuse_kiro_hook.py!
        
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test-123",
            "cwd": str(tmp_path),
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tf:
            json.dump(event, tf)
            tf_path = tf.name
        
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(launcher_dst)],
                stdin=open(tf_path),
                cwd="/tmp",
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            # Should still succeed (fail-open)
            assert result.returncode == 0
            
            # But should log the error
            launcher_log = kiro_dir / "langfuse-state-local" / "launcher.log"
            if launcher_log.exists():
                log_content = launcher_log.read_text()
                assert "hook script not found" in log_content or "hook_exists=False" in log_content
        finally:
            os.unlink(tf_path)


class TestHookLauncherFailOpen:
    """Test that launcher never blocks Kiro (fail-open behavior)."""

    def test_invalid_json_returns_zero(self, tmp_path: Path):
        """Launcher should return 0 even with invalid JSON input."""
        launcher_src = Path(__file__).parent.parent / ".kiro" / "hooks" / "hook_launcher.py"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tf:
            tf.write("this is not json")
            tf_path = tf.name
        
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(launcher_src)],
                stdin=open(tf_path),
                cwd="/tmp",
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            # Must return 0 to not block Kiro
            assert result.returncode == 0
            assert "JSON parse failed" in result.stderr
        finally:
            os.unlink(tf_path)

    def test_empty_stdin_returns_zero(self, tmp_path: Path):
        """Launcher should return 0 even with empty stdin."""
        launcher_src = Path(__file__).parent.parent / ".kiro" / "hooks" / "hook_launcher.py"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tf:
            tf.write("")
            tf_path = tf.name
        
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(launcher_src)],
                stdin=open(tf_path),
                cwd="/tmp",
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            assert result.returncode == 0
        finally:
            os.unlink(tf_path)
