from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any


TELEMETRY_DIR = Path("runs/skilllayer_telemetry")
TELEMETRY_JSONL = TELEMETRY_DIR / "telemetry.jsonl"
CLI_ACTIVITY_JSONL = TELEMETRY_DIR / "cli_activity.jsonl"
TELEMETRY_SUMMARY = TELEMETRY_DIR / "summary.json"

WORKFLOW_MISS_CATEGORIES = [
    "browser_validation",
    "dependency_analysis",
    "ci_failure",
    "test_triage",
    "refactor",
    "config_validation",
    "repo_navigation",
    "unknown",
]


def record_tool_invocation(
    *,
    tool_name: str,
    repo_path: str | None = None,
    task: str | None = None,
    workflow: str | None = None,
    success: bool = False,
    duration_ms: float = 0.0,
    tool_calls: int = 0,
    macro_sequence: list[str] | None = None,
    workflow_found: bool | None = None,
    workflow_executed: bool | None = None,
    fallback_triggered: bool | None = None,
    reason: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    workflow_found = bool(workflow) if workflow_found is None else bool(workflow_found)
    fallback_triggered = (tool_name == "skilllayer_run" and not workflow_found) if fallback_triggered is None else bool(fallback_triggered)
    workflow_executed = (tool_name == "skilllayer_run" and workflow_found and not fallback_triggered) if workflow_executed is None else bool(workflow_executed)
    category = category or categorize_task(task)
    reason = reason or ("workflow_not_found" if fallback_triggered else None)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_name": tool_name,
        "repo_path": repo_path,
        "task": task,
        "workflow": workflow,
        "success": bool(success),
        "duration_ms": round(float(duration_ms), 3),
        "estimated_llm_calls_saved": estimate_llm_calls_saved(tool_name, workflow),
        "estimated_tool_steps_saved": estimate_tool_steps_saved(tool_name, tool_calls, macro_sequence or []),
        "estimated_file_reads_saved": estimate_file_reads_saved(tool_name, workflow, macro_sequence or []),
        "estimated_search_operations_saved": estimate_search_operations_saved(tool_name, workflow, macro_sequence or []),
        "estimated_savings_only": True,
        "workflow_found": workflow_found,
        "workflow_executed": workflow_executed,
        "fallback_triggered": fallback_triggered,
        "reason": reason,
        "category": category,
    }
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    with TELEMETRY_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    summary = summarize_telemetry()
    TELEMETRY_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return entry


def summarize_telemetry(path: str | Path | None = None) -> dict[str, Any]:
    entries = [normalize_telemetry_entry(entry) for entry in read_telemetry(path)]
    cli_activity = read_cli_activity()
    tool_counts = Counter(str(entry.get("tool_name")) for entry in entries)
    workflow_counts = Counter(
        str(entry.get("workflow"))
        for entry in entries
        if entry.get("tool_name") == "skilllayer_run" and entry.get("workflow")
    )
    success_count = sum(1 for entry in entries if bool(entry.get("success")))
    failure_count = len(entries) - success_count
    estimated_llm_calls_saved = sum(int(entry.get("estimated_llm_calls_saved", 0)) for entry in entries)
    estimated_tool_steps_saved = sum(int(entry.get("estimated_tool_steps_saved", 0)) for entry in entries)
    estimated_file_reads_saved = sum(int(entry.get("estimated_file_reads_saved", 0)) for entry in entries)
    estimated_search_operations_saved = sum(int(entry.get("estimated_search_operations_saved", 0)) for entry in entries)
    run_entries = [entry for entry in entries if entry.get("tool_name") == "skilllayer_run"]
    workflow_found_count = sum(1 for entry in run_entries if bool(entry.get("workflow_found")))
    workflow_executed_count = sum(1 for entry in run_entries if bool(entry.get("workflow_executed")))
    fallback_triggered_count = sum(1 for entry in run_entries if bool(entry.get("fallback_triggered")))
    return {
        "total_runs": len(run_entries),
        "successful_runs": sum(1 for entry in run_entries if bool(entry.get("success"))),
        "total_tasks_seen": len(run_entries),
        "workflow_found_count": workflow_found_count,
        "workflow_executed_count": workflow_executed_count,
        "fallback_triggered_count": fallback_triggered_count,
        "tool_invocations_total": len(entries),
        "skilllayer_run_count": tool_counts.get("skilllayer_run", 0),
        "skilllayer_inspect_count": tool_counts.get("skilllayer_inspect_repo", 0),
        "workflow_usage_count": dict(sorted(workflow_counts.items())),
        "workflow_breakdown": dict(sorted(workflow_counts.items())),
        "tool_usage_breakdown": dict(sorted(tool_counts.items())),
        "success_count": success_count,
        "failure_count": failure_count,
        "estimated_llm_calls_saved": estimated_llm_calls_saved,
        "estimated_tool_steps_saved": estimated_tool_steps_saved,
        "estimated_file_reads_saved": estimated_file_reads_saved,
        "estimated_search_operations_saved": estimated_search_operations_saved,
        "estimated_savings_only": True,
        "cli_activity": summarize_cli_activity(cli_activity),
        "telemetry_path": str(TELEMETRY_JSONL),
        "cli_activity_path": str(CLI_ACTIVITY_JSONL),
        "summary_path": str(TELEMETRY_SUMMARY),
    }


def record_cli_activity(
    *,
    command_name: str,
    success: bool,
    duration_ms: float,
    client: str = "cli",
) -> dict[str, Any]:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command_name": command_name,
        "success": bool(success),
        "duration_ms": round(float(duration_ms), 3),
        "client": client,
    }
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    with CLI_ACTIVITY_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    TELEMETRY_SUMMARY.write_text(json.dumps(summarize_telemetry(), indent=2), encoding="utf-8")
    return entry


def read_cli_activity(path: str | Path | None = None) -> list[dict[str, Any]]:
    activity_path = Path(path) if path else CLI_ACTIVITY_JSONL
    if not activity_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in activity_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(normalize_cli_activity_entry(entry))
    return entries


def summarize_cli_activity(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    activity = entries if entries is not None else read_cli_activity()
    command_counts = Counter(str(entry.get("command_name") or "unknown") for entry in activity)
    successful_counts = Counter(str(entry.get("command_name") or "unknown") for entry in activity if bool(entry.get("success")))
    failed_counts = Counter(str(entry.get("command_name") or "unknown") for entry in activity if not bool(entry.get("success")))
    timestamps = [str(entry.get("timestamp")) for entry in activity if entry.get("timestamp")]
    return {
        "total_cli_commands": len(activity),
        "command_counts": dict(sorted(command_counts.items())),
        "successful_command_counts": dict(sorted(successful_counts.items())),
        "failed_command_counts": dict(sorted(failed_counts.items())),
        "last_activity_at": max(timestamps) if timestamps else None,
        "activity_path": str(CLI_ACTIVITY_JSONL),
        "raw_args_recorded": False,
        "local_paths_recorded": False,
    }


def normalize_cli_activity_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": entry.get("timestamp"),
        "command_name": str(entry.get("command_name") or "unknown"),
        "success": bool(entry.get("success")),
        "duration_ms": float(entry.get("duration_ms") or 0.0),
        "client": str(entry.get("client") or "cli"),
    }


def read_telemetry(path: str | Path | None = None) -> list[dict[str, Any]]:
    telemetry_path = Path(path) if path else TELEMETRY_JSONL
    if not telemetry_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in telemetry_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def estimate_llm_calls_saved(tool_name: str, workflow: str | None = None) -> int:
    if tool_name == "skilllayer_run":
        return 2 if workflow else 1
    if tool_name == "skilllayer_inspect_repo":
        return 1
    return 0


def estimate_tool_steps_saved(tool_name: str, tool_calls: int, macro_sequence: list[str]) -> int:
    if tool_name == "skilllayer_run":
        return max(1, len(macro_sequence) * 2 - min(tool_calls, len(macro_sequence)))
    if tool_name == "skilllayer_inspect_repo":
        return 2
    return 0


def estimate_file_reads_saved(tool_name: str, workflow: str | None, macro_sequence: list[str]) -> int:
    if tool_name == "skilllayer_inspect_repo":
        return 3
    if tool_name != "skilllayer_run" or not workflow:
        return 0
    if workflow == "BrowserSmokeWorkflow":
        return 1
    return max(1, len(macro_sequence))


def estimate_search_operations_saved(tool_name: str, workflow: str | None, macro_sequence: list[str]) -> int:
    if tool_name == "skilllayer_inspect_repo":
        return 1
    if tool_name != "skilllayer_run" or not workflow:
        return 0
    if workflow in {"FindFunctionWorkflow", "RenameSymbolWorkflow", "FixBugWorkflow"}:
        return 1
    return 0


def normalize_telemetry_entry(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    tool_name = str(normalized.get("tool_name"))
    workflow = normalized.get("workflow")
    task = normalized.get("task")
    macro_sequence = normalized.get("macro_sequence") or []
    workflow_found = normalized.get("workflow_found")
    if workflow_found is None:
        workflow_found = tool_name == "skilllayer_run" and bool(workflow)
    fallback_triggered = normalized.get("fallback_triggered")
    if fallback_triggered is None:
        fallback_triggered = tool_name == "skilllayer_run" and not bool(workflow_found)
    workflow_executed = normalized.get("workflow_executed")
    if workflow_executed is None:
        workflow_executed = tool_name == "skilllayer_run" and bool(workflow_found) and not bool(fallback_triggered)

    normalized["workflow_found"] = bool(workflow_found)
    normalized["workflow_executed"] = bool(workflow_executed)
    normalized["fallback_triggered"] = bool(fallback_triggered)
    normalized["reason"] = normalized.get("reason") or ("workflow_not_found" if normalized["fallback_triggered"] else None)
    normalized["category"] = normalized.get("category") or categorize_task(task)
    normalized["estimated_file_reads_saved"] = int(
        normalized.get("estimated_file_reads_saved")
        or estimate_file_reads_saved(tool_name, workflow, list(macro_sequence) if isinstance(macro_sequence, list) else [])
    )
    normalized["estimated_search_operations_saved"] = int(
        normalized.get("estimated_search_operations_saved")
        or estimate_search_operations_saved(tool_name, workflow, list(macro_sequence) if isinstance(macro_sequence, list) else [])
    )
    normalized["estimated_tool_steps_saved"] = int(normalized.get("estimated_tool_steps_saved", 0) or 0)
    normalized["estimated_llm_calls_saved"] = int(normalized.get("estimated_llm_calls_saved", 0) or 0)
    normalized["estimated_savings_only"] = True
    return normalized


def categorize_task(task: str | None) -> str:
    if not task:
        return "unknown"
    text = task.lower()
    patterns = [
        ("browser_validation", r"\b(browser|frontend|front-end|page|form|button|console|network|smoke|ui|dom|screenshot)\b"),
        ("dependency_analysis", r"\b(dependency|dependencies|package|requirements|lockfile|pip|npm|version conflict|import graph)\b"),
        ("ci_failure", r"\b(ci|github actions|workflow failure|pipeline|check failed|build failed|failing check)\b"),
        ("test_triage", r"\b(test triage|failing tests?|pytest|unittest|stack trace|traceback|test failure)\b"),
        ("refactor", r"\b(refactor|rename|move|extract|cleanup|restructure|repository-wide)\b"),
        ("config_validation", r"\b(config|configuration|env|environment|yaml|toml|json|settings|vite|docker)\b"),
        ("repo_navigation", r"\b(find|locate|inspect|where|summarize|list files|repository structure|repo structure)\b"),
    ]
    for category, pattern in patterns:
        if re.search(pattern, text):
            return category
    return "unknown"
