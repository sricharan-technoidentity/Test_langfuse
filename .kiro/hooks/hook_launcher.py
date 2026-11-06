#!/usr/bin/env python3
"""Robust Kiro hook launcher: resolves workspace paths from event cwd, not shell cwd.

Kiro 1.4+ may invoke hook commands from an arbitrary working directory.
The hook event JSON (piped via stdin) contains the actual workspace path in the 'cwd' field.
This launcher:
1. Reads event JSON from stdin
2. Extracts workspace cwd from event
3. Sets up environment with workspace-relative paths
4. Invokes the actual hook handler with the event

Usage in langfuse-tracing.json:
  "command": "python3 -u /path/to/.kiro/hooks/hook_launcher.py"

For portability across repos, we cannot hardcode an absolute path. Instead:
1. The hook command is: python3 -u .kiro/hooks/hook_launcher.py
2. This launcher reads cwd from the event and uses that for all paths.

Diagnostic output goes to:
  <workspace>/.kiro/langfuse-state-local/launcher.log

This file is always written on invocation to confirm the hook ran.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path


def log_diagnostic(state_dir: Path, msg: str) -> None:
    """Write diagnostic message to launcher.log (always, for debugging)."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "launcher.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def main() -> int:
    # Read event from stdin FIRST - this must happen before any cwd resolution
    try:
        event_raw = sys.stdin.read()
    except Exception as e:
        print(f"launcher: stdin read failed: {e}", file=sys.stderr)
        return 0  # fail-open
    
    try:
        event = json.loads(event_raw)
    except json.JSONDecodeError as e:
        print(f"launcher: JSON parse failed: {e}", file=sys.stderr)
        return 0  # fail-open
    
    # Extract workspace cwd from event (this is the correct workspace path)
    workspace_cwd = event.get("cwd", "")
    if not workspace_cwd:
        # Fallback: try environment or current directory (less reliable)
        workspace_cwd = os.environ.get("KIRO_WORKSPACE_CWD", os.getcwd())
    
    workspace_path = Path(workspace_cwd)
    
    # Resolve paths relative to workspace
    env_file = os.environ.get("KIRO_LF_ENV_FILE")
    if env_file and not os.path.isabs(env_file):
        env_file = str(workspace_path / env_file)
    else:
        env_file = str(workspace_path / ".kiro" / "langfuse.local.env")
    
    state_dir_env = os.environ.get("KIRO_LF_STATE")
    if state_dir_env and not os.path.isabs(state_dir_env):
        state_dir = workspace_path / state_dir_env
    else:
        state_dir = workspace_path / ".kiro" / "langfuse-state-local"
    
    hook_script = workspace_path / ".kiro" / "hooks" / "langfuse_kiro_hook.py"
    
    # Set environment variables for the hook
    os.environ["KIRO_LF_ENV_FILE"] = env_file
    os.environ["KIRO_LF_STATE"] = str(state_dir)
    os.environ["KIRO_WORKSPACE_CWD"] = workspace_cwd
    
    # Always log diagnostic to confirm invocation (this creates state_dir if needed)
    hook_name = event.get("hook_event_name", "unknown")
    session_id = event.get("session_id", "unknown")
    log_diagnostic(
        state_dir,
        f"INVOKED hook={hook_name} session={session_id} workspace={workspace_cwd} "
        f"shell_cwd={os.getcwd()} hook_exists={hook_script.exists()} env_exists={Path(env_file).exists()}"
    )
    
    # Check if hook script exists
    if not hook_script.exists():
        log_diagnostic(state_dir, f"ERROR: hook script not found: {hook_script}")
        return 0  # fail-open
    
    # Check if env file exists (warning only)
    if not Path(env_file).exists():
        log_diagnostic(state_dir, f"WARNING: env file not found: {env_file} (may use fallback)")
    
    # Import and execute the hook
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("langfuse_kiro_hook", hook_script)
        if spec is None or spec.loader is None:
            log_diagnostic(state_dir, f"ERROR: failed to load hook spec: {hook_script}")
            return 0
        
        module = importlib.util.module_from_spec(spec)
        sys.modules["langfuse_kiro_hook"] = module
        spec.loader.exec_module(module)
        
        # Call the handler with the event
        module.handle_event(event)
        log_diagnostic(state_dir, f"SUCCESS hook={hook_name} session={session_id}")
        
    except Exception as e:
        log_diagnostic(state_dir, f"ERROR: hook execution failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return 0  # fail-open
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
