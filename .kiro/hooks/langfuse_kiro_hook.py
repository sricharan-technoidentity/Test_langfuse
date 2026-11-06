#!/usr/bin/env python3
"""Kiro IDE hook -> Langfuse OTLP/HTTP JSON (stdlib only, fail-open, always exit 0).

Enriches traces from ~/.kiro/sessions/<hash>/<session_id>/messages.jsonl:
  reply (Say), reasoning, tool_result outputs, credits, contextUsage, elapsed, requestIds,
  session title, modelId. Race: usage_summary lands ~10ms after Stop, so Stop spawns a
  detached child that polls briefly (and UserPromptSubmit can backfill the prior turn).

Config files (file values OVERRIDE inherited env — stale IDE env previously caused 401):
  1) <workspace>/.env  (cwd from stdin)
  2) ~/.kiro/langfuse.env
Required: TRACE_TO_LANGFUSE=true, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
Optional: LANGFUSE_BASE_URL (default https://us.cloud.langfuse.com), KIRO_LF_DEBUG=1

Task1 local enrichment (demo-only module tracing_context.py):
  Langfuse session_id = configured workspace_key (not Kiro conversation id).
  Kiro session/root ids preserved in metadata. Workflow run_id is separate.
  user/team only from .kiro/tracing.context.json — never OS/email inference.

"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Local Task1 enrichment (same directory as this hook). Fail-open if missing.
try:
    from tracing_context import build_enrichment, load_tracing_context, extract_kiro_workflow_meta
except ImportError:  # pragma: no cover - runtime path when executed as script
    import importlib.util

    _tc = Path(__file__).resolve().parent / "tracing_context.py"
    _spec = importlib.util.spec_from_file_location("tracing_context", _tc)
    tracing_context = importlib.util.module_from_spec(_spec)
    assert _spec and _spec.loader
    _spec.loader.exec_module(tracing_context)
    build_enrichment = tracing_context.build_enrichment
    load_tracing_context = tracing_context.load_tracing_context
    extract_kiro_workflow_meta = tracing_context.extract_kiro_workflow_meta

MAX = 20000
DEFAULT_BASE = "https://us.cloud.langfuse.com"
STATE_DIR = Path(os.environ.get("KIRO_LF_STATE", Path.home() / ".kiro" / "langfuse-state"))
POLL_SECONDS = float(os.environ.get("KIRO_LF_USAGE_POLL_SECONDS", "3.0"))
POLL_INTERVAL = float(os.environ.get("KIRO_LF_USAGE_POLL_INTERVAL", "0.05"))
CLAIM_TTL_SEC = float(os.environ.get("KIRO_LF_CLAIM_TTL_SECONDS", "120"))
DONE_TTL_SEC = float(os.environ.get("KIRO_LF_DONE_TTL_SECONDS", "3600"))
EVENT_TTL_SEC = float(os.environ.get("KIRO_LF_EVENT_TTL_SECONDS", "600"))
CLAIMS_DIR = STATE_DIR / "claims"

# Attribute keys shared with TypeScript/Python observability packages (contract tests).
ATTR = {
    "session_id": "langfuse.session.id",
    "trace_name": "langfuse.trace.name",
    "user_id": "langfuse.user.id",
    "tags": "langfuse.trace.tags",
    "obs_type": "langfuse.observation.type",
    "obs_input": "langfuse.observation.input",
    "obs_output": "langfuse.observation.output",
    "model": "langfuse.observation.model.name",
    "usage": "langfuse.observation.usage_details",
    "cost": "langfuse.observation.cost_details",
}


def load_env_file(path: Path) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip("'\"")


def load_config(cwd: str | None) -> None:
    """Workspace .env then user-level file; both override inherited env."""
    if cwd:
        load_env_file(Path(cwd) / ".env")
    load_env_file(Path(os.environ.get("KIRO_LF_ENV_FILE", Path.home() / ".kiro" / "langfuse.env")))


def now_ns() -> int:
    return time.time_ns()


def s(v) -> str:
    if not isinstance(v, str):
        v = json.dumps(v, default=str)
    return v[:MAX]


def attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # OTLP JSON: prefer string for Langfuse metadata; numbers as double/int when useful
        if isinstance(v, float):
            return {"key": k, "value": {"doubleValue": v}}
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, list):
        return {"key": k, "value": {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}}
    return {"key": k, "value": {"stringValue": s(v)}}


def meta(k, v):
    return attr(f"langfuse.observation.metadata.{k}", v)


def state_path(sid: str) -> Path:
    return STATE_DIR / f"{sid}.json"


def load_state(sid: str) -> dict:
    try:
        return json.loads(state_path(sid).read_text())
    except Exception:
        return {}


def save_state(sid: str, st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(sid).write_text(json.dumps(st))


def log_err(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "errors.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except Exception:
        pass
    if os.environ.get("KIRO_LF_DEBUG"):
        print(f"langfuse hook: {msg}", file=sys.stderr)


def _safe_key(*parts: str) -> str:
    raw = "_".join(str(p) for p in parts if p is not None)
    out = []
    for ch in raw:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("_") or "x"
    return s[:180]


def cleanup_claims(now: float | None = None) -> None:
    """Remove stale claim/done/event marker files under claims/."""
    now = time.time() if now is None else now
    try:
        CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
        for p in CLAIMS_DIR.iterdir():
            if not p.is_file():
                continue
            try:
                age = now - p.stat().st_mtime
            except OSError:
                continue
            name = p.name
            if name.endswith(".claim") and age > CLAIM_TTL_SEC:
                try:
                    p.unlink()
                except OSError:
                    pass
            elif name.endswith(".done") and age > DONE_TTL_SEC:
                try:
                    p.unlink()
                except OSError:
                    pass
            elif name.startswith("evt_") and age > EVENT_TTL_SEC:
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def try_claim_export(sid: str, turn_id: str) -> bool:
    """Atomically claim (session, turnId) for export. True if this process owns it."""
    cleanup_claims()
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    key = _safe_key(sid, turn_id)
    done_path = CLAIMS_DIR / f"{key}.done"
    claim_path = CLAIMS_DIR / f"{key}.claim"
    if done_path.exists():
        return False
    if claim_path.exists():
        try:
            age = time.time() - claim_path.stat().st_mtime
            if age < CLAIM_TTL_SEC:
                return False
            claim_path.unlink(missing_ok=True)
        except OSError:
            return False
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        log_err(f"claim: {type(e).__name__}: {e}")
        return False


def mark_export_done(sid: str, turn_id: str) -> None:
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    key = _safe_key(sid, turn_id)
    done_path = CLAIMS_DIR / f"{key}.done"
    claim_path = CLAIMS_DIR / f"{key}.claim"
    try:
        done_path.write_text(json.dumps({"done_at": time.time(), "pid": os.getpid()}))
    except OSError as e:
        log_err(f"mark_done: {type(e).__name__}: {e}")
    try:
        claim_path.unlink(missing_ok=True)
    except OSError:
        pass


def release_claim(sid: str, turn_id: str) -> None:
    claim_path = CLAIMS_DIR / f"{_safe_key(sid, turn_id)}.claim"
    try:
        claim_path.unlink(missing_ok=True)
    except OSError:
        pass


def event_signature(ev: dict) -> str:
    name = ev.get("hook_event_name") or ""
    tid = ev.get("tool_call_id") or ev.get("toolCallId") or ev.get("tool_use_id")
    if tid:
        return f"{name}:{tid}"
    if name == "UserPromptSubmit":
        h = hashlib.sha256((ev.get("prompt") or "").encode()).hexdigest()[:16]
        return f"{name}:{h}"
    if name in ("PreToolUse", "PostToolUse", "PreTaskExec", "PostTaskExec"):
        payload = {
            "t": ev.get("tool_name") or ev.get("task_name"),
            "i": ev.get("tool_input"),
        }
        if name.startswith("Post"):
            payload["r"] = ev.get("tool_response")
        h = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return f"{name}:{h}"
    if name in ("Stop", "agentStop", "stop"):
        # Export claim handles Stop idempotency; allow handlers to run.
        return ""
    h = hashlib.sha256(json.dumps(ev, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{name}:{h}"


def claim_event_once(sid: str, sig: str) -> bool:
    """True if this is the first time we see this event signature for the session."""
    if not sig:
        return True
    cleanup_claims()
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLAIMS_DIR / f"evt_{_safe_key(sid, sig)}.claim"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, f"{time.time()}\n".encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True  # fail-open: process the event rather than drop it


def workspace_hash(cwd: str) -> str:
    return hashlib.sha256(cwd.encode()).hexdigest()[:16]


def find_session_dir(sid: str, cwd: str | None) -> Path | None:
    root = Path.home() / ".kiro" / "sessions"
    if cwd:
        p = root / workspace_hash(cwd) / sid
        if (p / "messages.jsonl").exists():
            return p
    # fallback scan
    try:
        for d in root.iterdir():
            cand = d / sid
            if (cand / "messages.jsonl").exists():
                return cand
    except OSError:
        pass
    return None


def parse_traceparent(tp: str | None) -> tuple[str | None, str | None]:
    if not tp or not isinstance(tp, str):
        return None, None
    parts = tp.split("-")
    if len(parts) >= 3 and len(parts[1]) == 32 and len(parts[2]) == 16:
        return parts[1], parts[2]
    return None, None


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def enrich_from_session(sid: str, cwd: str | None, prompt: str | None, turn_hint: str | None = None) -> dict:
    """Pull reply/reasoning/tools/credits/model/title for the latest matching turn."""
    info = {
        "trace_id": None,
        "parent_span": None,
        "say": "",
        "reasoning": "",
        "tools": [],  # {name, input, output, toolCallId, start_iso, end_iso}
        "credits": None,
        "used_tools": [],
        "elapsed_ms": None,
        "request_ids": [],
        "context_usage_pct": None,
        "model_id": "auto",
        "title": "",
        "turn_id": turn_hint,
        "stop_reason": None,
        "agent_mode": None,
        "root_conversation_id": None,
        "session_meta": {},
    }
    sdir = find_session_dir(sid, cwd)
    if not sdir:
        return info
    sj = sdir / "session.json"
    try:
        meta_s = json.loads(sj.read_text())
        info["model_id"] = meta_s.get("modelId") or "auto"
        info["title"] = meta_s.get("title") or ""
        info["agent_mode"] = meta_s.get("agentMode")
        info["root_conversation_id"] = meta_s.get("rootConversationId")
        info["session_meta"] = {
            "title": meta_s.get("title"),
            "agentMode": meta_s.get("agentMode"),
            "rootConversationId": meta_s.get("rootConversationId"),
            "modelId": meta_s.get("modelId"),
            "workflowsEnabled": meta_s.get("workflowsEnabled"),
            "id": meta_s.get("id"),
        }
        try:
            info["session_meta"]["_kiro_workflow"] = extract_kiro_workflow_meta(sdir)
        except Exception:
            pass
    except Exception:
        pass

    msgs = read_jsonl(sdir / "messages.jsonl")
    # Find target turn: prefer turn_hint; else last turn whose user content matches prompt; else last turn
    turn_id = turn_hint
    if not turn_id and prompt:
        for o in reversed(msgs):
            p = o.get("payload") or {}
            if p.get("type") == "user" and (p.get("content") or "") == prompt:
                # find following turn_start
                idx = msgs.index(o)
                for o2 in msgs[idx:]:
                    p2 = o2.get("payload") or {}
                    if p2.get("type") == "turn_start":
                        turn_id = p2.get("turnId")
                        break
                tp = (p.get("_meta") or {}).get("traceparent")
                tid, sp = parse_traceparent(tp)
                info["trace_id"], info["parent_span"] = tid, sp
                break
    if not turn_id:
        for o in reversed(msgs):
            p = o.get("payload") or {}
            if p.get("type") == "turn_start":
                turn_id = p.get("turnId")
                break
    info["turn_id"] = turn_id

    # If traceparent not yet found, look up user message for this turn via turn_start userMessageTag
    if not info["trace_id"]:
        tag = None
        for o in msgs:
            p = o.get("payload") or {}
            if p.get("type") == "turn_start" and p.get("turnId") == turn_id:
                tag = p.get("userMessageTag")
                break
        for o in msgs:
            p = o.get("payload") or {}
            if p.get("type") == "user":
                kiro = (p.get("_meta") or {}).get("kiro") or {}
                if tag and kiro.get("userMessageTag") == tag:
                    tid, sp = parse_traceparent((p.get("_meta") or {}).get("traceparent"))
                    info["trace_id"], info["parent_span"] = tid, sp
                    if not prompt:
                        info["prompt"] = p.get("content") or ""
                    break
            if p.get("type") == "user" and turn_id is None:
                tid, sp = parse_traceparent((p.get("_meta") or {}).get("traceparent"))
                info["trace_id"], info["parent_span"] = tid, sp

    tool_calls = {}
    for o in msgs:
        p = o.get("payload") or {}
        if turn_id and p.get("turnId") and p.get("turnId") != turn_id and p.get("type") not in (
            "usage_summary",
            "session_metadata",
            "ContextualHookInvoked",
        ):
            # usage_summary has turnId; session_metadata uses executionId
            pass
        t = p.get("type")
        tid = p.get("turnId") or p.get("executionId")
        if turn_id and tid and tid != turn_id and t in (
            "assistant",
            "tool_call",
            "tool_result",
            "usage_summary",
            "turn_end",
        ):
            continue
        if t == "assistant":
            if p.get("operationType") == "Say":
                info["say"] = p.get("content") or info["say"]
            elif p.get("operationType") == "Reasoning":
                # keep last/concat brief
                r = p.get("content") or ""
                if r:
                    info["reasoning"] = r if not info["reasoning"] else info["reasoning"] + "\n" + r
        elif t == "tool_call":
            tool_calls[p.get("toolCallId")] = {
                "name": p.get("toolName"),
                "input": p.get("args") or {},
                "output": None,
                "toolCallId": p.get("toolCallId"),
                "ts": o.get("timestamp"),
            }
        elif t == "tool_result":
            tc = tool_calls.get(p.get("toolCallId"))
            if tc:
                tc["output"] = p.get("content")
                tc["end_ts"] = o.get("timestamp")
            else:
                tool_calls[p.get("toolCallId") or secrets.token_hex(4)] = {
                    "name": "?",
                    "input": {},
                    "output": p.get("content"),
                    "toolCallId": p.get("toolCallId"),
                    "ts": o.get("timestamp"),
                }
        elif t == "usage_summary" and (not turn_id or p.get("turnId") == turn_id):
            summaries = p.get("promptTurnSummaries") or []
            if summaries:
                info["credits"] = summaries[0].get("usage")
                info["used_tools"] = summaries[0].get("usedTools") or []
            info["elapsed_ms"] = p.get("elapsedTime")
            info["request_ids"] = p.get("requestIds") or []
        elif t == "session_metadata" and p.get("key") == "contextUsage":
            if not turn_id or p.get("executionId") == turn_id:
                val = p.get("value") or {}
                info["context_usage_pct"] = val.get("usagePercentage")
        elif t == "turn_end" and (not turn_id or p.get("turnId") == turn_id):
            info["stop_reason"] = p.get("stopReason")

    info["tools"] = list(tool_calls.values())
    return info


def wait_for_usage(sid: str, cwd: str | None, turn_id: str | None, timeout: float = POLL_SECONDS) -> dict:
    deadline = time.time() + timeout
    info = enrich_from_session(sid, cwd, None, turn_id)
    while time.time() < deadline:
        if info.get("credits") is not None and info.get("say") is not None:
            # credits found (0 is valid); also prefer having say text
            if info.get("credits") is not None:
                return info
        time.sleep(POLL_INTERVAL)
        info = enrich_from_session(sid, cwd, None, turn_id)
    return info


def send(spans: list) -> None:
    pk = os.environ["LANGFUSE_PUBLIC_KEY"]
    sk = os.environ["LANGFUSE_SECRET_KEY"]
    base = os.environ.get("LANGFUSE_BASE_URL", DEFAULT_BASE).rstrip("/")
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": [attr("service.name", "kiro")]},
                "scopeSpans": [{"scope": {"name": "kiro-hooks", "version": "1.0.0"}, "spans": spans}],
            }
        ]
    }
    req = urllib.request.Request(
        f"{base}/api/public/otel/v1/traces",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode(),
            "x-langfuse-ingestion-version": "4",
        },
    )
    urllib.request.urlopen(req, timeout=8).read()


def iso_to_ns(iso: str | None, fallback: int) -> int:
    if not iso:
        return fallback
    try:
        # 2026-11-05T06:17:40.477Z
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        from datetime import datetime

        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp() * 1e9)
    except Exception:
        return fallback



def _task_hook_attrs(sid: str, cwd: str | None, hook_name: str, ev: dict) -> list:
    """Shared Task1 identity for experimental task hooks."""
    info = enrich_from_session(sid, cwd, None, None)
    enrichment = build_enrichment(cwd, sid, info.get("session_meta") or {})
    lf_session = enrichment.get("langfuse_session_id") or f"unconfigured-workspace:{sid}"
    attrs = [
        attr(ATTR["session_id"], lf_session),
        attr(ATTR["trace_name"], "kiro-turn"),
        attr(ATTR["tags"], (enrichment.get("tags") or ["kiro"]) + ["experimental-task-hook"]),
        attr(ATTR["obs_type"], "span"),
        attr(ATTR["obs_input"], ev.get("tool_input") or ev),
        attr(ATTR["obs_output"], ev.get("tool_response") or {}),
        meta("hook_event", hook_name),
        meta("experimental", "true"),
        meta("kiro.conversation_session_id", sid),
    ]
    if enrichment.get("langfuse_user_id"):
        attrs.append(attr(ATTR["user_id"], enrichment["langfuse_user_id"]))
    else:
        attrs.append(meta("user.missing", "true"))
    for mk, mv in (enrichment.get("metadata") or {}).items():
        attrs.append(meta(mk, mv))
    return attrs


def build_spans(st: dict, info: dict, cwd: str | None) -> list:
    sid = st["session_id"]
    trace_id = info.get("trace_id") or st.get("trace_id") or secrets.token_hex(16)
    root_id = st.get("root_id") or info.get("parent_span") or secrets.token_hex(8)
    start = st.get("start") or now_ns()
    end = now_ns()
    prompt = st.get("prompt") or info.get("prompt") or ""
    enrichment = build_enrichment(
        cwd,
        sid,
        info.get("session_meta")
        or {
            "title": info.get("title"),
            "agentMode": info.get("agent_mode"),
            "rootConversationId": info.get("root_conversation_id"),
            "modelId": info.get("model_id"),
        },
    )
    # Langfuse session_id groups by workspace_key; Kiro conversation id stays in metadata.
    lf_session = enrichment.get("langfuse_session_id") or f"unconfigured-workspace:{sid}"
    common = [
        attr(ATTR["session_id"], lf_session),
        attr(ATTR["trace_name"], "kiro-turn"),
        attr(ATTR["tags"], enrichment.get("tags") or ["kiro", "local-langfuse", "metadata-incomplete"]),
    ]
    if enrichment.get("langfuse_user_id"):
        common.append(attr(ATTR["user_id"], enrichment["langfuse_user_id"]))
    else:
        common.append(meta("user.missing", "true"))
    for mk, mv in (enrichment.get("metadata") or {}).items():
        common.append(meta(mk, mv))
    # Preserve original Kiro conversation id explicitly (also in metadata).
    common.append(meta("kiro.conversation_session_id", sid))
    if info.get("title"):
        common.append(meta("session_title", info["title"]))
    if info.get("turn_id"):
        common.append(meta("turn_id", info["turn_id"]))

    spans = []

    # tool spans (prefer session file enrichment; fall back to live hook state)
    tools = info.get("tools") or []
    if not tools and st.get("tools_done"):
        tools = st["tools_done"]
    for i, t in enumerate(tools):
        t_start = iso_to_ns(t.get("ts"), start + i * 1000)
        t_end = iso_to_ns(t.get("end_ts"), t_start + 1_000_000)
        spans.append(
            {
                "traceId": trace_id,
                "spanId": secrets.token_hex(8),
                "parentSpanId": root_id,
                "name": t.get("name") or "tool",
                "kind": 1,
                "startTimeUnixNano": str(t_start),
                "endTimeUnixNano": str(t_end),
                "attributes": common
                + [
                    attr(ATTR["obs_type"], "tool"),
                    attr(ATTR["obs_input"], t.get("input") or {}),
                    attr(ATTR["obs_output"], t.get("output") if t.get("output") is not None else {}),
                    meta("tool_call_id", t.get("toolCallId") or ""),
                    meta("cwd", cwd or ""),
                ],
            }
        )

    # reasoning span
    if info.get("reasoning"):
        spans.append(
            {
                "traceId": trace_id,
                "spanId": secrets.token_hex(8),
                "parentSpanId": root_id,
                "name": "reasoning",
                "kind": 1,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(min(end, start + 1_000_000)),
                "attributes": common
                + [
                    attr(ATTR["obs_type"], "span"),
                    attr(ATTR["obs_output"], info["reasoning"]),
                    meta("kind", "reasoning"),
                ],
            }
        )

    # generation observation with credits
    usage = {}
    cost = {}
    if info.get("credits") is not None:
        usage = {"credits": info["credits"]}
        # Not USD — Langfuse cost_details is typically USD; we still send credits clearly labeled.
        cost = {"credits": info["credits"]}
    gen_attrs = common + [
        attr(ATTR["obs_type"], "generation"),
        attr(ATTR["obs_input"], prompt),
        attr(ATTR["obs_output"], info.get("say") or ""),
        attr(ATTR["model"], "kiro-auto" if (info.get("model_id") in (None, "auto")) else info.get("model_id")),
        attr("gen_ai.request.model", "kiro-auto" if (info.get("model_id") in (None, "auto")) else info.get("model_id")),
        meta("kiro.model_id", info.get("model_id") or "auto"),
        meta("kiro.credits_unit", "credit"),
    ]
    if usage:
        gen_attrs.append(attr(ATTR["usage"], json.dumps(usage)))
        gen_attrs.append(attr(ATTR["cost"], json.dumps(cost)))
        gen_attrs.append(meta("kiro.credits", str(info["credits"])))
    if info.get("context_usage_pct") is not None:
        gen_attrs.append(meta("kiro.context_usage_pct", str(info["context_usage_pct"])))
    if info.get("elapsed_ms") is not None:
        gen_attrs.append(meta("kiro.elapsed_ms", str(info["elapsed_ms"])))
    if info.get("request_ids"):
        gen_attrs.append(meta("kiro.request_ids", json.dumps(info["request_ids"])))
    if info.get("used_tools"):
        gen_attrs.append(meta("kiro.used_tools", json.dumps(info["used_tools"])))
    if info.get("stop_reason"):
        gen_attrs.append(meta("kiro.stop_reason", info["stop_reason"]))

    spans.append(
        {
            "traceId": trace_id,
            "spanId": secrets.token_hex(8),
            "parentSpanId": root_id,
            "name": "kiro-generation",
            "kind": 1,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": gen_attrs,
        }
    )

    # root agent span
    root_attrs = common + [
        attr(ATTR["obs_type"], "agent"),
        attr(ATTR["obs_input"], prompt),
        attr(ATTR["obs_output"], info.get("say") or ""),
        meta("cwd", cwd or ""),
        meta("tool_calls", str(len(tools))),
        meta("kiro.model_id", info.get("model_id") or "auto"),
    ]
    if info.get("credits") is not None:
        root_attrs.append(meta("kiro.credits", str(info["credits"])))
    if info.get("elapsed_ms") is not None:
        root_attrs.append(meta("kiro.elapsed_ms", str(info["elapsed_ms"])))
    spans.append(
        {
            "traceId": trace_id,
            "spanId": root_id,
            "name": "kiro-turn",
            "kind": 1,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": root_attrs,
        }
    )
    return spans


def export_turn(st: dict, cwd: str | None, wait: bool = True) -> None:
    sid = st["session_id"]
    # Resolve turn_id quickly, then atomically claim before any wait/send (concurrency).
    info = enrich_from_session(sid, cwd, st.get("prompt"), st.get("turn_id"))
    turn_id = info.get("turn_id") or st.get("turn_id") or f"anon-{(st.get('trace_id') or '')[:12]}"
    dedupe = load_state(sid)
    prev = set(dedupe.get("exported_turn_ids") or [])
    exported = set(st.get("exported_turn_ids") or [])
    if turn_id in prev or turn_id in exported:
        return
    if not try_claim_export(sid, turn_id):
        return
    try:
        if wait:
            info = wait_for_usage(sid, cwd, turn_id)
        # Re-check done in case a peer finished while we waited (stale-claim takeover path).
        done_path = CLAIMS_DIR / f"{_safe_key(sid, turn_id)}.done"
        if done_path.exists():
            return
        spans = build_spans(st, info, cwd)
        if spans:
            send(spans)
        mark_export_done(sid, turn_id)
        prev.add(turn_id)
        save_state(
            sid,
            {
                "session_id": sid,
                "exported_turn_ids": list(prev)[-50:],
                "last_exported_turn_id": turn_id,
            },
        )
    except Exception:
        release_claim(sid, turn_id)
        raise


def spawn_export(st: dict, cwd: str | None) -> None:
    """Detach a child that waits for usage_summary then exports. Never blocks the IDE."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = STATE_DIR / f"pending-{st.get('trace_id') or secrets.token_hex(8)}.json"
    payload_path.write_text(json.dumps({"st": st, "cwd": cwd}))
    script = str(Path(__file__).resolve())
    # Detached: double-fork style via nohup background
    cmd = (
        f"nohup python3 {json.dumps(script)} --export-pending {json.dumps(str(payload_path))} "
        f">>{json.dumps(str(STATE_DIR / 'export.log'))} 2>&1 &"
    )
    os.system(cmd)


def new_turn(sid: str, prompt: str = "", trace_id: str | None = None, root_id: str | None = None) -> dict:
    return {
        "session_id": sid,
        "trace_id": trace_id or secrets.token_hex(16),
        "root_id": root_id or secrets.token_hex(8),
        "start": now_ns(),
        "prompt": prompt,
        "tools": {},
        "tools_done": [],
        "n_tools": 0,
        "turn_id": None,
        "exported_turn_ids": [],
    }


def handle_event(ev: dict) -> None:
    name = ev.get("hook_event_name") or ""
    sid = ev.get("session_id") or "unknown"
    cwd = ev.get("cwd")
    load_config(cwd)
    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        return
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        log_err("missing LANGFUSE keys")
        return

    # Tolerate Kiro 1.6 double-firing the same hook event.
    sig = event_signature(ev)
    if not claim_event_once(sid, sig):
        return

    st = load_state(sid)
    # preserve exported_turn_ids across turns
    exported = list(st.get("exported_turn_ids") or [])

    if name == "UserPromptSubmit":
        # backfill previous incomplete turn
        if st.get("trace_id") and st.get("prompt") is not None and not st.get("closed"):
            try:
                export_turn(st, cwd, wait=False)
            except Exception as e:
                log_err(f"backfill: {type(e).__name__}: {e}")
        prompt = ev.get("prompt") or os.environ.get("USER_PROMPT", "")
        # try early traceparent from session (may already be written)
        info = enrich_from_session(sid, cwd, prompt)
        st = new_turn(sid, prompt, info.get("trace_id"), info.get("parent_span"))
        st["exported_turn_ids"] = exported
        st["turn_id"] = info.get("turn_id")
        save_state(sid, st)
        return

    if not st.get("trace_id"):
        st = new_turn(sid)
        st["exported_turn_ids"] = exported

    if name == "PreToolUse":
        tool = ev.get("tool_name", "?")
        st.setdefault("tools", {}).setdefault(tool, []).append([now_ns(), ev.get("tool_input") or {}])
        save_state(sid, st)
        return

    if name == "PostToolUse":
        tool = ev.get("tool_name", "?")
        q = st.setdefault("tools", {}).get(tool) or []
        start, pre_input = q.pop(0) if q else (now_ns(), {})
        if not q:
            st["tools"].pop(tool, None)
        st["n_tools"] = st.get("n_tools", 0) + 1
        tool_input = ev.get("tool_input") or pre_input
        st.setdefault("tools_done", []).append(
            {
                "name": tool,
                "input": tool_input,
                "output": ev.get("tool_response", {}),
                "ts": None,
                "end_ts": None,
            }
        )
        save_state(sid, st)
        return

    if name in ("PreTaskExec", "PostTaskExec"):
        # Experimental task hooks — observe only
        tool = ev.get("tool_name") or ev.get("task_name") or "task"
        spans = [
            {
                "traceId": st["trace_id"],
                "spanId": secrets.token_hex(8),
                "parentSpanId": st["root_id"],
                "name": f"task:{tool}",
                "kind": 1,
                "startTimeUnixNano": str(now_ns()),
                "endTimeUnixNano": str(now_ns()),
                "attributes": _task_hook_attrs(sid, cwd, name, ev),
            }
        ]
        try:
            send(spans)
        except Exception as e:
            log_err(f"task hook send: {type(e).__name__}: {e}")
        save_state(sid, st)
        return

    if name in ("Stop", "agentStop", "stop"):
        st["closed"] = True
        save_state(sid, st)
        spawn_export(st, cwd)
        return


def main(argv: list[str]) -> None:
    if len(argv) >= 3 and argv[1] == "--export-pending":
        payload = json.loads(Path(argv[2]).read_text())
        try:
            Path(argv[2]).unlink(missing_ok=True)
        except Exception:
            pass
        st = payload["st"]
        cwd = payload.get("cwd")
        load_config(cwd)
        if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
            return
        try:
            export_turn(st, cwd, wait=True)
        except Exception as e:
            log_err(f"export-pending: {type(e).__name__}: {e}")
        return

    ev = json.load(sys.stdin)
    handle_event(ev)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as e:
        log_err(f"{type(e).__name__}: {e}")
    sys.exit(0)
