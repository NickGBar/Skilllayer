"""Tests for MonitorTestFlakinessWorkflow."""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from skilllayer.runner.core import (
    _extract_failure_summary,
    _extract_flakiness_params,
    _flakiness_median,
    build_monitor_flakiness_artifacts,
)
from skilllayer.verifier.test_runner import TestRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_REPO = Path("/tmp/fake_flakiness_repo")
_FAKE_CMD = ["python", "-m", "pytest", "-q"]


def _ok(duration_ms: int = 100) -> dict:
    return {
        "returncode": 0,
        "passed": 1,
        "failed": 0,
        "stdout": "1 passed in 0.10s",
        "stderr": "",
        "duration_ms": duration_ms,
    }


def _fail(msg: str = "AssertionError: assert 1 == 2", duration_ms: int = 120) -> dict:
    return {
        "returncode": 1,
        "passed": 0,
        "failed": 1,
        "stdout": f"FAILED tests/test_foo.py::test_bar - {msg}",
        "stderr": "",
        "duration_ms": duration_ms,
    }


def _run_builder(
    run_results: list[dict],
    test_id: str = "tests/test_foo.py::test_bar",
    runs: int | None = None,
) -> dict:
    """Run builder with TestRunner.detect and run_command mocked."""
    if runs is None:
        runs = len(run_results)
    with patch.object(TestRunner, "detect", return_value=_FAKE_CMD):
        with patch.object(TestRunner, "run_command", side_effect=run_results):
            return build_monitor_flakiness_artifacts(_FAKE_REPO, test_id, runs=runs)


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

class TestSchemaFields:
    REQUIRED_KEYS = {
        "workflow", "test_identifier", "runs", "passed", "failed",
        "pass_rate", "flaky", "deterministic", "failure_messages",
        "duration_ms", "median_duration_ms", "checked_at",
        "tests_run", "tests_passed", "validation_status",
    }

    def test_all_fields_present(self) -> None:
        result = _run_builder([_ok()])
        assert self.REQUIRED_KEYS.issubset(result.keys()), \
            f"Missing: {self.REQUIRED_KEYS - result.keys()}"

    def test_workflow_name(self) -> None:
        assert _run_builder([_ok()])["workflow"] == "MonitorTestFlakinessWorkflow"

    def test_tests_run_true(self) -> None:
        assert _run_builder([_ok()])["tests_run"] is True

    def test_validation_status(self) -> None:
        assert _run_builder([_ok()])["validation_status"] == "not_applicable"


# ---------------------------------------------------------------------------
# All passing runs
# ---------------------------------------------------------------------------

class TestAllPassing:
    def _result(self, n: int = 3) -> dict:
        return _run_builder([_ok(100)] * n, runs=n)

    def test_pass_rate_is_one(self) -> None:
        assert self._result()["pass_rate"] == 1.0

    def test_passed_equals_runs(self) -> None:
        r = self._result(4)
        assert r["passed"] == 4
        assert r["failed"] == 0

    def test_flaky_is_false(self) -> None:
        assert self._result()["flaky"] is False

    def test_deterministic_is_true(self) -> None:
        assert self._result()["deterministic"] is True

    def test_failure_messages_empty(self) -> None:
        assert self._result()["failure_messages"] == []

    def test_tests_passed_is_true(self) -> None:
        assert self._result()["tests_passed"] is True


# ---------------------------------------------------------------------------
# Mixed results (flaky)
# ---------------------------------------------------------------------------

class TestMixedResults:
    def _result(self) -> dict:
        # 2 pass, 1 fail → pass_rate = 2/3
        return _run_builder([_ok(), _fail(), _ok()], runs=3)

    def test_flaky_is_true(self) -> None:
        assert self._result()["flaky"] is True

    def test_deterministic_is_false(self) -> None:
        assert self._result()["deterministic"] is False

    def test_pass_rate_between_0_and_1(self) -> None:
        pr = self._result()["pass_rate"]
        assert 0.0 < pr < 1.0

    def test_passed_and_failed_sum_to_runs(self) -> None:
        r = self._result()
        assert r["passed"] + r["failed"] == r["runs"]

    def test_tests_passed_is_false(self) -> None:
        assert self._result()["tests_passed"] is False

    def test_pass_rate_correct(self) -> None:
        assert self._result()["pass_rate"] == pytest.approx(2 / 3, abs=0.001)


# ---------------------------------------------------------------------------
# All failing runs
# ---------------------------------------------------------------------------

class TestAllFailing:
    def _result(self, n: int = 3) -> dict:
        return _run_builder([_fail()] * n, runs=n)

    def test_pass_rate_is_zero(self) -> None:
        assert self._result()["pass_rate"] == 0.0

    def test_flaky_is_false(self) -> None:
        assert self._result()["flaky"] is False

    def test_deterministic_is_true(self) -> None:
        assert self._result()["deterministic"] is True

    def test_failed_equals_runs(self) -> None:
        r = self._result(4)
        assert r["failed"] == 4
        assert r["passed"] == 0

    def test_tests_passed_is_false(self) -> None:
        assert self._result()["tests_passed"] is False


# ---------------------------------------------------------------------------
# Failure message deduplication
# ---------------------------------------------------------------------------

class TestFailureMessageDeduplication:
    def test_same_message_three_times_stored_once(self) -> None:
        msg = "AssertionError: assert 1 == 2"
        results = [_fail(msg), _fail(msg), _fail(msg)]
        r = _run_builder(results, runs=3)
        assert len(r["failure_messages"]) == 1

    def test_distinct_messages_all_stored(self) -> None:
        results = [_fail("AssertionError: A"), _fail("ValueError: B"), _fail("AssertionError: A")]
        r = _run_builder(results, runs=3)
        assert len(r["failure_messages"]) == 2

    def test_failure_messages_is_list_of_strings(self) -> None:
        r = _run_builder([_fail(), _fail()], runs=2)
        assert isinstance(r["failure_messages"], list)
        assert all(isinstance(m, str) for m in r["failure_messages"])

    def test_no_duplicates_in_list(self) -> None:
        r = _run_builder([_fail("same error")] * 5, runs=5)
        assert len(r["failure_messages"]) == len(set(r["failure_messages"]))


# ---------------------------------------------------------------------------
# Runs capping at 20
# ---------------------------------------------------------------------------

class TestRunsCapping:
    def test_runs_capped_at_20(self) -> None:
        results = [_ok()] * 20
        with patch.object(TestRunner, "detect", return_value=_FAKE_CMD):
            with patch.object(TestRunner, "run_command", side_effect=results) as mock_run:
                r = build_monitor_flakiness_artifacts(_FAKE_REPO, "test.py", runs=999)
        assert r["runs"] == 20
        assert mock_run.call_count == 20

    def test_runs_minimum_is_1(self) -> None:
        r = _run_builder([_ok()], runs=0)
        assert r["runs"] == 1

    def test_exactly_20_runs_allowed(self) -> None:
        results = [_ok()] * 20
        r = _run_builder(results, runs=20)
        assert r["runs"] == 20


# ---------------------------------------------------------------------------
# Duration list
# ---------------------------------------------------------------------------

class TestDurationList:
    def test_duration_list_length_matches_runs(self) -> None:
        n = 4
        results = [_ok(i * 50) for i in range(1, n + 1)]
        r = _run_builder(results, runs=n)
        assert len(r["duration_ms"]) == n

    def test_duration_values_are_ints(self) -> None:
        r = _run_builder([_ok(150), _ok(200)], runs=2)
        assert all(isinstance(d, int) for d in r["duration_ms"])

    def test_median_duration_is_float(self) -> None:
        r = _run_builder([_ok(100), _ok(200)], runs=2)
        assert isinstance(r["median_duration_ms"], float)

    def test_median_correct_odd_count(self) -> None:
        r = _run_builder([_ok(100), _ok(200), _ok(300)], runs=3)
        assert r["median_duration_ms"] == pytest.approx(200.0)

    def test_median_correct_even_count(self) -> None:
        r = _run_builder([_ok(100), _ok(200)], runs=2)
        assert r["median_duration_ms"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# No test runner found
# ---------------------------------------------------------------------------

class TestNoTestRunner:
    def test_error_code_returned(self) -> None:
        with patch.object(TestRunner, "detect", return_value=None):
            r = build_monitor_flakiness_artifacts(_FAKE_REPO, "test.py", runs=3)
        assert r.get("error_code") == "no_test_runner"

    def test_runs_zero_when_no_runner(self) -> None:
        with patch.object(TestRunner, "detect", return_value=None):
            r = build_monitor_flakiness_artifacts(_FAKE_REPO, "test.py", runs=3)
        assert r["runs"] == 0

    def test_tests_run_false_when_no_runner(self) -> None:
        with patch.object(TestRunner, "detect", return_value=None):
            r = build_monitor_flakiness_artifacts(_FAKE_REPO, "test.py")
        assert r["tests_run"] is False


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_builder_never_calls_skilllayer_llm(self) -> None:
        from skilllayer import SkillLayer
        with patch.object(SkillLayer, "run", side_effect=AssertionError("SkillLayer.run called")):
            with patch.object(TestRunner, "detect", return_value=_FAKE_CMD):
                with patch.object(TestRunner, "run_command", return_value=_ok()):
                    r = build_monitor_flakiness_artifacts(_FAKE_REPO, "test.py", runs=1)
        assert r["workflow"] == "MonitorTestFlakinessWorkflow"


# ---------------------------------------------------------------------------
# Extract helpers
# ---------------------------------------------------------------------------

class TestExtractFlakinesParams:
    def test_default_runs(self) -> None:
        _, runs = _extract_flakiness_params("is this test flaky")
        assert runs == 5

    def test_extracts_n_times(self) -> None:
        _, runs = _extract_flakiness_params("run test 7 times")
        assert runs == 7

    def test_runs_capped_at_20(self) -> None:
        _, runs = _extract_flakiness_params("run test 99 times")
        assert runs == 20

    def test_extracts_test_path(self) -> None:
        tid, _ = _extract_flakiness_params("is tests/test_foo.py flaky")
        assert tid == "tests/test_foo.py"

    def test_extracts_double_colon_path(self) -> None:
        tid, _ = _extract_flakiness_params("run tests/test_foo.py::test_bar 3 times")
        assert tid == "tests/test_foo.py::test_bar"

    def test_default_test_id_is_dot(self) -> None:
        tid, _ = _extract_flakiness_params("is this test flaky")
        assert tid == "."


class TestExtractFailureSummary:
    def test_extracts_pytest_failed_line(self) -> None:
        stdout = "FAILED tests/foo.py::bar - AssertionError: assert 1 == 2"
        msg = _extract_failure_summary(stdout, "")
        assert msg is not None
        assert "AssertionError" in msg

    def test_extracts_error_type_line(self) -> None:
        msg = _extract_failure_summary("", "ValueError: bad value")
        assert msg is not None
        assert "ValueError" in msg

    def test_falls_back_to_stderr_last_line(self) -> None:
        msg = _extract_failure_summary("", "line1\nline2\nlast error line")
        assert msg is not None
        assert "last error line" in msg

    def test_returns_none_for_empty_output(self) -> None:
        assert _extract_failure_summary("", "") is None

    def test_message_truncated_to_300_chars(self) -> None:
        long_msg = "X" * 500
        msg = _extract_failure_summary("", long_msg)
        assert msg is None or len(msg) <= 300


class TestFlakinesMedian:
    def test_empty_list(self) -> None:
        assert _flakiness_median([]) == 0.0

    def test_single_element(self) -> None:
        assert _flakiness_median([42]) == 42.0

    def test_odd_count(self) -> None:
        assert _flakiness_median([1, 3, 5]) == 3.0

    def test_even_count(self) -> None:
        assert _flakiness_median([1, 2, 3, 4]) == 2.5

    def test_unsorted_input(self) -> None:
        assert _flakiness_median([5, 1, 3]) == 3.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def _route(self, text: str) -> tuple[str, float] | None:
        from skilllayer.router.cascade import SkillRouter
        return SkillRouter()._match_task_type(text)  # type: ignore[attr-defined]

    def test_is_this_test_flaky(self) -> None:
        assert self._route("is this test flaky") == ("monitor_flakiness", 0.92)

    def test_run_test_n_times(self) -> None:
        assert self._route("run test 5 times") == ("monitor_flakiness", 0.92)

    def test_check_test_reliability(self) -> None:
        assert self._route("check test reliability") == ("monitor_flakiness", 0.92)

    def test_monitor_test_flakiness(self) -> None:
        assert self._route("monitor test flakiness") == ("monitor_flakiness", 0.92)

    def test_how_often_does_this_test_fail(self) -> None:
        assert self._route("how often does this test fail") == ("monitor_flakiness", 0.92)

    def test_run_tests_n_times_report_pass_rate(self) -> None:
        assert self._route("run tests 3 times and report pass rate") == ("monitor_flakiness", 0.92)

    def test_unrelated_does_not_match(self) -> None:
        assert self._route("find function detect_dead_code") != ("monitor_flakiness", 0.92)


# ---------------------------------------------------------------------------
# Full runner integration (against the SkillLayer repo itself)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent
_STABLE_TEST = "tests/test_check_port_workflow.py::TestAvailablePort::test_host_default"


class TestRunnerIntegration:
    def test_stable_test_returns_pass_rate_1(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, f"run {_STABLE_TEST} 2 times")
        assert result["success"] is True
        assert result.get("pass_rate") == 1.0

    def test_stable_test_not_flaky(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, f"run {_STABLE_TEST} 2 times")
        assert result.get("flaky") is False

    def test_duration_list_length_matches_runs(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, f"run {_STABLE_TEST} 2 times")
        durations = result.get("duration_ms", [])
        assert len(durations) == result.get("runs", 0)

    def test_zero_llm_calls_in_runner(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, f"run {_STABLE_TEST} 2 times")
        assert result.get("llm_calls", 0) == 0


# ---------------------------------------------------------------------------
# CLI human output
# ---------------------------------------------------------------------------

def _make_flakiness_result(**overrides: object) -> dict:
    base: dict = {
        "success": True,
        "repo_path": "/tmp/repo",
        "task": "run test 3 times",
        "workflow": "MonitorTestFlakinessWorkflow",
        "macro_sequence": [],
        "validation_status": "not_applicable",
        "tests_run": True,
        "tests_passed": False,
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
        "test_identifier": "tests/test_foo.py::test_bar",
        "runs": 3,
        "passed": 2,
        "failed": 1,
        "pass_rate": 0.6667,
        "flaky": True,
        "deterministic": False,
        "failure_messages": ["AssertionError: assert 1 == 2"],
        "duration_ms": [100, 120, 110],
        "median_duration_ms": 110.0,
        "checked_at": "2026-01-01T12:00:00+00:00",
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

    def test_test_identifier_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        assert "test_foo.py" in out

    def test_runs_count_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        assert "3" in out

    def test_passed_count_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        assert "2" in out

    def test_flaky_verdict_shown(self) -> None:
        out = self._capture(_make_flakiness_result(flaky=True))
        assert "FLAKY" in out

    def test_deterministic_verdict_shown(self) -> None:
        out = self._capture(_make_flakiness_result(flaky=False, deterministic=True, pass_rate=1.0))
        assert "deterministic" in out

    def test_failure_message_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        assert "AssertionError" in out

    def test_pass_rate_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        # 0.6667 → "67%"
        assert "%" in out

    def test_median_duration_shown(self) -> None:
        out = self._capture(_make_flakiness_result())
        assert "110" in out
