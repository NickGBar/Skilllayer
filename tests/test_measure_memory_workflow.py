"""Tests for MeasureMemoryUsageWorkflow (build_measure_memory_artifacts)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import _memory_rating, build_measure_memory_artifacts as _build_measure_memory_artifacts


def build_measure_memory_artifacts(repo: Path, target: str) -> dict[str, Any]:
    """Existing baseline-comparison cases opt in explicitly."""
    return _build_measure_memory_artifacts(repo, target, persist_baseline=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(workflow_artifacts: dict[str, Any]) -> dict[str, Any]:
    """Wrap artifacts in the minimal envelope print_human_run expects."""
    return {
        "success": workflow_artifacts.get("error_code") is None,
        "workflow": workflow_artifacts.get("workflow"),
        "llm_calls": 0,
        **workflow_artifacts,
    }


def _write_py(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(src, "utf-8")
    return p


# ---------------------------------------------------------------------------
# Unit: _memory_rating
# ---------------------------------------------------------------------------

class TestMemoryRating:
    def test_efficient_boundary(self):
        assert _memory_rating(0.0) == "efficient"
        assert _memory_rating(49.9) == "efficient"

    def test_moderate_boundary(self):
        assert _memory_rating(50.0) == "moderate"
        assert _memory_rating(199.9) == "moderate"

    def test_heavy_boundary(self):
        assert _memory_rating(200.0) == "heavy"
        assert _memory_rating(499.9) == "heavy"

    def test_very_heavy_boundary(self):
        assert _memory_rating(500.0) == "very_heavy"
        assert _memory_rating(9999.0) == "very_heavy"


# ---------------------------------------------------------------------------
# Unit: error cases (no subprocess spawned)
# ---------------------------------------------------------------------------

class TestErrors:
    def test_missing_file_returns_error(self, tmp_path):
        result = build_measure_memory_artifacts(tmp_path, "nonexistent.py")
        assert result["error_code"] == "file_not_found"
        assert result["workflow"] == "MeasureMemoryUsageWorkflow"
        assert "nonexistent.py" in result["error"]

    def test_non_py_file_returns_error(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello", "utf-8")
        result = build_measure_memory_artifacts(tmp_path, "data.txt")
        assert result["error_code"] == "not_python_file"
        assert ".txt" in result["error"]

    def test_non_py_directory_path_returns_error(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        result = build_measure_memory_artifacts(tmp_path, "subdir")
        assert result["error_code"] in ("file_not_found", "not_python_file")

    def test_error_result_has_all_required_fields(self, tmp_path):
        result = build_measure_memory_artifacts(tmp_path, "missing.py")
        for key in (
            "workflow", "error_code", "error", "target",
            "peak_memory_mb", "baseline_memory_mb", "delta_memory_mb",
            "duration_ms", "tracemalloc_top", "rating",
            "baseline_delta_mb", "baseline_saved_at", "checked_at",
        ):
            assert key in result, f"Missing field: {key}"


# ---------------------------------------------------------------------------
# Integration: real subprocess runs
# ---------------------------------------------------------------------------

class TestRealSubprocess:
    @pytest.fixture
    def simple_target(self, tmp_path):
        return _write_py(tmp_path, "simple.py", "x = list(range(10_000))\n")

    def test_measures_real_py_file(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        assert result.get("error_code") is None
        assert result["workflow"] == "MeasureMemoryUsageWorkflow"
        assert result["peak_memory_mb"] > 0
        assert result["baseline_memory_mb"] > 0
        assert result["duration_ms"] >= 0

    def test_delta_nonnegative(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        assert result["delta_memory_mb"] >= 0.0

    def test_rating_is_valid_string(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        assert result["rating"] in ("efficient", "moderate", "heavy", "very_heavy")

    def test_checked_at_is_iso8601(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        ts = result["checked_at"]
        assert "T" in ts and ("+00:00" in ts or "Z" in ts)

    def test_tracemalloc_top_is_list(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        assert isinstance(result["tracemalloc_top"], list)

    def test_tracemalloc_top_bounded(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        assert len(result["tracemalloc_top"]) <= 10

    def test_tracemalloc_entry_fields(self, tmp_path, simple_target):
        result = build_measure_memory_artifacts(tmp_path, "simple.py")
        for entry in result["tracemalloc_top"]:
            assert "file" in entry
            assert "line" in entry
            assert "size_kb" in entry
            assert "count" in entry

    def test_tracemalloc_sorted_descending(self, tmp_path):
        # Allocate many different-sized objects to populate tracemalloc
        target = _write_py(
            tmp_path,
            "alloc.py",
            "a=[0]*50_000\nb=list(range(20_000))\nc={'k'+str(i):i for i in range(5_000)}\n",
        )
        result = build_measure_memory_artifacts(tmp_path, "alloc.py")
        top = result["tracemalloc_top"]
        if len(top) >= 2:
            sizes = [e["size_kb"] for e in top]
            assert sizes == sorted(sizes, reverse=True), "tracemalloc_top must be sorted by size_kb descending"


# ---------------------------------------------------------------------------
# Integration: baseline persistence
# ---------------------------------------------------------------------------

class TestBaselinePersistence:
    def test_baseline_none_on_first_run(self, tmp_path):
        _write_py(tmp_path, "script.py", "pass\n")
        result = build_measure_memory_artifacts(tmp_path, "script.py")
        assert result["baseline_delta_mb"] is None
        assert result["baseline_saved_at"] is None

    def test_baseline_saved_after_first_run(self, tmp_path):
        _write_py(tmp_path, "script.py", "pass\n")
        build_measure_memory_artifacts(tmp_path, "script.py")
        baseline_path = tmp_path / ".skilllayer" / "memory_baseline.json"
        assert baseline_path.exists()

    def test_baseline_delta_present_on_second_run(self, tmp_path):
        _write_py(tmp_path, "script.py", "pass\n")
        build_measure_memory_artifacts(tmp_path, "script.py")
        result2 = build_measure_memory_artifacts(tmp_path, "script.py")
        assert result2["baseline_delta_mb"] is not None
        assert result2["baseline_saved_at"] is not None

    def test_baseline_not_applied_for_different_target(self, tmp_path):
        _write_py(tmp_path, "a.py", "pass\n")
        _write_py(tmp_path, "b.py", "pass\n")
        build_measure_memory_artifacts(tmp_path, "a.py")
        result = build_measure_memory_artifacts(tmp_path, "b.py")
        # Baseline was for a.py, so b.py should have no delta
        assert result["baseline_delta_mb"] is None


# ---------------------------------------------------------------------------
# Integration: gitignore
# ---------------------------------------------------------------------------

class TestGitignore:
    def test_gitignore_not_created(self, tmp_path):
        _write_py(tmp_path, "script.py", "pass\n")
        result = build_measure_memory_artifacts(tmp_path, "script.py")
        assert not (tmp_path / ".gitignore").exists()
        assert result["gitignore_suggestions"] == [".skilllayer/memory_baseline.json"]

    def test_existing_gitignore_not_modified(self, tmp_path):
        _write_py(tmp_path, "script.py", "pass\n")
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.tmp\n", "utf-8")
        build_measure_memory_artifacts(tmp_path, "script.py")
        build_measure_memory_artifacts(tmp_path, "script.py")
        assert gitignore.read_text("utf-8") == "*.tmp\n"


# ---------------------------------------------------------------------------
# Return structure
# ---------------------------------------------------------------------------

class TestReturnStructure:
    def test_all_fields_present_success(self, tmp_path):
        _write_py(tmp_path, "t.py", "x = 1\n")
        result = build_measure_memory_artifacts(tmp_path, "t.py")
        required = {
            "workflow", "target", "peak_memory_mb", "baseline_memory_mb",
            "delta_memory_mb", "duration_ms", "tracemalloc_top", "rating",
            "baseline_delta_mb", "baseline_saved_at", "checked_at",
            "tests_run", "tests_passed", "validation_status",
        }
        assert required.issubset(result.keys())

    def test_tests_run_is_false(self, tmp_path):
        _write_py(tmp_path, "t.py", "pass\n")
        result = build_measure_memory_artifacts(tmp_path, "t.py")
        assert result["tests_run"] is False
        assert result["tests_passed"] is None
        assert result["validation_status"] == "not_applicable"


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _write_py(tmp_path, "t.py", "pass\n")
        with patch("src.skilllayer.runner.core.LLMClient") as mock_cls:
            build_measure_memory_artifacts(tmp_path, "t.py")
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        route = self.router.route(text)
        return route.task_type

    def test_measure_memory_usage(self):
        assert self._route("measure memory usage of src/app.py") == "measure_memory"

    def test_how_much_memory(self):
        assert self._route("how much memory does main.py use?") == "measure_memory"

    def test_profile_memory(self):
        assert self._route("profile memory for scripts/heavy.py") == "measure_memory"

    def test_check_memory_usage(self):
        assert self._route("check memory usage of data_pipeline.py") == "measure_memory"

    def test_is_memory_efficient(self):
        assert self._route("is my_module.py memory efficient?") == "measure_memory"

    def test_memory_profile(self):
        assert self._route("memory profile loader.py") == "measure_memory"
