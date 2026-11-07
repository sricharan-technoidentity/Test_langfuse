#!/usr/bin/env python3
"""Optional diagnostics. Never prints credential values. Never calls Langfuse write APIs."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ok(msg: str) -> None:
    print(f"ok  {msg}")


def _warn(msg: str) -> None:
    print(f"warn {msg}")


def _fail(msg: str) -> None:
    print(f"fail {msg}")


def env_trace_flag(path: Path) -> str:
    """Return present/true/false/missing without echoing other lines."""
    if not path.is_file():
        return "missing_file"
    true = false = present = False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("TRACE_TO_LANGFUSE="):
            present = True
            val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
            if val == "true":
                true = True
            else:
                false = True
    if true:
        return "true"
    if false:
        return "set_not_true"
    if present:
        return "present"
    return "key_absent"


def main() -> int:
    print(f"repo={ROOT}")
    print("note this script never cats .kiro/langfuse.local.env and never POSTs scores")
    failed = 0

    for rel in (
        "README.md",
        "requirements.txt",
        ".kiro/hooks/langfuse_kiro_hook.py",
        ".kiro/hooks/finalize_workflow.py",
        ".kiro/hooks/dashboard_provision.py",
        ".kiro/hooks/hook_launcher.py",
        ".kiro/workflows/two-step-demo.workflow.json",
        ".kiro/tracing.context.example.json",
        ".kiro/langfuse.local.env.example",
        "sample/checkout_helper.py",
        "tests",
        "scripts",
        "docs",
        "config",
        "examples",
    ):
        p = ROOT / rel
        if p.exists():
            _ok(f"path {rel}")
        else:
            _fail(f"missing {rel}")
            failed += 1

    ctx = ROOT / ".kiro" / "tracing.context.json"
    if ctx.exists():
        _ok("tracing.context.json present (gitignored user file)")
    else:
        _warn("tracing.context.json absent — copy from .kiro/tracing.context.example.json")

    env_path = ROOT / ".kiro" / "langfuse.local.env"
    flag = env_trace_flag(env_path)
    if flag == "missing_file":
        _warn("langfuse.local.env absent — copy example, chmod 600, add YOUR keys")
    else:
        mode = stat.S_IMODE(env_path.stat().st_mode)
        _ok(f"langfuse.local.env exists mode={oct(mode)} (expect 0o600)")
        if flag == "true":
            _ok("TRACE_TO_LANGFUSE=true")
        elif flag == "set_not_true":
            _fail("TRACE_TO_LANGFUSE is set but not true")
            failed += 1
        else:
            _warn("TRACE_TO_LANGFUSE not set — run scripts/enable-tracing.sh")

        # Credential presence without values
        text = env_path.read_text(errors="replace")
        pk = any(line.startswith("LANGFUSE_PUBLIC_KEY=") and "your-public-key" not in line
                 for line in text.splitlines())
        sk = any(line.startswith("LANGFUSE_SECRET_KEY=") and "your-secret-key" not in line
                 for line in text.splitlines())
        if pk and sk:
            _ok("public+secret key lines look filled (values not printed)")
        else:
            _warn("key lines still look like placeholders")

    print("note Kiro two-step-demo is an IDE workflow, not a CLI substitute")
    print("note do not treat unit tests as live ingest/score proof")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
