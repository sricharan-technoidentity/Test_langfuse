"""Fail if tracked files look like they contain real secrets or live run defaults."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
PLACEHOLDER_NEEDLES = (
    "your-public-key",
    "your-secret-key",
    "pk-lf-your-",
    "sk-lf-your-",
    "CHANGEME",
    "placeholder",
)

# Historical fixtures / archive may mention old ids; active config must not.
ACTIVE_CONFIG = (
    ROOT / ".kiro" / "tracing.context.example.json",
    ROOT / ".kiro" / "langfuse.local.env.example",
    ROOT / ".kiro" / "langfuse.local.env.template",
    ROOT / ".kiro" / "objective-checks.example.json",
)


def _tracked_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Isolated copy without git: walk
        out = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            out.append(p)
        return out
    return [ROOT / line for line in proc.stdout.splitlines() if line]


def test_no_aws_or_private_pem_in_tracked_files():
    files = _tracked_files()
    # Also walk the worktree so unstaged handoff files are scanned before commit.
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP_PARTS for part in p.parts):
            files.append(p)
    for path in files:
        if path.suffix in {".png", ".jpg", ".pyc"}:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        lower = text.lower()
        # Split markers so this test file does not match itself.
        assert ("begin " + "rsa private key") not in lower, path
        assert ("begin " + "openssh private key") not in lower, path
        assert ("begin " + "ec private key") not in lower, path
        if path.name != "test_secret_scan.py":
            assert "akia" not in lower, path


def test_env_examples_are_placeholders():
    for path in ACTIVE_CONFIG:
        text = path.read_text()
        if "PUBLIC_KEY" in text or "SECRET_KEY" in text:
            assert any(n in text for n in PLACEHOLDER_NEEDLES), path
            assert "pk-lf-your-" in text or "PUBLIC_KEY" not in text


def test_active_examples_have_no_host_run_ids():
    for path in ACTIVE_CONFIG:
        text = path.read_text()
        assert "wf_7482" not in text
        assert "wf_172a" not in text
        assert "8bf382ae" not in text
        assert "cmw56" not in text
