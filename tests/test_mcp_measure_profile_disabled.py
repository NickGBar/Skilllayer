"""Regression tests for disabling MeasureMemoryUsageWorkflow and
ProfileCodeExecutionWorkflow over MCP (SkillLayer safety remediation Task 1).

Context: both workflows accept a target path that is not confined to the
authorized repository (an absolute path outside the repo is accepted as-is),
and their probe subprocesses suppress target failures via
``except BaseException: pass``, so a failing or malicious target could be
reported as a successful measurement/profiling run. Until that execution model
is fixed, neither workflow may be publicly reachable through MCP — by direct
tool, by generic ``skilllayer_run`` task routing, or via MCP discovery/schemas.

The underlying implementations (``build_measure_memory_artifacts`` /
``build_profile_execution_artifacts``) and their own tests
(test_measure_memory_workflow.py / test_profile_execution_workflow.py) are
untouched and still exercised directly; CLI/internal access is preserved
behind the existing ``--allow-internal`` / ``SKILLLAYER_ALLOW_INTERNAL``
override, unchanged from how every other internal-tier workflow behaves.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.skilllayer import SkillLayer
from src.skilllayer.config.defaults import WORKFLOW_METADATA, WORKFLOWS
from src.skilllayer.mcp_server import (
    MCP_DISABLED_WORKFLOWS,
    create_mcp_server,
    list_tool_schemas,
    skilllayer_list_workflows,
    skilllayer_measure_memory,
    skilllayer_profile_execution,
    skilllayer_run,
)
from src.skilllayer.runner.core import (
    build_measure_memory_artifacts,
    build_profile_execution_artifacts,
)
from src.skilllayer.security import InternalWorkflowBlockedError

TARGET_WORKFLOWS = ("MeasureMemoryUsageWorkflow", "ProfileCodeExecutionWorkflow")
TARGET_TOOL_NAMES = ("skilllayer_measure_memory", "skilllayer_profile_execution")


class _FakeMCPServer:
    """Minimal stand-in for mcp.server.fastmcp.FastMCP — records every
    function passed to .tool(), so create_mcp_server()'s real registration
    logic can be exercised without the optional `mcp` package installed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.registered: list = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn)
            return fn
        return decorator


def _write_marker_target(tmp_path: Path) -> tuple[Path, Path]:
    """A .py file that proves execution by writing a marker file if run."""
    marker = tmp_path / "EXECUTED_MARKER.txt"
    target = tmp_path / "would_execute.py"
    target.write_text(f"open(r'{marker}', 'w').write('ran')\n", encoding="utf-8")
    return target, marker


# ---------------------------------------------------------------------------
# Registry: workflows preserved, not deleted, marked internal
# ---------------------------------------------------------------------------

class TestRegistryPreserved:
    def test_workflows_still_registered(self):
        for name in TARGET_WORKFLOWS:
            assert name in WORKFLOWS, f"{name} must remain registered, not deleted"

    def test_marked_internal(self):
        for name in TARGET_WORKFLOWS:
            assert WORKFLOW_METADATA[name]["stability"] == "internal"

    def test_builders_still_importable_and_callable(self, tmp_path):
        # The underlying implementations must be untouched and directly usable.
        target = tmp_path / "ok.py"
        target.write_text("x = 1\n", encoding="utf-8")
        mem = build_measure_memory_artifacts(tmp_path, "ok.py")
        prof = build_profile_execution_artifacts(tmp_path, "ok.py")
        assert mem["workflow"] == "MeasureMemoryUsageWorkflow"
        assert prof["workflow"] == "ProfileCodeExecutionWorkflow"
        assert "peak_memory_mb" in mem
        assert "total_duration_ms" in prof


# ---------------------------------------------------------------------------
# MCP_DISABLED_WORKFLOWS: explicit, reasoned denial list
# ---------------------------------------------------------------------------

class TestMcpDisabledWorkflowsList:
    def test_both_present_with_reasons(self):
        for name in TARGET_WORKFLOWS:
            assert name in MCP_DISABLED_WORKFLOWS
            assert isinstance(MCP_DISABLED_WORKFLOWS[name], str)
            assert MCP_DISABLED_WORKFLOWS[name].strip()

    def test_rename_symbol_precedent_untouched(self):
        # Pre-existing entry must survive this change unmodified in kind.
        assert "RenameSymbolWorkflow" in MCP_DISABLED_WORKFLOWS


# ---------------------------------------------------------------------------
# MCP discovery (skilllayer_list_workflows) must reflect disablement
# ---------------------------------------------------------------------------

class TestMcpDiscovery:
    def test_both_report_mcp_enabled_false(self):
        by_name = {w["name"]: w for w in skilllayer_list_workflows()["workflows"]}
        for name in TARGET_WORKFLOWS:
            assert by_name[name]["mcp_enabled"] is False
            assert by_name[name]["mcp_disabled_reason"]

    def test_workflow_still_appears_in_registry_listing(self):
        # Discovery lists ALL registered workflows (registry, not MCP-exposure,
        # inventory) — both must still be present, just flagged disabled.
        names = {w["name"] for w in skilllayer_list_workflows()["workflows"]}
        for name in TARGET_WORKFLOWS:
            assert name in names

    def test_unrelated_workflow_still_mcp_enabled(self):
        by_name = {w["name"]: w for w in skilllayer_list_workflows()["workflows"]}
        assert by_name["GitLogWorkflow"]["mcp_enabled"] is True
        assert by_name["GitLogWorkflow"]["mcp_disabled_reason"] is None


# ---------------------------------------------------------------------------
# MCP tool schemas: neither tool listed; unrelated tools unaffected
# ---------------------------------------------------------------------------

class TestMcpSchemas:
    def test_neither_tool_in_schema_list(self):
        names = {t["name"] for t in list_tool_schemas()["tools"]}
        for tool_name in TARGET_TOOL_NAMES:
            assert tool_name not in names

    def test_unrelated_tools_still_in_schema_list(self):
        names = {t["name"] for t in list_tool_schemas()["tools"]}
        for tool_name in ("skilllayer_git_log", "skilllayer_search", "skilllayer_detect_secrets", "skilllayer_run"):
            assert tool_name in names

    def test_schema_count_reflects_removal(self):
        # 45 registered (38 original + 3 professional skill packs + 6 VTE
        # public tools from Milestone E), minus the 2 disabled (internal) tools.
        assert len(list_tool_schemas()["tools"]) == 45


# ---------------------------------------------------------------------------
# Real MCP registration (create_mcp_server): neither tool actually registered
# ---------------------------------------------------------------------------

class TestMcpServerRegistration:
    def test_neither_tool_registered_with_server(self, monkeypatch):
        import src.skilllayer.mcp_server as mcp_server_module

        fake_server = _FakeMCPServer("SkillLayer")
        monkeypatch.setattr(mcp_server_module, "FastMCP", lambda name: fake_server)

        mcp_server_module.create_mcp_server()

        registered_names = {fn.__name__ for fn in fake_server.registered}
        assert "skilllayer_measure_memory" not in registered_names
        assert "skilllayer_profile_execution" not in registered_names

    def test_unrelated_tools_still_registered_with_server(self, monkeypatch):
        import src.skilllayer.mcp_server as mcp_server_module

        fake_server = _FakeMCPServer("SkillLayer")
        monkeypatch.setattr(mcp_server_module, "FastMCP", lambda name: fake_server)

        mcp_server_module.create_mcp_server()

        registered_names = {fn.__name__ for fn in fake_server.registered}
        assert "skilllayer_git_log" in registered_names
        assert "skilllayer_run" in registered_names
        assert "skilllayer_search" in registered_names

    def test_registered_count_reflects_removal(self, monkeypatch):
        import src.skilllayer.mcp_server as mcp_server_module

        fake_server = _FakeMCPServer("SkillLayer")
        monkeypatch.setattr(mcp_server_module, "FastMCP", lambda name: fake_server)

        mcp_server_module.create_mcp_server()

        assert len(fake_server.registered) == 45


# ---------------------------------------------------------------------------
# Direct MCP tool access: impossible, structured denial, never executes
# ---------------------------------------------------------------------------

class TestDirectMcpToolDenial:
    def test_measure_memory_denied(self, tmp_path):
        target, _ = _write_marker_target(tmp_path)
        result = skilllayer_measure_memory(str(tmp_path), str(target))
        assert result["success"] is False
        assert result["error_code"] == "workflow_disabled_over_mcp"
        assert result["workflow"] == "MeasureMemoryUsageWorkflow"
        assert result["llm_calls"] == 0
        assert result["error"]

    def test_profile_execution_denied(self, tmp_path):
        target, _ = _write_marker_target(tmp_path)
        result = skilllayer_profile_execution(str(tmp_path), str(target))
        assert result["success"] is False
        assert result["error_code"] == "workflow_disabled_over_mcp"
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"
        assert result["llm_calls"] == 0
        assert result["error"]

    def test_measure_memory_never_executes_target(self, tmp_path):
        target, marker = _write_marker_target(tmp_path)
        skilllayer_measure_memory(str(tmp_path), str(target))
        assert not marker.exists(), "target script was executed during a denied MCP call"

    def test_profile_execution_never_executes_target(self, tmp_path):
        target, marker = _write_marker_target(tmp_path)
        skilllayer_profile_execution(str(tmp_path), str(target))
        assert not marker.exists(), "target script was executed during a denied MCP call"

    def test_denial_even_with_absolute_path_outside_repo(self, tmp_path):
        # The exact attack shape this remediation closes: an absolute path
        # pointing entirely outside the "repo_path" argument.
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        target, marker = _write_marker_target(outside_dir)

        r1 = skilllayer_measure_memory(str(repo_dir), str(target))
        r2 = skilllayer_profile_execution(str(repo_dir), str(target))

        assert r1["success"] is False and r2["success"] is False
        assert not marker.exists()

    def test_denial_does_not_write_baseline_files(self, tmp_path):
        # A denied call must not create .skilllayer/memory_baseline.json or
        # profile_baseline.json — no side effects at all, not even bookkeeping.
        target, _ = _write_marker_target(tmp_path)
        skilllayer_measure_memory(str(tmp_path), str(target))
        skilllayer_profile_execution(str(tmp_path), str(target))
        assert not (tmp_path / ".skilllayer" / "memory_baseline.json").exists()
        assert not (tmp_path / ".skilllayer" / "profile_baseline.json").exists()


# ---------------------------------------------------------------------------
# Generic skilllayer_run: cannot route to either workflow via task text
# ---------------------------------------------------------------------------

class TestGenericRunCannotReach:
    def test_profile_task_text_denied(self, tmp_path):
        target, marker = _write_marker_target(tmp_path)
        result = skilllayer_run(str(tmp_path), f"profile execution of {target.name}")
        assert result["success"] is False
        assert result["unsupported"] is True
        assert result["unsupported_reason"] == "workflow_disabled_over_mcp"
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"
        assert not marker.exists()

    def test_measure_memory_task_text_denied(self, tmp_path):
        target, marker = _write_marker_target(tmp_path)
        result = skilllayer_run(str(tmp_path), f"measure memory usage of {target.name}")
        assert result["success"] is False
        assert result["unsupported"] is True
        assert result["unsupported_reason"] == "workflow_disabled_over_mcp"
        assert result["workflow"] == "MeasureMemoryUsageWorkflow"
        assert not marker.exists()

    def test_does_not_route_to_a_different_workflow(self, tmp_path):
        # Denial must not silently fall through to some other workflow.
        target, _ = _write_marker_target(tmp_path)
        result = skilllayer_run(str(tmp_path), f"profile execution of {target.name}")
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"
        assert result["diff"] == ""
        assert result["tests_run"] is False

    def test_unrelated_generic_task_still_works(self, tmp_path):
        # A completely unrelated workflow must still route and execute normally.
        (tmp_path / "sample.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        result = skilllayer_run(str(tmp_path), "find function foo")
        assert result.get("unsupported_reason") != "workflow_disabled_over_mcp"


# ---------------------------------------------------------------------------
# CLI / internal-override access: unchanged, still gated the same way as
# every other internal-tier workflow
# ---------------------------------------------------------------------------

class TestCliInternalAccessUnchanged:
    def test_blocked_without_allow_internal(self, tmp_path):
        (tmp_path / "demo.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(InternalWorkflowBlockedError) as exc_info:
            SkillLayer().run(tmp_path, "profile execution of demo.py", dry_run=False, allow_internal=False)
        assert exc_info.value.workflow == "ProfileCodeExecutionWorkflow"

    def test_measure_memory_blocked_without_allow_internal(self, tmp_path):
        (tmp_path / "demo.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(InternalWorkflowBlockedError) as exc_info:
            SkillLayer().run(tmp_path, "measure memory usage of demo.py", dry_run=False, allow_internal=False)
        assert exc_info.value.workflow == "MeasureMemoryUsageWorkflow"

    def test_allowed_with_allow_internal_true(self, tmp_path):
        # The explicit maintainer override still reaches the real implementation.
        (tmp_path / "demo.py").write_text("x = 1 + 1\n", encoding="utf-8")
        result = SkillLayer().run(tmp_path, "profile execution of demo.py", dry_run=False, allow_internal=True)
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"
        assert "hotspots" in result
        assert "total_duration_ms" in result

    def test_measure_memory_allowed_with_allow_internal_true(self, tmp_path):
        (tmp_path / "demo.py").write_text("x = 1 + 1\n", encoding="utf-8")
        result = SkillLayer().run(tmp_path, "measure memory usage of demo.py", dry_run=False, allow_internal=True)
        assert result["workflow"] == "MeasureMemoryUsageWorkflow"
        assert "peak_memory_mb" in result


# ---------------------------------------------------------------------------
# MCP tool functions remain present in source (for later remediation) but
# unregistered — not deleted
# ---------------------------------------------------------------------------

class TestFunctionsPreservedNotDeleted:
    def test_functions_still_importable(self):
        assert callable(skilllayer_measure_memory)
        assert callable(skilllayer_profile_execution)

    def test_functions_do_not_reference_the_builders_anymore(self):
        # Defense-in-depth: confirm the wrapper genuinely never calls the
        # builder, rather than merely happening to short-circuit today.
        import inspect
        src_mem = inspect.getsource(skilllayer_measure_memory)
        src_prof = inspect.getsource(skilllayer_profile_execution)
        assert "build_measure_memory_artifacts(" not in src_mem
        assert "build_profile_execution_artifacts(" not in src_prof
