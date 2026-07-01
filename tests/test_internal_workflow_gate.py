"""Stability-gate enforcement for the direct Python API (SkillLayer.run).

The CLI and MCP entry points gate INTERNAL workflows before calling run().
These tests pin the deeper guarantee: a direct SkillLayer().run() call is
blocked too, so the gate cannot be bypassed by any entry point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skilllayer import InternalWorkflowBlockedError, SkillLayer
from skilllayer.router import SkillRouter


# "Add helper ..." routes to AddHelperWorkflow, which is marked INTERNAL.
INTERNAL_TASK = "Add helper function normalize_value"
# "Find function ..." routes to FindFunctionWorkflow, which is STABLE.
STABLE_TASK = "Find function normalize_value"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def normalize_value(value):\n    return value\n", encoding="utf-8")
    return repo


def test_internal_task_routes_to_internal_workflow() -> None:
    # Guard the premise: if routing changes, the rest of the test is meaningless.
    assert SkillRouter().route(INTERNAL_TASK).workflow == "AddHelperWorkflow"


def test_direct_api_blocks_internal_workflow(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = (repo / "mod.py").read_text(encoding="utf-8")

    with pytest.raises(InternalWorkflowBlockedError) as excinfo:
        SkillLayer().run(repo, INTERNAL_TASK)

    error = excinfo.value
    # The error is explicit about what was blocked and why.
    assert error.workflow == "AddHelperWorkflow"
    assert "AddHelperWorkflow" in str(error)
    assert "internal" in str(error).lower()
    # And the repository was never touched.
    assert (repo / "mod.py").read_text(encoding="utf-8") == before


def test_maintainer_override_bypasses_gate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # allow_internal=True must not raise the stability gate; it should execute.
    result = SkillLayer().run(repo, INTERNAL_TASK, allow_internal=True)
    assert result["workflow"] == "AddHelperWorkflow"


def test_non_internal_workflow_is_not_blocked(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A stable workflow runs normally with no override.
    result = SkillLayer().run(repo, STABLE_TASK)
    assert result["workflow"] == "FindFunctionWorkflow"
