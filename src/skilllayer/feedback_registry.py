from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = PROJECT_ROOT / "feedback" / "feedback_registry.json"
STATUS_DOC_PATH = PROJECT_ROOT / "FEEDBACK_STATUS.md"
ALLOWED_STATUSES = ["OPEN", "IN_PROGRESS", "FIXED", "WAITING_VALIDATION", "VALIDATED", "REJECTED"]
ALLOWED_PLATFORMS = ["cursor", "claude_code", "codex", "windows", "macos", "linux"]
ALLOWED_SEVERITIES = ["low", "medium", "high", "critical"]
ALLOWED_CATEGORIES = ["install", "mcp", "workflow_output", "telemetry", "testing", "docs", "security", "other"]


def load_feedback_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path else REGISTRY_PATH
    if not registry_path.exists():
        return {
            "schema_version": "1.0",
            "source_of_truth": "FEEDBACK_STATUS.md",
            "allowed_statuses": ALLOWED_STATUSES,
            "feedback_items": [],
            "missing_registry": True,
        }
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("feedback registry must be a JSON object")
    payload.setdefault("allowed_statuses", ALLOWED_STATUSES)
    payload.setdefault("feedback_items", [])
    return payload


def summarize_feedback_registry(path: str | Path | None = None, *, recent_limit: int = 5, stale_threshold_days: int = 30) -> dict[str, Any]:
    registry = load_feedback_registry(path)
    items = [normalize_feedback_item(item, stale_threshold_days=stale_threshold_days) for item in registry.get("feedback_items", []) if isinstance(item, dict)]
    status_counts = status_counts_for(items)
    recent_items = recent_feedback_items(items, recent_limit)
    needs_validation_items = [item for item in items if item["needs_validation"]]
    metrics = build_feedback_metrics(items)
    return {
        "schema_version": registry.get("schema_version", "1.0"),
        "source_of_truth": registry.get("source_of_truth", "FEEDBACK_STATUS.md"),
        "registry_path": str(Path(path) if path else REGISTRY_PATH),
        "status_doc_path": str(STATUS_DOC_PATH),
        "total_items": len(items),
        "status_counts": status_counts,
        "open": status_counts["OPEN"],
        "in_progress": status_counts["IN_PROGRESS"],
        "fixed": status_counts["FIXED"],
        "waiting_validation": status_counts["WAITING_VALIDATION"],
        "validated": status_counts["VALIDATED"],
        "rejected": status_counts["REJECTED"],
        "needs_validation": len(needs_validation_items),
        "metrics": metrics,
        "oldest_open": oldest_item([item for item in items if item["status"] == "OPEN"]),
        "oldest_waiting_validation": oldest_item([item for item in items if item["status"] == "WAITING_VALIDATION"]),
        "recently_validated": recently_validated_items(items, recent_limit),
        "stale_open_items": [item for item in items if item["stale_open"]],
        "stale_validation_wait_items": [item for item in items if item["stale_validation_wait"]],
        "open_items": [item for item in items if item["status"] == "OPEN"],
        "needs_validation_items": needs_validation_items,
        "recent_items": recent_items,
        "items": items,
        "network_usage": False,
        "database_used": False,
    }


def build_feedback_report(
    *,
    status: str | None = None,
    source: str | None = None,
    platform: str | None = None,
    needs_validation: bool = False,
    open_only: bool = False,
    waiting_validation: bool = False,
    stale: bool = False,
    recently_validated: bool = False,
    item_id: str | None = None,
    path: str | Path | None = None,
    recent_limit: int = 5,
    stale_threshold_days: int = 30,
) -> dict[str, Any]:
    registry = load_feedback_registry(path)
    all_items = [
        normalize_feedback_item(item, stale_threshold_days=stale_threshold_days)
        for item in registry.get("feedback_items", [])
        if isinstance(item, dict)
    ]
    errors: list[str] = []
    normalized_status = normalize_status(status) if status else None
    normalized_platform = normalize_platform(platform) if platform else None

    if normalized_status and normalized_status not in ALLOWED_STATUSES:
        errors.append(f"invalid status: {status}")
    if normalized_platform and normalized_platform not in ALLOWED_PLATFORMS:
        errors.append(f"invalid platform: {platform}")
    if open_only and normalized_status and normalized_status != "OPEN":
        errors.append("--open-only cannot be combined with a non-OPEN --status")
    if waiting_validation and normalized_status and normalized_status != "WAITING_VALIDATION":
        errors.append("--waiting-validation cannot be combined with a non-WAITING_VALIDATION --status")
    if recently_validated and normalized_status and normalized_status != "VALIDATED":
        errors.append("--recently-validated cannot be combined with a non-VALIDATED --status")

    if errors:
        return feedback_error_report(
            errors=errors,
            filters=build_filters(status, source, platform, needs_validation, open_only, waiting_validation, stale, recently_validated, item_id, stale_threshold_days),
            all_items=all_items,
        )

    filtered = list(all_items)
    if item_id:
        filtered = [item for item in filtered if item["id"].upper() == item_id.upper()]
        if not filtered:
            return feedback_error_report(
                errors=[f"feedback item not found: {item_id}"],
                filters=build_filters(status, source, platform, needs_validation, open_only, waiting_validation, stale, recently_validated, item_id, stale_threshold_days),
                all_items=all_items,
                error_code="feedback_item_not_found",
            )
    if open_only:
        filtered = [item for item in filtered if item["status"] == "OPEN"]
    if waiting_validation:
        filtered = [item for item in filtered if item["status"] == "WAITING_VALIDATION"]
    if normalized_status:
        filtered = [item for item in filtered if item["status"] == normalized_status]
    if source:
        filtered = [item for item in filtered if item["source"] == source]
    if normalized_platform:
        filtered = [item for item in filtered if item["platform"] == normalized_platform]
    if needs_validation:
        filtered = [item for item in filtered if item["needs_validation"]]
    if stale:
        filtered = [item for item in filtered if item["stale_open"] or item["stale_validation_wait"]]
    if recently_validated:
        validated_ids = {item["id"] for item in recently_validated_items(all_items, recent_limit)}
        filtered = [item for item in filtered if item["id"] in validated_ids]

    summary = build_summary(all_items, filtered)
    return {
        "success": True,
        "summary": summary,
        "filters": build_filters(status, source, platform, needs_validation, open_only, waiting_validation, stale, recently_validated, item_id, stale_threshold_days),
        "items": filtered,
        "recent_items": recent_feedback_items(filtered, recent_limit),
        "open_items": [item for item in filtered if item["status"] == "OPEN"],
        "waiting_validation_items": [item for item in filtered if item["status"] == "WAITING_VALIDATION"],
        "needs_validation_items": [item for item in filtered if item["needs_validation"]],
        "stale_open_items": [item for item in filtered if item["stale_open"]],
        "stale_validation_wait_items": [item for item in filtered if item["stale_validation_wait"]],
        "recently_validated_items": recently_validated_items(filtered, recent_limit),
        "oldest_open": oldest_item([item for item in all_items if item["status"] == "OPEN"]),
        "oldest_waiting_validation": oldest_item([item for item in all_items if item["status"] == "WAITING_VALIDATION"]),
        "feedback_metrics": build_feedback_metrics(all_items),
        "error_code": None,
        "errors": [],
        "source_of_truth": registry.get("source_of_truth", "FEEDBACK_STATUS.md"),
        "registry_path": str(Path(path) if path else REGISTRY_PATH),
        "network_usage": False,
        "database_used": False,
    }


def validate_feedback_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path else REGISTRY_PATH
    markdown_exists = STATUS_DOC_PATH.exists()
    registry_exists = registry_path.exists()
    errors: list[str] = []
    warnings: list[str] = []
    items: list[dict[str, Any]] = []

    if not markdown_exists:
        errors.append("FEEDBACK_STATUS.md is missing")
    if not registry_exists:
        errors.append("feedback/feedback_registry.json is missing")
    else:
        try:
            registry = load_feedback_registry(registry_path)
            items = [normalize_feedback_item(item) for item in registry.get("feedback_items", []) if isinstance(item, dict)]
        except Exception as exc:
            errors.append(f"feedback registry JSON could not be parsed: {exc}")

    ids = [str(item.get("id") or "") for item in items]
    duplicate_ids = sorted(item_id for item_id, count in Counter(ids).items() if item_id and count > 1)
    missing_ids = [index for index, item_id in enumerate(ids) if not item_id]
    invalid_status_items = [
        {"id": item.get("id"), "status": item.get("status")}
        for item in items
        if normalize_status(str(item.get("status") or "")) not in ALLOWED_STATUSES
    ]
    missing_required_fields = []
    for item in items:
        missing = [field for field in ["id", "source", "platform", "status", "description"] if not item.get(field)]
        if missing:
            missing_required_fields.append({"id": item.get("id"), "missing_fields": missing})

    if duplicate_ids:
        errors.append(f"duplicate feedback IDs: {', '.join(duplicate_ids)}")
    if missing_ids:
        errors.append(f"feedback items with missing IDs: {missing_ids}")
    if invalid_status_items:
        errors.append("feedback items contain invalid statuses")
    if missing_required_fields:
        errors.append("feedback items are missing required fields")
    invalid_platform_items = [item["id"] for item in items if item.get("platform") not in ALLOWED_PLATFORMS and item.get("platform") != "unknown"]
    invalid_severity_items = [item["id"] for item in items if item.get("severity") not in ALLOWED_SEVERITIES]
    invalid_category_items = [item["id"] for item in items if item.get("category") not in ALLOWED_CATEGORIES]
    if invalid_platform_items:
        errors.append("feedback items contain invalid platforms")
    if invalid_severity_items:
        errors.append("feedback items contain invalid severities")
    if invalid_category_items:
        errors.append("feedback items contain invalid categories")
    if not items:
        warnings.append("feedback registry contains no items")

    return {
        "ok": not errors,
        "markdown_exists": markdown_exists,
        "registry_exists": registry_exists,
        "item_count": len(items),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": duplicate_ids,
        "missing_id_indexes": missing_ids,
        "valid_statuses": ALLOWED_STATUSES,
        "valid_platforms": ALLOWED_PLATFORMS,
        "valid_severities": ALLOWED_SEVERITIES,
        "valid_categories": ALLOWED_CATEGORIES,
        "invalid_status_items": invalid_status_items,
        "invalid_platform_items": invalid_platform_items,
        "invalid_severity_items": invalid_severity_items,
        "invalid_category_items": invalid_category_items,
        "missing_required_fields": missing_required_fields,
        "errors": errors,
        "warnings": warnings,
        "network_usage": False,
        "database_used": False,
    }


def normalize_feedback_item(item: dict[str, Any], *, stale_threshold_days: int = 30) -> dict[str, Any]:
    normalized = dict(item)
    normalized["id"] = str(normalized.get("id") or "")
    normalized["source"] = str(normalized.get("source") or "")
    normalized["platform"] = str(normalized.get("platform") or "unknown")
    normalized["status"] = normalize_status(str(normalized.get("status") or "OPEN"))
    normalized["description"] = str(normalized.get("description") or "")
    normalized["platform"] = normalize_platform(normalized["platform"])
    normalized["severity"] = normalize_severity(str(normalized.get("severity") or "medium"))
    normalized["category"] = normalize_category(str(normalized.get("category") or "other"))
    normalized["validation_confirmed"] = bool(normalized.get("validation_confirmed", normalized["status"] == "VALIDATED"))
    normalized["created_date"] = normalize_date_string(normalized.get("created_date") or normalized.get("date"))
    normalized["fixed_date"] = normalize_date_string(normalized.get("fixed_date"))
    normalized["validated_date"] = normalize_date_string(normalized.get("validated_date"))
    if normalized.get("fixed_in") is None:
        normalized["fixed_in"] = None
    if normalized.get("validation_notes") is None:
        normalized["validation_notes"] = None
    if not isinstance(normalized.get("related_files"), list):
        normalized["related_files"] = []
    normalized["needs_validation"] = compute_needs_validation(normalized)
    normalized["age_days"] = days_between(normalized["created_date"], today_iso())
    normalized["days_to_fix"] = days_between(normalized["created_date"], normalized["fixed_date"])
    normalized["days_to_validation"] = days_between(normalized["created_date"], normalized["validated_date"])
    if normalized["status"] == "WAITING_VALIDATION":
        normalized["days_waiting_validation"] = days_between(normalized["fixed_date"], today_iso())
    else:
        normalized["days_waiting_validation"] = None
    normalized["stale_open"] = bool(
        normalized["status"] == "OPEN"
        and normalized["age_days"] is not None
        and normalized["age_days"] > stale_threshold_days
    )
    normalized["stale_validation_wait"] = bool(
        normalized["status"] == "WAITING_VALIDATION"
        and normalized["days_waiting_validation"] is not None
        and normalized["days_waiting_validation"] > stale_threshold_days
    )
    return normalized


def normalize_status(status: str) -> str:
    value = status.strip().upper().replace("-", "_").replace(" ", "_")
    if value == "INPROGRESS":
        return "IN_PROGRESS"
    if value in {"WAITINGVALIDATION", "WAITING_FOR_VALIDATION", "AWAITING_VALIDATION"}:
        return "WAITING_VALIDATION"
    return value


def normalize_platform(platform: str) -> str:
    value = platform.strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"win", "windows"}:
        return "windows"
    if value in {"mac", "darwin", "osx", "macos"}:
        return "macos"
    return value


def normalize_severity(severity: str) -> str:
    value = severity.strip().lower()
    return value if value in ALLOWED_SEVERITIES else "medium"


def normalize_category(category: str) -> str:
    value = category.strip().lower().replace("-", "_").replace(" ", "_")
    return value if value in ALLOWED_CATEGORIES else "other"


def compute_needs_validation(item: dict[str, Any]) -> bool:
    if item.get("status") == "WAITING_VALIDATION":
        return True
    if item.get("status") != "FIXED":
        return False
    notes = str(item.get("validation_notes") or "").strip()
    return not notes or not bool(item.get("validation_confirmed"))


def status_counts_for(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(item["status"] for item in items)
    return {status: int(counts.get(status, 0)) for status in ALLOWED_STATUSES}


def recent_feedback_items(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (str(item.get("created_date") or item.get("date") or ""), str(item.get("id") or "")), reverse=True)[:limit]


def recently_validated_items(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    validated = [item for item in items if item["status"] == "VALIDATED"]
    return sorted(validated, key=lambda item: (str(item.get("validated_date") or item.get("created_date") or ""), str(item.get("id") or "")), reverse=True)[:limit]


def oldest_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return sorted(items, key=lambda item: (item.get("created_date") or "9999-12-31", item.get("id") or ""))[0]


def build_summary(all_items: list[dict[str, Any]], filtered_items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = status_counts_for(all_items)
    metrics = build_feedback_metrics(all_items)
    return {
        "open": counts["OPEN"],
        "in_progress": counts["IN_PROGRESS"],
        "fixed": counts["FIXED"],
        "waiting_validation": counts["WAITING_VALIDATION"],
        "validated": counts["VALIDATED"],
        "rejected": counts["REJECTED"],
        "needs_validation": sum(1 for item in all_items if item["needs_validation"]),
        "stale_open": sum(1 for item in all_items if item["stale_open"]),
        "stale_validation_wait": sum(1 for item in all_items if item["stale_validation_wait"]),
        "total_items": len(all_items),
        "matching_items": len(filtered_items),
        "metrics": metrics,
    }


def build_filters(
    status: str | None,
    source: str | None,
    platform: str | None,
    needs_validation: bool,
    open_only: bool,
    waiting_validation: bool,
    stale: bool,
    recently_validated: bool,
    item_id: str | None,
    stale_threshold_days: int,
) -> dict[str, Any]:
    return {
        "status": normalize_status(status) if status else None,
        "source": source,
        "platform": normalize_platform(platform) if platform else None,
        "needs_validation": bool(needs_validation),
        "open_only": bool(open_only),
        "waiting_validation": bool(waiting_validation),
        "stale": bool(stale),
        "recently_validated": bool(recently_validated),
        "id": item_id,
        "stale_threshold_days": int(stale_threshold_days),
    }


def feedback_error_report(
    *,
    errors: list[str],
    filters: dict[str, Any],
    all_items: list[dict[str, Any]],
    error_code: str = "invalid_feedback_filter",
) -> dict[str, Any]:
    return {
        "success": False,
        "summary": build_summary(all_items, []),
        "filters": filters,
        "items": [],
        "recent_items": [],
        "open_items": [],
        "waiting_validation_items": [],
        "needs_validation_items": [],
        "stale_open_items": [],
        "stale_validation_wait_items": [],
        "recently_validated_items": [],
        "oldest_open": None,
        "oldest_waiting_validation": None,
        "feedback_metrics": build_feedback_metrics(all_items),
        "error_code": error_code,
        "errors": errors,
        "source_of_truth": "FEEDBACK_STATUS.md",
        "registry_path": str(REGISTRY_PATH),
        "network_usage": False,
        "database_used": False,
    }


def build_feedback_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = status_counts_for(items)
    days_to_fix = [item["days_to_fix"] for item in items if item.get("days_to_fix") is not None]
    days_to_validation = [item["days_to_validation"] for item in items if item.get("days_to_validation") is not None]
    total = len(items)
    return {
        "total_feedback": total,
        "open_count": counts["OPEN"],
        "in_progress_count": counts["IN_PROGRESS"],
        "fixed_count": counts["FIXED"],
        "waiting_validation_count": counts["WAITING_VALIDATION"],
        "validated_count": counts["VALIDATED"],
        "rejected_count": counts["REJECTED"],
        "validation_rate": round(counts["VALIDATED"] / total, 4) if total else None,
        "average_days_to_fix": average(days_to_fix),
        "average_days_to_validation": average(days_to_validation),
    }


def average(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def normalize_date_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def days_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return None
    return (end_date - start_date).days


def today_iso() -> str:
    return datetime.now().date().isoformat()


def main() -> int:
    print(json.dumps(summarize_feedback_registry(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
