"""Tests for SingleTestWorkflow — stable-tier verification.

Covers all cases required for stable promotion:
  1. Valid pytest target (file::function) — passes
  2. Valid pytest target (file::function) — fails
  3. Valid function name — unique match, resolves and runs
  4. Invalid function name — not found → explicit error code
  5. Ambiguous function name — multiple files → explicit error code
  6. Invalid file path — file not found → explicit error code
  7. No target specified → explicit error code
  8. Timeout handling — TIMEOUT outcome, no false success
  9. Schema consistency across all outcomes
 10. success=False for all failure modes
 11. Zero LLM calls — purely deterministic
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.config.defaults import WORKFLOW_METADATA
from skilllayer.router import SkillRouter
from skilllayer.runner.core import (
    build_single_test_error_artifacts,
    build_single_test_plan_artifacts,
    command_for_test_function_name,
    resolve_single_test_target,
)
from skilllayer.verifier import TestRunner


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({
    "workflow",
    "test_target",
    "target_source",
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
def pytest_repo(tmp_path: Path) -> Path:
    """Repo with one passing and one failing test."""
    (tmp_path / "test_sample.py").write_text(
        "def test_passes():\n    assert 1 + 1 == 2\n\ndef test_fails():\n    assert 1 == 2, 'deliberate'\n"
    )
    return tmp_path


@pytest.fixture
def ambiguous_repo(tmp_path: Path) -> Path:
    """Repo with test_shared defined in two files — ambiguous resolution."""
    mod_a = tmp_path / "module_a"
    mod_a.mkdir()
    mod_b = tmp_path / "module_b"
    mod_b.mkdir()
    (mod_a / "test_a.py").write_text("def test_shared():\n    assert True\n")
    (mod_b / "test_b.py").write_text("def test_shared():\n    assert True\n")
    return tmp_path


@pytest.fixture
def hang_repo(tmp_path: Path) -> Path:
    """Repo with a test that sleeps indefinitely."""
    (tmp_path / "test_hangs.py").write_text(
        "import time\ndef test_hangs():\n    time.sleep(120)\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Stability metadata
# ---------------------------------------------------------------------------


def test_workflow_stability_is_stable() -> None:
    meta = WORKFLOW_METADATA["SingleTestWorkflow"]
    assert meta["stability"] == "stable", (
        f"SingleTestWorkflow stability must be 'stable', got {meta['stability']!r}"
    )


def test_workflow_summary_documents_llm_calls() -> None:
    meta = WORKFLOW_METADATA["SingleTestWorkflow"]
    summary = meta["summary"].lower()
    assert "zero llm" in summary or "0 llm" in summary or "no llm" in summary or "deterministic" in summary, (
        f"summary must document LLM call behaviour: {meta['summary']!r}"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_single_test_phrases() -> None:
    for phrase in [
        "run test test_sample.py::test_passes",
        "run single test test_foo",
        "run test test_my_function",
        "run only test_my_function",
        "rerun failing test",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "single_test", (
            f"expected single_test for {phrase!r}, got {decision.task_type!r}"
        )
        assert decision.workflow == "SingleTestWorkflow"


def test_router_does_not_route_full_suite_to_single_test() -> None:
    for phrase in ["run all tests", "run the entire test suite"]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type != "single_test", (
            f"should NOT route to single_test for {phrase!r}"
        )


# ---------------------------------------------------------------------------
# Schema consistency — all outcomes must produce identical key sets
# ---------------------------------------------------------------------------


def _error_artifacts(error_code: str, summary: str = "err") -> dict:
    return build_single_test_error_artifacts({
        "error_code": error_code,
        "test_target": "test_foo",
        "target_source": "task_description",
        "summary": summary,
    })


def test_schema_valid_run_has_all_required_fields(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"valid run: missing field {field!r}"


def test_schema_failing_run_has_all_required_fields(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"failing run: missing field {field!r}"


def test_schema_not_found_has_all_required_fields(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"not-found: missing field {field!r}"


def test_schema_ambiguous_has_all_required_fields(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"ambiguous: missing field {field!r}"


def test_schema_no_target_has_all_required_fields(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"no-target: missing field {field!r}"


def test_schema_timeout_has_all_required_fields(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    for field in _REQUIRED_FIELDS:
        assert field in result["artifacts"], f"timeout: missing field {field!r}"


def test_schema_plan_has_all_required_fields(pytest_repo: Path) -> None:
    artifacts = build_single_test_plan_artifacts(pytest_repo, "run test test_sample.py::test_passes", None)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"plan (supported): missing field {field!r}"


def test_schema_plan_unsupported_has_all_required_fields(pytest_repo: Path) -> None:
    artifacts = build_single_test_plan_artifacts(pytest_repo, "run single test", None)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"plan (unsupported): missing field {field!r}"


def test_schema_error_artifacts_has_all_required_fields() -> None:
    for code in ["test_target_not_found", "ambiguous_test_target", "test_file_not_found",
                 "test_target_not_specified", "single_test_not_supported", "no_failed_test_available"]:
        arts = _error_artifacts(code)
        for field in _REQUIRED_FIELDS:
            assert field in arts, f"error artifacts ({code}): missing field {field!r}"


def test_schema_keys_identical_across_all_outcomes(
    pytest_repo: Path, ambiguous_repo: Path, hang_repo: Path
) -> None:
    """Every outcome path must produce the identical top-level key set."""
    cases = [
        SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")["artifacts"],
        SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")["artifacts"],
        SkillLayer().run(pytest_repo, "run test test_nonexistent")["artifacts"],
        SkillLayer().run(ambiguous_repo, "run test test_shared")["artifacts"],
        SkillLayer().run(pytest_repo, "run single test")["artifacts"],
        SkillLayer(test_runner=TestRunner(timeout=1)).run(hang_repo, "run test test_hangs.py::test_hangs")["artifacts"],
        build_single_test_plan_artifacts(pytest_repo, "run test test_sample.py::test_passes", None),
        build_single_test_plan_artifacts(pytest_repo, "run single test", None),
        _error_artifacts("test_target_not_found"),
        _error_artifacts("ambiguous_test_target"),
    ]
    keys = [set(a.keys()) for a in cases]
    ref = keys[0]
    for i, k in enumerate(keys[1:], 1):
        assert k == ref, f"Key set #{i} differs from #0:\n  extra in #{i}: {k - ref}\n  missing from #{i}: {ref - k}"


# ---------------------------------------------------------------------------
# Case 1: Valid target — passes
# ---------------------------------------------------------------------------


def test_valid_pass_success_true(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["success"] is True


def test_valid_pass_tests_run(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["tests_run"] is True


def test_valid_pass_tests_passed(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["tests_passed"] is True


def test_valid_pass_outcome(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["outcome"] == "PASSED"


def test_valid_pass_error_code_none(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["error_code"] is None


def test_valid_pass_test_target(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert "test_passes" in result["artifacts"]["test_target"]


def test_valid_pass_pass_count_positive(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["pass_count"] >= 1


def test_valid_pass_failure_count_zero(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["failure_count"] == 0


def test_valid_pass_duration_ms_positive(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["duration_ms"] > 0


def test_valid_pass_timed_out_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["timed_out"] is False


def test_valid_pass_workflow_field(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["artifacts"]["workflow"] == "SingleTestWorkflow"


# ---------------------------------------------------------------------------
# Case 2: Valid target — fails
# ---------------------------------------------------------------------------


def test_valid_fail_success_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["success"] is False


def test_valid_fail_tests_passed_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["artifacts"]["tests_passed"] is False


def test_valid_fail_outcome(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["artifacts"]["outcome"] == "FAILED_TESTS"


def test_valid_fail_failure_count_positive(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["artifacts"]["failure_count"] >= 1


def test_valid_fail_duration_ms_positive(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["artifacts"]["duration_ms"] > 0


# ---------------------------------------------------------------------------
# Case 3: Valid function name — unique match
# ---------------------------------------------------------------------------


def test_function_name_unique_resolves(pytest_repo: Path) -> None:
    result = command_for_test_function_name(pytest_repo, "test_passes")
    assert result["supported"] is True
    assert "test_passes" in result["test_target"]


def test_function_name_unique_runs(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_passes")
    assert result["success"] is True
    assert result["artifacts"]["tests_run"] is True


# ---------------------------------------------------------------------------
# Case 4: Invalid function name — not found
# ---------------------------------------------------------------------------


def test_not_found_error_code(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run test test_nonexistent")
    assert result["error_code"] == "test_target_not_found", (
        f"expected 'test_target_not_found', got {result['error_code']!r}"
    )


def test_not_found_supported_false(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run test test_nonexistent")
    assert result["supported"] is False


def test_not_found_run_success_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert result["success"] is False


def test_not_found_artifacts_error_code(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert result["artifacts"]["error_code"] == "test_target_not_found"


def test_not_found_summary_non_empty(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert isinstance(result["artifacts"]["summary"], str)
    assert len(result["artifacts"]["summary"]) > 0


def test_not_found_tests_run_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert result["artifacts"]["tests_run"] is False


# ---------------------------------------------------------------------------
# Case 5: Ambiguous function name — matches multiple files
# ---------------------------------------------------------------------------


def test_ambiguous_error_code(ambiguous_repo: Path) -> None:
    result = resolve_single_test_target(ambiguous_repo, "run test test_shared")
    assert result["error_code"] == "ambiguous_test_target", (
        f"expected 'ambiguous_test_target', got {result['error_code']!r}"
    )


def test_ambiguous_supported_false(ambiguous_repo: Path) -> None:
    result = resolve_single_test_target(ambiguous_repo, "run test test_shared")
    assert result["supported"] is False


def test_ambiguous_summary_names_files(ambiguous_repo: Path) -> None:
    result = resolve_single_test_target(ambiguous_repo, "run test test_shared")
    assert "ambiguous" in result["summary"].lower() or "files" in result["summary"].lower(), (
        f"summary must mention ambiguity: {result['summary']!r}"
    )


def test_ambiguous_run_success_false(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    assert result["success"] is False


def test_ambiguous_artifacts_error_code(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    assert result["artifacts"]["error_code"] == "ambiguous_test_target"


def test_ambiguous_tests_run_false(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    assert result["artifacts"]["tests_run"] is False


# ---------------------------------------------------------------------------
# Case 6: Invalid file path
# ---------------------------------------------------------------------------


def test_file_not_found_error_code(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run test test_nonexistent.py::test_foo")
    assert result["error_code"] == "test_file_not_found", (
        f"expected 'test_file_not_found', got {result['error_code']!r}"
    )


def test_file_not_found_supported_false(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run test test_nonexistent.py::test_foo")
    assert result["supported"] is False


def test_file_not_found_run_success_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent.py::test_foo")
    assert result["success"] is False


def test_file_not_found_artifacts_error_code(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent.py::test_foo")
    assert result["artifacts"]["error_code"] == "test_file_not_found"


# ---------------------------------------------------------------------------
# Case 7: No target specified
# ---------------------------------------------------------------------------


def test_no_target_error_code(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run single test")
    assert result["error_code"] == "test_target_not_specified", (
        f"expected 'test_target_not_specified', got {result['error_code']!r}"
    )


def test_no_target_supported_false(pytest_repo: Path) -> None:
    result = resolve_single_test_target(pytest_repo, "run single test")
    assert result["supported"] is False


def test_no_target_run_success_false(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    assert result["success"] is False


def test_no_target_artifacts_error_code(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    assert result["artifacts"]["error_code"] == "test_target_not_specified"


def test_no_target_summary_non_empty(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    assert isinstance(result["artifacts"]["summary"], str)
    assert len(result["artifacts"]["summary"]) > 0


# ---------------------------------------------------------------------------
# Case 8: Timeout handling
# ---------------------------------------------------------------------------


def test_timeout_outcome_is_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["artifacts"]["outcome"] == "TIMEOUT", (
        f"expected TIMEOUT, got {result['artifacts']['outcome']!r}"
    )


def test_timeout_error_code(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["artifacts"]["error_code"] == "test_run_timed_out"


def test_timeout_timed_out_true(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["artifacts"]["timed_out"] is True


def test_timeout_success_false(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["success"] is False


def test_timeout_not_classified_as_failed_tests(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["artifacts"]["outcome"] != "FAILED_TESTS"


def test_timeout_summary_mentions_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    summary = result["artifacts"]["summary"].lower()
    assert "timed out" in summary or "timeout" in summary


# ---------------------------------------------------------------------------
# Requirement: success=False for all failure modes
# ---------------------------------------------------------------------------


def test_success_false_when_test_fails(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["success"] is False


def test_success_true_when_test_passes(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["success"] is True


def test_success_false_when_not_found(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert result["success"] is False


def test_success_false_when_ambiguous(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    assert result["success"] is False


def test_success_false_when_file_not_found(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test missing.py::test_foo")
    assert result["success"] is False


def test_success_false_when_no_target(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    assert result["success"] is False


def test_success_false_when_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["success"] is False


# ---------------------------------------------------------------------------
# Zero LLM calls — purely deterministic
# ---------------------------------------------------------------------------


def test_llm_calls_zero_passing(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_passes")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_failing(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_sample.py::test_fails")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_not_found(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run test test_nonexistent")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_ambiguous(ambiguous_repo: Path) -> None:
    result = SkillLayer().run(ambiguous_repo, "run test test_shared")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_no_target(pytest_repo: Path) -> None:
    result = SkillLayer().run(pytest_repo, "run single test")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "run test test_hangs.py::test_hangs")
    assert result["llm_calls"] == 0
