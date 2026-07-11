from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from .benchmark_environment import summarize_environment_status
from .token_savings_benchmark import PROJECT_ROOT, TEST_OUTCOMES, run_token_savings_benchmark


OUTPUT_DIR = PROJECT_ROOT / "runs" / "benchmark_automation"
DEFAULT_MATRIX_PATH = PROJECT_ROOT / "benchmark_repos.json"


def run_benchmark_automation(
    matrix_path: str | Path | None = None,
    *,
    write_outputs: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    matrix_file = Path(matrix_path or DEFAULT_MATRIX_PATH).resolve()
    matrix_report = load_benchmark_matrix(matrix_file)
    repo_results: list[dict[str, Any]] = []
    for entry in matrix_report["repos"]:
        repo_results.append(run_repo_entry(entry, matrix_file.parent))

    aggregate_metrics = build_aggregate_metrics(repo_results)
    benchmark_health = build_benchmark_health(repo_results)
    methodology_notes = build_methodology_notes(matrix_file, matrix_report)
    report = {
        "milestone": "Benchmark Automation",
        "success": benchmark_health["failed_benchmarks"] == 0,
        "duration_ms": elapsed_ms(started),
        "matrix_path": str(matrix_file),
        "repo_results": repo_results,
        "aggregate_metrics": aggregate_metrics,
        "benchmark_health": benchmark_health,
        "methodology_notes": methodology_notes,
        "estimated_savings_only": True,
        "proxy_metrics_only": True,
        "does_not_prove_token_savings": True,
        "workflow_behavior_changed": False,
        "routing_changed": False,
        "llm_usage": "none",
        "network_access_required": False,
        "outputs": {
            "repo_results": "runs/benchmark_automation/repo_results.json",
            "aggregate_metrics": "runs/benchmark_automation/aggregate_metrics.json",
            "benchmark_health": "runs/benchmark_automation/benchmark_health.json",
            "methodology_notes": "runs/benchmark_automation/methodology_notes.json",
            "report": "runs/benchmark_automation/report.json",
        },
    }
    if write_outputs:
        write_json(OUTPUT_DIR / "repo_results.json", repo_results)
        write_json(OUTPUT_DIR / "aggregate_metrics.json", aggregate_metrics)
        write_json(OUTPUT_DIR / "benchmark_health.json", benchmark_health)
        write_json(OUTPUT_DIR / "methodology_notes.json", methodology_notes)
        write_json(OUTPUT_DIR / "report.json", report)
    return report


def load_benchmark_matrix(matrix_path: Path) -> dict[str, Any]:
    if not matrix_path.exists():
        raise FileNotFoundError(f"benchmark matrix not found: {matrix_path}")
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    repos = payload.get("repos")
    if not isinstance(repos, list):
        raise ValueError("benchmark matrix must contain a 'repos' list")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(repos):
        if not isinstance(item, dict):
            raise ValueError(f"repo entry {index} must be an object")
        name = str(item.get("name") or f"repo_{index}")
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValueError(f"repo entry {name!r} is missing path")
        normalized.append({"name": name, "path": path})
    return {"repos": normalized}


def run_repo_entry(entry: dict[str, Any], matrix_dir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    name = str(entry["name"])
    raw_path = str(entry["path"])
    base_result: dict[str, Any] = {
        "name": name,
        "path": raw_path,
        "benchmark_failed": False,
        "failure_reason": None,
        "duration_ms": None,
        "scenario_count": 0,
        "workflow_success_rate": 0.0,
        "benchmark_summary": None,
        "scenario_results": [],
    }
    try:
        repo_path = resolve_local_repo_path(raw_path, matrix_dir)
        report = run_token_savings_benchmark(repo_path, write_outputs=False)
        scenario_results = report.get("scenario_results", [])
        benchmark_summary = report.get("benchmark_summary", {})
        base_result.update(
            {
                "resolved_path": str(repo_path),
                "environment_probe": report.get("environment_probe"),
                "scenario_count": len(scenario_results),
                "workflow_success_rate": float(benchmark_summary.get("workflow_success_rate", 0.0)),
                "benchmark_summary": benchmark_summary,
                "scenario_results": scenario_results,
                "duration_ms": elapsed_ms(started),
            }
        )
    except Exception as exc:  # noqa: BLE001 - benchmark automation must keep going.
        base_result.update(
            {
                "benchmark_failed": True,
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "duration_ms": elapsed_ms(started),
            }
        )
    return base_result


def resolve_local_repo_path(raw_path: str, matrix_dir: Path) -> Path:
    lowered = raw_path.lower()
    if "://" in lowered or lowered.startswith("git@"):
        raise ValueError("only local repository paths are allowed; cloning/network paths are not supported")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (matrix_dir / path).resolve()
    else:
        path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"repository path does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"repository path is not a directory: {path}")
    return path


def build_aggregate_metrics(repo_results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [item for item in repo_results if not item["benchmark_failed"]]
    all_scenarios = [
        scenario
        for repo in successful
        for scenario in repo.get("scenario_results", [])
    ]
    reductions = [float(item.get("reduction_percent", 0.0)) for item in all_scenarios]
    repo_scores = [
        {
            "name": item["name"],
            "average_action_reduction": float((item.get("benchmark_summary") or {}).get("average_step_reduction", 0.0)),
        }
        for item in successful
    ]
    best_repo = max(repo_scores, key=lambda item: item["average_action_reduction"]) if repo_scores else None
    worst_repo = min(repo_scores, key=lambda item: item["average_action_reduction"]) if repo_scores else None
    workflow_success_rate = (
        round(sum(1 for item in all_scenarios if item.get("workflow_success")) / len(all_scenarios), 3)
        if all_scenarios
        else 0.0
    )
    workflow_execution_success_rate = (
        round(sum(1 for item in all_scenarios if item.get("workflow_execution_success")) / len(all_scenarios), 3)
        if all_scenarios
        else 0.0
    )
    validation_rows = [item for item in all_scenarios if isinstance(item.get("task_validation_success"), bool)]
    task_validation_success_rate = (
        round(sum(1 for item in validation_rows if item.get("task_validation_success")) / len(validation_rows), 3)
        if validation_rows
        else 0.0
    )
    return {
        "repo_count": len(repo_results),
        "successful_repo_count": len(successful),
        "failed_repo_count": len(repo_results) - len(successful),
        "scenario_count": len(all_scenarios),
        "average_action_reduction": round(sum(reductions) / len(reductions), 3) if reductions else 0.0,
        "median_action_reduction": round(statistics.median(reductions), 3) if reductions else 0.0,
        "best_repo": best_repo,
        "worst_repo": worst_repo,
        "workflow_success_rate": workflow_success_rate,
        "workflow_execution_success_rate": workflow_execution_success_rate,
        "task_validation_success_rate": task_validation_success_rate,
        "benchmark_failure_count": sum(1 for item in all_scenarios if item.get("counts_as_benchmark_failure")),
        "not_applicable_count": sum(
            1
            for item in all_scenarios
            if item.get("benchmark_outcome") in {"NO_TESTS_DISCOVERED", "NO_TEST_COMMAND", "UNSUPPORTED"}
        ),
        "test_outcome_distribution": build_outcome_distribution(all_scenarios),
        **summarize_environment_status(all_scenarios),
        "estimated_savings_only": True,
        "proxy_metrics_only": True,
        "does_not_prove_token_savings": True,
    }


def build_outcome_distribution(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {outcome: 0 for outcome in TEST_OUTCOMES}
    for item in scenarios:
        outcome = item.get("benchmark_outcome")
        if outcome in distribution:
            distribution[str(outcome)] += 1
    return distribution


def build_benchmark_health(repo_results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item for item in repo_results if item["benchmark_failed"]]
    successful = [item for item in repo_results if not item["benchmark_failed"]]
    return {
        "repositories_tested": len(repo_results),
        "repositories_failed": len(failed),
        "successful_benchmarks": len(successful),
        "failed_benchmarks": len(failed),
        "failed_repositories": [
            {
                "name": item["name"],
                "path": item["path"],
                "failure_reason": item["failure_reason"],
            }
            for item in failed
        ],
        "conservative_status": "complete" if not failed else "completed_with_failures",
    }


def build_methodology_notes(matrix_path: Path, matrix_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "matrix_path": str(matrix_path),
        "configured_repo_count": len(matrix_report["repos"]),
        "local_repositories_only": True,
        "network_access": "not used",
        "cloning": "not supported",
        "baseline_model": "inherits scripted proxy baseline per scenario",
        "context_units": "estimated proxy units, not provider tokens",
        "aggregation_scope": "only successful repository benchmarks contribute scenario-level reduction metrics",
        "failed_repo_handling": "failed repositories are recorded and remaining repositories continue",
        "limitations": [
            "This automation does not prove token savings.",
            "Repository coverage depends entirely on benchmark_repos.json.",
            "The baseline is still a scripted proxy, not observed Claude Code/Cursor telemetry.",
            "Workflow failures are reported as benchmark observations, not optimized away.",
            "A small matrix should not be generalized to broad coding-agent performance.",
        ],
        "conservative_language_required": [
            "estimated",
            "proxy metric",
            "requires external validation",
            "does not prove token savings",
        ],
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def main() -> int:
    report = run_benchmark_automation(write_outputs=True)
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
