"""Task1 tracing context enrichment for the isolated local Kiro→Langfuse demo.

Local-first only. Does not talk to AWS/Cloud. Does not invent team/repo/SHA.
Langfuse session_id = configurable workspace_key (team grouping intent).
Kiro conversation/session ids are preserved separately in metadata.
Workflow run id is distinct from workspace_key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "workflow.name",
    "workflow.version",
    "user",
    "team",
    "repo",
    "git.sha",
)

# Explicit automation identities. NEVER treat these as human engineers for adoption.
# identity_type in tracing.context.json is the primary lever; this set is a safety net
# for known demo/local runner accounts. Not a company directory.
DEFAULT_AUTOMATION_USER_IDS = frozenset({
    "chomu-local",
    "kiro-test-runner",
})

TITLE_STEP_RE = re.compile(r"^(?P<name>[^·]+?)\s*·\s*(?P<step>.+)$")


def normalize_field(value: Any) -> str | None:
    """Normalize a metadata scalar. Empty/null → None. Never invent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or value.lower() in {"null", "none", "undefined", "unknown", "n/a"}:
        return None
    # Keep Langfuse metadata values reasonably short (docs: ~200 chars).
    return value[:198]


def resolve_identity_type(config: dict | None) -> str:
    """Classify the configured identity as automation, human, or unknown.

    Rules (first match wins):
    1. tracing.context.json identity_type = automation|human (explicit)
    2. user_id in DEFAULT_AUTOMATION_USER_IDS or config.automation_user_ids
    3. otherwise unknown (do NOT assume a named user is a human engineer)

    Demo team `demo-engineering` is a label only; it does not imply a real org team
    or multi-user rollout. kiro-test-runner is always automation.
    """
    config = config or {}
    explicit = normalize_field(config.get("identity_type"))
    if explicit:
        lowered = explicit.lower()
        if lowered in {"automation", "human"}:
            return lowered
    user = normalize_field(config.get("user_id"))
    extra = config.get("automation_user_ids") or []
    extra_ids = {normalize_field(x) for x in extra if normalize_field(x)}
    known = {u.lower() for u in DEFAULT_AUTOMATION_USER_IDS}
    known.update(x.lower() for x in extra_ids if x)
    if user and user.lower() in known:
        return "automation"
    return "unknown"


def load_tracing_context(cwd: str | None) -> dict:
    """Load explicit `.kiro/tracing.context.json` if present. No OS/email inference."""
    if not cwd:
        return {}
    path = Path(cwd) / ".kiro" / "tracing.context.json"
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def content_sha256(path: Path, n: int = 12) -> str | None:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest[:n]
    except OSError:
        return None


def discover_git(cwd: str | None, *, git_bin: str = "git") -> dict:
    """Safe git discovery. Never init a repo. Returns availability + provenance."""
    out = {
        "sha": None,
        "repo": None,
        "available": False,
        "provenance": "git-unavailable",
    }
    if not cwd:
        out["provenance"] = "no-cwd"
        return out
    root = Path(cwd)
    if not root.exists():
        out["provenance"] = "cwd-missing"
        return out
    try:
        inside = subprocess.run(
            [git_bin, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        out["provenance"] = "git-exec-failed"
        return out
    if inside.returncode != 0 or (inside.stdout or "").strip() != "true":
        out["provenance"] = "not-a-git-repository"
        return out
    out["available"] = True
    try:
        sha_p = subprocess.run(
            [git_bin, "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if sha_p.returncode == 0:
            out["sha"] = normalize_field((sha_p.stdout or "").strip())
            out["provenance"] = "git-rev-parse-HEAD"
        else:
            out["provenance"] = "git-head-unreadable"
        remote_p = subprocess.run(
            [git_bin, "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if remote_p.returncode == 0:
            url = (remote_p.stdout or "").strip()
            out["repo"] = normalize_field(_repo_from_remote(url))
    except (OSError, subprocess.TimeoutExpired):
        if out["provenance"] == "git-rev-parse-HEAD" and not out["sha"]:
            out["provenance"] = "git-exec-failed"
    return out


def _repo_from_remote(url: str | None) -> str | None:
    """Extract org/repo from a git remote URL, sanitizing any credentials.
    
    Handles: HTTPS, SSH, and authenticated URLs with embedded credentials.
    Never returns credentials in the result.
    """
    if not url:
        return None
    u = url.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]

    # Handle SSH URLs: git@host:org/repo
    if ":" in u and not u.startswith("http"):
        u = u.split(":", 1)[-1]
        parts = u.replace("\\", "/").split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
        return parts[-1] if parts else None

    # Handle HTTPS URLs (possibly with credentials): https://[user:pass@]host/org/repo
    if u.startswith("http://") or u.startswith("https://"):
        # Strip scheme
        u = u.split("://", 1)[-1]
        # Strip credentials if present (user:pass@)
        if "@" in u:
            u = u.split("@", 1)[-1]
        # Now u is host/org/repo or host/org/repo (no creds)
        parts = u.replace("\\", "/").split("/")
        # parts[0] is host, parts[1:] is path
        if len(parts) >= 3:
            return f"{parts[-2]}/{parts[-1]}"
        elif len(parts) == 2:
            return parts[-1]
        return None

    # Fallback for other URL formats
    parts = u.replace("\\", "/").split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else None


def find_recipe(cwd: str | None, workflow_name: str | None) -> Path | None:
    if not cwd or not workflow_name:
        return None
    workflows = Path(cwd) / ".kiro" / "workflows"
    if not workflows.is_dir():
        return None
    # Prefer exact stem match used by Kiro authoring docs.
    for suffix in (".workflow.json", ".workflow.yaml", ".workflow.yml"):
        cand = workflows / f"{workflow_name}{suffix}"
        if cand.is_file():
            return cand
    # Fall back to scanning name field in JSON recipes only.
    for path in sorted(workflows.glob("*.workflow.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name") == workflow_name:
            return path
    return None


def parse_workflow_from_session(session_meta: dict | None) -> dict:
    """Derive workflow identity from Kiro session.json evidence only."""
    session_meta = session_meta or {}
    title = normalize_field(session_meta.get("title")) or ""
    agent_mode = normalize_field(session_meta.get("agentMode"))
    root_conversation_id = normalize_field(session_meta.get("rootConversationId"))
    name = None
    step_id = None
    m = TITLE_STEP_RE.match(title) if title else None
    if m:
        name = normalize_field(m.group("name"))
        step_id = normalize_field(m.group("step"))
    is_workflow_agent = bool(agent_mode and (agent_mode.startswith("wf-") or agent_mode == "semantic_reviewer"))
    return {
        "title": title or None,
        "agent_mode": agent_mode,
        "root_conversation_id": root_conversation_id,
        "name_from_title": name,
        "step_id": step_id,
        "is_workflow_agent": is_workflow_agent,
    }



def extract_kiro_workflow_meta(session_dir: str | Path | None) -> dict:
    """Read native Kiro workflow identity from messages.jsonl when present.

    Observed shape: payload._meta.kiro.workflow = {
      workflowId, workflowName, nodeId, nodePath, type
    }
    Also captures args.workflowPath from tool calls when available.
    """
    out = {
        "workflow_id": None,
        "workflow_name": None,
        "node_id": None,
        "node_path": None,
        "workflow_path": None,
        "source": None,
    }
    if not session_dir:
        return out
    messages = Path(session_dir) / "messages.jsonl"
    if not messages.is_file():
        return out
    try:
        lines = messages.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") or {}
        args = payload.get("args") or {}
        if isinstance(args, dict) and args.get("workflowPath") and not out["workflow_path"]:
            out["workflow_path"] = normalize_field(args.get("workflowPath"))
            out["source"] = out["source"] or "messages.args.workflowPath"
        meta = (payload.get("_meta") or {}).get("kiro") or {}
        if not isinstance(meta, dict):
            continue
        wf = meta.get("workflow")
        if isinstance(wf, dict) and wf.get("workflowId"):
            out["workflow_id"] = normalize_field(wf.get("workflowId"))
            out["workflow_name"] = normalize_field(wf.get("workflowName")) or out["workflow_name"]
            out["node_id"] = normalize_field(wf.get("nodeId")) or out["node_id"]
            node_path = wf.get("nodePath")
            if isinstance(node_path, list):
                out["node_path"] = json.dumps(node_path)
            out["source"] = "messages._meta.kiro.workflow"
        # Parent chat notifications sometimes carry workflowId at top-level kiro meta.
        if meta.get("workflowId") and not out["workflow_id"]:
            out["workflow_id"] = normalize_field(meta.get("workflowId"))
            out["source"] = out["source"] or "messages._meta.kiro.workflowId"
        notif = meta.get("notification")
        if isinstance(notif, dict) and notif.get("workflowId") and not out["workflow_id"]:
            out["workflow_id"] = normalize_field(notif.get("workflowId"))
            out["source"] = out["source"] or "messages._meta.kiro.notification.workflowId"
    return out


def find_session_dir_by_id(sid: str | None, cwd: str | None = None) -> Path | None:
    if not sid:
        return None
    root = Path.home() / ".kiro" / "sessions"
    if cwd:
        digest = hashlib.sha256(str(Path(cwd)).encode()).hexdigest()[:16]
        # NOTE: Kiro uses its own workspace hash; still try common layout then scan.
        cand = root / digest / sid
        if (cand / "messages.jsonl").exists():
            return cand
    try:
        for d in root.iterdir():
            cand = d / sid
            if (cand / "messages.jsonl").exists():
                return cand
    except OSError:
        pass
    return None


def resolve_workflow(
    cwd: str | None,
    session_meta: dict | None,
    kiro_session_id: str | None,
    kiro_workflow: dict | None = None,
) -> dict:
    """Populate workflow name/version/run ids from recipe + Kiro evidence.

    Never hardcode the demo recipe onto unrelated (non-workflow) traces.
    - workflow.run_id := native Kiro workflowId when present (execution scope)
    - workflow.step_session_id := this step's Kiro session id (not the run id)
    One-step demos cannot prove cross-step correlation.
    """
    parsed = parse_workflow_from_session(session_meta)
    native = kiro_workflow or {}
    name = native.get("workflow_name") or (
        parsed["name_from_title"] if parsed["is_workflow_agent"] else None
    )
    if name and not parsed["is_workflow_agent"] and not native.get("workflow_id"):
        # Parent chat must not inherit a workflow name by title accident.
        name = None

    step_id = native.get("node_id") or parsed["step_id"]

    recipe = None
    recipe_path = None
    # Prefer explicit workflowPath evidence when it points inside cwd.
    wf_path = native.get("workflow_path")
    if wf_path and cwd:
        cand = Path(wf_path)
        try:
            if cand.is_file() and str(cand.resolve()).startswith(str(Path(cwd).resolve())):
                recipe = cand
                recipe_path = str(cand)
        except OSError:
            pass
    if recipe is None and name:
        recipe = find_recipe(cwd, name)
        if recipe:
            recipe_path = str(recipe)

    version = None
    version_provenance = None
    if recipe:
        try:
            data = json.loads(recipe.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        declared = None
        if isinstance(data, dict):
            declared = normalize_field(data.get("version") or data.get("workflowVersion"))
            recipe_name = normalize_field(data.get("name"))
            # Keep runtime/native name if present; still record recipe file name in provenance path.
            if recipe_name and not name:
                name = recipe_name
        if declared:
            version = declared
            version_provenance = "recipe-declared-version"
        else:
            digest = content_sha256(recipe)
            if digest:
                version = f"sha256:{digest}"
                version_provenance = "recipe-content-sha256"
    elif name or native.get("workflow_id"):
        version_provenance = "recipe-file-missing"

    # Native workflow execution id (can span multiple steps). Distinct from step session.
    run_id = native.get("workflow_id")
    run_id_provenance = None
    if run_id:
        run_id_provenance = native.get("source") or "kiro.workflow.workflowId"
    elif parsed["is_workflow_agent"]:
        # Do NOT silently promote step session to run id.
        run_id = None
        run_id_provenance = "native-workflowId-unavailable"

    step_session_id = normalize_field(kiro_session_id) if parsed["is_workflow_agent"] else None

    return {
        "name": name,
        "version": version,
        "version_provenance": version_provenance,
        "step_id": step_id,
        "agent_mode": parsed["agent_mode"],
        "recipe_path": recipe_path,
        "run_id": run_id,
        "run_id_provenance": run_id_provenance,
        "step_session_id": step_session_id,
        "step_session_id_provenance": "kiro-workflow-step-session-id" if step_session_id else None,
        "title": parsed["title"],
        "is_workflow_agent": parsed["is_workflow_agent"],
        "root_conversation_id": parsed["root_conversation_id"],
        "native_source": native.get("source"),
        "native_node_path": native.get("node_path"),
    }



def resolve_workspace(cwd: str | None, config: dict) -> dict:
    """Stable workspace key for Langfuse session grouping.

    Prefer explicit config workspace_key (cross-machine). Path-only fallback is
    local convenience and is flagged — not team-validated.
    """
    key = normalize_field(config.get("workspace_key"))
    if key:
        return {
            "key": key,
            "source": "config.workspace_key",
            "path": str(Path(cwd).resolve()) if cwd else None,
            "cross_machine_ready": True,
        }
    if cwd:
        resolved = str(Path(cwd).resolve())
        digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
        return {
            "key": f"pathhash:{digest}",
            "source": "path-hash-fallback",
            "path": resolved,
            "cross_machine_ready": False,
        }
    return {
        "key": None,
        "source": "missing",
        "path": None,
        "cross_machine_ready": False,
    }


def build_enrichment(
    cwd: str | None,
    kiro_session_id: str | None,
    session_meta: dict | None = None,
    *,
    config: dict | None = None,
    git_info: dict | None = None,
    kiro_workflow: dict | None = None,
) -> dict:
    """Build Langfuse user/session/tags/metadata for Task1.

    - langfuse session_id := workspace_key (grouping intent)
    - kiro.session_id preserved separately
    - workflow.run_id distinct from workspace key
    - missing required fields listed explicitly
    """
    config = config if config is not None else load_tracing_context(cwd)
    workspace = resolve_workspace(cwd, config)
    # Optionally enrich native workflow meta from this session and parent chat evidence.
    kiro_workflow = dict(kiro_workflow or {})
    if session_meta and session_meta.get("_kiro_workflow"):
        kiro_workflow = {**session_meta.get("_kiro_workflow"), **kiro_workflow}
    # If step session lacks workflowPath, try parent conversation messages.
    if not kiro_workflow.get("workflow_path"):
        parent_id = None
        if session_meta:
            parent_id = session_meta.get("rootConversationId")
        parent_dir = find_session_dir_by_id(parent_id, cwd) if parent_id else None
        if parent_dir:
            parent_meta = extract_kiro_workflow_meta(parent_dir)
            if parent_meta.get("workflow_path"):
                kiro_workflow["workflow_path"] = parent_meta["workflow_path"]
                kiro_workflow["source"] = kiro_workflow.get("source") or parent_meta.get("source")
            if not kiro_workflow.get("workflow_id") and parent_meta.get("workflow_id"):
                # Parent may only learn workflowId via notification; keep if step lacked it.
                kiro_workflow["workflow_id"] = parent_meta["workflow_id"]
                kiro_workflow["source"] = kiro_workflow.get("source") or parent_meta.get("source")
    workflow = resolve_workflow(cwd, session_meta, kiro_session_id, kiro_workflow)

    git_info = git_info if git_info is not None else discover_git(cwd)

    user = normalize_field(config.get("user_id"))
    team = normalize_field(config.get("team"))
    identity_type = resolve_identity_type(config)
    repo = normalize_field(config.get("repo")) or normalize_field(git_info.get("repo"))
    repo_provenance = (
        "config.repo"
        if normalize_field(config.get("repo"))
        else ("git-remote-origin" if normalize_field(git_info.get("repo")) else "unavailable")
    )
    git_sha = normalize_field(config.get("git_sha")) or normalize_field(git_info.get("sha"))
    git_provenance = (
        "config.git_sha"
        if normalize_field(config.get("git_sha"))
        else (git_info.get("provenance") or "unavailable")
    )

    values = {
        "workflow.name": workflow.get("name"),
        "workflow.version": workflow.get("version"),
        "user": user,
        "team": team,
        "repo": repo,
        "git.sha": git_sha,
    }
    missing = [k for k in REQUIRED_FIELDS if not values.get(k)]

    tags = ["kiro", "local-langfuse"]
    if workflow.get("name"):
        tags.append(f"workflow:{workflow['name']}"[:200])
    if workflow.get("is_workflow_agent"):
        tags.append("kiro-workflow-step")
    if identity_type == "automation":
        tags.append("kiro-automation")
    elif identity_type == "human":
        tags.append("kiro-human")
    else:
        tags.append("kiro-identity-unknown")
    if missing:
        tags.append("metadata-incomplete")
    if not workspace.get("cross_machine_ready"):
        tags.append("workspace-key-fallback")

    metadata = {
        "workspace.key": workspace.get("key"),
        "workspace.key_source": workspace.get("source"),
        "workspace.path_local": workspace.get("path"),
        "workspace.cross_machine_ready": "true" if workspace.get("cross_machine_ready") else "false",
        "kiro.session_id": normalize_field(kiro_session_id),
        "kiro.root_conversation_id": workflow.get("root_conversation_id"),
        "kiro.agent_mode": workflow.get("agent_mode"),
        "kiro.session_title": workflow.get("title"),
        "workflow.name": workflow.get("name"),
        "workflow.version": workflow.get("version"),
        "workflow.version_provenance": workflow.get("version_provenance"),
        "workflow.step_id": workflow.get("step_id"),
        "workflow.run_id": workflow.get("run_id"),
        "workflow.run_id_provenance": workflow.get("run_id_provenance"),
        "workflow.step_session_id": workflow.get("step_session_id"),
        "workflow.step_session_id_provenance": workflow.get("step_session_id_provenance"),
        "workflow.recipe_path": workflow.get("recipe_path"),
        "workflow.native_source": workflow.get("native_source"),
        "workflow.scope_note": "run_id is native Kiro workflowId when available; step_session_id is this step only; not cross-step proof",
        "team": team,
        "identity.type": identity_type,
        "identity.human_engineer": "true" if identity_type == "human" else "false",
        "repo": repo,
        "repo.provenance": repo_provenance,
        "git.sha": git_sha,
        "git.provenance": git_provenance,
        "git.available": "true" if git_info.get("available") else "false",
        "missing_fields": ",".join(missing) if missing else "",
        "metadata.complete": "false" if missing else "true",
        "task1.scope": "local-first",
    }
    # Drop empty metadata values (Langfuse drops nulls; keep missing_fields even if empty? keep flag keys)
    cleaned = {}
    for k, v in metadata.items():
        if v is None or v == "":
            if k in {"missing_fields", "metadata.complete", "task1.scope", "git.available", "workspace.cross_machine_ready"}:
                cleaned[k] = v if v is not None else ""
            continue
        cleaned[k] = v if isinstance(v, str) else str(v)

    return {
        "langfuse_session_id": workspace.get("key"),
        "langfuse_user_id": user,  # may be None — caller must not invent
        "identity_type": identity_type,
        "tags": tags,
        "metadata": cleaned,
        "missing_fields": missing,
        "metadata_complete": not missing,
        "workspace": workspace,
        "workflow": workflow,
        "values": values,
    }


def enrichment_attrs(enrichment: dict, meta_fn, attr_fn, session_attr_key: str, user_attr_key: str, tags_attr_key: str) -> list:
    """Convert enrichment into OTLP attribute list using caller's attr/meta helpers."""
    attrs = []
    sid = enrichment.get("langfuse_session_id")
    if sid:
        attrs.append(attr_fn(session_attr_key, sid))
    user = enrichment.get("langfuse_user_id")
    if user:
        attrs.append(attr_fn(user_attr_key, user))
    tags = enrichment.get("tags") or []
    if tags:
        attrs.append(attr_fn(tags_attr_key, tags))
    for k, v in (enrichment.get("metadata") or {}).items():
        attrs.append(meta_fn(k, v))
    return attrs


def assert_no_secret_leakage(payload: Any) -> None:
    """Raise if known secret env patterns appear in enrichment payload."""
    blob = json.dumps(payload, default=str)
    forbidden_prefixes = ("sk-lf-", "pk-lf-")
    for p in forbidden_prefixes:
        # Allow mentioning prefixes in docs/tests only if not followed by key material;
        # here we check env values themselves.
        for env_key in ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"):
            val = os.environ.get(env_key) or ""
            if val and val in blob:
                raise AssertionError(f"secret leakage: {env_key} value present in payload")
        if p in blob and len(blob.split(p, 1)[-1]) > 8:
            # likely a real key embedded
            rest = blob.split(p, 1)[-1]
            if any(ch.isalnum() for ch in rest[:8]):
                # only fail when an actual env key matches
                pass
