#!/usr/bin/env python3
"""Test hook invocation locally without Kiro.

This script simulates a Kiro hook event to verify that:
1. The hook_launcher.py is found and executed correctly
2. Environment files are loaded
3. State directory is created with diagnostics
4. The langfuse_kiro_hook.py processes the event

Usage:
    cd /path/to/your/workspace
    python3 .kiro/hooks/test_hook_invocation.py

Or from any directory:
    python3 /path/to/workspace/.kiro/hooks/test_hook_invocation.py --workspace /path/to/workspace

After running, check:
    .kiro/langfuse-state-local/launcher.log  (should show INVOKED + SUCCESS/ERROR)
    .kiro/langfuse-state-local/errors.log    (hook-level errors if any)

If TRACE_TO_LANGFUSE=true and credentials are valid, traces should appear in Langfuse.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Test Langfuse hook invocation")
    parser.add_argument(
        "--workspace", "-w",
        type=str,
        default=None,
        help="Workspace path (default: current directory or parent with .kiro/)"
    )
    parser.add_argument(
        "--hook-event",
        type=str,
        default="UserPromptSubmit",
        choices=["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "PreTaskExec", "PostTaskExec"],
        help="Hook event type to simulate"
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default="test-session-" + str(int(time.time())),
        help="Session ID to use"
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default="Test prompt for hook verification",
        help="Prompt text for UserPromptSubmit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show verbose output"
    )
    args = parser.parse_args()
    
    # Find workspace
    workspace = args.workspace
    if not workspace:
        workspace = os.getcwd()
        # Walk up to find .kiro directory
        check = Path(workspace)
        while check != check.parent:
            if (check / ".kiro").is_dir():
                workspace = str(check)
                break
            check = check.parent
    
    workspace_path = Path(workspace).resolve()
    kiro_dir = workspace_path / ".kiro"
    hooks_dir = kiro_dir / "hooks"
    launcher = hooks_dir / "hook_launcher.py"
    state_dir = kiro_dir / "langfuse-state-local"
    env_file = kiro_dir / "langfuse.local.env"
    
    print(f"=== Langfuse Hook Test ===")
    print(f"Workspace:     {workspace_path}")
    print(f"Hooks dir:     {hooks_dir} (exists: {hooks_dir.exists()})")
    print(f"Launcher:      {launcher} (exists: {launcher.exists()})")
    print(f"Env file:      {env_file} (exists: {env_file.exists()})")
    print(f"State dir:     {state_dir} (exists: {state_dir.exists()})")
    print(f"Hook event:    {args.hook_event}")
    print(f"Session ID:    {args.session_id}")
    print()
    
    if not launcher.exists():
        print(f"ERROR: Launcher not found: {launcher}")
        print("Make sure you're in the correct workspace directory.")
        return 1
    
    # Build test event
    event = {
        "hook_event_name": args.hook_event,
        "session_id": args.session_id,
        "cwd": str(workspace_path),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    
    if args.hook_event == "UserPromptSubmit":
        event["prompt"] = args.prompt
    elif args.hook_event in ("PreToolUse", "PostToolUse"):
        event["tool_name"] = "test_tool"
        event["tool_call_id"] = f"tooluse_test_{int(time.time())}"
        event["tool_input"] = {"test": "input"}
        if args.hook_event == "PostToolUse":
            event["tool_response"] = {"test": "output"}
    elif args.hook_event == "Stop":
        event["reason"] = "test"
    
    print(f"Test event JSON:")
    print(json.dumps(event, indent=2))
    print()
    
    if args.dry_run:
        print("DRY RUN - not executing")
        return 0
    
    # Write event to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tf:
        json.dump(event, tf)
        tf_path = tf.name
    
    try:
        # Run launcher with event as stdin
        print(f"Executing launcher...")
        result = subprocess.run(
            ["python3", "-u", str(launcher)],
            stdin=open(tf_path),
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        print(f"Exit code: {result.returncode}")
        if result.stdout:
            print(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"STDERR:\n{result.stderr}")
        print()
        
        # Check diagnostics
        launcher_log = state_dir / "launcher.log"
        errors_log = state_dir / "errors.log"
        
        if launcher_log.exists():
            print(f"=== {launcher_log} (last 10 lines) ===")
            lines = launcher_log.read_text().strip().split('\n')
            for line in lines[-10:]:
                print(f"  {line}")
            print()
        else:
            print(f"WARNING: {launcher_log} was not created - hook may not have run")
            print()
        
        if errors_log.exists():
            print(f"=== {errors_log} (last 10 lines) ===")
            lines = errors_log.read_text().strip().split('\n')
            for line in lines[-10:]:
                print(f"  {line}")
            print()
        
        # Check for session state
        session_state = state_dir / f"{args.session_id}.json"
        if session_state.exists():
            print(f"=== Session state ({session_state.name}) ===")
            print(session_state.read_text())
            print()
        
        # Summary
        print("=== Summary ===")
        if launcher_log.exists() and "INVOKED" in launcher_log.read_text():
            if "SUCCESS" in launcher_log.read_text():
                print("✓ Hook launcher invoked and completed successfully")
            elif "ERROR" in launcher_log.read_text():
                print("✗ Hook launcher invoked but encountered errors - check launcher.log")
            else:
                print("? Hook launcher invoked but status unclear")
        else:
            print("✗ Hook launcher did not create diagnostic log - invocation failed")
        
        # Check tracing configuration
        if env_file.exists():
            env_content = env_file.read_text()
            env = {}
            for line in env_content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
            
            trace_enabled = env.get("TRACE_TO_LANGFUSE", "").lower() == "true"
            has_public = bool(env.get("LANGFUSE_PUBLIC_KEY"))
            has_secret = bool(env.get("LANGFUSE_SECRET_KEY"))
            base_url = env.get("LANGFUSE_BASE_URL", "not configured")
            
            print(f"  TRACE_TO_LANGFUSE={trace_enabled}")
            print(f"  Has credentials: public_key={has_public}, secret_key={has_secret}")
            print(f"  Base URL: {base_url}")
            
            if not trace_enabled:
                print("  ⚠ Tracing DISABLED - set TRACE_TO_LANGFUSE=true to enable")
            elif not has_public or not has_secret:
                print("  ⚠ Missing credentials - traces will not be sent")
            else:
                print("  ✓ Tracing enabled and configured")
        else:
            print(f"  ⚠ No env file at {env_file}")
            print(f"    Copy .kiro/langfuse.local.env.template to .kiro/langfuse.local.env")
            print(f"    and configure your Langfuse credentials")
        
        return result.returncode
        
    except subprocess.TimeoutExpired:
        print("ERROR: Hook execution timed out after 30s")
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            os.unlink(tf_path)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
