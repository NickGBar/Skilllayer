"""Tests for RunTestsWorkflow — stable-tier verification.

Covers all cases required for stable promotion:
  1. Repo with pytest tests (passing)
  2. Repo with pytest tests (failing)
  3. Repo with unittest tests
  4. No test runner detected — explicit structured error
  5. Timeout handling — explicit TIMEOUT outcome, no false success
  6. success=False whenever tests fail or runner errors
  7. Structured JSON captures: pass_count, failure_count, duration_ms, outcome
  8. Schema consistency across all cases
  9. Zero LLM calls — purely deterministic subprocess execution
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.config.defaults import WORKFLOW_METADATA
from skilllayer.router import SkillRouter
from skilllayer.runner.core import build_run_tests_artifacts, build_run_tests_plan_artifacts
from skilllayer.verifier import TestRunner


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({
    "workflow",
    "test_command",
    "command_argv",
    "tests_run",
    "tests_passed",
    "validation_status",
    "test_count",
    "no_tests_discovered",
    "exit_code",
    "duration_ms",
    "pass_count",
    "failure_count",
    "timed_out",
    "failed_tests",
    "summary",
    "stdout_snippet",
    "stderr_snippet",
    "error_code",
    "outcome",
    "outcome_reason",
    "workflow_execution_success",
    "task_validation_success",
    "recommendation",
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pytest_pass_repo(tmp_path: Path) -> Path:
    """Repo with a single passing pytest test."""
    repo = tmp_path / "pytest_pass_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text(
        "def test_adds():\n    assert 1 + 1 == 2\n\ndef test_truthy():\n    assert True\n"
    )
    return repo


@pytest.fixture
def pytest_fail_repo(tmp_path: Path) -> Path:
    """Repo with one passing and one failing pytest test."""
    repo = tmp_path / "pytest_fail_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text(
        "def test_passes():\n    assert 1 + 1 == 2\n\ndef test_fails():\n    assert 1 == 2, 'deliberate failure'\n"
    )
    return repo


@pytest.fixture
def unittest_pass_repo(tmp_path: Path) -> Path:
    """Repo with a passing unittest test — run with explicit unittest command."""
    repo = tmp_path / "unittest_pass_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text(
        "import unittest\n\nclass TestSample(unittest.TestCase):\n    def test_passes(self):\n        self.assertEqual(1 + 1, 2)\n\nif __name__ == '__main__':\n    unittest.main()\n"
    )
    return repo


@pytest.fixture
def no_runner_repo(tmp_path: Path) -> Path:
    """Empty directory — no test files, no package.json test script."""
    repo = tmp_path / "no_runner_repo"
    repo.mkdir()
    return repo


@pytest.fixture
def hang_repo(tmp_path: Path) -> Path:
    """Repo with a test that sleeps indefinitely — used to trigger timeout."""
    repo = tmp_path / "hang_repo"
    repo.mkdir()
    (repo / "test_hangs.py").write_text(
        "import time\n\ndef test_hangs():\n    time.sleep(120)\n"
    )
    return repo


# ---------------------------------------------------------------------------
# Stability metadata
# ---------------------------------------------------------------------------


def test_workflow_stability_is_stable() -> None:
    meta = WORKFLOW_METADATA["RunTestsWorkflow"]
    assert meta["stability"] == "stable", (
        f"RunTestsWorkflow stability must be 'stable', got {meta['stability']!r}"
    )


def test_workflow_summary_documents_llm_calls() -> None:
    meta = WORKFLOW_METADATA["RunTestsWorkflow"]
    summary = meta["summary"].lower()
    assert "zero llm" in summary or "0 llm" in summary or "no llm" in summary or "deterministic" in summary, (
        f"summary must document LLM call behaviour: {meta['summary']!r}"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_run_tests_phrases() -> None:
    for phrase in [
        "run tests",
        "run the test suite",
        "run pytest",
        "execute tests",
        "check tests",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "run_tests", (
            f"expected run_tests for {phrase!r}, got {decision.task_type!r}"
        )
        assert decision.workflow == "RunTestsWorkflow"
        assert decision.matched is True


# ---------------------------------------------------------------------------
# Schema consistency — all scenarios must produce identical key sets
# ---------------------------------------------------------------------------


def test_schema_passing_run_has_all_required_fields(pytest_pass_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(pytest_pass_repo)
    artifacts = build_run_tests_artifacts(result)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"passing run: missing field {field!r}"


def test_schema_failing_run_has_all_required_fields(pytest_fail_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(pytest_fail_repo)
    artifacts = build_run_tests_artifacts(result)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"failing run: missing field {field!r}"


def test_schema_no_runner_has_all_required_fields(no_runner_repo: Path) -> None:
    runner = TestRunner()
    result = runner.run(no_runner_repo)
    artifacts = build_run_tests_artifacts(result)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"no runner: missing field {field!r}"


def test_schema_timeout_has_all_required_fields(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(result)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"timeout: missing field {field!r}"


def test_schema_plan_has_all_required_fields() -> None:
    artifacts = build_run_tests_plan_artifacts([sys.executable, "-m", "pytest", "-q"])
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"plan: missing field {field!r}"


def test_schema_keys_identical_across_all_cases(
    pytest_pass_repo: Path,
    pytest_fail_repo: Path,
    no_runner_repo: Path,
    hang_repo: Path,
) -> None:
    pass_result = build_run_tests_artifacts(TestRunner().run(pytest_pass_repo))
    fail_result = build_run_tests_artifacts(TestRunner().run(pytest_fail_repo))
    no_runner_result = build_run_tests_artifacts(TestRunner().run(no_runner_repo))
    timeout_result = build_run_tests_artifacts(
        TestRunner(timeout=1).run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    )
    plan_result = build_run_tests_plan_artifacts([sys.executable, "-m", "pytest", "-q"])
    keys = [
        set(pass_result.keys()),
        set(fail_result.keys()),
        set(no_runner_result.keys()),
        set(timeout_result.keys()),
        set(plan_result.keys()),
    ]
    assert keys[0] == keys[1] == keys[2] == keys[3] == keys[4], (
        f"Key sets differ across cases:\n{keys}"
    )


# ---------------------------------------------------------------------------
# Case 1: Pytest with passing tests
# ---------------------------------------------------------------------------


def test_pytest_pass_tests_run(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["tests_run"] is True


def test_pytest_pass_tests_passed(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["tests_passed"] is True


def test_pytest_pass_outcome(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["outcome"] == "PASSED"


def test_pytest_pass_error_code_is_none(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["error_code"] is None


def test_pytest_pass_pass_count_positive(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["pass_count"] >= 2, (
        f"expected >= 2 passing tests, got {result['artifacts']['pass_count']}"
    )


def test_pytest_pass_failure_count_zero(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["failure_count"] == 0


def test_pytest_pass_duration_ms_positive(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["duration_ms"] > 0


def test_pytest_pass_timed_out_false(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["timed_out"] is False


def test_pytest_pass_summary_non_empty(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert isinstance(result["artifacts"]["summary"], str)
    assert len(result["artifacts"]["summary"]) > 0


def test_pytest_pass_workflow_field(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["workflow"] == "RunTestsWorkflow"


# ---------------------------------------------------------------------------
# Case 2: Pytest with failing tests
# ---------------------------------------------------------------------------


def test_pytest_fail_tests_run(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["tests_run"] is True


def test_pytest_fail_tests_passed_false(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["tests_passed"] is False


def test_pytest_fail_outcome(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["outcome"] == "FAILED_TESTS"


def test_pytest_fail_failure_count_positive(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["failure_count"] >= 1


def test_pytest_fail_pass_count_still_captured(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    # 1 test passes, 1 fails
    assert result["artifacts"]["pass_count"] >= 1


def test_pytest_fail_duration_ms_positive(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["duration_ms"] > 0


def test_pytest_fail_timed_out_false(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["timed_out"] is False


# ---------------------------------------------------------------------------
# Case 3: Unittest
# ---------------------------------------------------------------------------


def test_unittest_pass_tests_run(unittest_pass_repo: Path) -> None:
    runner = TestRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", "."])
    result = runner.run(unittest_pass_repo)
    artifacts = build_run_tests_artifacts(result)
    assert artifacts["tests_run"] is True


def test_unittest_pass_outcome(unittest_pass_repo: Path) -> None:
    runner = TestRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", "."])
    result = runner.run(unittest_pass_repo)
    artifacts = build_run_tests_artifacts(result)
    assert artifacts["outcome"] == "PASSED"
    assert artifacts["tests_passed"] is True


def test_unittest_artifacts_schema(unittest_pass_repo: Path) -> None:
    runner = TestRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", "."])
    result = runner.run(unittest_pass_repo)
    artifacts = build_run_tests_artifacts(result)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"unittest: missing field {field!r}"


def test_unittest_duration_ms_positive(unittest_pass_repo: Path) -> None:
    runner = TestRunner(command=[sys.executable, "-m", "unittest", "discover", "-s", "."])
    result = runner.run(unittest_pass_repo)
    artifacts = build_run_tests_artifacts(result)
    assert artifacts["duration_ms"] > 0


# ---------------------------------------------------------------------------
# Case 4: No test runner detected — explicit error, no silent failure
# ---------------------------------------------------------------------------


def test_no_runner_success_false(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["success"] is False


def test_no_runner_error_code(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["error_code"] == "no_test_command_detected", (
        f"expected 'no_test_command_detected', got {result['artifacts']['error_code']!r}"
    )


def test_no_runner_outcome(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["outcome"] == "NO_TEST_COMMAND"


def test_no_runner_tests_run_false(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["tests_run"] is False


def test_no_runner_summary_non_empty(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert isinstance(result["artifacts"]["summary"], str)
    assert len(result["artifacts"]["summary"]) > 0


def test_no_runner_pass_count_zero(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["pass_count"] == 0


def test_no_runner_failure_count_zero(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["failure_count"] == 0


# ---------------------------------------------------------------------------
# Case 5: Timeout handling
# ---------------------------------------------------------------------------


def test_timeout_timed_out_field(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    assert result.get("timed_out") is True


def test_timeout_returncode_124(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    assert result.get("returncode") == 124


def test_timeout_outcome_is_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert artifacts["outcome"] == "TIMEOUT", (
        f"expected TIMEOUT outcome, got {artifacts['outcome']!r}"
    )


def test_timeout_error_code(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert artifacts["error_code"] == "test_run_timed_out"


def test_timeout_timed_out_artifact_true(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert artifacts["timed_out"] is True


def test_timeout_summary_mentions_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert "timed out" in artifacts["summary"].lower() or "timeout" in artifacts["summary"].lower(), (
        f"summary must mention timeout: {artifacts['summary']!r}"
    )


def test_timeout_not_classified_as_failed_tests(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert artifacts["outcome"] != "FAILED_TESTS", (
        "timeout must not be classified as FAILED_TESTS"
    )


def test_timeout_full_run_success_false(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run tests")
    assert result["success"] is False


def test_timeout_workflow_execution_success_false(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    raw = runner.run_command(hang_repo, [sys.executable, "-m", "pytest", "-q"])
    artifacts = build_run_tests_artifacts(raw)
    assert artifacts["workflow_execution_success"] is False


# ---------------------------------------------------------------------------
# Case 5b: timeout is configurable, and its message clearly states the bound
#
# Regression coverage: RunTestsWorkflow had a hardcoded 60s internal timeout,
# so any real repo with a large enough suite got a confusing TIMEOUT /
# exit_code 124 result instead of real test output, with a message ("test
# command timed out") that never said what the bound actually was.
# ---------------------------------------------------------------------------


def test_default_timeout_is_not_the_old_hardcoded_60s() -> None:
    assert TestRunner().timeout != 60
    assert TestRunner().timeout >= 300


def test_skilllayer_run_respects_explicit_timeout_kwarg(hang_repo: Path) -> None:
    """A caller can lower the effective timeout per-call without constructing a TestRunner."""
    result = SkillLayer().run(hang_repo, "run tests", test_timeout_seconds=1)
    assert result.get("artifacts", {}).get("outcome") == "TIMEOUT"


def test_skilllayer_run_respects_config_timeout(hang_repo: Path) -> None:
    """execution.test_timeout_seconds in config also lowers the effective timeout."""
    result = SkillLayer().run(
        hang_repo, "run tests", config={"execution": {"test_timeout_seconds": 1}}
    )
    assert result.get("artifacts", {}).get("outcome") == "TIMEOUT"


def test_explicit_kwarg_does_not_mutate_shared_test_runner(pytest_pass_repo: Path) -> None:
    """A per-call override must not leak into the SkillLayer instance's shared runner."""
    layer = SkillLayer()
    original_timeout = layer.test_runner.timeout
    layer.run(pytest_pass_repo, "run tests", test_timeout_seconds=1)
    assert layer.test_runner.timeout == original_timeout, (
        "per-call test_timeout_seconds must not mutate the shared self.test_runner"
    )


def test_injected_test_runner_timeout_still_respected_without_override(hang_repo: Path) -> None:
    """Constructor-injected TestRunner(timeout=...) must not be silently overridden
    by the new default/config resolution when the caller passes neither
    test_timeout_seconds nor a config override — the exact pattern the existing
    timeout tests above rely on."""
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run tests")
    assert result.get("artifacts", {}).get("outcome") == "TIMEOUT"


def test_timeout_message_states_the_bound_in_summary(hang_repo: Path) -> None:
    result = SkillLayer().run(hang_repo, "run tests", test_timeout_seconds=1)
    summary = result.get("summary") or ""
    assert "1s timeout" in summary, f"summary must clearly state the bound: {summary!r}"
    assert "exceeded" in summary.lower(), f"summary must say the suite exceeded the bound: {summary!r}"


def test_timeout_message_states_the_bound_in_outcome_reason(hang_repo: Path) -> None:
    result = SkillLayer().run(hang_repo, "run tests", test_timeout_seconds=1)
    reason = result.get("artifacts", {}).get("outcome_reason") or ""
    assert "1s" in reason, f"outcome_reason must clearly state the bound: {reason!r}"


def test_timeout_message_no_longer_the_old_unclear_text(hang_repo: Path) -> None:
    result = SkillLayer().run(hang_repo, "run tests", test_timeout_seconds=1)
    summary = result.get("summary") or ""
    assert summary != "Test command timed out before completion.", (
        "the old unclear timeout message must be replaced with one that states the bound"
    )


# ---------------------------------------------------------------------------
# Requirement 4: success=False whenever tests fail — never True on failure
# ---------------------------------------------------------------------------


def test_success_false_when_tests_fail(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["success"] is False, (
        "success must be False when tests fail"
    )


def test_success_true_when_tests_pass(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["success"] is True


def test_success_false_when_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["success"] is False


def test_success_false_when_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run tests")
    assert result["success"] is False


def test_task_validation_success_true_on_pass(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["artifacts"]["task_validation_success"] is True


def test_task_validation_success_false_on_fail(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["artifacts"]["task_validation_success"] is False


# ---------------------------------------------------------------------------
# Structured output fields: pass_count, failure_count, duration_ms
# ---------------------------------------------------------------------------


def test_pass_count_is_integer(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert isinstance(result["artifacts"]["pass_count"], int)


def test_failure_count_is_integer(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert isinstance(result["artifacts"]["failure_count"], int)


def test_duration_ms_is_integer(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert isinstance(result["artifacts"]["duration_ms"], int)


def test_duration_ms_zero_when_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["duration_ms"] == 0


def test_pass_count_zero_when_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["artifacts"]["pass_count"] == 0


# ---------------------------------------------------------------------------
# Zero LLM calls — purely deterministic
# ---------------------------------------------------------------------------


def test_llm_calls_zero_passing_tests(pytest_pass_repo: Path) -> None:
    result = SkillLayer().run(pytest_pass_repo, "run tests")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_failing_tests(pytest_fail_repo: Path) -> None:
    result = SkillLayer().run(pytest_fail_repo, "run tests")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "run tests")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run tests")
    assert result["llm_calls"] == 0


# ---------------------------------------------------------------------------
# Detection priority: pytest must be preferred over unittest when any
# pytest signal is present, regardless of whether pytest is importable
# in the current interpreter.
# ---------------------------------------------------------------------------


@pytest.fixture
def pytest_pyproject_repo(tmp_path: Path) -> Path:
    """Repo with test files and pyproject.toml that declares pytest as a dep."""
    repo = tmp_path / "pytest_pyproject_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "example"\n\n[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n'
    )
    return repo


@pytest.fixture
def pytest_ini_repo(tmp_path: Path) -> Path:
    """Repo with test files and an explicit pytest.ini."""
    repo = tmp_path / "pytest_ini_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    return repo


@pytest.fixture
def conftest_repo(tmp_path: Path) -> Path:
    """Repo with test files and a conftest.py at the root."""
    repo = tmp_path / "conftest_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    (repo / "conftest.py").write_text("# conftest\n")
    return repo


@pytest.fixture
def pyproject_tool_pytest_repo(tmp_path: Path) -> Path:
    """Repo with test files and [tool.pytest.ini_options] in pyproject.toml."""
    repo = tmp_path / "pyproject_tool_pytest_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\".\"]"
    )
    return repo


@pytest.fixture
def setup_cfg_pytest_repo(tmp_path: Path) -> Path:
    """Repo with test files and [tool:pytest] in setup.cfg."""
    repo = tmp_path / "setup_cfg_pytest_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    (repo / "setup.cfg").write_text("[tool:pytest]\naddopts = -q\n")
    return repo


@pytest.fixture
def venv_pytest_repo(tmp_path: Path) -> Path:
    """Repo with test files and a .venv/bin/pytest binary present."""
    repo = tmp_path / "venv_pytest_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok(): assert True\n")
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pytest").write_text("#!/bin/sh\npytest $@\n")
    return repo


@pytest.fixture
def no_pytest_signal_repo(tmp_path: Path) -> Path:
    """Repo with test files but zero pytest signals — should fall back to unittest."""
    repo = tmp_path / "no_pytest_signal_repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"
    )
    return repo


class TestDetectionPriority:
    """pytest must be preferred over unittest when any repo-level signal is present."""

    def test_pyproject_dep_signals_pytest(self, pytest_pyproject_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(pytest_pyproject_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), (
            f"expected pytest in command, got {cmd!r}"
        )

    def test_pytest_ini_signals_pytest(self, pytest_ini_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(pytest_ini_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), f"expected pytest, got {cmd!r}"

    def test_conftest_signals_pytest(self, conftest_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(conftest_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), f"expected pytest, got {cmd!r}"

    def test_tool_pytest_ini_options_signals_pytest(self, pyproject_tool_pytest_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(pyproject_tool_pytest_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), f"expected pytest, got {cmd!r}"

    def test_setup_cfg_signals_pytest(self, setup_cfg_pytest_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(setup_cfg_pytest_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), f"expected pytest, got {cmd!r}"

    def test_venv_pytest_binary_signals_pytest(self, venv_pytest_repo: Path) -> None:
        runner = TestRunner()
        cmd = runner.detect(venv_pytest_repo)
        assert cmd is not None
        assert "pytest" in " ".join(cmd), f"expected pytest, got {cmd!r}"

    def test_no_signal_falls_back_to_unittest(self, no_pytest_signal_repo: Path) -> None:
        """With no pytest signals, unittest discover should be returned."""
        import importlib.util
        if importlib.util.find_spec("pytest") is None:
            runner = TestRunner()
            cmd = runner.detect(no_pytest_signal_repo)
            assert cmd is not None
            assert "unittest" in " ".join(cmd), f"expected unittest, got {cmd!r}"
        else:
            pytest.skip("pytest importable in current interpreter — cannot test fallback")

    def test_has_pytest_signal_true_for_pyproject_dep(self, pytest_pyproject_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(pytest_pyproject_repo) is True

    def test_has_pytest_signal_true_for_pytest_ini(self, pytest_ini_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(pytest_ini_repo) is True

    def test_has_pytest_signal_true_for_conftest(self, conftest_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(conftest_repo) is True

    def test_has_pytest_signal_true_for_tool_pytest_section(self, pyproject_tool_pytest_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(pyproject_tool_pytest_repo) is True

    def test_has_pytest_signal_true_for_setup_cfg(self, setup_cfg_pytest_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(setup_cfg_pytest_repo) is True

    def test_has_pytest_signal_true_for_venv_binary(self, venv_pytest_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(venv_pytest_repo) is True

    def test_has_pytest_signal_false_with_no_signals(self, no_pytest_signal_repo: Path) -> None:
        runner = TestRunner()
        assert runner._has_pytest_signal(no_pytest_signal_repo) is False


# ---------------------------------------------------------------------------
# Self-test: RunTestsWorkflow on the SkillLayer repo itself must detect pytest
# ---------------------------------------------------------------------------

_SKILLLAYER_REPO = Path(__file__).parent.parent


def test_skilllayer_repo_detects_pytest() -> None:
    """detect() must return a pytest command scoped to tests/ on the SkillLayer
    repo itself — not a bare, repo-wide invocation. Scoping is what makes "run
    the tests" mean the real suite rather than sweeping in unrelated scratch
    files elsewhere in the repo."""
    runner = TestRunner()
    cmd = runner.detect(_SKILLLAYER_REPO)
    assert cmd is not None, "no test command detected for SkillLayer repo"
    assert "pytest" in " ".join(cmd), (
        f"expected pytest command for SkillLayer repo, got {cmd!r}"
    )
    assert "tests" in cmd, (
        f"expected the command to be scoped to the tests/ directory, got {cmd!r}"
    )


def test_skilllayer_repo_has_pytest_signal() -> None:
    """_has_pytest_signal must return True for the SkillLayer repo (pyproject.toml declares pytest)."""
    runner = TestRunner()
    assert runner._has_pytest_signal(_SKILLLAYER_REPO) is True


def test_skilllayer_repo_runs_tests_and_finds_some() -> None:
    """RunTestsWorkflow, called on the SkillLayer repo from within a currently
    running pytest test, must refuse rather than recurse.

    This test itself runs under pytest, so PYTEST_CURRENT_TEST is set for its
    entire duration — exactly the condition the nested-pytest guard exists to
    catch. Before the guard existed, this call recursively re-discovered and
    re-executed this very test (and everything else in tests/), spawning a
    nested pytest run inside a nested pytest run inside a nested pytest run
    with no depth bound, confirmed directly via process-tree inspection during
    the incident that prompted this fix (multiple live `pytest -q` descendant
    processes, each spawned by the one above it).

    This assertion — outcome == BLOCKED_NESTED_EXECUTION — IS the "only one
    level of pytest execution occurred" check: if the guard had not fired, this
    call would have actually spawned a subprocess and either hung (bounded only
    by test_timeout_seconds) or recursed further. Requiring the blocked outcome
    instead of "eventually passes" means this test fails fast (no subprocess,
    no timeout wait) if the recursion protection ever regresses.
    """
    assert os.environ.get("PYTEST_CURRENT_TEST"), (
        "expected to be running inside pytest (PYTEST_CURRENT_TEST unset) — "
        "this test's whole premise depends on it"
    )
    result = SkillLayer().run(_SKILLLAYER_REPO, "run tests", test_timeout_seconds=5)
    artifacts = result.get("artifacts", {})
    assert artifacts.get("outcome") == "BLOCKED_NESTED_EXECUTION", (
        f"expected the nested-pytest guard to block this call from within a "
        f"running test; got outcome={artifacts.get('outcome')!r} instead — "
        f"if this ever becomes a real outcome again, recursion is back. "
        f"artifacts={artifacts!r}"
    )
    assert artifacts.get("error_code") == "nested_test_execution_blocked"
    assert result.get("llm_calls", 0) == 0


class TestNestedRecursionDoesNotOccurEndToEnd:
    """Real, hard-timeout-wrapped, fully isolated reproduction of the exact bug
    pattern: a test that calls SkillLayer().run(self, "run tests"). Uses a
    throwaway tmp_path repo, never the real SkillLayer checkout, and every
    subprocess is spawned in its own process group so a hard wall-clock cap can
    kill the whole tree — not just the top process — if anything ever regresses.
    """

    _HARD_CAP_SECONDS = 25

    def test_self_referential_repo_does_not_recurse(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_self.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(_SKILLLAYER_REPO / 'src')!r})\n"
            "from pathlib import Path\n"
            "from skilllayer import SkillLayer\n"
            "\n"
            "def test_calls_run_tests_on_self():\n"
            "    result = SkillLayer().run(Path(__file__).parent.parent, 'run tests', test_timeout_seconds=5)\n"
            "    artifacts = result.get('artifacts', {})\n"
            "    assert artifacts.get('outcome') == 'BLOCKED_NESTED_EXECUTION', artifacts\n"
        )
        cmd = [sys.executable, "-m", "pytest", "-q", "tests/test_self.py"]

        started = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # own process group — killable as a unit
        )
        try:
            stdout, _ = proc.communicate(timeout=self._HARD_CAP_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
            pytest.fail(
                f"self-referential repro did not complete within the "
                f"{self._HARD_CAP_SECONDS}s hard cap — recursion protection "
                f"has regressed. Process group was forcibly killed."
            )
        elapsed = time.time() - started
        assert proc.returncode == 0, f"self-referential repro failed:\n{stdout}"
        assert "1 passed" in stdout, f"expected the guarded test to pass:\n{stdout}"
        # A real recursion would take much longer than a single fast, blocked
        # call. This is a generous ceiling, not a tight timing assertion.
        assert elapsed < 15, (
            f"self-referential repro took {elapsed:.1f}s — suspiciously slow "
            f"for a call that should be blocked almost instantly"
        )
