"""CLI dispatch behaviour for the four memory workflows.

Regression coverage for the bug where memory task strings routed to the right
workflow name but fell through the runner's dispatch chain to FixBugWorkflow's
"no automated fix is available for this task" message.

Outcome after the fix:
  * The three memory *write* workflows (save context, track decision, remember
    preferences) require structured input the free-text CLI cannot provide, so
    they return a clear, honest "available via MCP only" message — never the old
    misleading FixBug fallthrough.
  * RehydrateContextWorkflow is read-only and works end-to-end via the CLI.
  * ``run --repo`` is optional and defaults to the current working directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import build_save_context_artifacts

_MISLEADING = "no_supported_automated_fix"

_WRITE_CASES = [
    ("Save context snapshot", "SaveContextSnapshotWorkflow", "skilllayer_save_context"),
    ("Track decision", "TrackDecisionLogWorkflow", "skilllayer_track_decision"),
    ("Remember preference", "RememberUserPreferencesWorkflow", "skilllayer_remember_preferences"),
]


# ---------------------------------------------------------------------------
# Router — task strings must reach the correct task types
# ---------------------------------------------------------------------------

class TestRouter:
    """Route through the real ``route()`` entry point the CLI uses (it handles
    case-normalization), asserting both task_type and workflow."""

    def _route(self, text: str):
        return SkillRouter().route(text)

    def test_save_context_routes(self) -> None:
        d = self._route("Save context snapshot")
        assert d.task_type == "save_context"
        assert d.workflow == "SaveContextSnapshotWorkflow"

    def test_track_decision_routes(self) -> None:
        d = self._route("Track decision")
        assert d.task_type == "track_decision"
        assert d.workflow == "TrackDecisionLogWorkflow"

    def test_remember_preferences_routes(self) -> None:
        d = self._route("Remember preference")
        assert d.task_type == "remember_preferences"
        assert d.workflow == "RememberUserPreferencesWorkflow"

    def test_rehydrate_context_routes(self) -> None:
        d = self._route("Rehydrate context")
        assert d.task_type == "rehydrate_context"
        assert d.workflow == "RehydrateContextWorkflow"


# ---------------------------------------------------------------------------
# Write workflows via the runner — honest MCP-only message, not the FixBug one
# ---------------------------------------------------------------------------

class TestWriteWorkflowsUnsupportedViaCLI:
    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_routes_to_correct_workflow(self, tmp_path, task, workflow, mcp_tool) -> None:
        result = SkillLayer().run(tmp_path, task)
        assert result["workflow"] == workflow

    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_not_the_misleading_fixbug_message(self, tmp_path, task, workflow, mcp_tool) -> None:
        result = SkillLayer().run(tmp_path, task)
        artifacts = result.get("artifacts", {})
        assert artifacts.get("unsupported_reason") != _MISLEADING
        assert _MISLEADING not in (result.get("summary") or "")
        assert "no automated fix is available" not in (result.get("summary") or "").lower()

    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_honest_mcp_only_artifact(self, tmp_path, task, workflow, mcp_tool) -> None:
        result = SkillLayer().run(tmp_path, task)
        artifacts = result.get("artifacts", {})
        assert result["success"] is False
        assert artifacts.get("unsupported_reason") == "memory_write_requires_mcp"
        assert artifacts.get("cli_supported") is False
        assert artifacts.get("mcp_tool") == mcp_tool

    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_summary_names_tool_and_points_to_docs(self, tmp_path, task, workflow, mcp_tool) -> None:
        result = SkillLayer().run(tmp_path, task)
        summary = result.get("summary") or ""
        assert mcp_tool in summary
        assert "MCP" in summary
        assert "docs/CLAUDE_CODE_SETUP.md" in summary

    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_zero_llm_calls(self, tmp_path, task, workflow, mcp_tool) -> None:
        result = SkillLayer().run(tmp_path, task)
        assert result.get("llm_calls", 0) == 0

    @pytest.mark.parametrize("task,workflow,mcp_tool", _WRITE_CASES)
    def test_writes_no_memory_store(self, tmp_path, task, workflow, mcp_tool) -> None:
        SkillLayer().run(tmp_path, task)
        assert not (tmp_path / ".skilllayer").exists()


# ---------------------------------------------------------------------------
# Rehydrate — read-only, works end-to-end via the CLI
# ---------------------------------------------------------------------------

class TestRehydrateViaCLI:
    def test_no_store_is_a_warning_not_an_error(self, tmp_path) -> None:
        # An empty, never-saved-to repo is a valid, usable empty state — not a
        # failure. success=True/status="warning"/error_code=None; the guidance
        # that used to live in error_code="no_memory_store" now lives in
        # the warnings list instead.
        result = SkillLayer().run(tmp_path, "Rehydrate context")
        assert result["workflow"] == "RehydrateContextWorkflow"
        assert result["success"] is True
        assert result["status"] == "warning"
        assert result.get("artifacts", {}).get("error_code") is None
        assert result.get("artifacts", {}).get("status") == "warning"
        assert any("no .skilllayer" in w.lower() for w in result.get("warnings", []))

    def test_no_store_error_is_not_the_fixbug_message(self, tmp_path) -> None:
        result = SkillLayer().run(tmp_path, "Rehydrate context")
        assert _MISLEADING not in (result.get("summary") or "")

    def test_roundtrip_after_snapshot_exists(self, tmp_path) -> None:
        # Snapshot is written the way the MCP tool would, then read back via CLI.
        build_save_context_artifacts(tmp_path, "auth refactor in progress", ["rotate keys?"])
        result = SkillLayer().run(tmp_path, "Rehydrate context")
        assert result["success"] is True
        assert result["status"] == "healthy"
        assert result.get("artifacts", {}).get("error_code") is None

    def test_rehydrate_zero_llm_calls(self, tmp_path) -> None:
        result = SkillLayer().run(tmp_path, "Rehydrate context")
        assert result.get("llm_calls", 0) == 0


# ---------------------------------------------------------------------------
# CLI: --repo is optional and defaults to the current working directory
# ---------------------------------------------------------------------------

class TestRepoOptional:
    def test_parser_makes_repo_optional(self) -> None:
        from skilllayer.cli import build_parser

        args = build_parser().parse_args(["run", "--task", "Rehydrate context", "--json"])
        assert args.repo is None

    def test_handle_run_defaults_repo_to_cwd(self, tmp_path, monkeypatch, capsys) -> None:
        from skilllayer.cli import build_parser

        # A snapshot exists in the working directory we cd into.
        build_save_context_artifacts(tmp_path, "state", ["q"])
        monkeypatch.chdir(tmp_path)
        args = build_parser().parse_args(["run", "--task", "Rehydrate context", "--json"])
        exit_code = args.handler(args)
        payload = json.loads(capsys.readouterr().out)
        assert Path(payload["repo_path"]).resolve() == tmp_path.resolve()
        assert exit_code == 0
        assert payload["success"] is True
