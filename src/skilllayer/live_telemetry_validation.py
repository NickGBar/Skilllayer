from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .cost_tracking import adapter_for_provider, normalize_usage_event, read_field, read_usage_events
from .pricing import PRICING_TABLE, calculate_event_cost


OUTPUT_DIR = Path("runs/p5_6_4_live_provider_validation")
TARGET_PROVIDERS = ["openai", "anthropic", "openrouter", "generic"]
REAL_PROVIDER_NAMES = {"openai", "anthropic", "openrouter"}

ADAPTER_CAPABILITIES: dict[str, dict[str, Any]] = {
    "openai": {
        "adapter": "OpenAIUsageAdapter",
        "usage_payload_available": True,
        "input_tokens_available": True,
        "output_tokens_available": True,
        "cached_tokens_available": True,
        "reasoning_tokens_available": True,
        "model_id_available": True,
        "supported_usage_shapes": ["usage.input_tokens", "usage.output_tokens", "usage.prompt_tokens", "usage.completion_tokens"],
    },
    "anthropic": {
        "adapter": "AnthropicUsageAdapter",
        "usage_payload_available": True,
        "input_tokens_available": True,
        "output_tokens_available": True,
        "cached_tokens_available": True,
        "reasoning_tokens_available": False,
        "model_id_available": True,
        "supported_usage_shapes": ["usage.input_tokens", "usage.output_tokens", "usage.cache_creation_input_tokens", "usage.cache_read_input_tokens"],
    },
    "openrouter": {
        "adapter": "OpenRouterUsageAdapter",
        "usage_payload_available": True,
        "input_tokens_available": True,
        "output_tokens_available": True,
        "cached_tokens_available": True,
        "reasoning_tokens_available": True,
        "model_id_available": True,
        "supported_usage_shapes": ["OpenAI-compatible usage payload"],
    },
    "generic": {
        "adapter": "GenericUsageAdapter",
        "usage_payload_available": "best_effort",
        "input_tokens_available": "if_payload_provides_field",
        "output_tokens_available": "if_payload_provides_field",
        "cached_tokens_available": "if_payload_provides_field",
        "reasoning_tokens_available": "if_payload_provides_field",
        "model_id_available": "if_payload_provides_field",
        "supported_usage_shapes": ["usage.*", "top-level token fields"],
    },
}


def run_live_provider_validation() -> dict[str, Any]:
    events = read_usage_events()
    provider_capability_matrix = build_provider_capability_matrix(events)
    telemetry_completeness = build_telemetry_completeness(events)
    end_to_end_validation = build_end_to_end_validation(events, telemetry_completeness)
    pipeline_health = build_pipeline_health(events, telemetry_completeness)
    readiness_summary = build_readiness_summary(pipeline_health, telemetry_completeness)
    report = {
        "milestone": "Live Provider Telemetry Validation",
        "provider_capability_matrix": provider_capability_matrix,
        "telemetry_completeness": telemetry_completeness,
        "end_to_end_validation": end_to_end_validation,
        "pipeline_health": pipeline_health,
        "readiness_summary": readiness_summary,
        "outputs": {
            "provider_capability_matrix": str(OUTPUT_DIR / "provider_capability_matrix.json"),
            "telemetry_completeness": str(OUTPUT_DIR / "telemetry_completeness.json"),
            "pipeline_health": str(OUTPUT_DIR / "pipeline_health.json"),
            "readiness_summary": str(OUTPUT_DIR / "readiness_summary.json"),
            "report": str(OUTPUT_DIR / "report.json"),
        },
        "no_provider_payloads_fabricated": True,
        "savings_calculated": False,
        "cost_savings_claimed": False,
        "interpretation": readiness_summary["interpretation"],
    }
    write_outputs(provider_capability_matrix, telemetry_completeness, pipeline_health, readiness_summary, report)
    return report


def build_provider_capability_matrix(events: list[dict[str, Any]]) -> dict[str, Any]:
    observed_by_provider = provider_event_counts(events)
    providers: dict[str, dict[str, Any]] = {}
    for provider in TARGET_PROVIDERS:
        observed_events = [event for event in events if provider_key(event.get("provider")) == provider]
        providers[provider] = {
            "provider": provider,
            **ADAPTER_CAPABILITIES[provider],
            "pricing_table_model_count": len(PRICING_TABLE.get(provider, {})),
            "observed_event_count": len(observed_events),
            "observed_model_count": len({event.get("model") for event in observed_events if event.get("model")}),
            "observed_input_tokens_available": any(event.get("input_tokens") is not None for event in observed_events),
            "observed_output_tokens_available": any(event.get("output_tokens") is not None for event in observed_events),
            "observed_cached_tokens_available": any(event.get("cached_input_tokens") is not None for event in observed_events),
            "observed_reasoning_tokens_available": any(event.get("reasoning_tokens") is not None for event in observed_events),
            "observed_model_id_available": any(bool(event.get("model")) for event in observed_events),
        }
    return {
        "providers": providers,
        "observed_provider_counts": observed_by_provider,
        "observed_non_target_providers": {
            provider: count
            for provider, count in observed_by_provider.items()
            if provider not in TARGET_PROVIDERS
        },
        "audit_scope": "adapter_capabilities_plus_observed_usage_events",
        "api_calls_performed": False,
    }


def build_telemetry_completeness(events: list[dict[str, Any]]) -> dict[str, Any]:
    event_rows = []
    cost_calculable_count = 0
    real_provider_event_count = 0
    for index, event in enumerate(events):
        normalized = normalize_usage_event(event)
        cost = calculate_event_cost(normalized)
        provider = provider_key(normalized.get("provider"))
        row = {
            "event_index": index,
            "timestamp": normalized.get("timestamp"),
            "provider": normalized.get("provider"),
            "model": normalized.get("model"),
            "workflow": normalized.get("workflow"),
            "provider_known": bool(normalized.get("provider")),
            "supported_provider": provider in TARGET_PROVIDERS,
            "real_provider": provider in REAL_PROVIDER_NAMES,
            "model_known": bool(normalized.get("model")),
            "tokens_known": normalized.get("input_tokens") is not None and normalized.get("output_tokens") is not None,
            "nonzero_token_usage": any(int(normalized.get(field) or 0) > 0 for field in ["input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"]),
            "pricing_known": bool(cost.get("pricing_known")),
            "cost_calculable": bool(cost.get("cost_known")),
            "cost_unknown_reasons": list(cost.get("unknown_reasons") or []),
            "adapter_used": adapter_for_provider(str(normalized.get("provider") or "")).__class__.__name__,
            "usage_payload_shape": "normalized_usage_event",
        }
        if row["cost_calculable"]:
            cost_calculable_count += 1
        if row["real_provider"]:
            real_provider_event_count += 1
        event_rows.append(row)

    raw_payload_validations = validate_observed_raw_payloads(events)
    return {
        "total_events": len(events),
        "event_completeness": event_rows,
        "provider_known_count": sum(1 for row in event_rows if row["provider_known"]),
        "model_known_count": sum(1 for row in event_rows if row["model_known"]),
        "tokens_known_count": sum(1 for row in event_rows if row["tokens_known"]),
        "nonzero_token_usage_count": sum(1 for row in event_rows if row["nonzero_token_usage"]),
        "pricing_known_count": sum(1 for row in event_rows if row["pricing_known"]),
        "cost_calculable_count": cost_calculable_count,
        "real_provider_event_count": real_provider_event_count,
        "raw_provider_payload_count": len(raw_payload_validations),
        "raw_provider_payload_validations": raw_payload_validations,
        "unknown_cost_reasons": aggregate_unknown_reasons(event_rows),
    }


def validate_observed_raw_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        metadata = event.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        payload = first_present(metadata, ["provider_response", "raw_response", "response", "usage_payload"])
        if payload is None:
            continue
        provider = metadata.get("provider") or event.get("provider")
        validations.append(validate_provider_payload(payload, provider=provider, workflow=event.get("workflow"), task=event.get("task"), event_index=index))
    return validations


def validate_provider_payload(
    response: Any,
    *,
    provider: str | None = None,
    workflow: str | None = None,
    task: str | None = None,
    model: str | None = None,
    event_index: int | None = None,
) -> dict[str, Any]:
    provider_name = provider_key(provider)
    adapter = adapter_for_provider(provider_name)
    try:
        normalized = adapter.normalize(response, workflow=workflow, task=task, model=model)
        cost = calculate_event_cost(normalized)
        return {
            "event_index": event_index,
            "provider": provider_name,
            "adapter_used": adapter.__class__.__name__,
            "usage_payload_shape": describe_usage_payload_shape(response),
            "normalization_success": True,
            "provider_known": bool(normalized.get("provider")),
            "model_known": bool(normalized.get("model")),
            "token_counts_known": normalized.get("input_tokens") is not None and normalized.get("output_tokens") is not None,
            "input_tokens_available": normalized.get("input_tokens") is not None,
            "output_tokens_available": normalized.get("output_tokens") is not None,
            "cached_tokens_available": normalized.get("cached_input_tokens") is not None,
            "reasoning_tokens_available": normalized.get("reasoning_tokens") is not None,
            "pricing_known": bool(cost.get("pricing_known")),
            "cost_calculable": bool(cost.get("cost_known")),
            "unknown_cost_reasons": list(cost.get("unknown_reasons") or []),
        }
    except Exception as exc:
        return {
            "event_index": event_index,
            "provider": provider_name,
            "adapter_used": adapter.__class__.__name__,
            "usage_payload_shape": describe_usage_payload_shape(response),
            "normalization_success": False,
            "error": str(exc),
            "cost_calculable": False,
        }


def build_pipeline_health(events: list[dict[str, Any]], telemetry: dict[str, Any]) -> dict[str, Any]:
    adapter_ok = adapter_static_health_ok()
    usage_capture_ok = len(events) > 0
    observed_cost_calculable = telemetry["cost_calculable_count"] > 0
    real_provider_observed = telemetry["real_provider_event_count"] > 0
    raw_provider_payload_observed = telemetry["raw_provider_payload_count"] > 0
    missing_requirements = []
    if not usage_capture_ok:
        missing_requirements.append("no_usage_events_observed")
    if not real_provider_observed:
        missing_requirements.append("no_real_provider_usage_events_observed")
    if not raw_provider_payload_observed:
        missing_requirements.append("no_raw_provider_response_payloads_observed")
    if telemetry["model_known_count"] == 0:
        missing_requirements.append("no_model_ids_observed")
    if telemetry["nonzero_token_usage_count"] == 0:
        missing_requirements.append("no_nonzero_token_usage_observed")
    if telemetry["pricing_known_count"] == 0:
        missing_requirements.append("no_observed_model_pricing_known")
    if not observed_cost_calculable:
        missing_requirements.append("no_observed_event_cost_calculable")
    return {
        "usage_capture_ok": usage_capture_ok,
        "adapter_ok": adapter_ok,
        "pricing_ok": bool(PRICING_TABLE),
        "cost_calculation_ok": cost_function_runs_for_observed_events(events),
        "cost_comparison_ready": bool(real_provider_observed and observed_cost_calculable),
        "raw_provider_payload_observed": raw_provider_payload_observed,
        "real_provider_observed": real_provider_observed,
        "observed_cost_calculable": observed_cost_calculable,
        "missing_requirements": missing_requirements,
        "api_calls_performed": False,
        "fabricated_provider_payloads": False,
    }


def build_end_to_end_validation(events: list[dict[str, Any]], telemetry: dict[str, Any]) -> dict[str, Any]:
    raw_payload_count = int(telemetry["raw_provider_payload_count"])
    return {
        "provider_response_to_adapter_normalization_validated": raw_payload_count > 0,
        "adapter_normalization_validation_count": raw_payload_count,
        "normalized_usage_event_to_cost_calculation_validated": cost_function_runs_for_observed_events(events),
        "observed_usage_event_count": len(events),
        "cost_calculable_observed_event_count": int(telemetry["cost_calculable_count"]),
        "limitation": None
        if raw_payload_count > 0
        else "No raw provider response payloads were observed. Validation is limited to normalized usage events already recorded by SkillLayer.",
        "provider_payloads_fabricated": False,
    }


def build_readiness_summary(pipeline: dict[str, Any], telemetry: dict[str, Any]) -> dict[str, Any]:
    if pipeline["real_provider_observed"] and pipeline["observed_cost_calculable"]:
        readiness = "READY_FOR_REAL_COST_MEASUREMENT"
        interpretation = "A real provider usage event with calculable cost has been observed. The pipeline is ready for observed cost measurement on comparable runs."
    elif pipeline["usage_capture_ok"] and pipeline["adapter_ok"] and pipeline["cost_calculation_ok"]:
        readiness = "PARTIALLY_READY"
        interpretation = "The telemetry pipeline works, but current observed events are mock/local or cost-incomplete. Real provider usage payloads are still required before cost comparison can be enabled."
    else:
        readiness = "NOT_READY"
        interpretation = "The telemetry pipeline is missing basic observed usage or cost calculation health."
    return {
        "readiness": readiness,
        "real_provider_event_count": telemetry["real_provider_event_count"],
        "raw_provider_payload_count": telemetry["raw_provider_payload_count"],
        "cost_calculable_count": telemetry["cost_calculable_count"],
        "cost_comparison_ready": pipeline["cost_comparison_ready"],
        "missing_requirements": pipeline["missing_requirements"],
        "interpretation": interpretation,
        "savings_claimed": False,
        "provider_payloads_fabricated": False,
    }


def adapter_static_health_ok() -> bool:
    try:
        for provider in TARGET_PROVIDERS:
            adapter_for_provider(provider)
        return True
    except Exception:
        return False


def cost_function_runs_for_observed_events(events: list[dict[str, Any]]) -> bool:
    try:
        for event in events:
            calculate_event_cost(event)
        return True
    except Exception:
        return False


def provider_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for event in events:
        counter[provider_key(event.get("provider")) or "unknown"] += 1
    return dict(sorted(counter.items()))


def aggregate_unknown_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for reason in row.get("cost_unknown_reasons") or []:
            counter[str(reason)] += 1
    return dict(sorted(counter.items()))


def describe_usage_payload_shape(response: Any) -> dict[str, Any]:
    usage = read_field(response, "usage")
    shape = {
        "response_type": type(response).__name__,
        "has_usage": usage is not None,
        "top_level_fields": sorted(str(key) for key in response.keys()) if isinstance(response, dict) else [],
        "usage_fields": sorted(str(key) for key in usage.keys()) if isinstance(usage, dict) else [],
    }
    return shape


def first_present(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def provider_key(value: Any) -> str:
    return str(value or "").strip().lower()


def write_outputs(
    provider_capability_matrix: dict[str, Any],
    telemetry_completeness: dict[str, Any],
    pipeline_health: dict[str, Any],
    readiness_summary: dict[str, Any],
    report: dict[str, Any],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "provider_capability_matrix.json": provider_capability_matrix,
        "telemetry_completeness.json": telemetry_completeness,
        "pipeline_health.json": pipeline_health,
        "readiness_summary.json": readiness_summary,
        "report.json": report,
    }
    for filename, payload in outputs.items():
        (OUTPUT_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
