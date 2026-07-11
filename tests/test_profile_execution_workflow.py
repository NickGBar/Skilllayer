"""Tests for ProfileCodeExecutionWorkflow (build_profile_execution_artifacts)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import _profile_rating, build_profile_execution_artifacts as _build_profile_execution_artifacts


def build_profile_execution_artifacts(
    repo: Path,
    target: str,
    *,
    include_stdlib: bool = False,
) -> dict[str, Any]:
    """Existing baseline-comparison cases opt in explicitly."""
    return _build_profile_execution_artifacts(
        repo,
        target,
        include_stdlib=include_stdlib,
        persist_baseline=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_py(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(src, "utf-8")
    return p


def _make_result(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": artifacts.get("error_code") is None,
        "workflow": artifacts.get("workflow"),
        "llm_calls": 0,
        **artifacts,
    }


# ---------------------------------------------------------------------------
# Unit: _profile_rating
# ---------------------------------------------------------------------------

class TestProfileRating:
    def test_fast_boundary(self):
        assert _profile_rating(0.0) == "fast"
        assert _profile_rating(99.9) == "fast"

    def test_normal_boundary(self):
        assert _profile_rating(100.0) == "normal"
        assert _profile_rating(999.9) == "normal"

    def test_slow_boundary(self):
        assert _profile_rating(1000.0) == "slow"
        assert _profile_rating(4999.9) == "slow"

    def test_very_slow_boundary(self):
        assert _profile_rating(5000.0) == "very_slow"
        assert _profile_rating(99999.0) == "very_slow"


# ---------------------------------------------------------------------------
# Unit: error cases (no subprocess)
# ---------------------------------------------------------------------------

class TestErrors:
    def test_missing_file_returns_error(self, tmp_path):
        result = build_profile_execution_artifacts(tmp_path, "nonexistent.py")
        assert result["error_code"] == "file_not_found"
        assert "nonexistent.py" in result["error"]

    def test_non_py_file_returns_error(self, tmp_path):
        f = tmp_path / "script.sh"
        f.write_text("#!/bin/bash\necho hi\n")
        result = build_profile_execution_artifacts(tmp_path, "script.sh")
        assert result["error_code"] == "not_python_file"
        assert ".sh" in result["error"]

    def test_non_py_txt_returns_error(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello", "utf-8")
        result = build_profile_execution_artifacts(tmp_path, "data.txt")
        assert result["error_code"] == "not_python_file"

    def test_error_result_has_all_fields(self, tmp_path):
        result = build_profile_execution_artifacts(tmp_path, "missing.py")
        for key in (
            "workflow", "error_code", "error", "target",
            "total_duration_ms", "cpu_time_ms", "function_calls",
            "hotspots", "rating", "baseline_delta_ms",
            "baseline_saved_at", "checked_at",
        ):
            assert key in result, f"Missing field: {key}"

    def test_workflow_name_in_error(self, tmp_path):
        result = build_profile_execution_artifacts(tmp_path, "missing.py")
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"


# ---------------------------------------------------------------------------
# Integration: real subprocess profiling
# ---------------------------------------------------------------------------

class TestRealSubprocess:
    @pytest.fixture
    def simple_target(self, tmp_path):
        return _write_py(tmp_path, "simple.py", "x = list(range(50_000))\n")

    def test_profiles_real_py_file(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        assert result.get("error_code") is None
        assert result["workflow"] == "ProfileCodeExecutionWorkflow"
        assert result["total_duration_ms"] >= 0
        assert result["cpu_time_ms"] >= 0
        assert result["function_calls"] >= 0

    def test_all_fields_present_on_success(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        required = {
            "workflow", "target", "total_duration_ms", "cpu_time_ms",
            "function_calls", "hotspots", "rating", "baseline_delta_ms",
            "baseline_saved_at", "checked_at", "tests_run", "tests_passed",
            "validation_status",
        }
        assert required.issubset(result.keys())

    def test_rating_is_valid(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        assert result["rating"] in ("fast", "normal", "slow", "very_slow")

    def test_hotspots_is_list(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        assert isinstance(result["hotspots"], list)

    def test_hotspots_bounded(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        assert len(result["hotspots"]) <= 10

    def test_hotspot_entry_fields(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        for h in result["hotspots"]:
            assert "function" in h
            assert "file" in h
            assert "line" in h
            assert "calls" in h
            assert "total_time_ms" in h
            assert "per_call_ms" in h

    def test_checked_at_is_iso8601(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        ts = result["checked_at"]
        assert "T" in ts and ("+00:00" in ts or "Z" in ts)

    def test_tests_run_false(self, tmp_path, simple_target):
        result = build_profile_execution_artifacts(tmp_path, "simple.py")
        assert result["tests_run"] is False
        assert result["tests_passed"] is None
        assert result["validation_status"] == "not_applicable"


# ---------------------------------------------------------------------------
# Integration: hotspots ordering and per_call accuracy
# ---------------------------------------------------------------------------

class TestHotspots:
    def test_hotspots_sorted_by_total_time_descending(self, tmp_path):
        # Two functions with different work amounts; slow one must rank first
        _write_py(
            tmp_path,
            "multi.py",
            "def slow(): return [i**2 for i in range(200_000)]\n"
            "def fast(): return sum(range(100))\n"
            "slow(); fast()\n",
        )
        result = build_profile_execution_artifacts(tmp_path, "multi.py")
        top = result["hotspots"]
        if len(top) >= 2:
            times = [h["total_time_ms"] for h in top]
            assert times == sorted(times, reverse=True), (
                "hotspots must be sorted by total_time_ms descending"
            )

    def test_per_call_ms_equals_total_divided_by_calls(self, tmp_path):
        _write_py(tmp_path, "t.py", "x = list(range(10_000))\n")
        result = build_profile_execution_artifacts(tmp_path, "t.py")
        for h in result["hotspots"]:
            calls = h["calls"]
            if calls > 0:
                expected = h["total_time_ms"] / calls
                assert abs(h["per_call_ms"] - expected) < 0.01, (
                    f"per_call_ms {h['per_call_ms']} != total/calls {expected}"
                )


# ---------------------------------------------------------------------------
# Integration: stdlib filtering
# ---------------------------------------------------------------------------

class TestStdlibFiltering:
    def test_stdlib_filtered_by_default(self, tmp_path):
        # Importing json causes stdlib calls; default filter should remove them
        _write_py(tmp_path, "uses_stdlib.py", "import json; json.dumps(list(range(1000)))\n")
        result = build_profile_execution_artifacts(tmp_path, "uses_stdlib.py")
        for h in result["hotspots"]:
            # No stdlib file paths (no <frozen ...>, no /lib/python...)
            assert not h["file"].startswith("<"), (
                f"stdlib frame leaked: {h['file']}"
            )

    def test_stdlib_included_when_flag_set(self, tmp_path):
        _write_py(tmp_path, "uses_stdlib.py", "import json; json.dumps(list(range(1000)))\n")
        result_default = build_profile_execution_artifacts(tmp_path, "uses_stdlib.py")
        result_with = build_profile_execution_artifacts(
            tmp_path, "uses_stdlib.py", include_stdlib=True
        )
        # include_stdlib=True should produce at least as many hotspots
        assert len(result_with["hotspots"]) >= len(result_default["hotspots"])


# ---------------------------------------------------------------------------
# Integration: baseline persistence
# ---------------------------------------------------------------------------

class TestBaselinePersistence:
    def test_baseline_none_on_first_run(self, tmp_path):
        _write_py(tmp_path, "s.py", "pass\n")
        result = build_profile_execution_artifacts(tmp_path, "s.py")
        assert result["baseline_delta_ms"] is None
        assert result["baseline_saved_at"] is None

    def test_baseline_file_saved_after_first_run(self, tmp_path):
        _write_py(tmp_path, "s.py", "pass\n")
        build_profile_execution_artifacts(tmp_path, "s.py")
        assert (tmp_path / ".skilllayer" / "profile_baseline.json").exists()

    def test_baseline_delta_present_on_second_run(self, tmp_path):
        _write_py(tmp_path, "s.py", "pass\n")
        build_profile_execution_artifacts(tmp_path, "s.py")
        result2 = build_profile_execution_artifacts(tmp_path, "s.py")
        assert result2["baseline_delta_ms"] is not None
        assert result2["baseline_saved_at"] is not None

    def test_baseline_not_applied_for_different_target(self, tmp_path):
        _write_py(tmp_path, "a.py", "pass\n")
        _write_py(tmp_path, "b.py", "pass\n")
        build_profile_execution_artifacts(tmp_path, "a.py")
        result = build_profile_execution_artifacts(tmp_path, "b.py")
        assert result["baseline_delta_ms"] is None


# ---------------------------------------------------------------------------
# Integration: gitignore
# ---------------------------------------------------------------------------

class TestGitignore:
    def test_gitignore_not_created(self, tmp_path):
        _write_py(tmp_path, "s.py", "pass\n")
        result = build_profile_execution_artifacts(tmp_path, "s.py")
        assert not (tmp_path / ".gitignore").exists()
        assert result["gitignore_suggestions"] == [".skilllayer/profile_baseline.json"]

    def test_existing_gitignore_not_modified(self, tmp_path):
        _write_py(tmp_path, "s.py", "pass\n")
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.tmp\n", "utf-8")
        build_profile_execution_artifacts(tmp_path, "s.py")
        build_profile_execution_artifacts(tmp_path, "s.py")
        assert gitignore.read_text("utf-8") == "*.tmp\n"


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _write_py(tmp_path, "t.py", "pass\n")
        with patch("src.skilllayer.runner.core.LLMClient") as mock_cls:
            build_profile_execution_artifacts(tmp_path, "t.py")
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_profile_x(self):
        assert self._route("profile src/heavy.py") == "profile_execution"

    def test_how_fast_is(self):
        assert self._route("how fast is loader.py?") == "profile_execution"

    def test_benchmark(self):
        assert self._route("benchmark the data pipeline") == "profile_execution"

    def test_profile_execution_of(self):
        assert self._route("profile execution of scripts/runner.py") == "profile_execution"

    def test_what_is_slow_in(self):
        assert self._route("what is slow in my_module.py?") == "profile_execution"

    def test_measure_execution_time_of(self):
        assert self._route("measure execution time of app/main.py") == "profile_execution"
