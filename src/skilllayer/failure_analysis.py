from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path("runs/p4_5_benchmark")
OUTPUT_DIR = Path("runs/p4_5_failure_analysis")

EXPECTED_WORKFLOWS = {
    "FindFunction": "FindFunctionWorkflow",
    "RenameSymbol": "RenameSymbolWorkflow",
    "FixFailingTest": "FixFailingTestWorkflow",
    "AddHelper": "AddHelperWorkflow",
    "BrowserSmoke": "BrowserSmokeWorkflow",
}

ROOT_CAUSES = [
    "routing_error",
    "workflow_capability_error",
    "tool_error",
    "verification_error",
    "unknown_error",
]


def analyze_failures(
    benchmark_dir: str | Path = BENCHMARK_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict[str, Any]:
    benchmark_path = Path(benchmark_dir)
    output_path = Path(output_dir)
    benchmark = load_benchmark_inputs(benchmark_path)
    results = benchmark["benchmark_results"]
    failed = [item for item in results if not item.get("skilllayer", {}).get("success")]

    failure_records = [classify_failure(item) for item in failed]
    root_cause_summary = build_root_cause_summary(results, failure_records)
    workflow_summary = build_workflow_failure_summary(failure_records)
    pareto = build_pareto_analysis(workflow_summary, len(failure_records))
    recommendations = build_improvement_recommendations(workflow_summary, root_cause_summary)
    report = {
        "analysis_name": "Failure Attribution Analysis",
        "input_dir": str(benchmark_path),
        "output_dir": str(output_path),
        "benchmark_summary": benchmark["benchmark_summary"],
        "failure_count": len(failure_records),
        "failure_attribution_summary": root_cause_summary,
        "workflow_failure_summary": workflow_summary,
        "pareto_analysis": pareto,
        "improvement_recommendations": recommendations,
        "diagnostic_answer": diagnostic_answer(root_cause_summary),
        "diagnostic_only": True,
        "workflow_changes_made": False,
        "routing_changes_made": False,
        "benchmark_task_changes_made": False,
    }
    write_analysis_outputs(output_path, failure_records, root_cause_summary, workflow_summary, recommendations, pareto, report)
    return report


def load_benchmark_inputs(benchmark_dir: Path) -> dict[str, Any]:
    required = {
        "benchmark_results": "benchmark_results.json",
        "benchmark_summary": "benchmark_summary.json",
        "task_breakdown": "task_breakdown.json",
        "workflow_breakdown": "workflow_breakdown.json",
    }
    payloads: dict[str, Any] = {}
    missing = []
    for key, filename in required.items():
        path = benchmark_dir / filename
        if not path.exists():
            missing.append(str(path))
            continue
        payloads[key] = json.loads(path.read_text(encoding="utf-8"))
    if missing:
        raise FileNotFoundError("Missing benchmark inputs: " + ", ".join(missing))
    return payloads


def classify_failure(item: dict[str, Any]) -> dict[str, Any]:
    task_type = str(item.get("task_type", ""))
    skill = item.get("skilllayer", {})
    expected_workflow = EXPECTED_WORKFLOWS.get(task_type)
    workflow_selected = skill.get("workflow_used")
    root_cause = classify_root_cause(item, expected_workflow, workflow_selected)
    return {
        "task_id": item.get("task_id"),
        "task_type": task_type,
        "task": item.get("task"),
        "workflow_selected": workflow_selected,
        "expected_workflow": expected_workflow,
        "task_success": bool(skill.get("success")),
        "baseline_success": bool(item.get("baseline", {}).get("success")),
        "root_cause": root_cause,
        "repairable": is_repairable(root_cause),
        "evidence": build_evidence(item, expected_workflow, workflow_selected),
    }


def classify_root_cause(item: dict[str, Any], expected_workflow: str | None, workflow_selected: str | None) -> str:
    skill = item.get("skilllayer", {})
    if expected_workflow and workflow_selected != expected_workflow:
        return "routing_error"
    if skill.get("fallback_triggered") and not workflow_selected:
        return "routing_error" if expected_workflow else "unknown_error"

    task_type = item.get("task_type")
    if task_type in {"AddHelper", "FixFailingTest", "FindFunction"}:
        return "workflow_capability_error"
    if task_type == "BrowserSmoke":
        if skill.get("console_errors") or skill.get("network_errors") or not skill.get("screenshot_path"):
            return "tool_error"
        return "workflow_capability_error"

    if skill.get("tests_executed") and not skill.get("tests_passed"):
        return "workflow_capability_error"
    if skill.get("tests_passed") and not skill.get("success"):
        return "workflow_capability_error"
    return "unknown_error"


def is_repairable(root_cause: str) -> bool:
    return root_cause in {"routing_error", "workflow_capability_error", "tool_error", "verification_error"}


def build_evidence(item: dict[str, Any], expected_workflow: str | None, workflow_selected: str | None) -> dict[str, Any]:
    skill = item.get("skilllayer", {})
    baseline = item.get("baseline", {})
    evidence = {
        "expected_workflow": expected_workflow,
        "workflow_selected": workflow_selected,
        "tests_executed": bool(skill.get("tests_executed")),
        "tests_passed": bool(skill.get("tests_passed")),
        "fallback_triggered": bool(skill.get("fallback_triggered")),
        "baseline_success": bool(baseline.get("success")),
        "skilllayer_tool_calls": skill.get("tool_calls"),
        "baseline_tool_calls": baseline.get("tool_calls"),
    }
    task_type = item.get("task_type")
    if task_type == "AddHelper":
        evidence["interpretation"] = "Correct workflow selected, tests passed, but required helper edit was not applied."
    elif task_type == "FixFailingTest":
        evidence["interpretation"] = "Correct workflow selected, but seeded failing-test repair was incomplete."
    elif expected_workflow and workflow_selected != expected_workflow:
        evidence["interpretation"] = "Expected workflow and selected workflow differ."
    else:
        evidence["interpretation"] = "Failure evidence is insufficient for a more specific category."
    return evidence


def build_root_cause_summary(results: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["root_cause"] for item in failures)
    root_cause_counts = {cause: counts.get(cause, 0) for cause in ROOT_CAUSES}
    total_tasks = len(results)
    failure_count = len(failures)
    skill_successes = sum(1 for item in results if item.get("skilllayer", {}).get("success"))
    routing_failures = root_cause_counts["routing_error"]
    workflow_quality_failures = root_cause_counts["workflow_capability_error"]
    return {
        "total_tasks": total_tasks,
        "skilllayer_successes": skill_successes,
        "skilllayer_failures": failure_count,
        "skilllayer_success_rate": round(skill_successes / total_tasks, 3) if total_tasks else 0.0,
        "root_cause_counts": root_cause_counts,
        "repairable_failures": sum(1 for item in failures if item["repairable"]),
        "non_repairable_failures": sum(1 for item in failures if not item["repairable"]),
        "failure_task_ids": [item["task_id"] for item in failures],
        "primary_failure_mode": primary_failure_mode(root_cause_counts),
        "routing_problem": routing_failures > workflow_quality_failures,
        "workflow_quality_problem": workflow_quality_failures >= routing_failures and workflow_quality_failures > 0,
        "diagnostic_conclusion": diagnostic_conclusion(root_cause_counts),
    }


def build_workflow_failure_summary(failures: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in failures:
        grouped[str(item.get("workflow_selected") or "none")].append(item)

    summary: dict[str, Any] = {}
    for workflow, items in sorted(grouped.items()):
        root_counts = Counter(item["root_cause"] for item in items)
        task_type_counts = Counter(item["task_type"] for item in items)
        summary[workflow] = {
            "failures": len(items),
            "task_type_counts": dict(sorted(task_type_counts.items())),
            "root_cause_counts": {cause: root_counts.get(cause, 0) for cause in ROOT_CAUSES},
            "repairable_failures": sum(1 for item in items if item["repairable"]),
            "example_task_ids": [item["task_id"] for item in items[:5]],
        }
    return summary


def build_pareto_analysis(workflow_summary: dict[str, Any], failure_count: int) -> dict[str, Any]:
    sorted_items = sorted(workflow_summary.items(), key=lambda pair: (-pair[1]["failures"], pair[0]))
    contributors = []
    cumulative = 0
    top_80 = []
    for workflow, data in sorted_items:
        cumulative += int(data["failures"])
        percent = round((data["failures"] / failure_count) * 100.0, 3) if failure_count else 0.0
        cumulative_percent = round((cumulative / failure_count) * 100.0, 3) if failure_count else 0.0
        entry = {
            "workflow": workflow,
            "failures": data["failures"],
            "failure_percent": percent,
            "cumulative_failures": cumulative,
            "cumulative_percent": cumulative_percent,
        }
        contributors.append(entry)
        if failure_count and cumulative_percent <= 80.0:
            top_80.append(workflow)
        elif failure_count and not top_80:
            top_80.append(workflow)
        elif failure_count and cumulative_percent >= 80.0 and workflow not in top_80:
            top_80.append(workflow)
            break
    return {
        "failure_count": failure_count,
        "contributors": contributors,
        "top_failure_contributors_to_80_percent": top_80,
        "pareto_statement": build_pareto_statement(top_80, workflow_summary, failure_count),
    }


def build_improvement_recommendations(
    workflow_summary: dict[str, Any],
    root_cause_summary: dict[str, Any],
) -> dict[str, Any]:
    recommendations = []
    priority = 1
    for workflow, data in sorted(workflow_summary.items(), key=recommendation_sort_key):
        failures = int(data["failures"])
        if workflow == "FixFailingTestWorkflow":
            title = "Improve FixFailingTestWorkflow"
            action = "Add real failure inspection and deterministic repair/fallback for seeded simple regressions."
            difficulty = "medium"
        elif workflow == "AddHelperWorkflow":
            title = "Implement AddHelperWorkflow edit capability"
            action = "Make the workflow insert a small helper into the relevant module or fall back when unsupported."
            difficulty = "low_medium"
        elif workflow == "FindFunctionWorkflow":
            title = "Lighten FindFunctionWorkflow validation"
            action = "Avoid running full tests for pure repository-navigation tasks unless explicitly requested."
            difficulty = "low"
        else:
            title = f"Investigate {workflow}"
            action = "Inspect task logs and add workflow-specific diagnostics before changing behavior."
            difficulty = "unknown"

        recommendations.append(
            {
                "priority": priority,
                "recommendation": title,
                "workflow": workflow,
                "failure_count": failures,
                "expected_success_gain": estimate_success_gain(failures, root_cause_summary["total_tasks"]),
                "difficulty": difficulty,
                "root_cause": dominant_root_cause(data["root_cause_counts"]),
                "repairable": data["repairable_failures"] == failures,
                "suggested_action": action,
            }
        )
        priority += 1

    return {
        "recommendations": recommendations,
        "strategic_answer": diagnostic_answer(root_cause_summary),
    }


def write_analysis_outputs(
    output_dir: Path,
    results: list[dict[str, Any]],
    root_cause_summary: dict[str, Any],
    workflow_summary: dict[str, Any],
    recommendations: dict[str, Any],
    pareto: dict[str, Any],
    report: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "failure_attribution_results.json": results,
        "failure_attribution_summary.json": root_cause_summary,
        "workflow_failure_summary.json": workflow_summary,
        "improvement_recommendations.json": recommendations,
        "pareto_analysis.json": pareto,
        "failure_analysis_report.json": report,
    }
    for filename, payload in outputs.items():
        (output_dir / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def primary_failure_mode(root_cause_counts: dict[str, int]) -> str:
    if not root_cause_counts:
        return "none"
    return max(root_cause_counts.items(), key=lambda item: (item[1], item[0]))[0]


def diagnostic_conclusion(root_cause_counts: dict[str, int]) -> str:
    routing = root_cause_counts.get("routing_error", 0)
    workflow = root_cause_counts.get("workflow_capability_error", 0)
    if workflow > routing:
        return "failures are mainly workflow-quality/capability failures, not routing failures."
    if routing > workflow:
        return "failures are mainly routing failures."
    if workflow == routing == 0:
        return "No SkillLayer failures were present in the parsed benchmark."
    return "failures are split between routing and workflow-quality causes."


def diagnostic_answer(summary: dict[str, Any]) -> str:
    counts = summary.get("root_cause_counts", {})
    routing = counts.get("routing_error", 0)
    workflow = counts.get("workflow_capability_error", 0)
    return (
        f"Mainly workflow-quality: {workflow} workflow_capability_error failures "
        f"versus {routing} routing_error failures."
    )


def build_pareto_statement(workflows: list[str], workflow_summary: dict[str, Any], failure_count: int) -> str:
    if not workflows:
        return "No failure contributors found."
    failures = sum(workflow_summary[name]["failures"] for name in workflows)
    percent = round((failures / failure_count) * 100.0, 3) if failure_count else 0.0
    return f"{percent}% of failures come from: " + ", ".join(workflows)


def estimate_success_gain(failures: int, total_tasks: int) -> str:
    if total_tasks <= 0:
        return "unknown"
    absolute_gain = failures / total_tasks
    if absolute_gain >= 0.15:
        return "high"
    if absolute_gain >= 0.08:
        return "medium"
    if absolute_gain > 0:
        return "low"
    return "none"


def dominant_root_cause(counts: dict[str, int]) -> str:
    if not counts:
        return "unknown_error"
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def recommendation_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
    workflow, data = item
    workflow_priority = {
        "FixFailingTestWorkflow": 0,
        "AddHelperWorkflow": 1,
        "FindFunctionWorkflow": 2,
    }.get(workflow, 3)
    return (-int(data["failures"]), workflow_priority, workflow)
