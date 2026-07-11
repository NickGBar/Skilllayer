from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SkillLayer
from .benchmark_environment import (
    annotate_scenario_environment,
    probe_benchmark_environment,
    summarize_environment_status,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "runs" / "token_savings_benchmark"
TEST_OUTCOMES = (
    "PASSED",
    "FAILED_TESTS",
    "NO_TESTS_DISCOVERED",
    "NO_TEST_COMMAND",
    "COMMAND_ERROR",
    "UNSUPPORTED",
    "UNKNOWN",
)


@dataclass(frozen=True)
class BaselineAction:
    name: str
    kind: str
    estimated_context_units: int
    estimated_seconds: float


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario: str
    task: str
    workflow: str
    baseline_actions: tuple[BaselineAction, ...]
    rationale: str


SCENARIOS: tuple[BenchmarkScenario, ...] = (
    BenchmarkScenario(
        scenario="Find Function",
        task="Find function inspect_repo",
        workflow="FindFunctionWorkflow",
        baseline_actions=(
            BaselineAction("search_symbol_inspect_repo", "search", 3, 1.0),
            BaselineAction("read_candidate_file", "file_read", 8, 1.5),
            BaselineAction("inspect_match_context", "output_inspection", 2, 0.8),
            BaselineAction("summarize_location", "summary", 1, 0.5),
        ),
        rationale="Manual agent path usually searches for the symbol, opens a candidate file, inspects nearby code, then summarizes the match.",
    ),
    BenchmarkScenario(
        scenario="Git Status Summary",
        task="Git status",
        workflow="GitStatusWorkflow",
        baseline_actions=(
            BaselineAction("run_git_status_short", "command", 2, 0.8),
            BaselineAction("run_git_diff_stat", "command", 4, 1.0),
            BaselineAction("run_git_name_status", "command", 4, 1.0),
            BaselineAction("inspect_changed_file_list", "output_inspection", 2, 0.8),
            BaselineAction("summarize_working_tree", "summary", 1, 0.5),
        ),
        rationale="Manual agent path usually runs several read-only git commands and then summarizes staged, unstaged, and untracked files.",
    ),
    BenchmarkScenario(
        scenario="Dependency Lookup",
        task="Check dependency pytest",
        workflow="DependencyCheckWorkflow",
        baseline_actions=(
            BaselineAction("read_pyproject", "file_read", 6, 1.2),
            BaselineAction("read_requirements_files", "file_read", 4, 1.0),
            BaselineAction("search_python_imports", "search", 5, 1.2),
            BaselineAction("inspect_usage_files", "file_read", 6, 1.5),
            BaselineAction("summarize_dependency_state", "summary", 1, 0.5),
        ),
        rationale="Manual agent path checks dependency declaration files and searches source imports/usages.",
    ),
    BenchmarkScenario(
        scenario="Run Tests",
        task="Run tests",
        workflow="RunTestsWorkflow",
        baseline_actions=(
            BaselineAction("detect_test_command", "repo_inspection", 3, 1.0),
            BaselineAction("run_test_command", "command", 8, 2.5),
            BaselineAction("inspect_test_output", "output_inspection", 5, 1.5),
            BaselineAction("summarize_test_result", "summary", 1, 0.5),
        ),
        rationale="Manual agent path detects the test command, runs it, inspects output, and summarizes status.",
    ),
    BenchmarkScenario(
        scenario="Explain Failure",
        task="Explain failing tests",
        workflow="ExplainFailureWorkflow",
        baseline_actions=(
            BaselineAction("run_test_command", "command", 8, 2.5),
            BaselineAction("inspect_failure_output", "output_inspection", 8, 2.0),
            BaselineAction("locate_failure_file", "search", 4, 1.0),
            BaselineAction("read_failure_context", "file_read", 8, 1.5),
            BaselineAction("summarize_likely_cause", "summary", 2, 0.8),
        ),
        rationale="Manual agent path reads test output, locates relevant files, reads local context, then explains the likely cause.",
    ),
)


def run_token_savings_benchmark(repo_path: str | Path | None = None, *, write_outputs: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    repo = Path(repo_path or PROJECT_ROOT).resolve()
    log_dir = OUTPUT_DIR / "skilllayer_logs"
    layer = SkillLayer()
    environment_probe = probe_benchmark_environment()
    scenario_results: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        scenario_results.append(run_scenario(layer, repo, scenario, log_dir=log_dir, environment_probe=environment_probe))

    benchmark_summary = summarize_results(scenario_results)
    methodology_report = build_methodology_report(repo, scenario_results)
    report = {
        "milestone": "Token Savings Benchmark v1",
        "success": True,
        "duration_ms": elapsed_ms(started),
        "repo_path": str(repo),
        "scenario_count": len(scenario_results),
        "environment_probe": environment_probe,
        "scenario_results": scenario_results,
        "benchmark_summary": benchmark_summary,
        "methodology_report": methodology_report,
        "conservative_interpretation": (
            "This benchmark reports estimated action/context proxy reductions only. "
            "It does not prove real token savings and requires external validation in Claude Code/Cursor."
        ),
        "estimated_savings_only": True,
        "proxy_metrics_only": True,
        "does_not_prove_token_savings": True,
        "llm_usage": "none",
        "workflow_behavior_changed": False,
        "routing_changed": False,
        "outputs": {
            "scenario_results": "runs/token_savings_benchmark/scenario_results.json",
            "benchmark_summary": "runs/token_savings_benchmark/benchmark_summary.json",
            "methodology_report": "runs/token_savings_benchmark/methodology_report.json",
            "report": "runs/token_savings_benchmark/report.json",
        },
    }
    if write_outputs:
        write_json(OUTPUT_DIR / "scenario_results.json", scenario_results)
        write_json(OUTPUT_DIR / "benchmark_summary.json", benchmark_summary)
        write_json(OUTPUT_DIR / "methodology_report.json", methodology_report)
        write_json(OUTPUT_DIR / "report.json", report)
    return report


def run_scenario(
    layer: SkillLayer,
    repo: Path,
    scenario: BenchmarkScenario,
    *,
    log_dir: Path,
    environment_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = layer.run(repo, scenario.task, dry_run=False, log_dir=log_dir, allow_internal=True)
    observed_seconds = round(time.perf_counter() - started, 3)
    baseline_steps = len(scenario.baseline_actions)
    skilllayer_steps = 1
    actions_saved = baseline_steps - skilllayer_steps
    baseline_context_units = sum(action.estimated_context_units for action in scenario.baseline_actions)
    skilllayer_context_units = estimate_skilllayer_context_units(result)
    outcome = classify_benchmark_outcome(result, scenario)
    result_with_outcome = {**result, **outcome}
    environment = annotate_scenario_environment(
        scenario.scenario,
        scenario.workflow,
        repo,
        result_with_outcome,
        environment_probe or probe_benchmark_environment(),
    )
    return {
        "scenario": scenario.scenario,
        "task": scenario.task,
        "workflow_expected": scenario.workflow,
        "workflow_observed": result.get("workflow"),
        "raw_workflow_success": bool(result.get("success")),
        "workflow_success": bool(result.get("success")),
        "workflow_execution_success": outcome["workflow_execution_success"],
        "task_validation_success": outcome["task_validation_success"],
        "benchmark_outcome": outcome["benchmark_outcome"],
        "outcome_reason": outcome["outcome_reason"],
        "counts_as_benchmark_failure": outcome["counts_as_benchmark_failure"],
        "environment_status": environment["environment_status"],
        "environment_requirements": environment["environment_requirements"],
        "missing_dependencies": environment["missing_dependencies"],
        "environment_issue_attribution": environment["environment_issue_attribution"],
        "validation_status": result.get("validation_status"),
        "baseline_steps": baseline_steps,
        "skilllayer_steps": skilllayer_steps,
        "actions_saved": actions_saved,
        "reduction_percent": percent_reduction(baseline_steps, skilllayer_steps),
        "baseline_estimated_context_units": baseline_context_units,
        "skilllayer_estimated_context_units": skilllayer_context_units,
        "estimated_context_units_saved": baseline_context_units - skilllayer_context_units,
        "estimated_context_reduction_percent": percent_reduction(baseline_context_units, skilllayer_context_units),
        "baseline_estimated_seconds": round(sum(action.estimated_seconds for action in scenario.baseline_actions), 3),
        "skilllayer_observed_seconds": observed_seconds,
        "baseline_actions": [action.__dict__ for action in scenario.baseline_actions],
        "skilllayer_tool_calls": int(result.get("tool_calls", 0)),
        "skilllayer_llm_calls": int(result.get("llm_calls", 0)),
        "tests_run": bool(result.get("tests_run", False)),
        "tests_passed": result.get("tests_passed"),
        "artifact_keys": sorted(list((result.get("artifacts") or {}).keys())),
        "result_summary": compact_result_summary(result),
        "rationale": scenario.rationale,
        "estimated_savings_only": True,
        "proxy_metric": True,
        "requires_external_validation": True,
    }


def classify_benchmark_outcome(result: dict[str, Any], scenario: BenchmarkScenario | str) -> dict[str, Any]:
    workflow = scenario.workflow if isinstance(scenario, BenchmarkScenario) else str(scenario)
    if workflow != "RunTestsWorkflow":
        success = bool(result.get("success"))
        return {
            "benchmark_outcome": "PASSED" if success else "UNKNOWN",
            "outcome_reason": "Workflow returned success." if success else "Workflow did not return success.",
            "workflow_execution_success": success,
            "task_validation_success": success,
            "counts_as_benchmark_failure": not success,
        }
    return classify_test_benchmark_outcome(result)


def classify_test_benchmark_outcome(result: dict[str, Any]) -> dict[str, Any]:
    artifacts = result.get("artifacts") or {}
    error_code = normalized_str(first_present(result, artifacts, "error_code"))
    validation_status = normalized_str(first_present(result, artifacts, "validation_status"))
    tests_run = first_present(result, artifacts, "tests_run")
    tests_passed = first_present(result, artifacts, "tests_passed")
    exit_code = first_present(result, artifacts, "exit_code")
    failure_count = first_present(result, artifacts, "failure_count")
    failed_tests = first_present(result, artifacts, "failed_tests") or []
    no_tests_discovered = bool(first_present(result, artifacts, "no_tests_discovered"))
    test_count = first_present(result, artifacts, "test_count")
    unsupported = bool(first_present(result, artifacts, "unsupported"))

    if error_code == "no_tests_discovered" or no_tests_discovered or test_count == 0:
        return test_outcome(
            "NO_TESTS_DISCOVERED",
            "No tests were discovered; this is not a passed test run.",
            workflow_execution_success=True,
            task_validation_success=False,
            counts_as_benchmark_failure=False,
        )
    if error_code == "no_test_command_detected":
        return test_outcome(
            "NO_TEST_COMMAND",
            "No test command was detected.",
            workflow_execution_success=True,
            task_validation_success=False,
            counts_as_benchmark_failure=False,
        )
    if unsupported or error_code in {"single_test_not_supported", "unsupported_test_runner", "unsupported_environment"}:
        return test_outcome(
            "UNSUPPORTED",
            "The test request was recognized as unsupported.",
            workflow_execution_success=True,
            task_validation_success=False,
            counts_as_benchmark_failure=False,
        )
    if tests_run is True and tests_passed is True and validation_status == "passed":
        return test_outcome(
            "PASSED",
            "Tests ran and passed.",
            workflow_execution_success=True,
            task_validation_success=True,
            counts_as_benchmark_failure=False,
        )
    if tests_run is True and tests_passed is False and validation_status == "failed":
        if known_failure_count(failure_count) or failed_tests:
            return test_outcome(
                "FAILED_TESTS",
                "Tests ran and failed.",
                workflow_execution_success=True,
                task_validation_success=False,
                counts_as_benchmark_failure=False,
            )
        if exit_code not in (None, 0):
            return test_outcome(
                "COMMAND_ERROR",
                "Test command failed without a parseable test failure count.",
                workflow_execution_success=False,
                task_validation_success=False,
                counts_as_benchmark_failure=True,
            )
    if exit_code not in (None, 0) and not known_failure_count(failure_count):
        return test_outcome(
            "COMMAND_ERROR",
            "Command exited nonzero without a known test failure outcome.",
            workflow_execution_success=False,
            task_validation_success=False,
            counts_as_benchmark_failure=True,
        )
    return test_outcome(
        "UNKNOWN",
        "Benchmark could not classify the test workflow result.",
        workflow_execution_success=False,
        task_validation_success=False,
        counts_as_benchmark_failure=True,
    )


def test_outcome(
    benchmark_outcome: str,
    outcome_reason: str,
    *,
    workflow_execution_success: bool,
    task_validation_success: bool,
    counts_as_benchmark_failure: bool,
) -> dict[str, Any]:
    return {
        "benchmark_outcome": benchmark_outcome,
        "outcome_reason": outcome_reason,
        "workflow_execution_success": workflow_execution_success,
        "task_validation_success": task_validation_success,
        "counts_as_benchmark_failure": counts_as_benchmark_failure,
    }


def first_present(primary: dict[str, Any], secondary: dict[str, Any], key: str) -> Any:
    if key in primary:
        return primary[key]
    return secondary.get(key)


def normalized_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower()


def known_failure_count(value: Any) -> bool:
    if value is None:
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def estimate_skilllayer_context_units(result: dict[str, Any]) -> int:
    visible_payload = {
        "success": result.get("success"),
        "workflow": result.get("workflow"),
        "validation_status": result.get("validation_status"),
        "tests_run": result.get("tests_run"),
        "tests_passed": result.get("tests_passed"),
        "artifacts": result.get("artifacts", {}),
        "summary": result.get("summary"),
        "matches": result.get("matches"),
        "changed_files": result.get("changed_files"),
        "usage_files": result.get("usage_files"),
        "failed_tests": result.get("failed_tests"),
        "diagnoses": result.get("diagnoses"),
    }
    encoded = json.dumps(visible_payload, sort_keys=True, default=str)
    return max(1, math.ceil(len(encoded) / 1000))


def compact_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "success",
        "workflow",
        "validation_status",
        "tests_run",
        "tests_passed",
        "error_code",
        "summary",
        "matches",
        "changed_files",
        "usage_count",
        "failure_count",
        "diagnosis_count",
        "exit_code",
        "test_count",
        "no_tests_discovered",
    ]
    summary: dict[str, Any] = {}
    for key in keys:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, list):
            summary[key] = {"count": len(value), "sample": value[:3]}
        else:
            summary[key] = value
    return summary


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    reductions = [float(item["reduction_percent"]) for item in results]
    context_reductions = [float(item["estimated_context_reduction_percent"]) for item in results]
    best = max(results, key=lambda item: item["reduction_percent"]) if results else None
    worst = min(results, key=lambda item: item["reduction_percent"]) if results else None
    total_baseline_steps = sum(int(item["baseline_steps"]) for item in results)
    total_skilllayer_steps = sum(int(item["skilllayer_steps"]) for item in results)
    total_baseline_context = sum(int(item["baseline_estimated_context_units"]) for item in results)
    total_skilllayer_context = sum(int(item["skilllayer_estimated_context_units"]) for item in results)
    validation_rows = [item for item in results if isinstance(item.get("task_validation_success"), bool)]
    return {
        "scenario_count": len(results),
        "average_step_reduction": round(sum(reductions) / len(reductions), 3) if reductions else 0.0,
        "average_estimated_context_reduction": round(sum(context_reductions) / len(context_reductions), 3) if context_reductions else 0.0,
        "best_case": best["scenario"] if best else None,
        "best_case_reduction_percent": best["reduction_percent"] if best else None,
        "worst_case": worst["scenario"] if worst else None,
        "worst_case_reduction_percent": worst["reduction_percent"] if worst else None,
        "total_baseline_steps": total_baseline_steps,
        "total_skilllayer_steps": total_skilllayer_steps,
        "total_actions_saved": total_baseline_steps - total_skilllayer_steps,
        "total_step_reduction_percent": percent_reduction(total_baseline_steps, total_skilllayer_steps),
        "total_baseline_estimated_context_units": total_baseline_context,
        "total_skilllayer_estimated_context_units": total_skilllayer_context,
        "total_estimated_context_reduction_percent": percent_reduction(total_baseline_context, total_skilllayer_context),
        "workflow_success_rate": round(sum(1 for item in results if item["workflow_success"]) / len(results), 3) if results else 0.0,
        "workflow_execution_success_rate": (
            round(sum(1 for item in results if item.get("workflow_execution_success")) / len(results), 3)
            if results
            else 0.0
        ),
        "task_validation_success_rate": (
            round(sum(1 for item in validation_rows if item.get("task_validation_success")) / len(validation_rows), 3)
            if validation_rows
            else 0.0
        ),
        "benchmark_failure_count": sum(1 for item in results if item.get("counts_as_benchmark_failure")),
        "not_applicable_count": sum(
            1
            for item in results
            if item.get("benchmark_outcome") in {"NO_TESTS_DISCOVERED", "NO_TEST_COMMAND", "UNSUPPORTED"}
        ),
        "test_outcome_distribution": build_outcome_distribution(results),
        **summarize_environment_status(results),
        "estimated_savings_only": True,
        "does_not_prove_token_savings": True,
        "requires_external_validation": True,
    }


def build_methodology_report(repo: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "target_repository": str(repo),
        "scenarios": [scenario.scenario for scenario in SCENARIOS],
        "baseline_model": {
            "description": "Manual baseline is a proxy model of routine agent actions: commands executed, files read, and outputs inspected.",
            "not_measured": [
                "real Claude Code token usage",
                "real Cursor token usage",
                "real paid LLM cost",
                "human cognitive cost",
            ],
            "action_step_definition": "One command, search, file read, output inspection, or summary operation counts as one baseline step.",
            "skilllayer_step_definition": "One external SkillLayer workflow invocation counts as one agent action; internal tool calls are reported separately.",
        },
        "context_unit_model": {
            "description": "estimated_context_units are rough proxy units, not tokens.",
            "baseline": "Sum of per-action context-unit estimates based on expected command output, file reads, and logs inspected.",
            "skilllayer": "Approximate size of the structured workflow result divided into 1000-character units.",
            "why_not_tokens": "Provider-level token accounting is unavailable for external Claude Code/Cursor sessions in this local benchmark.",
        },
        "environment_model": {
            "description": "Benchmark reports local tool availability separately from workflow and validation outcomes.",
            "detected_dependencies": ["python", "pytest", "node", "npm", "git"],
            "status_categories": [
                "ENVIRONMENT_OK",
                "ENVIRONMENT_MISSING_DEPENDENCY",
                "ENVIRONMENT_UNSUPPORTED",
            ],
            "does_not_change_scoring": True,
            "why_it_matters": "Missing pytest/node/npm/git can affect benchmark outcomes without implying a workflow defect.",
        },
        "conservative_language_required": [
            "estimated",
            "proxy metric",
            "requires external validation",
            "does not prove token savings",
        ],
        "limitations": [
            "The baseline is scripted and approximate.",
            "SkillLayer is benchmarked on its own repository first, not a multi-repository sample.",
            "Time comparisons mix estimated baseline seconds with observed SkillLayer seconds.",
            "ExplainFailureWorkflow may not have a real failing test in the current repository.",
            "Results should not be generalized beyond these scenarios.",
        ],
        "scenario_success_states": {
            item["scenario"]: {
                "workflow_success": item["workflow_success"],
                "workflow_execution_success": item.get("workflow_execution_success"),
                "task_validation_success": item.get("task_validation_success"),
                "benchmark_outcome": item.get("benchmark_outcome"),
                "counts_as_benchmark_failure": item.get("counts_as_benchmark_failure"),
                "validation_status": item["validation_status"],
            }
            for item in results
        },
    }


def build_outcome_distribution(results: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {outcome: 0 for outcome in TEST_OUTCOMES}
    for item in results:
        outcome = item.get("benchmark_outcome")
        if outcome in distribution:
            distribution[str(outcome)] += 1
    return distribution


def percent_reduction(baseline: int | float, skilllayer: int | float) -> float:
    if baseline <= 0:
        return 0.0
    return round(((baseline - skilllayer) / baseline) * 100.0, 3)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def main() -> int:
    report = run_token_savings_benchmark(write_outputs=True)
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
