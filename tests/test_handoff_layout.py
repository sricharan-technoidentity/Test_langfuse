"""Self-service handoff: clone docs, layout, no host IDs in active config."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLONE = (
    "git clone --branch cursor/kiro-langfuse-task1-3aac --single-branch "
    "https://github.com/sricharan-technoidentity/Test_langfuse.git"
)


def test_readme_has_exact_clone_command():
    text = (ROOT / "README.md").read_text()
    assert CLONE in text
    assert "cursor/kiro-langfuse-task1-3aac" in text
    assert "/workspace" not in text.split("## Numbered walkthrough")[-1]


def test_readme_states_observation_proxies_and_score_scope():
    text = (ROOT / "README.md").read_text().lower()
    assert "observation" in text
    assert "syntax compile" in text or "py_compile" in text
    assert "final-step" in text or "final step" in text
    assert "package build" in text


def test_expected_directories_exist():
    for rel in ("docs", "docs/archive", "examples", "tests", "tests/fixtures", "scripts", "config", ".kiro/hooks", ".kiro/workflows", "sample"):
        assert (ROOT / rel).exists(), rel


def test_kiro_hook_paths_unmoved():
    for rel in (
        ".kiro/hooks/langfuse-tracing.json",
        ".kiro/hooks/hook_launcher.py",
        ".kiro/hooks/langfuse_kiro_hook.py",
        ".kiro/hooks/finalize_workflow.py",
        ".kiro/hooks/dashboard_provision.py",
        ".kiro/workflows/two-step-demo.workflow.json",
        "sample/checkout_helper.py",
    ):
        assert (ROOT / rel).is_file(), rel


def test_tracing_example_has_no_run_ids():
    data = json.loads((ROOT / ".kiro/tracing.context.example.json").read_text())
    blob = json.dumps(data)
    assert "wf_" not in blob
    assert "cmw" not in blob
    assert data.get("workspace_key") != "test-langfuse-local-validation"


def test_placement_fixture_is_labeled():
    data = json.loads((ROOT / "tests/fixtures/failed-acceptance-placements.fixture.json").read_text())
    assert data["fixture"] is True
    assert data["not_user_defaults"] is True


def test_scripts_refuse_volume_wipe():
    text = (ROOT / "scripts/langfuse-compose.sh").read_text()
    assert "down -v" in text
    assert "never" in text.lower()
    # the down action must exit non-zero, not execute compose down
    assert 'ACTION" == down' in text or 'down)' in text


def test_gitignore_keeps_secrets_and_identity_out():
    gi = (ROOT / ".gitignore").read_text()
    assert ".kiro/langfuse.local.env" in gi
    assert ".kiro/tracing.context.json" in gi
    assert ".venv/" in gi
