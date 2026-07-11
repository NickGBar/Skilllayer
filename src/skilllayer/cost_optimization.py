from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .benchmark import BENCHMARK_DIR, run_benchmark


OUTPUT_DIR = Path("runs/p4_6_1_cost_optimization")
P4_5_BEFORE_SUMMARY = Path("runs/p4_6_workflow_repair/before_benchmark/benchmark_summary.json")
P4_5_BEFORE_RESULTS = Path("runs/p4_6_workflow_repair/before_benchmark/benchmark_results.json")
P4_6_SUMMARY = Path("runs/p4_6_workflow_repair/summary.json")
P4_6_RESULTS = Path("runs/p4_6_workflow_repair/results.json")


def run_cost_optimization_report(output_dir: str | Path = OUTPUT_DIR) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    p4_5_summary = load_json(P4_5_BEFORE_SUMMARY)
    p4_5_results = load_json(P4_5_BEFORE_RESULTS)
    p4_6_summary = load_json(P4_6_SUMMARY)
    p4_6_results = load_json(P4_6_RESULTS)["after_benchmark_results"]

    cost_hotspots = build_cost_hotspots(p4_6_results)
    write_json(output_path / "cost_hotspots.json", cost_hotspots)

    logs_dir = output_path / "logs"
    p4_6_1_report = run_benchmark(skilllayer_log_dir=logs_dir)
    p4_6_1_summary = p4_6_1_report["comparison"]
    p4_6_1_results = load_json(BENCHMARK_DIR / "benchmark_results.json")

    redundancy_analysis = build_redundancy_analysis(p4_6_1_results)
    comparison = build_three_way_comparison(p4_5_summary, p4_6_summary, p4_6_1_summary, redundancy_analysis)
    summary = {
        "milestone": "Cost Re-Optimization After Workflow Repair",
        "success_rate_preserved": p4_6_1_summary["skilllayer_success_rate"] >= 0.95,
        "success_rate_target": 0.95,
        "p4_5_skilllayer_success_rate": p4_5_summary["skilllayer_success_rate"],
        "p4_6_skilllayer_success_rate": p4_6_summary["after_skilllayer_success_rate"],
        "p4_6_1_skilllayer_success_rate": p4_6_1_summary["skilllayer_success_rate"],
        "tool_call_proxy_improved_vs_p4_6": p4_6_1_summary["tool_call_reduction_percent"] > p4_6_summary["comparison"]["tool_call_reduction_percent"]["after"],
        "cost_hotspot_count": len(cost_hotspots["workflow_hotspots"]),
        "duplicate_operation_count": redundancy_analysis["duplicate_operation_count"],
        "test_run_count": redundancy_analysis["test_run_count"],
        "comparison": comparison,
        "diagnostic": build_diagnostic(p4_6_summary, p4_6_1_summary),
        "routing_changed": False,
        "benchmark_tasks_changed": False,
        "paid_llm_used": False,
    }
    results = {
        "p4_6_1_benchmark_report": p4_6_1_report,
        "p4_6_1_benchmark_results": p4_6_1_results,
    }
    report = {
        "summary": summary,
        "cost_hotspots": cost_hotspots,
        "redundancy_analysis": redundancy_analysis,
        "before_after_cost_comparison": comparison,
        "results_path": str(output_path / "optimization_results.json"),
        "summary_path": str(output_path / "summary.json"),
        "cost_hotspots_path": str(output_path / "cost_hotspots.json"),
        "redundancy_analysis_path": str(output_path / "redundancy_analysis.json"),
        "before_after_cost_comparison_path": str(output_path / "before_after_cost_comparison.json"),
        "report_path": str(output_path / "report.json"),
        "interpretation": "optimizes safe repeated operations while preserving repaired workflow success.",
    }

    write_json(output_path / "redundancy_analysis.json", redundancy_analysis)
    write_json(output_path / "optimization_results.json", results)
    write_json(output_path / "before_after_cost_comparison.json", comparison)
    write_json(output_path / "summary.json", summary)
    write_json(output_path / "report.json", report)
    return report


def build_cost_hotspots(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        workflow = item.get("skilllayer", {}).get("workflow_used") or "none"
        grouped[workflow].append(item)

    workflow_hotspots = {}
    for workflow, items in sorted(grouped.items()):
        workflow_hotspots[workflow] = {
            "task_count": len(items),
            "skilllayer_tool_calls": sum(int(item["skilllayer"]["tool_calls"]) for item in items),
            "baseline_tool_calls": sum(int(item["baseline"]["tool_calls"]) for item in items),
            "skilllayer_file_reads": sum(int(item["skilllayer"]["file_reads"]) for item in items),
            "baseline_file_reads": sum(int(item["baseline"]["file_reads"]) for item in items),
            "skilllayer_search_operations": sum(int(item["skilllayer"]["search_operations"]) for item in items),
            "baseline_search_operations": sum(int(item["baseline"]["search_operations"]) for item in items),
            "skilllayer_duration_ms": round(sum(float(item["skilllayer"]["duration_ms"]) for item in items), 3),
            "baseline_duration_ms": round(sum(float(item["baseline"]["duration_ms"]) for item in items), 3),
            "test_runs_estimate": estimate_test_runs(items),
            "hotspot_reason": hotspot_reason(workflow, items),
        }
    return {
        "source": "runs/p4_6_workflow_repair/results.json",
        "workflow_hotspots": workflow_hotspots,
        "top_tool_call_hotspots": sorted(
            [{"workflow": workflow, "skilllayer_tool_calls": data["skilllayer_tool_calls"]} for workflow, data in workflow_hotspots.items()],
            key=lambda item: (-item["skilllayer_tool_calls"], item["workflow"]),
        ),
    }


def estimate_test_runs(items: list[dict[str, Any]]) -> int:
    count = 0
    for item in items:
        workflow = item.get("skilllayer", {}).get("workflow_used")
        task_type = item.get("task_type")
        if workflow == "FixFailingTestWorkflow":
            count += 2 if item.get("skilllayer", {}).get("success") else 1
        elif workflow in {"RenameSymbolWorkflow", "AddHelperWorkflow"}:
            count += 1
        elif workflow == "FindFunctionWorkflow" and item.get("skilllayer", {}).get("tests_executed"):
            count += 1
        elif task_type == "BrowserSmoke":
            count += 0
    return count


def hotspot_reason(workflow: str, items: list[dict[str, Any]]) -> str:
    skill_calls = sum(int(item["skilllayer"]["tool_calls"]) for item in items)
    base_calls = sum(int(item["baseline"]["tool_calls"]) for item in items)
    if skill_calls > base_calls:
        return "skilllayer_uses_more_tool_calls_than_baseline"
    if workflow == "FindFunctionWorkflow":
        return "navigation_workflow_should_avoid_validation_when_no_files_changed"
    return "not_a_major_hotspot"


def build_redundancy_analysis(results: list[dict[str, Any]]) -> dict[str, Any]:
    task_records = []
    category_counts = Counter()
    cache_hits = Counter()
    test_run_count = 0
    for item in results:
        trace = load_trace(item.get("skilllayer", {}).get("logs_path"))
        task_analysis = analyze_trace(item, trace)
        task_records.append(task_analysis)
        category_counts.update(task_analysis["redundancy_counts"])
        cache_hits.update(task_analysis["cache_hit_counts"])
        test_run_count += task_analysis["test_run_count"]
    return {
        "task_records": task_records,
        "redundancy_counts": dict(sorted(category_counts.items())),
        "cache_hit_counts": dict(sorted(cache_hits.items())),
        "duplicate_operation_count": sum(category_counts.values()),
        "test_run_count": test_run_count,
    }


def analyze_trace(item: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    cache_hits = Counter()
    seen_reads = Counter()
    seen_searches = Counter()
    changed_since_test = False
    previous_test_without_change = False
    test_run_count = 0
    for event in trace:
        tool = event.get("tool")
        if event.get("cached"):
            cache_hits[str(tool)] += 1
        if tool in {"read_file", "open_file"} and not event.get("cached"):
            file_name = str(event.get("file"))
            seen_reads[file_name] += 1
            if seen_reads[file_name] > 1:
                counts["duplicate_file_read"] += 1
        if tool == "search_symbol" and not event.get("cached"):
            symbol = str(event.get("symbol"))
            seen_searches[symbol] += 1
            if seen_searches[symbol] > 1:
                counts["duplicate_symbol_search"] += 1
        if tool in {"replace_text", "insert_text"}:
            changed = bool(event.get("replaced") or event.get("inserted") or event.get("changed_files"))
            if changed:
                changed_since_test = True
                previous_test_without_change = False
            else:
                counts["no_op_edit_validation"] += 0
        if tool == "inspect_error" and event.get("skipped"):
            counts["redundant_error_inspection"] += 1
        if tool == "run_tests":
            if event.get("skipped"):
                counts["redundant_validation"] += 1
                continue
            test_run_count += 1
            if previous_test_without_change and not changed_since_test:
                counts["redundant_test_run"] += 1
            previous_test_without_change = True
            changed_since_test = False
    return {
        "task_id": item.get("task_id"),
        "task_type": item.get("task_type"),
        "workflow": item.get("skilllayer", {}).get("workflow_used"),
        "success": bool(item.get("skilllayer", {}).get("success")),
        "tool_calls": int(item.get("skilllayer", {}).get("tool_calls", 0)),
        "test_run_count": test_run_count,
        "redundancy_counts": dict(sorted(counts.items())),
        "cache_hit_counts": dict(sorted(cache_hits.items())),
    }


def build_three_way_comparison(p4_5: dict[str, Any], p4_6: dict[str, Any], p4_6_1: dict[str, Any], redundancy: dict[str, Any]) -> dict[str, Any]:
    p4_6_flat = {
        "skilllayer_success_rate": p4_6["after_skilllayer_success_rate"],
        "tool_call_reduction_percent": p4_6["comparison"]["tool_call_reduction_percent"]["after"],
        "file_read_reduction_percent": p4_6["comparison"]["file_read_reduction_percent"]["after"],
        "search_reduction_percent": p4_6["comparison"]["search_reduction_percent"]["after"],
        "duration_reduction_percent": p4_6["comparison"]["duration_reduction_percent"]["after"],
    }
    metrics = [
        "skilllayer_success_rate",
        "tool_call_reduction_percent",
        "file_read_reduction_percent",
        "search_reduction_percent",
        "duration_reduction_percent",
    ]
    comparison = {}
    for metric in metrics:
        comparison[metric] = {
            "p4_5": p4_5.get(metric),
            "p4_6": p4_6_flat.get(metric),
            "p4_6_1": p4_6_1.get(metric),
            "p4_6_to_p4_6_1_delta": round(float(p4_6_1.get(metric, 0)) - float(p4_6_flat.get(metric, 0)), 3),
        }
    comparison["test_run_count"] = {"p4_6_1": redundancy["test_run_count"]}
    comparison["duplicate_operation_count"] = {"p4_6_1": redundancy["duplicate_operation_count"]}
    return comparison


def build_diagnostic(p4_6: dict[str, Any], p4_6_1: dict[str, Any]) -> str:
    success = p4_6_1["skilllayer_success_rate"]
    before_tool = p4_6["comparison"]["tool_call_reduction_percent"]["after"]
    after_tool = p4_6_1["tool_call_reduction_percent"]
    if success >= 0.95 and after_tool > before_tool:
        return f"Safe optimization preserved success ({success:.3f}) and improved tool-call proxy from {before_tool:.3f} to {after_tool:.3f}."
    if success < 0.95:
        return f"Optimization harmed success ({success:.3f}); reliability target was not preserved."
    return f"Success was preserved ({success:.3f}), but tool-call proxy did not improve."


def load_trace(logs_path: str | None) -> list[dict[str, Any]]:
    if not logs_path:
        return []
    path = Path(logs_path) / "trace.json"
    if not path.exists():
        return []
    return load_json(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
