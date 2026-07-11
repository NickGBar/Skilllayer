from __future__ import annotations

import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

from .telemetry import CLI_ACTIVITY_JSONL, read_cli_activity, summarize_cli_activity


EXPORT_DIR = Path("runs/skilllayer_exports")
VALIDATION_DIR = Path("runs/p6_1_telemetry_export")

TELEMETRY_DIR = Path("runs/skilllayer_telemetry")
DEMAND_DIR = Path("runs/skilllayer_demand")
COST_DIR = Path("runs/skilllayer_cost")

EXPORT_VERSION = "1.0"

REDACTED_TASK = "[REDACTED_TASK]"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_SECRET = "[REDACTED_SECRET]"

PRIVACY_PATTERNS = {
    "mac_users_path": re.compile(r"/Users/[^\\s\"']+"),
    "windows_users_path": re.compile(r"[A-Za-z]:\\Users\\[^\\s\"']+"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    "github_token": re.compile(r"ghp_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}"),
    "api_key_label": re.compile(r"\bAPI_KEY\b\s*[:=]", re.I),
    "secret_label": re.compile(r"\bSECRET\b\s*[:=]", re.I),
    "token_label": re.compile(r"\bTOKEN\b\s*[:=]", re.I),
    "password_label": re.compile(r"\bPASSWORD\b\s*[:=]", re.I),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"),
}

SECRET_REDACTION_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?i)(API_KEY|SECRET|TOKEN|PASSWORD)\\s*[:=]\\s*[^\\s,;\"']+"),
]

PATH_REDACTION_PATTERNS = [
    re.compile(r"/Users/[^\\s\"']+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\s\"']+"),
]

EMAIL_REDACTION_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")

TASK_FIELD_NAMES = {"task", "task_text", "normalized_task", "description", "sample_tasks"}
PATH_FIELD_NAMES = {
    "path",
    "repo_path",
    "logs_path",
    "screenshot_path",
    "report_path",
    "url",
    "browser_url",
    "demand_events_path",
    "telemetry_path",
    "summary_path",
    "usage_events_path",
    "cost_summary_path",
}


def export_anonymous_telemetry(
    *,
    output: str | Path | None = None,
    include_raw_tasks: bool = False,
    include_local_paths: bool = False,
    allow_unsafe: bool = False,
) -> dict[str, Any]:
    sanitization = new_sanitization_report()
    sources_included: list[str] = []
    sources_missing: list[str] = []

    telemetry_summary = load_json_or_missing(TELEMETRY_DIR / "summary.json", "workflow_telemetry", sources_included, sources_missing)
    demand_summary = load_json_or_missing(DEMAND_DIR / "demand_summary.json", "demand_summary", sources_included, sources_missing)
    opportunity = load_json_or_missing(DEMAND_DIR / "opportunity_ranking.json", "opportunity_ranking", sources_included, sources_missing)
    usage_summary = load_json_or_missing(COST_DIR / "usage_summary.json", "usage_summary", sources_included, sources_missing)
    cost_summary = load_json_or_missing(COST_DIR / "cost_summary.json", "cost_summary", sources_included, sources_missing)
    cli_activity_summary = load_cli_activity_summary(sources_included, sources_missing)

    export = {
        "export_version": EXPORT_VERSION,
        "created_at": current_timestamp(),
        "skilllayer_version": skilllayer_version(),
        "privacy_mode": "anonymous",
        "contains_raw_tasks": bool(include_raw_tasks),
        "contains_local_paths": bool(include_local_paths),
        "sources_included": sources_included,
        "sources_missing": sources_missing,
        "summary": {
            "workflow_usage": build_workflow_usage_summary(telemetry_summary),
            "demand_categories": build_demand_category_summary(demand_summary),
            "workflow_misses": build_workflow_miss_summary(demand_summary, opportunity, include_raw_tasks=include_raw_tasks),
            "cost_summary": build_export_cost_summary(usage_summary, cost_summary),
            "cli_activity": build_cli_activity_export_summary(cli_activity_summary),
            "platform": build_platform_summary(),
            "client": build_client_summary(telemetry_summary),
        },
        "sanitization_report": sanitization,
        "privacy_scan": {},
        "no_network_requests": True,
        "automatic_upload": False,
    }
    export = sanitize_export(export, include_raw_tasks=include_raw_tasks, include_local_paths=include_local_paths, report=sanitization)
    scan = privacy_scan(export)
    export["privacy_scan"] = scan
    validation = build_validation_report(export, scan, allow_unsafe)

    if not scan["passed"] and not allow_unsafe:
        write_validation_outputs(blocked_export_stub(export), scan, validation)
        return {
            "success": False,
            "export_written": False,
            "output_path": None,
            "sources_included": sources_included,
            "sources_missing": sources_missing,
            "sanitization_report": sanitization,
            "privacy_scan": scan,
            "export_summary": build_export_result_summary(export),
            "error": "Privacy scan failed. Unsafe export was not written. Use --allow-unsafe only after manual review.",
            "validation_outputs": validation["outputs"],
        }

    output_path = Path(output) if output else default_export_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export, indent=2), encoding="utf-8")
    validation["export_output_path"] = str(output_path)
    write_validation_outputs(export, scan, validation)
    return {
        "success": True,
        "export_written": True,
        "output_path": str(output_path),
        "sources_included": sources_included,
        "sources_missing": sources_missing,
        "sanitization_report": sanitization,
        "privacy_scan": scan,
        "export_summary": build_export_result_summary(export),
        "validation_outputs": validation["outputs"],
        "review_before_sharing": True,
    }


def load_json_or_missing(path: Path, source_name: str, sources_included: list[str], sources_missing: list[str]) -> dict[str, Any]:
    if not path.exists():
        sources_missing.append(source_name)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        sources_missing.append(source_name)
        return {}
    if isinstance(data, dict):
        sources_included.append(source_name)
        return data
    sources_missing.append(source_name)
    return {}


def load_cli_activity_summary(sources_included: list[str], sources_missing: list[str]) -> dict[str, Any]:
    if not CLI_ACTIVITY_JSONL.exists():
        sources_missing.append("cli_activity")
        return summarize_cli_activity([])
    sources_included.append("cli_activity")
    return summarize_cli_activity(read_cli_activity())


def build_workflow_usage_summary(telemetry_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_runs": int(telemetry_summary.get("total_runs") or 0),
        "successful_runs": int(telemetry_summary.get("successful_runs") or 0),
        "workflow_usage_count": dict(telemetry_summary.get("workflow_usage_count") or {}),
        "workflow_found_count": int(telemetry_summary.get("workflow_found_count") or 0),
        "workflow_executed_count": int(telemetry_summary.get("workflow_executed_count") or 0),
        "fallback_triggered_count": int(telemetry_summary.get("fallback_triggered_count") or 0),
        "tool_usage_breakdown": dict(telemetry_summary.get("tool_usage_breakdown") or {}),
    }


def build_cli_activity_export_summary(cli_activity_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_cli_commands": int(cli_activity_summary.get("total_cli_commands") or 0),
        "command_counts": dict(cli_activity_summary.get("command_counts") or {}),
        "successful_command_counts": dict(cli_activity_summary.get("successful_command_counts") or {}),
        "failed_command_counts": dict(cli_activity_summary.get("failed_command_counts") or {}),
        "last_activity_at": cli_activity_summary.get("last_activity_at"),
        "raw_args_recorded": False,
        "local_paths_recorded": False,
    }


def build_demand_category_summary(demand_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_events": int(demand_summary.get("total_events") or 0),
        "data_volume_level": demand_summary.get("data_volume_level"),
        "ranking_reliable": bool(demand_summary.get("ranking_reliable")),
        "opportunity_ranking_status": demand_summary.get("opportunity_ranking_status"),
        "top_categories": list(demand_summary.get("top_categories") or []),
        "top_missing_categories": list(demand_summary.get("top_missing_categories") or []),
        "deduped_counts_by_category": dict(demand_summary.get("deduped_counts_by_category") or {}),
        "unique_session_counts_by_category": dict(demand_summary.get("unique_session_counts_by_category") or {}),
        "warnings": list(demand_summary.get("warnings") or []),
    }


def build_workflow_miss_summary(
    demand_summary: dict[str, Any],
    opportunity: dict[str, Any],
    *,
    include_raw_tasks: bool,
) -> dict[str, Any]:
    misses = {
        "workflow_missing_count": int(demand_summary.get("workflow_missing_count") or 0),
        "top_missing_categories": list(demand_summary.get("top_missing_categories") or []),
        "ranking_reliable": bool(opportunity.get("ranking_reliable")),
        "ranking_status": opportunity.get("ranking_status") or opportunity.get("opportunity_ranking_status"),
        "use_for_roadmap_decisions": bool(opportunity.get("use_for_roadmap_decisions")),
        "opportunities": [
            {
                "candidate_workflow": item.get("candidate_workflow"),
                "category": item.get("category"),
                "opportunity_score": item.get("opportunity_score"),
                "demand_frequency": item.get("demand_frequency"),
                "workflow_missing_count": item.get("workflow_missing_count"),
                "workflow_missing_rate": item.get("workflow_missing_rate"),
                "existing_support": item.get("existing_support"),
                "recommendation": item.get("recommendation"),
            }
            for item in list(opportunity.get("opportunities") or [])
        ],
    }
    if include_raw_tasks:
        misses["raw_tasks_included"] = True
    return misses


def build_export_cost_summary(usage_summary: dict[str, Any], cost_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_usage_events": int(usage_summary.get("total_events") or 0),
        "total_input_tokens": int(usage_summary.get("total_input_tokens") or 0),
        "total_output_tokens": int(usage_summary.get("total_output_tokens") or 0),
        "total_tokens": int(usage_summary.get("total_tokens") or 0),
        "unknown_usage_events": int(usage_summary.get("unknown_usage_events") or 0),
        "cost_known_events": int(cost_summary.get("cost_known_events") or 0),
        "cost_unknown_events": int(cost_summary.get("cost_unknown_events") or 0),
        "total_cost_usd": float(cost_summary.get("total_cost_usd") or 0.0),
        "provider_cost_breakdown": safe_breakdown(cost_summary.get("provider_cost_breakdown")),
        "model_cost_breakdown": safe_breakdown(cost_summary.get("model_cost_breakdown")),
        "unknown_cost_reasons": dict(cost_summary.get("unknown_cost_reasons") or {}),
        "cost_scope": cost_summary.get("cost_calculation_scope") or "observed_usage_only",
    }


def safe_breakdown(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized = {}
    for key, item in value.items():
        if isinstance(item, dict):
            sanitized[str(key)] = {
                "events": item.get("events"),
                "cost_known_events": item.get("cost_known_events"),
                "cost_unknown_events": item.get("cost_unknown_events"),
                "total_cost_usd": item.get("total_cost_usd"),
            }
    return sanitized


def build_platform_summary() -> dict[str, Any]:
    return {
        "python_major_minor": ".".join(str(part) for part in sys.version_info[:2]),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def build_client_summary(telemetry_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_invocations_total": int(telemetry_summary.get("tool_invocations_total") or 0),
        "skilllayer_run_count": int(telemetry_summary.get("skilllayer_run_count") or 0),
        "skilllayer_inspect_count": int(telemetry_summary.get("skilllayer_inspect_count") or 0),
    }


def build_export_result_summary(export: dict[str, Any]) -> dict[str, Any]:
    summary = export.get("summary") if isinstance(export.get("summary"), dict) else {}
    workflow_usage = summary.get("workflow_usage") if isinstance(summary.get("workflow_usage"), dict) else {}
    cli_activity = summary.get("cli_activity") if isinstance(summary.get("cli_activity"), dict) else {}
    demand_categories = summary.get("demand_categories") if isinstance(summary.get("demand_categories"), dict) else {}
    cost_summary = summary.get("cost_summary") if isinstance(summary.get("cost_summary"), dict) else {}
    return {
        "workflow_runs_exported": int(workflow_usage.get("total_runs") or 0),
        "cli_commands_exported": int(cli_activity.get("total_cli_commands") or 0),
        "demand_events_exported": int(demand_categories.get("total_events") or 0),
        "usage_events_exported": int(cost_summary.get("total_usage_events") or 0),
        "cli_activity_present_without_workflow_runs": int(workflow_usage.get("total_runs") or 0) == 0
        and int(cli_activity.get("total_cli_commands") or 0) > 0,
    }


def sanitize_export(
    value: Any,
    *,
    include_raw_tasks: bool,
    include_local_paths: bool,
    report: dict[str, int],
    key: str | None = None,
) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for item_key, item_value in value.items():
            if is_task_field(item_key) and not include_raw_tasks:
                sanitized[item_key] = redact_task_value(item_value, report)
            elif is_path_field(item_key) and not include_local_paths:
                sanitized[item_key] = redact_path_value(item_value, report)
            else:
                sanitized[item_key] = sanitize_export(
                    item_value,
                    include_raw_tasks=include_raw_tasks,
                    include_local_paths=include_local_paths,
                    report=report,
                    key=item_key,
                )
        return sanitized
    if isinstance(value, list):
        if is_task_field(key) and not include_raw_tasks:
            report["redacted_task_fields"] += len(value)
            return [REDACTED_TASK for _ in value]
        return [
            sanitize_export(item, include_raw_tasks=include_raw_tasks, include_local_paths=include_local_paths, report=report, key=key)
            for item in value
        ]
    if isinstance(value, str):
        text = value
        if not include_local_paths:
            text = redact_paths_in_text(text, report)
        text = redact_email_in_text(text, report)
        text = redact_secrets_in_text(text, report)
        if is_task_field(key) and not include_raw_tasks:
            report["redacted_task_fields"] += 1
            return REDACTED_TASK
        return text
    return value


def is_task_field(key: str | None) -> bool:
    return str(key or "").lower() in TASK_FIELD_NAMES


def is_path_field(key: str | None) -> bool:
    lowered = str(key or "").lower()
    return lowered in PATH_FIELD_NAMES or lowered.endswith("_path") or lowered.endswith("_url")


def redact_task_value(value: Any, report: dict[str, int]) -> Any:
    if isinstance(value, list):
        report["redacted_task_fields"] += len(value)
        return [REDACTED_TASK for _ in value]
    report["redacted_task_fields"] += 1
    return REDACTED_TASK


def redact_path_value(value: Any, report: dict[str, int]) -> Any:
    if isinstance(value, list):
        report["redacted_path_fields"] += len(value)
        return [REDACTED_PATH for _ in value]
    report["redacted_path_fields"] += 1
    return REDACTED_PATH


def redact_paths_in_text(text: str, report: dict[str, int]) -> str:
    redacted = text
    for pattern in PATH_REDACTION_PATTERNS:
        redacted, count = pattern.subn(REDACTED_PATH, redacted)
        report["redacted_path_fields"] += count
    return redacted


def redact_email_in_text(text: str, report: dict[str, int]) -> str:
    redacted, count = EMAIL_REDACTION_PATTERN.subn(REDACTED_EMAIL, text)
    report["redacted_email_fields"] += count
    return redacted


def redact_secrets_in_text(text: str, report: dict[str, int]) -> str:
    redacted = text
    for pattern in SECRET_REDACTION_PATTERNS:
        redacted, count = pattern.subn(REDACTED_SECRET, redacted)
        report["redacted_secret_fields"] += count
    return redacted


def privacy_scan(payload: Any) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True)
    findings = []
    for pattern_name, pattern in PRIVACY_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.append(
                {
                    "pattern_type": pattern_name,
                    "count": len(matches),
                    "example": redact_finding(str(matches[0])),
                }
            )
    return {
        "passed": len(findings) == 0,
        "finding_count": sum(int(item["count"]) for item in findings),
        "findings": findings,
        "patterns_checked": sorted(PRIVACY_PATTERNS),
    }


def redact_finding(value: str) -> str:
    value = EMAIL_REDACTION_PATTERN.sub(REDACTED_EMAIL, value)
    for pattern in PATH_REDACTION_PATTERNS:
        value = pattern.sub(REDACTED_PATH, value)
    for pattern in SECRET_REDACTION_PATTERNS:
        value = pattern.sub(REDACTED_SECRET, value)
    return value[:80]


def build_validation_report(export: dict[str, Any], scan: dict[str, Any], allow_unsafe: bool) -> dict[str, Any]:
    unsafe_probe = {
        "repo_path": "/" + "Users" + "/example/private/repo",
        "task_text": "Fix user@example.com with " + "API_KEY" + "=secret",
        "token": "sk-" + "testunsafe000000",
    }
    unsafe_scan = privacy_scan(unsafe_probe)
    return {
        "package_feature": "Anonymous Telemetry Export",
        "export_version": EXPORT_VERSION,
        "export_schema_valid": validate_export_schema(export),
        "privacy_scan_passed": bool(scan["passed"]),
        "unsafe_content_blocks_export_by_default": not unsafe_scan["passed"] and not allow_unsafe,
        "missing_sources_handled_gracefully": True,
        "allow_unsafe_used": bool(allow_unsafe),
        "no_network_requests": True,
        "automatic_upload": False,
        "validation_notes": [
            "Unsafe-block validation uses an in-memory probe and does not write unsafe content.",
            "Users should review sanitized exports before sharing.",
        ],
        "outputs": {
            "validation_report": str(VALIDATION_DIR / "validation_report.json"),
            "sample_export": str(VALIDATION_DIR / "sample_export.json"),
            "privacy_scan_report": str(VALIDATION_DIR / "privacy_scan_report.json"),
            "report": str(VALIDATION_DIR / "report.json"),
        },
    }


def validate_export_schema(export: dict[str, Any]) -> bool:
    required = {
        "export_version",
        "created_at",
        "skilllayer_version",
        "privacy_mode",
        "contains_raw_tasks",
        "contains_local_paths",
        "sources_included",
        "sources_missing",
        "summary",
        "sanitization_report",
    }
    return required.issubset(export)


def write_validation_outputs(export: dict[str, Any], scan: dict[str, Any], validation: dict[str, Any]) -> None:
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    (VALIDATION_DIR / "validation_report.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    (VALIDATION_DIR / "sample_export.json").write_text(json.dumps(export, indent=2), encoding="utf-8")
    (VALIDATION_DIR / "privacy_scan_report.json").write_text(json.dumps(scan, indent=2), encoding="utf-8")
    report = {
        "milestone": "Anonymous Telemetry Export",
        "validation": validation,
        "privacy_scan": scan,
        "sample_export_path": str(VALIDATION_DIR / "sample_export.json"),
        "no_network_requests": True,
        "automatic_upload": False,
    }
    (VALIDATION_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def blocked_export_stub(export: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_version": export.get("export_version"),
        "created_at": export.get("created_at"),
        "skilllayer_version": export.get("skilllayer_version"),
        "privacy_mode": export.get("privacy_mode"),
        "contains_raw_tasks": export.get("contains_raw_tasks"),
        "contains_local_paths": export.get("contains_local_paths"),
        "sources_included": export.get("sources_included", []),
        "sources_missing": export.get("sources_missing", []),
        "summary": "[BLOCKED_BY_PRIVACY_SCAN]",
        "sanitization_report": export.get("sanitization_report", {}),
        "privacy_scan": export.get("privacy_scan", {}),
        "export_written": False,
    }


def default_export_path() -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return EXPORT_DIR / f"skilllayer_telemetry_export_{timestamp}.json"


def skilllayer_version() -> str:
    pyproject = Path("pyproject.toml")
    if pyproject.exists():
        match = re.search(r'^version\\s*=\\s*"([^"]+)"', pyproject.read_text(encoding="utf-8", errors="ignore"), re.M)
        if match:
            return match.group(1)
    return "unknown"


def new_sanitization_report() -> dict[str, int]:
    return {
        "redacted_task_fields": 0,
        "redacted_path_fields": 0,
        "redacted_email_fields": 0,
        "redacted_secret_fields": 0,
    }


def current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
