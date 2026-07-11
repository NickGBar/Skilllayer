from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .benchmark import BaselineAgent, create_task_dataset, materialize_task, prepare_task_repo
from .benchmark import run_skilllayer_agent
from .cost_tracking import read_usage_events
from .pricing import calculate_event_cost


AB_BENCHMARK_DIR = Path("runs/p5_6_3_ab_benchmark")

WORKFLOW_BY_TASK_TYPE = {
    "FindFunction": "FindFunctionWorkflow",
    "RenameSymbol": "RenameSymbolWorkflow",
    "AddHelper": "AddHelperWorkflow",
    "FixFailingTest": "FixFailingTestWorkflow",
    "BrowserSmoke": "BrowserSmokeWorkflow",
}

OUTCOME_INSUFFICIENT = "Insufficient data"


def run_ab_benchmark() -> dict[str, Any]:
    """Run baseline-vs-SkillLayer benchmark without estimating missing usage."""
    AB_BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    workdir = AB_BENCHMARK_DIR / "workdirs"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    tasks = create_task_dataset()
    records: list[dict[str, Any]] = []
    for task in tasks:
        baseline_repo = prepare_task_repo(workdir, task, "baseline")
        skilllayer_repo = prepare_task_repo(workdir, task, "skilllayer")
        baseline_task = materialize_task(task, baseline_repo)
        skilllayer_task = materialize_task(task, skilllayer_repo)

        baseline_metrics, baseline_usage = run_baseline_mode(baseline_repo, baseline_task)
        skilllayer_metrics, skilllayer_usage = run_skilllayer_mode(skilllayer_repo, skilllayer_task, task["task_id"])
        workflow_candidate = workflow_for_task(task)

        records.append(build_task_record(task, "baseline", workflow_candidate, baseline_metrics.to_dict(), baseline_usage))
        records.append(build_task_record(task, "skilllayer", workflow_candidate, skilllayer_metrics.to_dict(), skilllayer_usage))

    baseline_records = [record for record in records if record["mode"] == "baseline"]
    skilllayer_records = [record for record in records if record["mode"] == "skilllayer"]
    baseline_summary = summarize_mode(baseline_records)
    skilllayer_summary = summarize_mode(skilllayer_records)
    comparison = compare_modes(baseline_summary, skilllayer_summary)
    cost_per_successful_task = build_cost_per_successful_task(baseline_summary, skilllayer_summary, comparison)
    report = build_report(tasks, baseline_summary, skilllayer_summary, comparison, cost_per_successful_task)
    write_outputs(records, baseline_summary, skilllayer_summary, comparison, cost_per_successful_task, report)
    return report


def run_baseline_mode(repo_path: Path, task: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    before = usage_event_count()
    metrics = BaselineAgent(repo_path).run(task)
    usage = summarize_usage_delta(before)
    return metrics, usage


def run_skilllayer_mode(repo_path: Path, task: dict[str, Any], task_id: str) -> tuple[Any, dict[str, Any]]:
    before = usage_event_count()
    log_dir = AB_BENCHMARK_DIR / "skilllayer_logs" / task_id
    metrics = run_skilllayer_agent(repo_path, task, log_dir=log_dir)
    usage = summarize_usage_delta(before)
    return metrics, usage


def usage_event_count() -> int:
    return len(read_usage_events())


def summarize_usage_delta(before_count: int) -> dict[str, Any]:
    events = read_usage_events()[before_count:]
    if not events:
        return {
            "usage_event_count": 0,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_known": False,
            "cost_known_event_count": 0,
            "cost_unknown_event_count": 0,
            "unknown_cost_reasons": {"no_usage_event": 1},
        }
    costs = [calculate_event_cost(event) for event in events]
    cost_known = all(cost["cost_known"] for cost in costs)
    return {
        "usage_event_count": len(events),
        "input_tokens": sum_field_or_none(events, "input_tokens"),
        "output_tokens": sum_field_or_none(events, "output_tokens"),
        "total_tokens": sum_field_or_none(events, "total_tokens"),
        "cost_usd": round(sum(float(cost["total_cost_usd"]) for cost in costs), 12) if cost_known else None,
        "cost_known": bool(cost_known),
        "cost_known_event_count": sum(1 for cost in costs if cost["cost_known"]),
        "cost_unknown_event_count": sum(1 for cost in costs if not cost["cost_known"]),
        "unknown_cost_reasons": unknown_reason_counts(costs),
    }


def sum_field_or_none(events: list[dict[str, Any]], field: str) -> int | None:
    values = [event.get(field) for event in events if event.get(field) is not None]
    if not values:
        return None
    return sum(int(value) for value in values)


def unknown_reason_counts(costs: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for cost in costs:
        if cost["cost_known"]:
            continue
        reasons = cost.get("unknown_reasons") or ["unknown"]
        for reason in reasons:
            counter[str(reason)] += 1
    return dict(sorted(counter.items()))


def build_task_record(
    task: dict[str, Any],
    mode: str,
    workflow_candidate: str,
    metrics: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "task_type": task["task_type"],
        "workflow_candidate": workflow_candidate,
        "mode": mode,
        "success": bool(metrics.get("success")),
        "duration_ms": float(metrics.get("duration_ms") or 0.0),
        "tool_calls": int(metrics.get("tool_calls") or 0),
        "file_reads": int(metrics.get("file_reads") or 0),
        "search_operations": int(metrics.get("search_operations") or 0),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost_usd"],
        "cost_known": bool(usage["cost_known"]),
        "usage_event_count": int(usage["usage_event_count"]),
        "cost_unknown_reasons": usage["unknown_cost_reasons"],
    }


def workflow_for_task(task: dict[str, Any]) -> str:
    return WORKFLOW_BY_TASK_TYPE.get(task["task_type"], "unknown")


def summarize_mode(records: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(1 for record in records if record["success"])
    cost_known_records = [record for record in records if record["cost_known"]]
    token_known_records = [record for record in records if record["total_tokens"] is not None]
    return {
        "mode": records[0]["mode"] if records else "unknown",
        "task_count": len(records),
        "success_count": successes,
        "success_rate": round(successes / len(records), 3) if records else 0.0,
        "total_duration_ms": round(sum(float(record["duration_ms"]) for record in records), 3),
        "total_tool_calls": sum(int(record["tool_calls"]) for record in records),
        "total_file_reads": sum(int(record["file_reads"]) for record in records),
        "total_search_operations": sum(int(record["search_operations"]) for record in records),
        "total_input_tokens": sum(int(record["input_tokens"] or 0) for record in token_known_records),
        "total_output_tokens": sum(int(record["output_tokens"] or 0) for record in token_known_records),
        "total_tokens": sum(int(record["total_tokens"] or 0) for record in token_known_records),
        "token_known_task_count": len(token_known_records),
        "token_unknown_task_count": len(records) - len(token_known_records),
        "total_cost_usd": round(sum(float(record["cost_usd"] or 0.0) for record in cost_known_records), 12),
        "cost_known_task_count": len(cost_known_records),
        "cost_unknown_task_count": len(records) - len(cost_known_records),
        "workflow_breakdown": summarize_by_field(records, "workflow_candidate"),
        "task_type_breakdown": summarize_by_field(records, "task_type"),
        "unknown_cost_reasons": summarize_unknown_cost_reasons(records),
    }


def summarize_by_field(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get(field) or "unknown")].append(record)
    return {
        key: {
            "task_count": len(items),
            "success_rate": round(sum(1 for item in items if item["success"]) / len(items), 3) if items else 0.0,
            "tool_calls": sum(int(item["tool_calls"]) for item in items),
            "file_reads": sum(int(item["file_reads"]) for item in items),
            "search_operations": sum(int(item["search_operations"]) for item in items),
            "duration_ms": round(sum(float(item["duration_ms"]) for item in items), 3),
        }
        for key, items in sorted(grouped.items())
    }


def summarize_unknown_cost_reasons(records: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        for reason, count in (record.get("cost_unknown_reasons") or {}).items():
            counter[str(reason)] += int(count)
    return dict(sorted(counter.items()))


def compare_modes(baseline: dict[str, Any], skilllayer: dict[str, Any]) -> dict[str, Any]:
    cost_comparison_possible = baseline["cost_unknown_task_count"] == 0 and skilllayer["cost_unknown_task_count"] == 0
    token_comparison_possible = baseline["token_unknown_task_count"] == 0 and skilllayer["token_unknown_task_count"] == 0
    comparison = {
        "baseline_success_rate": baseline["success_rate"],
        "skilllayer_success_rate": skilllayer["success_rate"],
        "baseline_total_cost_usd": baseline["total_cost_usd"],
        "skilllayer_total_cost_usd": skilllayer["total_cost_usd"],
        "baseline_total_tokens": baseline["total_tokens"],
        "skilllayer_total_tokens": skilllayer["total_tokens"],
        "baseline_duration_ms": baseline["total_duration_ms"],
        "skilllayer_duration_ms": skilllayer["total_duration_ms"],
        "cost_comparison_possible": cost_comparison_possible,
        "token_comparison_possible": token_comparison_possible,
        "cost_difference_percent": percent_difference(baseline["total_cost_usd"], skilllayer["total_cost_usd"]) if cost_comparison_possible else None,
        "token_difference_percent": percent_difference(baseline["total_tokens"], skilllayer["total_tokens"]) if token_comparison_possible else None,
        "duration_difference_percent": percent_difference(baseline["total_duration_ms"], skilllayer["total_duration_ms"]),
        "tool_call_difference_percent": percent_difference(baseline["total_tool_calls"], skilllayer["total_tool_calls"]),
        "file_read_difference_percent": percent_difference(baseline["total_file_reads"], skilllayer["total_file_reads"]),
        "search_difference_percent": percent_difference(baseline["total_search_operations"], skilllayer["total_search_operations"]),
        "honest_outcome": classify_outcome(baseline, skilllayer, cost_comparison_possible),
        "unknown_cost_handling": "Costs are compared only when every task record has observed usage and known pricing. Missing token counts are not inferred.",
    }
    return comparison


def build_cost_per_successful_task(
    baseline: dict[str, Any],
    skilllayer: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    possible = bool(comparison["cost_comparison_possible"])
    return {
        "cost_comparison_possible": possible,
        "baseline_successful_tasks": baseline["success_count"],
        "skilllayer_successful_tasks": skilllayer["success_count"],
        "cost_per_successful_task_baseline": safe_divide(baseline["total_cost_usd"], baseline["success_count"]) if possible else None,
        "cost_per_successful_task_skilllayer": safe_divide(skilllayer["total_cost_usd"], skilllayer["success_count"]) if possible else None,
        "reason_if_not_possible": None if possible else "One or more task records lack observed token usage or known pricing.",
    }


def classify_outcome(baseline: dict[str, Any], skilllayer: dict[str, Any], cost_comparison_possible: bool) -> str:
    if not cost_comparison_possible:
        return OUTCOME_INSUFFICIENT
    baseline_cost = float(baseline["total_cost_usd"])
    skilllayer_cost = float(skilllayer["total_cost_usd"])
    tolerance = 1e-12
    if skilllayer_cost < baseline_cost - tolerance:
        return "SkillLayer cheaper"
    if skilllayer_cost > baseline_cost + tolerance:
        return "SkillLayer more expensive"
    return "SkillLayer same cost"


def percent_difference(baseline_value: float, skilllayer_value: float) -> float | None:
    baseline_float = float(baseline_value)
    skilllayer_float = float(skilllayer_value)
    if baseline_float == 0.0:
        if skilllayer_float == 0.0:
            return 0.0
        return None
    return round(((baseline_float - skilllayer_float) / baseline_float) * 100.0, 3)


def safe_divide(numerator: float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / float(denominator), 12)


def build_report(
    tasks: list[dict[str, Any]],
    baseline_summary: dict[str, Any],
    skilllayer_summary: dict[str, Any],
    comparison: dict[str, Any],
    cost_per_successful_task: dict[str, Any],
) -> dict[str, Any]:
    return {
        "benchmark_name": "Controlled A/B Cost Benchmark",
        "task_count": len(tasks),
        "modes": ["baseline", "skilllayer"],
        "task_types": sorted({task["task_type"] for task in tasks}),
        "baseline_summary": baseline_summary,
        "skilllayer_summary": skilllayer_summary,
        "comparison": comparison,
        "cost_per_successful_task": cost_per_successful_task,
        "outputs": {
            "task_results": str(AB_BENCHMARK_DIR / "task_results.json"),
            "baseline_summary": str(AB_BENCHMARK_DIR / "baseline_summary.json"),
            "skilllayer_summary": str(AB_BENCHMARK_DIR / "skilllayer_summary.json"),
            "comparison": str(AB_BENCHMARK_DIR / "comparison.json"),
            "cost_per_successful_task": str(AB_BENCHMARK_DIR / "cost_per_successful_task.json"),
            "report": str(AB_BENCHMARK_DIR / "report.json"),
        },
        "measurement_scope": "controlled_local_tasks",
        "cost_scope": "observed_usage_only",
        "missing_token_counts_inferred": False,
        "savings_estimated": False,
        "roi_calculated": False,
        "interpretation": "This benchmark compares baseline and SkillLayer-assisted execution on identical controlled tasks. Cost conclusions require observed token usage and known pricing.",
    }


def write_outputs(
    task_results: list[dict[str, Any]],
    baseline_summary: dict[str, Any],
    skilllayer_summary: dict[str, Any],
    comparison: dict[str, Any],
    cost_per_successful_task: dict[str, Any],
    report: dict[str, Any],
) -> None:
    AB_BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "task_results.json": task_results,
        "baseline_summary.json": baseline_summary,
        "skilllayer_summary.json": skilllayer_summary,
        "comparison.json": comparison,
        "cost_per_successful_task.json": cost_per_successful_task,
        "report.json": report,
    }
    for filename, payload in outputs.items():
        (AB_BENCHMARK_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
