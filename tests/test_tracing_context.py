#!/usr/bin/env python3
"""Unit tests for Task1 tracing_context (no network, no real Langfuse writes).

Coverage areas:
- Field normalization and nullish handling
- Missing field detection and honest flagging
- Workspace grouping (cross-machine readiness)
- Workflow evidence vs chat sessions
- Git metadata discovery on real worktrees
- Remote URL sanitization (credentials stripping)
- Recipe content hash stability despite display names
- Parent-chat correlation (only when evidenced)
- Stable workspace session grouping across runs/machines
- cwd changes and path normalization
- No secret tracking or OS user inference
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / ".kiro" / "hooks"
sys.path.insert(0, str(HOOKS))

from tracing_context import (  # noqa: E402
    REQUIRED_FIELDS,
    build_enrichment,
    content_sha256,
    discover_git,
    load_tracing_context,
    normalize_field,
    resolve_workflow,
    resolve_workspace,
    extract_kiro_workflow_meta,
    _repo_from_remote,
)


class NormalizeTests(unittest.TestCase):
    def test_normalize_empty_and_nullish(self):
        for v in [None, "", "  ", "null", "None", "UNKNOWN", "n/a", "undefined"]:
            self.assertIsNone(normalize_field(v))

    def test_normalize_keeps_value(self):
        self.assertEqual(normalize_field(" chomu-local "), "chomu-local")

    def test_normalize_truncates_long_values(self):
        long_val = "x" * 300
        result = normalize_field(long_val)
        self.assertEqual(len(result), 198)

    def test_normalize_bool_to_string(self):
        self.assertEqual(normalize_field(True), "true")
        self.assertEqual(normalize_field(False), "false")

    def test_normalize_numbers(self):
        self.assertEqual(normalize_field(42), "42")
        self.assertEqual(normalize_field(3.14), "3.14")


class MissingHandlingTests(unittest.TestCase):
    def test_missing_fields_flagged_when_team_repo_git_absent(self):
        with tempfile.TemporaryDirectory() as td:
            kiro = Path(td) / ".kiro"
            kiro.mkdir()
            (kiro / "tracing.context.json").write_text(
                json.dumps(
                    {
                        "user_id": "chomu-local",
                        "team": None,
                        "workspace_key": "demo-ws",
                        "repo": None,
                        "git_sha": None,
                    }
                )
            )
            enr = build_enrichment(
                td,
                "sess_chat_1",
                {"title": "New Session", "agentMode": "vibe"},
            )
            self.assertFalse(enr["metadata_complete"])
            for req in ("workflow.name", "workflow.version", "team", "repo", "git.sha"):
                self.assertIn(req, enr["missing_fields"])
            self.assertNotIn("user", enr["missing_fields"])
            self.assertIn("metadata-incomplete", enr["tags"])
            self.assertEqual(enr["metadata"].get("metadata.complete"), "false")
            self.assertEqual(enr["langfuse_user_id"], "chomu-local")
            self.assertIsNone(enr["values"]["team"])

    def test_user_missing_when_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            kiro = Path(td) / ".kiro"
            kiro.mkdir()
            (kiro / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws"})
            )
            enr = build_enrichment(td, "sess", {"title": "Chat", "agentMode": "vibe"})
            self.assertIsNone(enr["langfuse_user_id"])
            self.assertIn("user", enr["missing_fields"])


class WorkspaceGroupingTests(unittest.TestCase):
    def test_config_workspace_key_preferred(self):
        ws = resolve_workspace("/tmp/anything", {"workspace_key": "shared-ws"})
        self.assertEqual(ws["key"], "shared-ws")
        self.assertTrue(ws["cross_machine_ready"])
        self.assertEqual(ws["source"], "config.workspace_key")

    def test_path_fallback_deterministic_and_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            a = resolve_workspace(td, {})
            b = resolve_workspace(td, {})
            self.assertEqual(a["key"], b["key"])
            self.assertTrue(str(a["key"]).startswith("pathhash:"))
            self.assertFalse(a["cross_machine_ready"])
            enr = build_enrichment(td, "sess_x", {"title": "New Session", "agentMode": "vibe"}, config={})
            self.assertIn("workspace-key-fallback", enr["tags"])
            self.assertEqual(enr["langfuse_session_id"], a["key"])

    def test_workspace_key_stable_across_runs(self):
        """Same workspace_key should produce same Langfuse session across runs."""
        with tempfile.TemporaryDirectory() as td:
            kiro = Path(td) / ".kiro"
            kiro.mkdir()
            (kiro / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "stable-team-ws", "user_id": "alice"})
            )
            enr1 = build_enrichment(td, "sess_run1", {"title": "Chat", "agentMode": "vibe"})
            enr2 = build_enrichment(td, "sess_run2", {"title": "Chat", "agentMode": "vibe"})
            self.assertEqual(enr1["langfuse_session_id"], enr2["langfuse_session_id"])
            self.assertEqual(enr1["langfuse_session_id"], "stable-team-ws")

    def test_different_workspace_keys_different_sessions(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            kiro1 = Path(td1) / ".kiro"
            kiro1.mkdir()
            (kiro1 / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws-project-a"})
            )
            kiro2 = Path(td2) / ".kiro"
            kiro2.mkdir()
            (kiro2 / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws-project-b"})
            )
            enr1 = build_enrichment(td1, "sess", {"title": "Chat", "agentMode": "vibe"})
            enr2 = build_enrichment(td2, "sess", {"title": "Chat", "agentMode": "vibe"})
            self.assertNotEqual(enr1["langfuse_session_id"], enr2["langfuse_session_id"])


class WorkflowEvidenceTests(unittest.TestCase):
    def _demo_recipe(self, root: Path, name: str = "sample-file-review") -> Path:
        wf = root / ".kiro" / "workflows"
        wf.mkdir(parents=True)
        path = wf / f"{name}.workflow.json"
        path.write_text(
            json.dumps(
                {
                    "name": name,
                    "description": "demo",
                    "inputs": {"target_file": "file"},
                    "steps": [{"type": "step", "id": "inspect", "agent": "wf-planner", "prompt": "x"}],
                },
                indent=2,
            )
        )
        return path

    def test_workflow_from_session_evidence_not_hardcoded_on_chat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recipe = self._demo_recipe(root)
            digest = content_sha256(recipe)
            chat = resolve_workflow(
                td,
                {"title": "New Session", "agentMode": "vibe"},
                "sess_chat",
            )
            self.assertIsNone(chat["name"])
            self.assertIsNone(chat["run_id"])

            step = resolve_workflow(
                td,
                {
                    "title": "sample-file-review · inspect",
                    "agentMode": "wf-planner",
                    "rootConversationId": "sess_parent",
                },
                "sess_step",
            )
            self.assertEqual(step["name"], "sample-file-review")
            self.assertEqual(step["step_id"], "inspect")
            self.assertIsNone(step["run_id"])
            self.assertEqual(step["step_session_id"], "sess_step")
            self.assertEqual(step["version"], f"sha256:{digest}")
            self.assertEqual(step["version_provenance"], "recipe-content-sha256")
            self.assertEqual(step["root_conversation_id"], "sess_parent")

    def test_run_id_distinct_from_workspace_session(self):
        with tempfile.TemporaryDirectory() as td:
            self._demo_recipe(Path(td))
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"user_id": "chomu-local", "workspace_key": "ws-1"})
            )
            enr = build_enrichment(
                td,
                "sess_step_abc",
                {
                    "title": "sample-file-review · inspect",
                    "agentMode": "wf-planner",
                    "rootConversationId": "sess_parent",
                },
            )
            self.assertEqual(enr["langfuse_session_id"], "ws-1")
            self.assertFalse(enr["metadata"].get("workflow.run_id"))
            self.assertEqual(enr["metadata"].get("workflow.run_id_provenance"), "native-workflowId-unavailable")
            self.assertEqual(enr["metadata"]["workflow.step_session_id"], "sess_step_abc")
            self.assertEqual(enr["metadata"]["kiro.session_id"], "sess_step_abc")
            self.assertEqual(enr["metadata"]["kiro.root_conversation_id"], "sess_parent")
            self.assertIn("team", enr["missing_fields"])
            self.assertIn("repo", enr["missing_fields"])
            self.assertIn("git.sha", enr["missing_fields"])
            self.assertNotIn("workflow.name", enr["missing_fields"])
            self.assertNotIn("workflow.version", enr["missing_fields"])


class TwoStepWorkflowTests(unittest.TestCase):
    """Tests for multi-step workflows sharing the same run_id."""

    def _two_step_recipe(self, root: Path) -> Path:
        wf = root / ".kiro" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        path = wf / "two-step-demo.workflow.json"
        path.write_text(
            json.dumps(
                {
                    "name": "two-step-demo",
                    "description": "Two-step workflow for testing",
                    "inputs": {"target_file": "file"},
                    "steps": [
                        {"type": "step", "id": "analyze", "agent": "wf-planner", "prompt": "analyze"},
                        {"type": "step", "id": "summarize", "agent": "wf-planner", "prompt": "summarize"},
                    ],
                },
                indent=2,
            )
        )
        return path

    def test_same_run_id_across_steps(self):
        """Both steps of a workflow should share the same run_id (native workflowId)."""
        with tempfile.TemporaryDirectory() as td:
            recipe = self._two_step_recipe(Path(td))
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws", "user_id": "tester"})
            )
            run_id = "wf_shared_run_abc123"
            enr_step1 = build_enrichment(
                td,
                "sess_step1",
                {"title": "two-step-demo · analyze", "agentMode": "wf-planner"},
                kiro_workflow={"workflow_id": run_id, "workflow_name": "two-step-demo", "node_id": "analyze"},
            )
            enr_step2 = build_enrichment(
                td,
                "sess_step2",
                {"title": "two-step-demo · summarize", "agentMode": "wf-planner"},
                kiro_workflow={"workflow_id": run_id, "workflow_name": "two-step-demo", "node_id": "summarize"},
            )
            self.assertEqual(enr_step1["metadata"]["workflow.run_id"], run_id)
            self.assertEqual(enr_step2["metadata"]["workflow.run_id"], run_id)
            self.assertNotEqual(enr_step1["metadata"]["workflow.step_session_id"], 
                               enr_step2["metadata"]["workflow.step_session_id"])
            self.assertEqual(enr_step1["metadata"]["workflow.step_id"], "analyze")
            self.assertEqual(enr_step2["metadata"]["workflow.step_id"], "summarize")

    def test_separate_executions_different_run_ids(self):
        """Separate workflow executions should have different run_ids."""
        with tempfile.TemporaryDirectory() as td:
            self._two_step_recipe(Path(td))
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws"})
            )
            enr_exec1 = build_enrichment(
                td,
                "sess_exec1_step1",
                {"title": "two-step-demo · analyze", "agentMode": "wf-planner"},
                kiro_workflow={"workflow_id": "wf_execution_1", "workflow_name": "two-step-demo"},
            )
            enr_exec2 = build_enrichment(
                td,
                "sess_exec2_step1",
                {"title": "two-step-demo · analyze", "agentMode": "wf-planner"},
                kiro_workflow={"workflow_id": "wf_execution_2", "workflow_name": "two-step-demo"},
            )
            self.assertNotEqual(enr_exec1["metadata"]["workflow.run_id"], 
                               enr_exec2["metadata"]["workflow.run_id"])


class GitDiscoveryTests(unittest.TestCase):
    def test_non_git_directory_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            info = discover_git(td)
            self.assertFalse(info["available"])
            self.assertIsNone(info["sha"])
            self.assertEqual(info["provenance"], "not-a-git-repository")

    def test_git_fixture_temp_repo(self):
        """Test git metadata on a real git worktree."""
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True, capture_output=True)
            (Path(td) / "f.txt").write_text("x")
            subprocess.run(["git", "add", "f.txt"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=td, check=True, capture_output=True)
            info = discover_git(td)
            self.assertTrue(info["available"])
            self.assertTrue(info["sha"] and len(info["sha"]) >= 7)
            self.assertEqual(info["provenance"], "git-rev-parse-HEAD")

    def test_git_sha_is_real_head_sha(self):
        """Verify the SHA matches the actual HEAD commit."""
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True, capture_output=True)
            (Path(td) / "f.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "test commit"], cwd=td, check=True, capture_output=True)
            expected_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=td, capture_output=True, text=True, check=True
            ).stdout.strip()
            info = discover_git(td)
            self.assertEqual(info["sha"], expected_sha[:len(info["sha"])])

    def test_git_worktree_from_subdirectory(self):
        """Git discovery should work from a subdirectory."""
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True, capture_output=True)
            subdir = Path(td) / "src" / "deep"
            subdir.mkdir(parents=True)
            (Path(td) / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=td, check=True, capture_output=True)
            info = discover_git(str(subdir))
            self.assertTrue(info["available"])
            self.assertIsNotNone(info["sha"])


class RemoteURLSanitizationTests(unittest.TestCase):
    """Tests for authenticated remote URL sanitization."""

    def test_https_url_extracts_repo(self):
        result = _repo_from_remote("https://github.com/org/repo.git")
        self.assertEqual(result, "org/repo")

    def test_https_url_without_git_suffix(self):
        result = _repo_from_remote("https://github.com/myorg/myrepo")
        self.assertEqual(result, "myorg/myrepo")

    def test_ssh_url_extracts_repo(self):
        result = _repo_from_remote("git@github.com:org/repo.git")
        self.assertEqual(result, "org/repo")

    def test_ssh_url_without_git_suffix(self):
        result = _repo_from_remote("git@gitlab.com:team/project")
        self.assertEqual(result, "team/project")

    def test_authenticated_https_url_sanitized(self):
        """Credentials in URL must not leak into repo identifier."""
        result = _repo_from_remote("https://user:secret_token@github.com/org/repo.git")
        self.assertNotIn("secret_token", result)
        self.assertNotIn("user:", result)
        self.assertIn("org/repo", result)

    def test_authenticated_https_complex_credentials(self):
        result = _repo_from_remote("https://oauth2:ghp_ABC123xyz@github.com/company/private-repo.git")
        self.assertNotIn("ghp_ABC123xyz", result)
        self.assertNotIn("oauth2", result)
        self.assertEqual(result, "company/private-repo")

    def test_empty_url_returns_none(self):
        self.assertIsNone(_repo_from_remote(""))
        self.assertIsNone(_repo_from_remote(None))

    def test_trailing_slashes_stripped(self):
        result = _repo_from_remote("https://github.com/org/repo/")
        self.assertEqual(result, "org/repo")


class CwdChangesTests(unittest.TestCase):
    """Tests for cwd path handling and changes."""

    def test_enrichment_with_none_cwd(self):
        enr = build_enrichment(None, "sess", {"title": "Chat", "agentMode": "vibe"})
        self.assertIsNone(enr["langfuse_session_id"])
        self.assertEqual(enr["workspace"]["source"], "missing")

    def test_enrichment_different_cwd_paths(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            enr1 = build_enrichment(td1, "sess", {"title": "Chat", "agentMode": "vibe"}, config={})
            enr2 = build_enrichment(td2, "sess", {"title": "Chat", "agentMode": "vibe"}, config={})
            self.assertNotEqual(enr1["langfuse_session_id"], enr2["langfuse_session_id"])

    def test_cwd_path_normalization(self):
        """Paths with trailing slashes and symlinks should normalize consistently."""
        with tempfile.TemporaryDirectory() as td:
            path_with_slash = td + "/"
            enr1 = build_enrichment(td, "sess", {"title": "Chat", "agentMode": "vibe"}, config={})
            enr2 = build_enrichment(path_with_slash, "sess", {"title": "Chat", "agentMode": "vibe"}, config={})
            self.assertEqual(enr1["workspace"]["path"], enr2["workspace"]["path"])


class RecipeHashStabilityTests(unittest.TestCase):
    """Tests for recipe content hash stability."""

    def test_recipe_hash_stable_despite_display_name_changes(self):
        """Recipe hash should be stable when same recipe is used with consistent title."""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".kiro" / "workflows"
            wf.mkdir(parents=True)
            recipe = wf / "my-workflow.workflow.json"
            recipe.write_text(json.dumps({
                "name": "my-workflow",
                "steps": [{"type": "step", "id": "step1", "prompt": "do stuff"}]
            }))
            digest1 = content_sha256(recipe)
            wf1 = resolve_workflow(
                td,
                {"title": "my-workflow · step1", "agentMode": "wf-planner"},
                "sess1",
            )
            wf2 = resolve_workflow(
                td,
                {"title": "my-workflow · step1", "agentMode": "wf-planner"},
                "sess2",
            )
            self.assertEqual(wf1["version"], f"sha256:{digest1}")
            self.assertEqual(wf1["version"], wf2["version"])
            self.assertEqual(wf2["version"], f"sha256:{digest1}")

    def test_recipe_hash_changes_with_content(self):
        """Recipe hash should change when content changes."""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".kiro" / "workflows"
            wf.mkdir(parents=True)
            recipe = wf / "evolving.workflow.json"
            recipe.write_text(json.dumps({"name": "evolving", "steps": [{"id": "v1"}]}))
            digest1 = content_sha256(recipe)
            recipe.write_text(json.dumps({"name": "evolving", "steps": [{"id": "v2"}]}))
            digest2 = content_sha256(recipe)
            self.assertNotEqual(digest1, digest2)


class ParentCorrelationTests(unittest.TestCase):
    """Tests for parent-chat correlation (only when evidenced)."""

    def test_root_conversation_id_preserved(self):
        """Parent chat ID should be preserved when provided."""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".kiro" / "workflows"
            wf.mkdir(parents=True)
            (wf / "test.workflow.json").write_text(json.dumps({"name": "test", "steps": []}))
            enr = build_enrichment(
                td,
                "sess_step",
                {
                    "title": "test · step1",
                    "agentMode": "wf-planner",
                    "rootConversationId": "parent_chat_123",
                },
            )
            self.assertEqual(enr["metadata"]["kiro.root_conversation_id"], "parent_chat_123")

    def test_no_parent_correlation_when_not_evidenced(self):
        """Should not invent parent correlation when not provided."""
        with tempfile.TemporaryDirectory() as td:
            enr = build_enrichment(
                td,
                "sess_chat",
                {"title": "New Chat", "agentMode": "vibe"},
            )
            self.assertNotIn("kiro.root_conversation_id", enr["metadata"])

    def test_multiple_workflows_same_chat_distinct_run_ids(self):
        """Multiple workflows in one chat must have distinct run_ids."""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".kiro" / "workflows"
            wf.mkdir(parents=True)
            (wf / "wf-a.workflow.json").write_text(json.dumps({"name": "wf-a", "steps": []}))
            (wf / "wf-b.workflow.json").write_text(json.dumps({"name": "wf-b", "steps": []}))
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "ws"})
            )
            enr1 = build_enrichment(
                td,
                "sess_step1",
                {"title": "wf-a · step", "agentMode": "wf-planner", "rootConversationId": "same_chat"},
                kiro_workflow={"workflow_id": "wf_run_1", "workflow_name": "wf-a"},
            )
            enr2 = build_enrichment(
                td,
                "sess_step2",
                {"title": "wf-b · step", "agentMode": "wf-planner", "rootConversationId": "same_chat"},
                kiro_workflow={"workflow_id": "wf_run_2", "workflow_name": "wf-b"},
            )
            self.assertEqual(enr1["metadata"]["kiro.root_conversation_id"], "same_chat")
            self.assertEqual(enr2["metadata"]["kiro.root_conversation_id"], "same_chat")
            self.assertNotEqual(enr1["metadata"]["workflow.run_id"], enr2["metadata"]["workflow.run_id"])


class EnvIsolationAndSecretsTests(unittest.TestCase):
    def test_no_os_user_inference_in_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["USER"] = "should-not-appear-as-langfuse-user"
            enr = build_enrichment(td, "sess", {"title": "New Session", "agentMode": "vibe"}, config={})
            self.assertIsNone(enr["langfuse_user_id"])
            blob = json.dumps(enr)
            self.assertNotIn("should-not-appear-as-langfuse-user", blob)

    def test_no_email_inference_in_enrichment(self):
        """Should not infer user from git email or environment."""
        with tempfile.TemporaryDirectory() as td:
            os.environ["EMAIL"] = "secret@company.com"
            os.environ["GIT_AUTHOR_EMAIL"] = "git@company.com"
            enr = build_enrichment(td, "sess", {"title": "Chat", "agentMode": "vibe"}, config={})
            self.assertIsNone(enr["langfuse_user_id"])
            blob = json.dumps(enr)
            self.assertNotIn("secret@company.com", blob)
            self.assertNotIn("git@company.com", blob)

    def test_secret_env_not_copied_into_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-TESTPUBLICVALUE000"
            os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-TESTSECRETVALUE000"
            (Path(td) / ".kiro").mkdir()
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"user_id": "chomu-local", "workspace_key": "ws"})
            )
            enr = build_enrichment(td, "sess", {"title": "New Session", "agentMode": "vibe"})
            blob = json.dumps(enr)
            self.assertNotIn("pk-lf-TESTPUBLICVALUE000", blob)
            self.assertNotIn("sk-lf-TESTSECRETVALUE000", blob)

    def test_load_tracing_context_reads_only_json_config(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".kiro").mkdir()
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"user_id": "chomu-local", "workspace_key": "ws"})
            )
            cfg = load_tracing_context(td)
            self.assertEqual(cfg["user_id"], "chomu-local")
            self.assertEqual(set(REQUIRED_FIELDS), set(REQUIRED_FIELDS))


class HookIntegrationSmokeTests(unittest.TestCase):
    def test_build_spans_uses_workspace_session_not_os_user(self):
        import importlib.util

        hook_path = HOOKS / "langfuse_kiro_hook.py"
        spec = importlib.util.spec_from_file_location("langfuse_kiro_hook_demo", hook_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".kiro").mkdir()
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"user_id": "chomu-local", "workspace_key": "ws-demo"})
            )
            st = {
                "session_id": "sess_step",
                "trace_id": "a" * 32,
                "root_id": "b" * 16,
                "start": 1_000_000_000,
                "prompt": "hello",
                "tools_done": [],
            }
            info = {
                "prompt": "hello",
                "say": "done",
                "title": "sample-file-review · inspect",
                "agent_mode": "wf-planner",
                "root_conversation_id": "sess_parent",
                "session_meta": {
                    "title": "sample-file-review · inspect",
                    "agentMode": "wf-planner",
                    "rootConversationId": "sess_parent",
                },
                "tools": [],
                "model_id": "claude-haiku-4.5",
                "turn_id": "turn1",
            }
            wf = Path(td) / ".kiro" / "workflows"
            wf.mkdir(parents=True, exist_ok=True)
            (wf / "sample-file-review.workflow.json").write_text(
                json.dumps({"name": "sample-file-review", "steps": []})
            )
            spans = mod.build_spans(st, info, td)
            self.assertTrue(spans)
            root = [s for s in spans if s["name"] == "kiro-turn"][0]
            attrs = {a["key"]: a["value"] for a in root["attributes"]}
            sess_val = attrs["langfuse.session.id"]["stringValue"]
            self.assertEqual(sess_val, "ws-demo")
            user_val = attrs["langfuse.user.id"]["stringValue"]
            self.assertEqual(user_val, "chomu-local")
            meta_keys = [a["key"] for a in root["attributes"] if a["key"].startswith("langfuse.observation.metadata.")]
            self.assertIn("langfuse.observation.metadata.kiro.conversation_session_id", meta_keys)
            self.assertIn("langfuse.observation.metadata.workflow.step_session_id", meta_keys)
            self.assertIn("langfuse.observation.metadata.workflow.run_id_provenance", meta_keys)
            self.assertIn("langfuse.observation.metadata.missing_fields", meta_keys)


class NativeWorkflowIdTests(unittest.TestCase):
    def test_native_workflow_id_preferred_over_step_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wf = root / ".kiro" / "workflows"
            wf.mkdir(parents=True)
            recipe = wf / "sample-file-review.workflow.json"
            recipe.write_text(json.dumps({"name": "sample-file-review", "steps": []}))
            (root / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"user_id": "chomu-local", "workspace_key": "ws-1"})
            )
            enr = build_enrichment(
                td,
                "sess_step_abc",
                {
                    "title": "review-checkout_helper · inspect",
                    "agentMode": "wf-planner",
                    "rootConversationId": "sess_parent",
                },
                kiro_workflow={
                    "workflow_id": "wf_abc123",
                    "workflow_name": "review-checkout_helper",
                    "node_id": "inspect",
                    "workflow_path": str(recipe),
                    "source": "messages._meta.kiro.workflow",
                },
            )
            self.assertEqual(enr["metadata"]["workflow.run_id"], "wf_abc123")
            self.assertEqual(enr["metadata"]["workflow.step_session_id"], "sess_step_abc")
            self.assertNotEqual(enr["metadata"]["workflow.run_id"], enr["metadata"]["workflow.step_session_id"])
            self.assertEqual(enr["metadata"]["workflow.name"], "review-checkout_helper")
            self.assertTrue(str(enr["metadata"]["workflow.version"]).startswith("sha256:"))
            self.assertNotIn("workflow.version", enr["missing_fields"])
            self.assertNotIn("workflow.name", enr["missing_fields"])


class GitIntegrationWithEnrichmentTests(unittest.TestCase):
    """Integration tests for git metadata flowing into enrichment."""

    def test_git_sha_flows_to_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True, capture_output=True)
            (Path(td) / "file.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=td, check=True, capture_output=True)
            (Path(td) / ".kiro").mkdir()
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "git-test-ws"})
            )
            enr = build_enrichment(td, "sess", {"title": "Chat", "agentMode": "vibe"})
            self.assertIsNotNone(enr["metadata"].get("git.sha"))
            self.assertEqual(enr["metadata"]["git.provenance"], "git-rev-parse-HEAD")
            self.assertEqual(enr["metadata"]["git.available"], "true")
            self.assertNotIn("git.sha", enr["missing_fields"])

    def test_git_remote_flows_to_repo(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/testorg/testrepo.git"], 
                          cwd=td, check=True, capture_output=True)
            (Path(td) / "file.txt").write_text("content")
            subprocess.run(["git", "add", "."], cwd=td, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=td, check=True, capture_output=True)
            (Path(td) / ".kiro").mkdir()
            (Path(td) / ".kiro" / "tracing.context.json").write_text(
                json.dumps({"workspace_key": "git-remote-ws"})
            )
            enr = build_enrichment(td, "sess", {"title": "Chat", "agentMode": "vibe"})
            self.assertEqual(enr["metadata"]["repo"], "testorg/testrepo")
            self.assertEqual(enr["metadata"]["repo.provenance"], "git-remote-origin")
            self.assertNotIn("repo", enr["missing_fields"])


if __name__ == "__main__":
    unittest.main()
