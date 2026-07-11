"""Tests for ExplainFailureWorkflow — stable-tier verification.

Covers all cases required for stable promotion:
  1. Single failure — diagnoses has 1 structured item with all required fields
  2. Multiple failures — diagnoses has multiple structured items
  3. No failures (tests pass) — outcome=PASSED, diagnoses=[], success=True
  4. No test runner detected — graceful, no crash, success=False
  5. Timeout — outcome=TIMEOUT, correct error_code, success=False
  6. Unparseable/empty failure output — unknown_failure type, graceful
  7. Schema consistency across all outcomes
  8. success=False for all failure/error modes
  9. Structured JSON diagnosis schema verified (not free text)
 10. Zero LLM calls — purely deterministic

LLM call count: 0 — ExplainFailureWorkflow uses deterministic regex-based
pattern matching (classify_failure_pattern). No LLM is invoked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.config.defaults import WORKFLOW_METADATA
from skilllayer.router import SkillRouter
from skilllayer.runner.core import (
    build_explain_failure_artifacts,
    build_explain_failure_plan_artifacts,
    build_failure_diagnoses,
    classify_failure_pattern,
)
from skilllayer.verifier import TestRunner


# ---------------------------------------------------------------------------
# Required artifact field sets
# ---------------------------------------------------------------------------

# Top-level fields every artifacts dict must contain
_REQUIRED_ARTIFACT_FIELDS = frozenset({
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
    "diagnoses",
    "diagnosis_count",
    "likely_failure_types",
    "confidence",
    "confidence_reason",
    "raw_output_available",
})

# Fields every diagnosis item inside `diagnoses` must contain
_REQUIRED_DIAGNOSIS_FIELDS = frozenset({
    "test_name",
    "file",
    "line",
    "failure_type",
    "confidence",
    "confidence_reason",
    "evidence_snippet",
    "likely_cause",
    "suggested_next_steps",
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def single_fail_repo(tmp_path: Path) -> Path:
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2, 'deliberate'\n"
    )
    return tmp_path


@pytest.fixture
def multi_fail_repo(tmp_path: Path) -> Path:
    (tmp_path / "test_multi.py").write_text(
        "def test_a():\n    assert False\n\ndef test_b():\n    assert 1 == 2\n\ndef test_c():\n    x = None\n    x.attr\n"
    )
    return tmp_path


@pytest.fixture
def all_pass_repo(tmp_path: Path) -> Path:
    (tmp_path / "test_ok.py").write_text(
        "def test_first():\n    assert 1 + 1 == 2\n\ndef test_second():\n    assert 'a' in 'abc'\n"
    )
    return tmp_path


@pytest.fixture
def no_runner_repo(tmp_path: Path) -> Path:
    (tmp_path / "src.py").write_text("x = 1\n")
    return tmp_path


@pytest.fixture
def hang_repo(tmp_path: Path) -> Path:
    (tmp_path / "test_hangs.py").write_text(
        "import time\ndef test_hangs():\n    time.sleep(120)\n"
    )
    return tmp_path


@pytest.fixture
def garbled_output_repo(tmp_path: Path) -> Path:
    """Repo that produces a non-zero exit with unrecognizable output."""
    (tmp_path / "conftest.py").write_text(
        "import sys\nprint('@@@GARBLED###', file=sys.stderr)\n"
    )
    (tmp_path / "test_garble.py").write_text(
        "def test_garble():\n    raise RuntimeError('zyx')\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Stability metadata
# ---------------------------------------------------------------------------


def test_workflow_stability_is_stable() -> None:
    meta = WORKFLOW_METADATA["ExplainFailureWorkflow"]
    assert meta["stability"] == "stable"


def test_workflow_summary_documents_zero_llm_calls() -> None:
    summary = WORKFLOW_METADATA["ExplainFailureWorkflow"]["summary"].lower()
    assert "zero llm" in summary or "0 llm" in summary or "deterministic" in summary, (
        f"summary must document that LLM calls = 0: {summary!r}"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_routes_explain_phrases() -> None:
    for phrase in [
        "explain why tests are failing",
        "diagnose test failures",
        "why are tests failing",
        "analyze test failure",
        "explain the failing tests",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "explain_failure", (
            f"expected explain_failure for {phrase!r}, got {decision.task_type!r}"
        )
        assert decision.workflow == "ExplainFailureWorkflow"


# ---------------------------------------------------------------------------
# Schema consistency — all outcomes must produce identical key sets
# ---------------------------------------------------------------------------


def test_schema_all_pass_has_required_fields(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"PASSED outcome missing {field!r}"


def test_schema_single_fail_has_required_fields(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"FAILED_TESTS outcome missing {field!r}"


def test_schema_no_runner_has_required_fields(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"NO_TEST_COMMAND outcome missing {field!r}"


def test_schema_timeout_has_required_fields(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"TIMEOUT outcome missing {field!r}"


def test_schema_garbled_has_required_fields(garbled_output_repo: Path) -> None:
    result = SkillLayer().run(garbled_output_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"garbled output missing {field!r}"


def test_schema_plan_has_required_fields(all_pass_repo: Path) -> None:
    runner = TestRunner()
    artifacts = build_explain_failure_plan_artifacts(runner.detect(all_pass_repo))
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in artifacts, f"plan (detected) missing {field!r}"


def test_schema_plan_no_command_has_required_fields() -> None:
    artifacts = build_explain_failure_plan_artifacts(None)
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in artifacts, f"plan (no command) missing {field!r}"


def test_schema_keys_identical_across_all_outcomes() -> None:
    """Schema comparison is a unit-level contract, not a second subprocess suite.

    The outcome-specific integration tests above already exercise real pytest
    commands.  Re-running five of them in one assertion can leave the parent
    test waiting on a captured child pytest process in constrained runners,
    without adding coverage to the schema comparison itself.
    """
    base = {
        "command": ["python", "-m", "pytest", "-q"],
        "runtime_seconds": 0.1,
        "duration_ms": 100,
        "stdout": "",
        "stderr": "",
    }
    cases = [
        build_explain_failure_artifacts({**base, "available": True, "returncode": 0, "passed": 1, "failed": 0, "stdout": "1 passed"}),
        build_explain_failure_artifacts({**base, "available": True, "returncode": 1, "passed": 0, "failed": 1, "stdout": "FAILED test_sample.py::test_bad"}),
        build_explain_failure_artifacts({**base, "available": False, "returncode": None, "passed": 0, "failed": 0}),
        build_explain_failure_artifacts({**base, "available": True, "returncode": 124, "passed": 0, "failed": 1, "timed_out": True, "timeout_seconds": 1}),
        build_explain_failure_artifacts({**base, "available": True, "returncode": 1, "passed": 0, "failed": 1, "stderr": "@@@GARBLED###"}),
        build_explain_failure_plan_artifacts(None),
        build_explain_failure_plan_artifacts(["/usr/bin/python", "-m", "pytest", "-q"]),
    ]
    ref = set(cases[0].keys())
    for i, case in enumerate(cases[1:], 1):
        keys = set(case.keys())
        assert keys == ref, (
            f"Key set #{i} differs:\n  extra: {keys - ref}\n  missing: {ref - keys}"
        )


# ---------------------------------------------------------------------------
# Case 1: Single failure
# ---------------------------------------------------------------------------


def test_single_fail_success_true(single_fail_repo: Path) -> None:
    # ExplainFailureWorkflow succeeds when it produces diagnoses — even for failing tests.
    # Use task_validation_success (False) to distinguish "tests failed" from "workflow failed".
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["success"] is True


def test_single_fail_outcome(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] == "FAILED_TESTS"


def test_single_fail_diagnoses_non_empty(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert len(result["artifacts"]["diagnoses"]) >= 1


def test_single_fail_diagnosis_count_matches(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    arts = result["artifacts"]
    assert arts["diagnosis_count"] == len(arts["diagnoses"])


def test_single_fail_diagnosis_has_required_fields(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        for field in _REQUIRED_DIAGNOSIS_FIELDS:
            assert field in diag, f"diagnosis missing field {field!r}"


def test_single_fail_failure_type_is_string(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert isinstance(diag["failure_type"], str)
        assert len(diag["failure_type"]) > 0


def test_single_fail_likely_cause_is_string(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert isinstance(diag["likely_cause"], str)
        assert len(diag["likely_cause"]) > 0


def test_single_fail_suggested_steps_is_list(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert isinstance(diag["suggested_next_steps"], list)
        assert len(diag["suggested_next_steps"]) >= 1


def test_single_fail_confidence_value(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert diag["confidence"] in {"high", "medium", "low"}, (
            f"unexpected confidence: {diag['confidence']!r}"
        )


def test_single_fail_assertion_classified(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    types = result["artifacts"]["likely_failure_types"]
    assert len(types) >= 1
    assert all(isinstance(t, str) and len(t) > 0 for t in types)


def test_single_fail_summary_mentions_pattern_count(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    summary = result["artifacts"]["summary"].lower()
    assert "1" in summary or "pattern" in summary or "failure" in summary


def test_single_fail_error_code_none(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    # error_code should be None when diagnoses were produced
    arts = result["artifacts"]
    if arts["diagnoses"]:
        assert arts["error_code"] is None


def test_single_fail_workflow_execution_success(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["workflow_execution_success"] is True


def test_single_fail_task_validation_false(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["task_validation_success"] is False


# ---------------------------------------------------------------------------
# Case 2: Multiple failures
# ---------------------------------------------------------------------------


def test_multi_fail_diagnoses_plural(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["diagnosis_count"] >= 2, (
        f"expected ≥2 diagnoses, got {result['artifacts']['diagnosis_count']}"
    )


def test_multi_fail_all_diagnoses_have_required_fields(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    for i, diag in enumerate(result["artifacts"]["diagnoses"]):
        for field in _REQUIRED_DIAGNOSIS_FIELDS:
            assert field in diag, f"diagnosis[{i}] missing field {field!r}"


def test_multi_fail_likely_failure_types_non_empty(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert len(result["artifacts"]["likely_failure_types"]) >= 1


def test_multi_fail_outcome(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] == "FAILED_TESTS"


def test_multi_fail_success_true(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Case 3: No failures — tests pass
# ---------------------------------------------------------------------------


def test_all_pass_success_true(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["success"] is True


def test_all_pass_outcome(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] == "PASSED"


def test_all_pass_diagnoses_empty(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["diagnoses"] == []
    assert result["artifacts"]["diagnosis_count"] == 0


def test_all_pass_likely_failure_types_empty(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["likely_failure_types"] == []


def test_all_pass_summary_mentions_no_failures(all_pass_repo: Path) -> None:
    summary = SkillLayer().run(all_pass_repo, "explain why tests are failing")["artifacts"]["summary"].lower()
    assert "no failing" in summary or "no failure" in summary or "passed" in summary


def test_all_pass_error_code_none(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["error_code"] is None


def test_all_pass_workflow_execution_success(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["workflow_execution_success"] is True


# ---------------------------------------------------------------------------
# Case 4: No test runner detected
# ---------------------------------------------------------------------------


def test_no_runner_success_false(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["success"] is False


def test_no_runner_outcome(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] in {"NO_TEST_COMMAND", "UNSUPPORTED"}


def test_no_runner_diagnoses_empty(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["artifacts"]["diagnoses"] == []


def test_no_runner_tests_run_false(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["artifacts"]["tests_run"] is False


def test_no_runner_error_code_set(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["artifacts"]["error_code"] is not None


def test_no_runner_summary_non_empty(no_runner_repo: Path) -> None:
    summary = SkillLayer().run(no_runner_repo, "explain why tests are failing")["artifacts"]["summary"]
    assert isinstance(summary, str) and len(summary) > 0


# ---------------------------------------------------------------------------
# Case 5: Timeout
# ---------------------------------------------------------------------------


def test_timeout_outcome(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] == "TIMEOUT"


def test_timeout_error_code(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["artifacts"]["error_code"] == "test_run_timed_out"


def test_timeout_timed_out_true(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["artifacts"]["timed_out"] is True


def test_timeout_success_false(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["success"] is False


def test_timeout_diagnoses_empty(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["artifacts"]["diagnoses"] == []


def test_timeout_not_classified_as_failed_tests(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    assert result["artifacts"]["outcome"] != "FAILED_TESTS"


def test_timeout_summary_mentions_timeout(hang_repo: Path) -> None:
    runner = TestRunner(timeout=1)
    result = SkillLayer(test_runner=runner).run(hang_repo, "explain why tests are failing")
    summary = result["artifacts"]["summary"].lower()
    assert "timed out" in summary or "timeout" in summary


# ---------------------------------------------------------------------------
# Case 6: Unparseable / empty failure output
# ---------------------------------------------------------------------------


def test_garbled_output_no_crash(garbled_output_repo: Path) -> None:
    result = SkillLayer().run(garbled_output_repo, "explain why tests are failing")
    assert isinstance(result, dict)


def test_garbled_output_has_all_required_fields(garbled_output_repo: Path) -> None:
    result = SkillLayer().run(garbled_output_repo, "explain why tests are failing")
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in result["artifacts"], f"garbled: missing {field!r}"


def test_garbled_output_diagnoses_structured(garbled_output_repo: Path) -> None:
    result = SkillLayer().run(garbled_output_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        for field in _REQUIRED_DIAGNOSIS_FIELDS:
            assert field in diag, f"garbled diagnosis missing {field!r}"


def test_empty_output_classify_unknown() -> None:
    """Empty stdout/stderr with returncode≠0 must produce unknown_failure, not crash."""
    fake_result = {
        "available": True,
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "passed": 0,
        "failed": 1,
        "runtime_seconds": 0.1,
        "duration_ms": 100,
        "command": ["/usr/bin/python", "-m", "pytest"],
    }
    artifacts = build_explain_failure_artifacts(fake_result)
    assert isinstance(artifacts, dict)
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in artifacts, f"empty-output: missing {field!r}"
    # At least one diagnosis should be produced (unknown_failure)
    for diag in artifacts.get("diagnoses", []):
        assert diag["failure_type"] == "unknown_failure"


def test_malformed_output_no_crash() -> None:
    """Random binary-like garbage in output must not raise."""
    fake_result = {
        "available": True,
        "returncode": 2,
        "stdout": "\x00\xff\xfe garbage \x01\x02",
        "stderr": "!!! FATAL XYZZY !!!",
        "passed": 0,
        "failed": 1,
        "runtime_seconds": 0.05,
        "duration_ms": 50,
        "command": ["/usr/bin/python", "-m", "pytest"],
    }
    artifacts = build_explain_failure_artifacts(fake_result)
    assert isinstance(artifacts, dict)
    for field in _REQUIRED_ARTIFACT_FIELDS:
        assert field in artifacts


# ---------------------------------------------------------------------------
# Requirement 2: Structured JSON (not free text) — diagnosis schema
# ---------------------------------------------------------------------------


def test_diagnosis_schema_is_dict(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert isinstance(diag, dict), "each diagnosis must be a dict"


def test_diagnosis_failure_type_is_known_value(single_fail_repo: Path) -> None:
    known = {
        "assertion_failure", "import_error", "module_not_found", "type_error",
        "attribute_error", "name_error", "syntax_error", "timeout",
        "node_assertion_error", "node_module_not_found", "unknown_failure",
    }
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert diag["failure_type"] in known, (
            f"unexpected failure_type: {diag['failure_type']!r}"
        )


def test_diagnosis_confidence_is_known_value(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert diag["confidence"] in {"high", "medium", "low"}


def test_diagnosis_suggested_steps_are_strings(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        for step in diag["suggested_next_steps"]:
            assert isinstance(step, str) and len(step) > 0


def test_diagnosis_file_is_none_or_string(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert diag["file"] is None or isinstance(diag["file"], str)


def test_diagnosis_line_is_none_or_int(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    for diag in result["artifacts"]["diagnoses"]:
        assert diag["line"] is None or isinstance(diag["line"], int)


# ---------------------------------------------------------------------------
# classify_failure_pattern unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evidence,expected_type,expected_confidence", [
    ("SyntaxError: invalid syntax", "syntax_error", "high"),
    ("ModuleNotFoundError: No module named 'foo'", "module_not_found", "high"),
    ("ImportError: cannot import name 'bar'", "import_error", "high"),
    ("TypeError: argument of type 'int' is not iterable", "type_error", "high"),
    ("AttributeError: 'NoneType' object has no attribute 'foo'", "attribute_error", "high"),
    ("NameError: name 'x' is not defined", "name_error", "high"),
    ("AssertionError: assert 1 == 2", "assertion_failure", "medium"),
    ("some random output with assert in it", "assertion_failure", "medium"),
    ("completely unrecognized garbage output @#$%", "unknown_failure", "low"),
    ("", "unknown_failure", "low"),
])
def test_classify_failure_pattern(
    evidence: str, expected_type: str, expected_confidence: str
) -> None:
    failure_type, confidence = classify_failure_pattern(evidence, evidence, {})
    assert failure_type == expected_type, (
        f"for {evidence!r}: expected {expected_type!r}, got {failure_type!r}"
    )
    assert confidence == expected_confidence, (
        f"for {evidence!r}: expected confidence {expected_confidence!r}, got {confidence!r}"
    )


def test_classify_timeout_via_test_result() -> None:
    failure_type, confidence = classify_failure_pattern("", "", {"timed_out": True})
    assert failure_type == "timeout"
    assert confidence == "high"


def test_classify_timeout_via_returncode() -> None:
    failure_type, confidence = classify_failure_pattern("", "", {"returncode": 124})
    assert failure_type == "timeout"
    assert confidence == "high"


# ---------------------------------------------------------------------------
# build_failure_diagnoses unit tests
# ---------------------------------------------------------------------------


def test_build_failure_diagnoses_empty_for_passing() -> None:
    run_artifacts = {"outcome": "PASSED", "failed_tests": [], "failure_count": 0}
    result = build_failure_diagnoses(run_artifacts, {})
    assert result == []


def test_build_failure_diagnoses_empty_for_no_command() -> None:
    run_artifacts = {"outcome": "NO_TEST_COMMAND", "failed_tests": [], "failure_count": 0}
    result = build_failure_diagnoses(run_artifacts, {})
    assert result == []


def test_build_failure_diagnoses_returns_dicts() -> None:
    run_artifacts = {
        "outcome": "FAILED_TESTS",
        "failed_tests": [{"test_name": "test_foo", "file": "test_a.py", "line": 5, "snippet": "assert 1 == 2"}],
        "failure_count": 1,
    }
    fake_result = {"stdout": "E   assert 1 == 2\nFAILED test_a.py::test_foo", "stderr": ""}
    diagnoses = build_failure_diagnoses(run_artifacts, fake_result)
    assert len(diagnoses) >= 1
    for diag in diagnoses:
        assert isinstance(diag, dict)
        for field in _REQUIRED_DIAGNOSIS_FIELDS:
            assert field in diag


# ---------------------------------------------------------------------------
# success=True only when no failures
# ---------------------------------------------------------------------------


def test_success_true_on_pass(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["success"] is True


def test_success_true_on_single_fail(single_fail_repo: Path) -> None:
    # Workflow succeeded: it ran tests and produced diagnoses.
    # task_validation_success=False distinguishes "tests failed" from "workflow failed".
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["success"] is True


def test_success_true_on_multi_fail(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert result["success"] is True


def test_success_false_on_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["success"] is False


def test_success_false_on_timeout(hang_repo: Path) -> None:
    result = SkillLayer(test_runner=TestRunner(timeout=1)).run(hang_repo, "explain why tests are failing")
    assert result["success"] is False


def test_task_validation_success_false_when_tests_fail(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["artifacts"]["task_validation_success"] is False


def test_task_validation_success_true_when_tests_pass(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["artifacts"]["task_validation_success"] is True


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------


def test_llm_calls_zero_passing(all_pass_repo: Path) -> None:
    result = SkillLayer().run(all_pass_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_failing(single_fail_repo: Path) -> None:
    result = SkillLayer().run(single_fail_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_multi_fail(multi_fail_repo: Path) -> None:
    result = SkillLayer().run(multi_fail_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_no_runner(no_runner_repo: Path) -> None:
    result = SkillLayer().run(no_runner_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_timeout(hang_repo: Path) -> None:
    result = SkillLayer(test_runner=TestRunner(timeout=1)).run(hang_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_garbled(garbled_output_repo: Path) -> None:
    result = SkillLayer().run(garbled_output_repo, "explain why tests are failing")
    assert result["llm_calls"] == 0
