"""Smoke the documented non-GUI scripts without secrets or Docker."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], extra_env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    env.pop("LANGFUSE_PUBLIC_KEY", None)
    env.pop("LANGFUSE_SECRET_KEY", None)
    return subprocess.run(
        args,
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        env=env,
        **kwargs,
    )


def test_check_prereqs_runs():
    script = ROOT / "scripts" / "check-prereqs.sh"
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    proc = _run(["bash", str(script)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "python3" in proc.stdout
    assert "platform_tested=Linux" in proc.stdout
    assert "never" in proc.stdout.lower() or "down -v" in proc.stdout


def test_doctor_runs_without_printing_keys():
    proc = _run([sys.executable, str(ROOT / "scripts" / "doctor.py")])
    assert proc.returncode in (0, 1)
    assert "ok  path README.md" in proc.stdout
    combined = proc.stdout + proc.stderr
    assert "sk-lf-" not in combined or "your-secret-key" in combined
    assert "pk-lf-" not in combined or "placeholder" in combined.lower() or "your-public" in combined


def test_print_urls_uses_caller_host_and_project():
    proc = _run([
        sys.executable, str(ROOT / "scripts" / "print-urls.py"),
        "--base-url", "http://127.0.0.1:3000",
        "--project-id", "my-project",
        "--dashboard-id", "dash1",
        "--trace-id", "aa" * 16,
    ])
    assert proc.returncode == 0, proc.stderr
    assert "http://127.0.0.1:3000/project/my-project/dashboards/dash1" in proc.stdout
    assert "/workspace" not in proc.stdout


def test_langfuse_compose_help_does_not_start_docker():
    proc = _run(["bash", str(ROOT / "scripts" / "langfuse-compose.sh"), "help"])
    assert proc.returncode == 0
    assert "down -v" in proc.stdout


def test_langfuse_compose_refuses_unset_dir():
    proc = _run(
        ["bash", str(ROOT / "scripts" / "langfuse-compose.sh"), "up"],
        extra_env={"LANGFUSE_COMPOSE_DIR": ""},
    )
    assert proc.returncode == 2
    assert "refusing to auto-clone" in proc.stdout.lower() or "LANGFUSE_COMPOSE_DIR is unset" in proc.stdout


def test_enable_tracing_refuses_missing_env(tmp_path: Path):
    proc = _run(["bash", str(ROOT / "scripts" / "enable-tracing.sh"), str(tmp_path / "nope.env")])
    assert proc.returncode == 1


def test_enable_tracing_appends_without_clobber(tmp_path: Path):
    envf = tmp_path / "langfuse.local.env"
    envf.write_text("LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key-here\n")
    proc = _run(["bash", str(ROOT / "scripts" / "enable-tracing.sh"), str(envf)])
    assert proc.returncode == 0, proc.stderr
    text = envf.read_text()
    assert "TRACE_TO_LANGFUSE=true" in text
    assert "pk-lf-your-public-key-here" in text
    proc2 = _run(["bash", str(ROOT / "scripts" / "enable-tracing.sh"), str(envf)])
    assert proc2.returncode == 0
    assert text.count("TRACE_TO_LANGFUSE=true") == envf.read_text().count("TRACE_TO_LANGFUSE=true")


def test_discover_workflow_no_credentials():
    proc = _run([sys.executable, str(ROOT / "scripts" / "discover-workflow.py"), "--workspace", str(ROOT), "--json"])
    # 0 if a workflow exists on this machine, 2 if none — both OK without secrets
    assert proc.returncode in (0, 2), proc.stderr
    assert "LANGFUSE" not in proc.stdout
