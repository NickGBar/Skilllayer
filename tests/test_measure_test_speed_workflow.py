"""Tests for MeasureTestSuiteSpeedWorkflow."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import (
    _parse_pytest_durations,
    _speed_rating,
    build_measure_test_speed_artifacts as _build_measure_test_speed_artifacts,
)
from skilllayer.verifier.test_runner import TestRunner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PYTEST_CMD = ["python", "-m", "pytest", "-q"]
_NPM_CMD = ["npm", "test"]


def build_measure_test_speed_artifacts(repo: Path) -> dict:
    """Legacy persistence cases in this module opt in explicitly."""
    return _build_measure_test_speed_artifacts(repo, persist_baseline=True)

_DURATIONS_SECTION = """\
============================== slowest 5 durations ==============================
5.00s call     tests/test_heavy.py::test_slow1
3.00s call     tests/test_medium.py::test_slow2
1.00s call     tests/test_light.py::test_slow3
0.50s call     tests/test_a.py::test_fast1
0.10s call     tests/test_b.py::test_fast2
(0 durations < 0.005s hidden.  Use -v to show these durations.)
"""

_DURATIONS_UNORDERED = """\
============================== slowest 5 durations ==============================
1.00s call     tests/test_a.py::test_mid
5.00s call     tests/test_b.py::test_slow
0.50s call     tests/test_c.py::test_fast
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_result(
    duration_ms: int = 8000,
    passed: int = 10,
    failed: int = 0,
    skipped: int = 0,
    include_durations: bool = True,
) -> dict:
    skipped_str = f", {skipped} skipped" if skipped else ""
    failed_str = f", {failed} failed" if failed else ""
    stdout = f"{'.' * passed}  {passed} passed{failed_str}{skipped_str} in {duration_ms / 1000:.2f}s\n"
    if include_durations:
        stdout += "\n" + _DURATIONS_SECTION
    return {
        "returncode": 1 if failed else 0,
        "passed": passed,
        "failed": failed,
        "test_count": passed + failed + skipped,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": "",
    }


def _run_builder(
    tmp_path: Path,
    run_result: dict,
    cmd: list[str] = _PYTEST_CMD,
) -> dict:
    with (
        patch.object(TestRunner, "detect", return_value=cmd),
        patch.object(TestRunner, "run_command", return_value=run_result),
    ):
        return build_measure_test_speed_artifacts(tmp_path)


# ---------------------------------------------------------------------------
# _speed_rating unit tests
# ---------------------------------------------------------------------------

class TestSpeedRating:
    def test_fast_under_10s(self):
        assert _speed_rating(9_999) == "fast"

    def test_fast_boundary(self):
        assert _speed_rating(0) == "fast"

    def test_normal_at_10s(self):
        assert _speed_rating(10_000) == "normal"

    def test_normal_at_59s(self):
        assert _speed_rating(59_999) == "normal"

    def test_slow_at_60s(self):
        assert _speed_rating(60_000) == "slow"

    def test_slow_at_299s(self):
        assert _speed_rating(299_999) == "slow"

    def test_very_slow_at_300s(self):
        assert _speed_rating(300_000) == "very_slow"

    def test_very_slow_large(self):
        assert _speed_rating(600_000) == "very_slow"


# ---------------------------------------------------------------------------
# _parse_pytest_durations unit tests
# ---------------------------------------------------------------------------

class TestParsePytestDurations:
    def test_parses_five_entries(self):
        results = _parse_pytest_durations(_DURATIONS_SECTION)
        assert len(results) == 5

    def test_sorted_descending(self):
        results = _parse_pytest_durations(_DURATIONS_SECTION)
        durations = [r["duration_ms"] for r in results]
        assert durations == sorted(durations, reverse=True)

    def test_unordered_input_sorted(self):
        results = _parse_pytest_durations(_DURATIONS_UNORDERED)
        durations = [r["duration_ms"] for r in results]
        assert durations == sorted(durations, reverse=True)

    def test_name_field_present(self):
        results = _parse_pytest_durations(_DURATIONS_SECTION)
        assert all("name" in r for r in results)
        assert all("duration_ms" in r for r in results)

    def test_correct_durations(self):
        results = _parse_pytest_durations(_DURATIONS_SECTION)
        assert results[0]["duration_ms"] == 5000
        assert results[1]["duration_ms"] == 3000

    def test_test_name_extracted(self):
        results = _parse_pytest_durations(_DURATIONS_SECTION)
        assert results[0]["name"] == "tests/test_heavy.py::test_slow1"

    def test_empty_output_returns_empty(self):
        assert _parse_pytest_durations("") == []

    def test_no_durations_section(self):
        assert _parse_pytest_durations("10 passed in 5.00s") == []

    def test_caps_at_five(self):
        many = (
            "============================== slowest 10 durations ==============================\n"
            + "".join(f"0.{10 - i:02d}s call tests/test_{i}.py::test_x{i}\n" for i in range(10))
        )
        results = _parse_pytest_durations(many)
        assert len(results) <= 5


# ---------------------------------------------------------------------------
# Builder — speed_rating scenarios
# ---------------------------------------------------------------------------

class TestBuildMeasureSpeedRating:
    def test_fast_suite_returns_fast_rating(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(duration_ms=8_000))
        assert result["speed_rating"] == "fast"

    def test_normal_suite_rating(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(duration_ms=30_000))
        assert result["speed_rating"] == "normal"

    def test_slow_suite_rating(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(duration_ms=120_000))
        assert result["speed_rating"] == "slow"

    def test_very_slow_suite_rating(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(duration_ms=400_000))
        assert result["speed_rating"] == "very_slow"

    def test_total_duration_ms_returned(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(duration_ms=5_500))
        assert result["total_duration_ms"] == 5_500

    def test_workflow_name(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result())
        assert result["workflow"] == "MeasureTestSuiteSpeedWorkflow"

    def test_checked_at_present(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result())
        assert result["checked_at"] is not None
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Builder — test counts
# ---------------------------------------------------------------------------

class TestBuildMeasureCounts:
    def test_passed_count(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(passed=42, failed=0))
        assert result["passed"] == 42

    def test_failed_count(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(passed=8, failed=2))
        assert result["failed"] == 2

    def test_skipped_count_parsed_from_stdout(self, tmp_path):
        run_result = _make_run_result(passed=7, failed=0, skipped=3)
        result = _run_builder(tmp_path, run_result)
        assert result["skipped"] == 3

    def test_test_count_from_run_result(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(passed=10, failed=0, skipped=0))
        assert result["test_count"] == 10

    def test_test_count_fallback_when_none(self, tmp_path):
        run_result = _make_run_result(passed=5, failed=1, skipped=2)
        run_result["test_count"] = None
        result = _run_builder(tmp_path, run_result)
        assert result["test_count"] == 8  # 5 + 1 + 2


# ---------------------------------------------------------------------------
# Builder — slowest_tests
# ---------------------------------------------------------------------------

class TestBuildMeasureSlowestTests:
    def test_slowest_tests_present_for_pytest(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(include_durations=True))
        assert isinstance(result["slowest_tests"], list)
        assert len(result["slowest_tests"]) > 0

    def test_slowest_tests_sorted_descending(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(include_durations=True))
        durations = [t["duration_ms"] for t in result["slowest_tests"]]
        assert durations == sorted(durations, reverse=True)

    def test_slowest_tests_max_five(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(include_durations=True))
        assert len(result["slowest_tests"]) <= 5

    def test_slowest_tests_have_name_and_duration(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(include_durations=True))
        for t in result["slowest_tests"]:
            assert "name" in t
            assert "duration_ms" in t
            assert isinstance(t["duration_ms"], int)

    def test_empty_slowest_tests_when_not_pytest(self, tmp_path):
        """Non-pytest runners: slowest_tests is always empty."""
        result = _run_builder(tmp_path, _make_run_result(include_durations=True), cmd=_NPM_CMD)
        assert result["slowest_tests"] == []

    def test_empty_slowest_tests_when_no_durations_section(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result(include_durations=False))
        assert result["slowest_tests"] == []

    def test_slowest_tests_unordered_input_sorted(self, tmp_path):
        run_result = _make_run_result(include_durations=False)
        run_result["stdout"] += "\n" + _DURATIONS_UNORDERED
        result = _run_builder(tmp_path, run_result)
        durations = [t["duration_ms"] for t in result["slowest_tests"]]
        assert durations == sorted(durations, reverse=True)


# ---------------------------------------------------------------------------
# Builder — baseline persistence
# ---------------------------------------------------------------------------

class TestBuildMeasureBaseline:
    def test_null_delta_when_no_baseline(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result())
        assert result["baseline_delta_ms"] is None

    def test_null_baseline_saved_at_when_no_prior_baseline(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result())
        assert result["baseline_saved_at"] is None

    def test_baseline_file_created_after_first_run(self, tmp_path):
        _run_builder(tmp_path, _make_run_result(duration_ms=5_000))
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        assert baseline_path.exists()

    def test_baseline_file_contains_duration(self, tmp_path):
        _run_builder(tmp_path, _make_run_result(duration_ms=5_000))
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        data = json.loads(baseline_path.read_text("utf-8"))
        assert data["duration_ms"] == 5_000

    def test_baseline_file_contains_test_count(self, tmp_path):
        _run_builder(tmp_path, _make_run_result(passed=15, failed=0))
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        data = json.loads(baseline_path.read_text("utf-8"))
        assert data["test_count"] == 15

    def test_baseline_file_contains_checked_at(self, tmp_path):
        _run_builder(tmp_path, _make_run_result())
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        data = json.loads(baseline_path.read_text("utf-8"))
        assert "checked_at" in data

    def test_delta_computed_on_second_run_slower(self, tmp_path):
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"duration_ms": 5_000, "checked_at": "2026-01-01T00:00:00+00:00", "test_count": 10}),
            "utf-8",
        )
        result = _run_builder(tmp_path, _make_run_result(duration_ms=7_000))
        assert result["baseline_delta_ms"] == 2_000

    def test_delta_computed_on_second_run_faster(self, tmp_path):
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"duration_ms": 10_000, "checked_at": "2026-01-01T00:00:00+00:00", "test_count": 10}),
            "utf-8",
        )
        result = _run_builder(tmp_path, _make_run_result(duration_ms=7_000))
        assert result["baseline_delta_ms"] == -3_000

    def test_delta_zero_when_same_duration(self, tmp_path):
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"duration_ms": 8_000, "checked_at": "2026-01-01T00:00:00+00:00", "test_count": 10}),
            "utf-8",
        )
        result = _run_builder(tmp_path, _make_run_result(duration_ms=8_000))
        assert result["baseline_delta_ms"] == 0

    def test_baseline_saved_at_reflects_prior_run(self, tmp_path):
        prior_ts = "2026-01-01T00:00:00+00:00"
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"duration_ms": 5_000, "checked_at": prior_ts, "test_count": 10}),
            "utf-8",
        )
        result = _run_builder(tmp_path, _make_run_result(duration_ms=6_000))
        assert result["baseline_saved_at"] == prior_ts

    def test_baseline_updated_after_run(self, tmp_path):
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"duration_ms": 5_000, "checked_at": "2026-01-01T00:00:00+00:00", "test_count": 10}),
            "utf-8",
        )
        _run_builder(tmp_path, _make_run_result(duration_ms=9_000))
        data = json.loads(baseline_path.read_text("utf-8"))
        assert data["duration_ms"] == 9_000

    def test_baseline_corrupt_file_handled(self, tmp_path):
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text("not valid json", "utf-8")
        # Should not raise; delta stays None
        result = _run_builder(tmp_path, _make_run_result(duration_ms=5_000))
        assert result["baseline_delta_ms"] is None


# ---------------------------------------------------------------------------
# Builder — gitignore
# ---------------------------------------------------------------------------

class TestBuildMeasureGitignore:
    def test_gitignore_not_created(self, tmp_path):
        result = _run_builder(tmp_path, _make_run_result())
        assert not (tmp_path / ".gitignore").exists()
        assert result["gitignore_suggestions"] == [".skilllayer/test_speed_baseline.json"]

    def test_existing_gitignore_not_modified(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n", "utf-8")
        _run_builder(tmp_path, _make_run_result())
        assert gitignore.read_text() == "*.pyc\n"

    def test_existing_suggested_entry_left_unchanged(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".skilllayer/test_speed_baseline.json\n", "utf-8")
        _run_builder(tmp_path, _make_run_result())
        content = gitignore.read_text()
        assert content.count(".skilllayer/test_speed_baseline.json") == 1


# ---------------------------------------------------------------------------
# Builder — no test runner
# ---------------------------------------------------------------------------

class TestBuildMeasureNoRunner:
    def test_error_code_when_no_runner(self, tmp_path):
        with (
            patch.object(TestRunner, "detect", return_value=None),
        ):
            result = build_measure_test_speed_artifacts(tmp_path)
        assert result["error_code"] == "no_test_runner"

    def test_tests_run_false_when_no_runner(self, tmp_path):
        with patch.object(TestRunner, "detect", return_value=None):
            result = build_measure_test_speed_artifacts(tmp_path)
        assert result["tests_run"] is False

    def test_slowest_tests_empty_when_no_runner(self, tmp_path):
        with patch.object(TestRunner, "detect", return_value=None):
            result = build_measure_test_speed_artifacts(tmp_path)
        assert result["slowest_tests"] == []


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_builder_never_calls_llm(self, tmp_path):
        from skilllayer.runner.core import SkillLayer
        fake_llm = object.__new__(type("FakeLLM", (), {"complete": _raise_if_called}))

        with (
            patch.object(TestRunner, "detect", return_value=_PYTEST_CMD),
            patch.object(TestRunner, "run_command", return_value=_make_run_result()),
        ):
            # builder is module-level, not going through SkillLayer.run — ensure no LLM usage
            result = build_measure_test_speed_artifacts(tmp_path)
        assert result["workflow"] == "MeasureTestSuiteSpeedWorkflow"


def _raise_if_called(*args, **kwargs):
    raise AssertionError("LLM called unexpectedly")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_how_fast_is_test_suite(self):
        assert self._route("how fast is the test suite") == "measure_test_speed"

    def test_measure_test_speed_phrase(self):
        assert self._route("measure test speed") == "measure_test_speed"

    def test_how_long_do_tests_take(self):
        assert self._route("how long do tests take") == "measure_test_speed"

    def test_is_test_suite_getting_slower(self):
        assert self._route("is the test suite getting slower") == "measure_test_speed"

    def test_benchmark_test_suite(self):
        assert self._route("benchmark test suite") == "measure_test_speed"

    def test_compare_test_speed_to_baseline(self):
        assert self._route("compare test speed to baseline") == "measure_test_speed"

    def test_test_suite_baseline(self):
        assert self._route("compare the test suite to its baseline") == "measure_test_speed"

    def test_test_suite_speed_phrase(self):
        assert self._route("what is the test suite speed") == "measure_test_speed"

    def test_n_times_goes_to_flakiness_not_speed(self):
        assert self._route("run test 5 times") == "monitor_flakiness"

    def test_flaky_goes_to_flakiness(self):
        assert self._route("is this test flaky") == "monitor_flakiness"

    def test_run_tests_goes_to_run_tests(self):
        assert self._route("run the tests") == "run_tests"

    def test_how_slow_is_test(self):
        assert self._route("how slow is the test suite") == "measure_test_speed"


# ---------------------------------------------------------------------------
# CLI human output
# ---------------------------------------------------------------------------

class TestCLIOutput:
    def _capture(self, result: dict) -> str:
        from skilllayer.cli import print_human_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        return buf.getvalue()

    def _make_result(self, **overrides) -> dict:
        base = {
            "workflow": "MeasureTestSuiteSpeedWorkflow",
            "success": True,
            "macro_sequence": ["RunFullSuite", "ParseDurations", "ComputeSpeedRating"],
            "validation_status": "not_applicable",
            "tests_run": True,
            "tests_passed": True,
            "dry_run": False,
            "tool_calls": 0,
            "llm_calls": 0,
            "logs_path": None,
            "total_duration_ms": 8_000,
            "test_count": 10,
            "passed": 10,
            "failed": 0,
            "skipped": 0,
            "slowest_tests": [
                {"name": "tests/test_heavy.py::test_slow", "duration_ms": 5000},
                {"name": "tests/test_light.py::test_fast", "duration_ms": 500},
            ],
            "speed_rating": "fast",
            "baseline_delta_ms": None,
            "baseline_saved_at": None,
            "checked_at": "2026-01-01T00:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_speed_rating_in_output(self):
        out = self._capture(self._make_result(speed_rating="fast"))
        assert "fast" in out

    def test_total_duration_ms_in_output(self):
        out = self._capture(self._make_result(total_duration_ms=8000))
        assert "8000" in out

    def test_slowest_tests_in_output(self):
        out = self._capture(self._make_result())
        assert "tests/test_heavy.py::test_slow" in out

    def test_baseline_null_message_shown(self):
        out = self._capture(self._make_result(baseline_delta_ms=None))
        assert "null" in out or "no prior" in out

    def test_baseline_positive_delta_shown(self):
        out = self._capture(self._make_result(baseline_delta_ms=2000))
        assert "+2000" in out or "2000" in out
        assert "slower" in out

    def test_baseline_negative_delta_shown(self):
        out = self._capture(self._make_result(baseline_delta_ms=-1500))
        assert "faster" in out

    def test_test_counts_in_output(self):
        out = self._capture(self._make_result(passed=8, failed=1, skipped=1))
        assert "8" in out and "1" in out

    def test_checked_at_in_output(self):
        out = self._capture(self._make_result())
        assert "2026-01-01" in out

    def test_no_slowest_tests_when_empty(self):
        out = self._capture(self._make_result(slowest_tests=[]))
        assert "slowest_tests" not in out
