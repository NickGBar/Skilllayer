from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .telemetry import read_telemetry, normalize_telemetry_entry


ANALYTICS_DIR = Path("runs/skilllayer_analytics")


def build_analytics_report() -> dict[str, Any]:
    raw_entries = read_telemetry()
    entries = [normalize_telemetry_entry(entry) for entry in raw_entries]
    run_entries = [entry for entry in entries if entry.get("tool_name") == "skilllayer_run"]
    miss_entries = [entry for entry in run_entries if bool(entry.get("fallback_triggered"))]
    workflow_entries = [entry for entry in run_entries if entry.get("workflow")]

    total_tasks_seen = len(run_entries)
    workflow_found_count = sum(1 for entry in run_entries if bool(entry.get("workflow_found")))
    workflow_executed_count = sum(1 for entry in run_entries if bool(entry.get("workflow_executed")))
    fallback_triggered_count = len(miss_entries)
    successful_runs = sum(1 for entry in run_entries if bool(entry.get("success")))

    coverage_summary = {
        "total_tasks_seen": total_tasks_seen,
        "workflow_found_count": workflow_found_count,
        "workflow_executed_count": workflow_executed_count,
        "fallback_triggered_count": fallback_triggered_count,
        "coverage_percent": percent(workflow_found_count, total_tasks_seen),
        "execution_percent": percent(workflow_executed_count, total_tasks_seen),
        "workflow_adoption_percent": percent(workflow_executed_count, workflow_found_count),
        "fallback_percent": percent(fallback_triggered_count, total_tasks_seen),
        "successful_runs": successful_runs,
        "failed_runs": total_tasks_seen - successful_runs,
        "definition": {
            "coverage_percent": "How often SkillLayer had a workflow available.",
            "execution_percent": "How often a SkillLayer workflow was executed for seen tasks.",
            "not_measured": "How often an external agent could have used SkillLayer but ignored it.",
        },
    }

    workflow_counter = Counter(str(entry.get("workflow")) for entry in workflow_entries)
    workflow_success = defaultdict(int)
    workflow_failure = defaultdict(int)
    for entry in workflow_entries:
        workflow = str(entry.get("workflow"))
        if entry.get("success"):
            workflow_success[workflow] += 1
        else:
            workflow_failure[workflow] += 1
    workflow_usage_summary = {
        "top_workflows": [{"workflow": workflow, "count": count} for workflow, count in workflow_counter.most_common()],
        "workflow_usage_count": dict(sorted(workflow_counter.items())),
        "workflow_success_count": dict(sorted(workflow_success.items())),
        "workflow_failure_count": dict(sorted(workflow_failure.items())),
    }

    missing_counter = Counter(str(entry.get("category") or "unknown") for entry in miss_entries)
    workflow_miss_summary = {
        "missing_workflow_count": len(miss_entries),
        "top_missing_categories": [{"category": category, "count": count} for category, count in missing_counter.most_common()],
        "missing_category_count": dict(sorted(missing_counter.items())),
        "miss_events": [
            {
                "timestamp": entry.get("timestamp"),
                "task": entry.get("task"),
                "workflow_found": False,
                "fallback_triggered": True,
                "reason": entry.get("reason") or "workflow_not_found",
                "category": entry.get("category") or "unknown",
            }
            for entry in miss_entries
        ],
    }

    roi_summary = {
        "estimated_llm_calls_saved": sum_int(entries, "estimated_llm_calls_saved"),
        "estimated_tool_steps_saved": sum_int(entries, "estimated_tool_steps_saved"),
        "estimated_file_reads_saved": sum_int(entries, "estimated_file_reads_saved"),
        "estimated_search_operations_saved": sum_int(entries, "estimated_search_operations_saved"),
        "estimated_savings_only": True,
        "limitation": "These are rough local estimates for product telemetry, not measured token or cost savings.",
    }

    tool_counter = Counter(str(entry.get("tool_name")) for entry in entries)
    adoption_funnel = {
        "tool_invocations_total": len(entries),
        "workflow_found_count": workflow_found_count,
        "workflow_executed_count": workflow_executed_count,
        "fallback_triggered_count": fallback_triggered_count,
        "workflow_adoption_percent": percent(workflow_executed_count, workflow_found_count),
        "fallback_percent": percent(fallback_triggered_count, total_tasks_seen),
        "tool_usage_breakdown": dict(sorted(tool_counter.items())),
    }

    report = {
        "coverage": coverage_summary,
        "adoption": adoption_funnel,
        "roi": roi_summary,
        "workflow_usage": workflow_usage_summary,
        "workflow_misses": workflow_miss_summary,
        "migration": {
            "raw_entries": len(raw_entries),
            "normalized_entries": len(entries),
            "legacy_entries_normalized": sum(1 for entry in raw_entries if "workflow_found" not in entry),
        },
        "estimated_savings_only": True,
    }
    write_analytics_outputs(report)
    return report


def write_analytics_outputs(report: dict[str, Any]) -> None:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "coverage_summary.json": report["coverage"],
        "roi_summary.json": report["roi"],
        "workflow_usage_summary.json": report["workflow_usage"],
        "workflow_miss_summary.json": report["workflow_misses"],
        "analytics_report.json": report,
    }
    for filename, payload in outputs.items():
        (ANALYTICS_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 3)


def sum_int(entries: list[dict[str, Any]], key: str) -> int:
    return sum(int(entry.get(key, 0) or 0) for entry in entries)
