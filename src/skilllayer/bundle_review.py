from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


OUTPUT_DIR = Path("runs/p6_2_bundle_review")
EXPORT_GLOB = "skilllayer_telemetry_export*.json"
REQUIRED_EXPORT_FIELDS = {
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

WARNING_INSUFFICIENT = "Data volume is insufficient for roadmap decisions."


def review_telemetry_bundle(directory: str | Path) -> dict[str, Any]:
    root = Path(directory)
    export_paths = discover_exports(root)
    valid_exports: list[dict[str, Any]] = []
    invalid_exports: list[dict[str, Any]] = []
    for path in export_paths:
        loaded = load_and_validate_export(path)
        if loaded["valid"]:
            valid_exports.append(loaded)
        else:
            invalid_exports.append(loaded)

    bundle_summary = build_bundle_summary(root, export_paths, valid_exports, invalid_exports)
    workflow_summary = build_workflow_summary(valid_exports)
    demand_summary = build_demand_summary(valid_exports)
    cost_summary = build_cost_summary(valid_exports)
    client_summary = build_client_summary(valid_exports)
    validation_report = build_validation_report(root, export_paths, valid_exports, invalid_exports)
    report = {
        "milestone": "Telemetry Bundle Review",
        "bundle_summary": bundle_summary,
        "workflow_summary": workflow_summary,
        "demand_summary": demand_summary,
        "cost_summary": cost_summary,
        "client_summary": client_summary,
        "validation_report": validation_report,
        "outputs": {
            "bundle_summary": str(OUTPUT_DIR / "bundle_summary.json"),
            "workflow_summary": str(OUTPUT_DIR / "workflow_summary.json"),
            "demand_summary": str(OUTPUT_DIR / "demand_summary.json"),
            "cost_summary": str(OUTPUT_DIR / "cost_summary.json"),
            "client_summary": str(OUTPUT_DIR / "client_summary.json"),
            "validation_report": str(OUTPUT_DIR / "validation_report.json"),
            "report": str(OUTPUT_DIR / "report.json"),
            "valid_exports": str(OUTPUT_DIR / "valid_exports.json"),
            "invalid_exports": str(OUTPUT_DIR / "invalid_exports.json"),
        },
        "no_network_requests": True,
        "automatic_upload": False,
        "opportunity_ranking_generated": False,
        "roadmap_recommendations_generated": False,
        "automatic_workflow_priorities_generated": False,
        "interpretation": "This report aggregates anonymous telemetry exports. It does not decide what to build next.",
    }
    write_outputs(
        bundle_summary,
        workflow_summary,
        demand_summary,
        cost_summary,
        client_summary,
        validation_report,
        valid_exports,
        invalid_exports,
        report,
    )
    return report


def discover_exports(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(path for path in root.rglob(EXPORT_GLOB) if path.is_file())


def load_and_validate_export(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError) as exc:
        return invalid_export(path, f"json_load_failed: {exc}")
    if not isinstance(payload, dict):
        return invalid_export(path, "export_root_not_object")
    missing = sorted(REQUIRED_EXPORT_FIELDS - set(payload))
    if missing:
        return invalid_export(path, "missing_required_fields", missing_fields=missing)
    if payload.get("export_version") != "1.0":
        return invalid_export(path, "unsupported_export_version", export_version=payload.get("export_version"))
    if payload.get("privacy_mode") != "anonymous":
        return invalid_export(path, "privacy_mode_not_anonymous", privacy_mode=payload.get("privacy_mode"))
    if not isinstance(payload.get("summary"), dict):
        return invalid_export(path, "summary_not_object")
    return {
        "valid": True,
        "path": str(path),
        "export_version": payload.get("export_version"),
        "created_at": payload.get("created_at"),
        "skilllayer_version": payload.get("skilllayer_version"),
        "privacy_mode": payload.get("privacy_mode"),
        "contains_raw_tasks": bool(payload.get("contains_raw_tasks")),
        "contains_local_paths": bool(payload.get("contains_local_paths")),
        "sources_included": list(payload.get("sources_included") or []),
        "sources_missing": list(payload.get("sources_missing") or []),
        "summary": payload["summary"],
    }


def invalid_export(path: Path, reason: str, **extra: Any) -> dict[str, Any]:
    return {"valid": False, "path": str(path), "reason": reason, **extra}


def build_bundle_summary(
    root: Path,
    export_paths: list[Path],
    valid_exports: list[dict[str, Any]],
    invalid_exports: list[dict[str, Any]],
) -> dict[str, Any]:
    total_events = sum_export_events(valid_exports)
    data_volume_level = classify_data_volume(total_events)
    reliable = total_events >= 100
    warnings = []
    if not reliable:
        warnings.append(WARNING_INSUFFICIENT)
    warnings.append("No opportunity ranking, roadmap recommendation, or workflow priority is generated by bundle review.")
    return {
        "input_directory": str(root),
        "total_exports": len(export_paths),
        "valid_export_count": len(valid_exports),
        "invalid_export_count": len(invalid_exports),
        "unique_export_files": len({item["path"] for item in valid_exports}),
        "total_events": total_events,
        "data_volume_level": data_volume_level,
        "ranking_reliable": reliable,
        "use_for_roadmap_decisions": False,
        "roadmap_decisions_out_of_scope": True,
        "warnings": warnings,
        "no_network_requests": True,
        "automatic_upload": False,
        "opportunity_ranking_generated": False,
        "roadmap_recommendations_generated": False,
    }


def build_workflow_summary(valid_exports: list[dict[str, Any]]) -> dict[str, Any]:
    workflow_usage = Counter()
    workflow_success = Counter()
    workflow_misses = Counter()
    total_runs = 0
    successful_runs = 0
    for export in valid_exports:
        usage = export["summary"].get("workflow_usage") or {}
        misses = export["summary"].get("workflow_misses") or {}
        workflow_usage.update(as_int_mapping(usage.get("workflow_usage_count")))
        total_runs += int(usage.get("total_runs") or 0)
        successful_runs += int(usage.get("successful_runs") or 0)
        for item in misses.get("opportunities") or []:
            workflow = item.get("candidate_workflow")
            if workflow:
                workflow_misses[str(workflow)] += int(item.get("workflow_missing_count") or 0)
    for workflow, count in workflow_usage.items():
        workflow_success[workflow] += count
    return {
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "top_workflows": [{"workflow": key, "count": count} for key, count in workflow_usage.most_common()],
        "workflow_usage_totals": dict(sorted(workflow_usage.items())),
        "workflow_success_totals": dict(sorted(workflow_success.items())),
        "workflow_success_totals_available": False,
        "workflow_success_totals_note": (
            "Anonymous exports do not include per-workflow success counts; "
            "workflow usage totals are reported as an upper-bound proxy."
        ),
        "workflow_miss_totals": dict(sorted(workflow_misses.items())),
    }


def build_demand_summary(valid_exports: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts = Counter()
    missing_category_counts = Counter()
    total_demand_events = 0
    for export in valid_exports:
        demand = export["summary"].get("demand_categories") or {}
        total_demand_events += int(demand.get("total_events") or 0)
        for item in demand.get("top_categories") or []:
            category_counts[str(item.get("category") or "unknown")] += int(item.get("count") or 0)
        for item in demand.get("top_missing_categories") or []:
            missing_category_counts[str(item.get("category") or "unknown")] += int(item.get("count") or 0)
    return {
        "total_demand_events": total_demand_events,
        "top_categories": [{"category": key, "count": count} for key, count in category_counts.most_common()],
        "top_missing_categories": [{"category": key, "count": count} for key, count in missing_category_counts.most_common()],
        "demand_categories": dict(sorted(category_counts.items())),
        "workflow_misses": dict(sorted(missing_category_counts.items())),
    }


def build_cost_summary(valid_exports: list[dict[str, Any]]) -> dict[str, Any]:
    totals = Counter()
    provider_breakdown: dict[str, Counter] = {}
    model_breakdown: dict[str, Counter] = {}
    unknown_cost_reasons = Counter()
    total_cost_usd = 0.0

    for export in valid_exports:
        cost = export["summary"].get("cost_summary") or {}
        for key in [
            "total_usage_events",
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "unknown_usage_events",
            "cost_known_events",
            "cost_unknown_events",
        ]:
            totals[key] += int(cost.get(key) or 0)
        total_cost_usd += float(cost.get("total_cost_usd") or 0.0)
        merge_breakdown(provider_breakdown, cost.get("provider_cost_breakdown"))
        merge_breakdown(model_breakdown, cost.get("model_cost_breakdown"))
        unknown_cost_reasons.update(as_int_mapping(cost.get("unknown_cost_reasons")))

    return {
        "total_usage_events": totals["total_usage_events"],
        "total_input_tokens": totals["total_input_tokens"],
        "total_output_tokens": totals["total_output_tokens"],
        "total_tokens": totals["total_tokens"],
        "unknown_usage_events": totals["unknown_usage_events"],
        "cost_known_events": totals["cost_known_events"],
        "cost_unknown_events": totals["cost_unknown_events"],
        "total_cost_usd": round(total_cost_usd, 8),
        "provider_cost_breakdown": counters_to_plain_dict(provider_breakdown),
        "model_cost_breakdown": counters_to_plain_dict(model_breakdown),
        "unknown_cost_reasons": dict(sorted(unknown_cost_reasons.items())),
        "cost_scope": "observed_usage_only",
        "cost_savings_estimated": False,
        "roi_calculated": False,
    }


def build_client_summary(valid_exports: list[dict[str, Any]]) -> dict[str, Any]:
    client_distribution = Counter()
    platform_distribution = Counter()
    version_distribution = Counter()
    total_tool_invocations = 0
    for export in valid_exports:
        summary = export["summary"]
        client = summary.get("client") or {}
        platform_summary = summary.get("platform") or {}
        total_tool_invocations += int(client.get("tool_invocations_total") or 0)
        client_distribution["skilllayer_run"] += int(client.get("skilllayer_run_count") or 0)
        client_distribution["skilllayer_inspect"] += int(client.get("skilllayer_inspect_count") or 0)
        system = str(platform_summary.get("system") or "unknown")
        machine = str(platform_summary.get("machine") or "unknown")
        platform_distribution[f"{system}/{machine}"] += 1
        version_distribution[str(export.get("skilllayer_version") or "unknown")] += 1
    return {
        "total_tool_invocations": total_tool_invocations,
        "client_distribution": dict(sorted(client_distribution.items())),
        "platform_distribution": dict(sorted(platform_distribution.items())),
        "skilllayer_version_distribution": dict(sorted(version_distribution.items())),
    }


def build_validation_report(
    root: Path,
    export_paths: list[Path],
    valid_exports: list[dict[str, Any]],
    invalid_exports: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "input_directory": str(root),
        "exports_discovered": len(export_paths),
        "valid_export_count": len(valid_exports),
        "invalid_export_count": len(invalid_exports),
        "valid_exports": [
            {
                "path": item["path"],
                "export_version": item.get("export_version"),
                "privacy_mode": item.get("privacy_mode"),
                "contains_raw_tasks": item.get("contains_raw_tasks"),
                "contains_local_paths": item.get("contains_local_paths"),
            }
            for item in valid_exports
        ],
        "invalid_exports": invalid_exports,
        "required_fields": sorted(REQUIRED_EXPORT_FIELDS),
    }


def sum_export_events(valid_exports: list[dict[str, Any]]) -> int:
    total = 0
    for export in valid_exports:
        workflow_usage = export["summary"].get("workflow_usage") or {}
        demand = export["summary"].get("demand_categories") or {}
        cost = export["summary"].get("cost_summary") or {}
        total += int(workflow_usage.get("total_runs") or 0)
        total += int(demand.get("total_events") or 0)
        total += int(cost.get("total_usage_events") or 0)
    return total


def as_int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(count or 0) for key, count in value.items()}


def merge_breakdown(target: dict[str, Counter], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if not isinstance(item, dict):
            continue
        counter = target.setdefault(str(key), Counter())
        for metric, amount in item.items():
            if isinstance(amount, (int, float)):
                counter[str(metric)] += amount


def counters_to_plain_dict(value: dict[str, Counter]) -> dict[str, dict[str, int | float]]:
    return {
        key: {
            metric: round(amount, 8) if isinstance(amount, float) else int(amount)
            for metric, amount in sorted(counter.items())
        }
        for key, counter in sorted(value.items())
    }


def classify_data_volume(total_events: int) -> str:
    if total_events < 50:
        return "very_low"
    if total_events < 100:
        return "low"
    if total_events < 500:
        return "usable"
    return "stronger"


def write_outputs(
    bundle_summary: dict[str, Any],
    workflow_summary: dict[str, Any],
    demand_summary: dict[str, Any],
    cost_summary: dict[str, Any],
    client_summary: dict[str, Any],
    validation_report: dict[str, Any],
    valid_exports: list[dict[str, Any]],
    invalid_exports: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "bundle_summary.json": bundle_summary,
        "workflow_summary.json": workflow_summary,
        "demand_summary.json": demand_summary,
        "cost_summary.json": cost_summary,
        "client_summary.json": client_summary,
        "validation_report.json": validation_report,
        "valid_exports.json": strip_export_payloads(valid_exports),
        "invalid_exports.json": invalid_exports,
        "report.json": report,
    }
    for filename, payload in outputs.items():
        (OUTPUT_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def strip_export_payloads(valid_exports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": item["path"],
            "export_version": item.get("export_version"),
            "created_at": item.get("created_at"),
            "skilllayer_version": item.get("skilllayer_version"),
            "privacy_mode": item.get("privacy_mode"),
            "contains_raw_tasks": item.get("contains_raw_tasks"),
            "contains_local_paths": item.get("contains_local_paths"),
            "sources_included": item.get("sources_included", []),
            "sources_missing": item.get("sources_missing", []),
        }
        for item in valid_exports
    ]
