"""Router safety: vague/ambiguous prose must not route to write-capable workflows.

Regression for the report that "make the CLI faster" reached FixBugWorkflow (a
code-editing workflow), stopped only by the internal-tier gate. The no-match
fallback is now a read-only clarification response.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.router import SkillRouter

# Workflows that can modify files. Vague prose must never select one of these.
WRITE_CAPABLE_WORKFLOWS = {
    "FixBugWorkflow",
    "RenameSymbolWorkflow",
    "AddHelperWorkflow",
    "FixFailingTestWorkflow",
}

VAGUE_TASKS = [
    "make the CLI faster",
    "improve things",
    "fix it",
    "clean up the code",
    "make it better",
    "do something",
    "optimize the project",
    "please reticulate the splines",
    "handle the thing",
]


class TestVagueProseNeverRoutesToWriteWorkflow:
    @pytest.mark.parametrize("task", VAGUE_TASKS)
    def test_not_write_capable(self, task: str) -> None:
        decision = SkillRouter().route(task)
        assert decision.workflow not in WRITE_CAPABLE_WORKFLOWS, (
            f"vague task {task!r} routed to write-capable {decision.workflow}"
        )

    @pytest.mark.parametrize("task", VAGUE_TASKS)
    def test_routes_to_clarification(self, task: str) -> None:
        decision = SkillRouter().route(task)
        assert decision.task_type == "clarify_intent"
        assert decision.workflow == "ClarifyIntentWorkflow"
        assert decision.matched is False

    @pytest.mark.parametrize("task", VAGUE_TASKS)
    def test_low_confidence(self, task: str) -> None:
        assert SkillRouter().route(task).confidence == 0.0


class TestExplicitWriteIntentsStillRoute:
    """The hardening must not break genuine, explicit write requests."""

    def test_rename_still_routes(self) -> None:
        d = SkillRouter().route("rename parse_money to parse_decimal_money")
        assert d.workflow == "RenameSymbolWorkflow"
        assert d.matched is True

    def test_add_helper_still_routes(self) -> None:
        d = SkillRouter().route("add a helper function slugify")
        assert d.workflow == "AddHelperWorkflow"
        assert d.matched is True

    def test_fix_failing_test_still_routes(self) -> None:
        d = SkillRouter().route("fix the failing test in test_api.py")
        assert d.workflow == "FixFailingTestWorkflow"
        assert d.matched is True


class TestClarificationRunsReadOnly:
    def test_run_makes_no_edits_and_reports_failure(self, tmp_path: Path) -> None:
        (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
        before = (tmp_path / "keep.py").read_text(encoding="utf-8")

        result = SkillLayer().run(tmp_path, "make the CLI faster")

        assert result["workflow"] == "ClarifyIntentWorkflow"
        assert result["success"] is False
        assert result.get("llm_calls", 0) == 0
        # File is untouched — nothing was edited.
        assert (tmp_path / "keep.py").read_text(encoding="utf-8") == before

    def test_run_returns_suggestions(self, tmp_path: Path) -> None:
        result = SkillLayer().run(tmp_path, "improve things")
        artifacts = result.get("artifacts", {})
        assert artifacts.get("unsupported_reason") == "ambiguous_intent"
        assert len(artifacts.get("suggestions", [])) >= 3
        assert "did you mean" in (result.get("summary") or "").lower()

    def test_clarification_not_write_capable_at_runtime(self, tmp_path: Path) -> None:
        # The response workflow itself must not be one of the write-capable set.
        result = SkillLayer().run(tmp_path, "do something")
        assert result["workflow"] not in WRITE_CAPABLE_WORKFLOWS
