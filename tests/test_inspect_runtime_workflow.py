"""Tests for InspectRuntimeEnvironmentWorkflow."""
from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.runner.core import build_inspect_runtime_artifacts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_builder(**env_overrides: object) -> dict:
    """Run the builder with optional os.environ patches."""
    if env_overrides:
        with patch.dict(os.environ, {k: v for k, v in env_overrides.items() if v is not None}, clear=False):
            return build_inspect_runtime_artifacts()
    return build_inspect_runtime_artifacts()


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

class TestSchemaFields:
    REQUIRED_KEYS = {
        "workflow",
        "python_version",
        "python_executable",
        "virtual_env",
        "platform",
        "installed_packages",
        "environment_variables",
        "port_conflicts",
        "summary",
        "tests_run",
        "tests_passed",
        "validation_status",
    }

    def test_all_required_fields_present(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert self.REQUIRED_KEYS.issubset(result.keys())

    def test_workflow_name(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["workflow"] == "InspectRuntimeEnvironmentWorkflow"

    def test_tests_run_is_false(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["tests_run"] is False

    def test_tests_passed_is_none(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["tests_passed"] is None

    def test_validation_status_is_not_applicable(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["validation_status"] == "not_applicable"

    def test_port_conflicts_is_none(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["port_conflicts"] is None


# ---------------------------------------------------------------------------
# python_version
# ---------------------------------------------------------------------------

class TestPythonVersion:
    def test_version_format(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert re.match(r"^\d+\.\d+\.\d+$", result["python_version"])

    def test_version_matches_current_interpreter(self) -> None:
        expected = ".".join(str(x) for x in sys.version_info[:3])
        result = build_inspect_runtime_artifacts()
        assert result["python_version"] == expected


# ---------------------------------------------------------------------------
# python_executable
# ---------------------------------------------------------------------------

class TestPythonExecutable:
    def test_executable_is_non_empty_string(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert isinstance(result["python_executable"], str)
        assert len(result["python_executable"]) > 0

    def test_executable_matches_current_interpreter(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["python_executable"] == sys.executable


# ---------------------------------------------------------------------------
# virtual_env
# ---------------------------------------------------------------------------

class TestVirtualEnv:
    def test_virtual_env_from_env_var(self) -> None:
        with patch.dict(os.environ, {"VIRTUAL_ENV": "/home/user/project/.venv"}, clear=False):
            result = build_inspect_runtime_artifacts()
        assert result["virtual_env"] == "/home/user/project/.venv"

    def test_virtual_env_detected_from_prefix_mismatch(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(sys, "prefix", "/path/to/venv"):
                with patch.object(sys, "base_prefix", "/usr"):
                    result = build_inspect_runtime_artifacts()
        assert result["virtual_env"] == "/path/to/venv"

    def test_virtual_env_null_when_absent(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(sys, "prefix", "/usr/local"):
                with patch.object(sys, "base_prefix", "/usr/local"):
                    result = build_inspect_runtime_artifacts()
        assert result["virtual_env"] is None

    def test_virtual_env_is_string_or_none(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["virtual_env"] is None or isinstance(result["virtual_env"], str)


# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------

class TestPlatform:
    def test_platform_non_empty_string(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert isinstance(result["platform"], str)
        assert len(result["platform"]) > 0

    def test_platform_includes_machine(self) -> None:
        import platform
        result = build_inspect_runtime_artifacts()
        assert platform.machine() in result["platform"]


# ---------------------------------------------------------------------------
# installed_packages
# ---------------------------------------------------------------------------

class TestInstalledPackages:
    def test_is_list(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert isinstance(result["installed_packages"], list)

    def test_non_empty_in_venv(self) -> None:
        result = build_inspect_runtime_artifacts()
        # We're running inside a venv with skilllayer installed
        assert len(result["installed_packages"]) > 0

    def test_each_entry_has_name_and_version(self) -> None:
        result = build_inspect_runtime_artifacts()
        for pkg in result["installed_packages"]:
            assert "name" in pkg, f"Missing 'name' in {pkg}"
            assert "version" in pkg, f"Missing 'version' in {pkg}"
            assert isinstance(pkg["name"], str)
            assert isinstance(pkg["version"], str)

    def test_sorted_alphabetically(self) -> None:
        result = build_inspect_runtime_artifacts()
        names = [p["name"].lower() for p in result["installed_packages"]]
        assert names == sorted(names), "installed_packages must be sorted A-Z by name"

    def test_no_duplicate_names(self) -> None:
        result = build_inspect_runtime_artifacts()
        names = [p["name"].lower() for p in result["installed_packages"]]
        assert len(names) == len(set(names)), "Duplicate package names found"

    def test_skilllayer_present(self) -> None:
        result = build_inspect_runtime_artifacts()
        names_lower = {p["name"].lower() for p in result["installed_packages"]}
        if "skilllayer" not in names_lower:
            import pytest
            pytest.skip("skilllayer not pip-installed in this environment (run pip install -e .)")
        assert "skilllayer" in names_lower


# ---------------------------------------------------------------------------
# environment_variables
# ---------------------------------------------------------------------------

class TestEnvironmentVariables:
    REQUIRED_ENV_KEYS = {"CI", "VIRTUAL_ENV", "PATH", "PYTHONPATH", "NODE_ENV", "ANTHROPIC_API_KEY"}

    def test_all_required_keys_present(self) -> None:
        result = build_inspect_runtime_artifacts()
        env = result["environment_variables"]
        for key in self.REQUIRED_ENV_KEYS:
            assert key in env, f"Missing key {key!r} in environment_variables"

    def test_api_key_is_bool(self) -> None:
        result = build_inspect_runtime_artifacts()
        val = result["environment_variables"]["ANTHROPIC_API_KEY"]
        assert isinstance(val, bool), f"ANTHROPIC_API_KEY must be bool, got {type(val)}"

    def test_api_key_present_returns_true(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-value"}, clear=False):
            result = build_inspect_runtime_artifacts()
        assert result["environment_variables"]["ANTHROPIC_API_KEY"] is True

    def test_api_key_absent_returns_false(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            result = build_inspect_runtime_artifacts()
        assert result["environment_variables"]["ANTHROPIC_API_KEY"] is False

    def test_api_key_value_never_in_output(self) -> None:
        secret = "sk-ant-super-secret-12345"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}, clear=False):
            result = build_inspect_runtime_artifacts()
        result_str = json.dumps(result)
        assert secret not in result_str, "Secret key value leaked into result"

    def test_missing_optional_vars_return_none(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CI", "PYTHONPATH", "NODE_ENV")}
        with patch.dict(os.environ, env, clear=True):
            result = build_inspect_runtime_artifacts()
        env_vars = result["environment_variables"]
        assert env_vars.get("CI") is None
        assert env_vars.get("PYTHONPATH") is None
        assert env_vars.get("NODE_ENV") is None

    def test_ci_var_forwarded_when_set(self) -> None:
        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            result = build_inspect_runtime_artifacts()
        assert result["environment_variables"]["CI"] == "true"

    def test_path_is_string_or_none(self) -> None:
        result = build_inspect_runtime_artifacts()
        val = result["environment_variables"]["PATH"]
        assert val is None or isinstance(val, str)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_non_empty_string(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 0

    def test_summary_contains_python_version(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert result["python_version"] in result["summary"]

    def test_summary_mentions_packages(self) -> None:
        result = build_inspect_runtime_artifacts()
        assert "package" in result["summary"]

    def test_summary_mentions_venv_when_active(self) -> None:
        with patch.dict(os.environ, {"VIRTUAL_ENV": "/tmp/testvenv"}, clear=False):
            result = build_inspect_runtime_artifacts()
        assert "testvenv" in result["summary"] or "venv" in result["summary"].lower()

    def test_summary_says_no_venv_when_absent(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(sys, "prefix", "/usr/local"):
                with patch.object(sys, "base_prefix", "/usr/local"):
                    result = build_inspect_runtime_artifacts()
        assert "no active venv" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Zero LLM calls (builder does not call SkillLayer)
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_builder_does_not_call_skilllayer(self) -> None:
        from skilllayer import SkillLayer
        with patch.object(SkillLayer, "run", side_effect=AssertionError("SkillLayer.run called")):
            result = build_inspect_runtime_artifacts()
        assert result["workflow"] == "InspectRuntimeEnvironmentWorkflow"


# ---------------------------------------------------------------------------
# Full runner integration (SkillLayer().run)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent


class TestRunnerIntegration:
    def test_inspect_runtime_via_runner(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "inspect runtime environment")
        assert result["success"] is True
        assert result.get("python_version") is not None

    def test_runner_result_has_no_llm_calls(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "inspect runtime environment")
        assert result.get("llm_calls", 0) == 0

    def test_runner_result_installed_packages_non_empty(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "inspect runtime environment")
        pkgs = result.get("installed_packages", [])
        assert isinstance(pkgs, list)
        assert len(pkgs) > 0

    def test_runner_result_api_key_is_bool(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "inspect runtime environment")
        env_vars = result.get("environment_variables", {})
        assert isinstance(env_vars.get("ANTHROPIC_API_KEY"), bool)


# ---------------------------------------------------------------------------
# CLI human output
# ---------------------------------------------------------------------------

def _make_inspect_runtime_result(**overrides: object) -> dict:
    base: dict = {
        "success": True,
        "repo_path": "/tmp/repo",
        "task": "inspect runtime",
        "workflow": "InspectRuntimeEnvironmentWorkflow",
        "macro_sequence": [],
        "validation_status": "not_applicable",
        "tests_run": False,
        "tests_passed": None,
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
        "python_version": "3.11.9",
        "python_executable": "/usr/bin/python3",
        "virtual_env": "/home/user/.venv",
        "platform": "macOS 14.5 arm64",
        "installed_packages": [{"name": "pytest", "version": "8.1.1"}],
        "environment_variables": {"ANTHROPIC_API_KEY": False},
        "port_conflicts": None,
        "summary": "Python 3.11.9 on macOS 14.5 arm64. 1 package(s) installed. venv: /home/user/.venv.",
    }
    base.update(overrides)
    return base


class TestCLIHumanOutput:
    def _capture(self, result: dict) -> str:
        from skilllayer.cli import print_human_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        return buf.getvalue()

    def test_python_version_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert "3.11.9" in out

    def test_python_executable_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert "/usr/bin/python3" in out

    def test_virtual_env_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert ".venv" in out

    def test_virtual_env_none_shows_none_label(self) -> None:
        out = self._capture(_make_inspect_runtime_result(virtual_env=None))
        assert "(none)" in out

    def test_platform_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert "macOS" in out

    def test_package_count_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert "1 package" in out

    def test_summary_shown(self) -> None:
        out = self._capture(_make_inspect_runtime_result())
        assert "Python 3.11.9" in out


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def _route(self, text: str) -> tuple[str, float] | None:
        from skilllayer.router.cascade import SkillRouter
        return SkillRouter()._match_task_type(text)  # type: ignore[attr-defined]

    def test_inspect_runtime_matches(self) -> None:
        assert self._route("inspect runtime environment") == ("inspect_runtime", 0.92)

    def test_inspect_runtime_bare_matches(self) -> None:
        assert self._route("inspect runtime") == ("inspect_runtime", 0.92)

    def test_python_version_matches(self) -> None:
        assert self._route("what python version is this") == ("inspect_runtime", 0.92)

    def test_list_installed_packages_matches(self) -> None:
        assert self._route("list installed packages") == ("inspect_runtime", 0.92)

    def test_inspect_environment_matches(self) -> None:
        assert self._route("inspect the python environment") == ("inspect_runtime", 0.92)

    def test_unrelated_does_not_match(self) -> None:
        result = self._route("run all tests")
        assert result != ("inspect_runtime", 0.92)
