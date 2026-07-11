from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .cost_tracking import USAGE_EVENTS_JSONL, normalize_usage_event, read_usage_events


COST_SUMMARY_JSON = Path("runs/skilllayer_cost/cost_summary.json")
VALIDATION_DIR = Path("runs/p5_6_2_cost_calculator")
VALIDATION_EVENTS_JSONL = VALIDATION_DIR / "validation_events.jsonl"
VALIDATION_COSTS_JSON = VALIDATION_DIR / "validation_costs.json"

PRICE_TABLE_VERSION = "2026-06-18.p5.6.2"

PRICING_TABLE: dict[str, dict[str, dict[str, Any]]] = {
    "openai": {
        "validation-openai-model": {
            "pricing_known": True,
            "input_cost_per_1m_tokens": 1.00,
            "output_cost_per_1m_tokens": 2.00,
            "cached_input_cost_per_1m_tokens": 0.25,
            "reasoning_cost_per_1m_tokens": None,
            "currency": "USD",
            "pricing_source": "internal validation fixture; not provider production pricing",
            "last_updated": "2026-06-18",
        }
    },
    "anthropic": {
        "validation-anthropic-model": {
            "pricing_known": True,
            "input_cost_per_1m_tokens": 3.00,
            "output_cost_per_1m_tokens": 15.00,
            "cached_input_cost_per_1m_tokens": 0.30,
            "reasoning_cost_per_1m_tokens": None,
            "currency": "USD",
            "pricing_source": "internal validation fixture; not provider production pricing",
            "last_updated": "2026-06-18",
        }
    },
    "openrouter": {
        "validation/openrouter-model": {
            "pricing_known": True,
            "input_cost_per_1m_tokens": 0.50,
            "output_cost_per_1m_tokens": 1.50,
            "cached_input_cost_per_1m_tokens": None,
            "reasoning_cost_per_1m_tokens": None,
            "currency": "USD",
            "pricing_source": "internal validation fixture; not provider production pricing",
            "last_updated": "2026-06-18",
        }
    },
    "generic": {
        "validation-generic-free-model": {
            "pricing_known": True,
            "input_cost_per_1m_tokens": 0.00,
            "output_cost_per_1m_tokens": 0.00,
            "cached_input_cost_per_1m_tokens": 0.00,
            "reasoning_cost_per_1m_tokens": 0.00,
            "currency": "USD",
            "pricing_source": "internal validation fixture for zero-cost local models",
            "last_updated": "2026-06-18",
        }
    },
}


def calculate_event_cost(event: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_usage_event(event)
    provider = normalized.get("provider")
    model = normalized.get("model")
    missing_fields: list[str] = []
    unknown_reasons: list[str] = []

    if not provider:
        missing_fields.append("provider")
        unknown_reasons.append("provider_missing")
    if not model:
        missing_fields.append("model")
        unknown_reasons.append("model_missing")

    pricing = get_model_pricing(provider, model) if provider and model else None
    pricing_known = bool(pricing and pricing.get("pricing_known"))
    if provider and model and not pricing_known:
        unknown_reasons.append("pricing_unknown")

    input_tokens = normalized.get("input_tokens")
    output_tokens = normalized.get("output_tokens")
    cached_input_tokens = normalized.get("cached_input_tokens")
    reasoning_tokens = normalized.get("reasoning_tokens")

    if input_tokens is None:
        missing_fields.append("input_tokens")
        unknown_reasons.append("input_tokens_missing")
    if output_tokens is None:
        missing_fields.append("output_tokens")
        unknown_reasons.append("output_tokens_missing")

    if cached_input_tokens is not None and pricing_known and pricing.get("cached_input_cost_per_1m_tokens") is None:
        missing_fields.append("cached_input_cost_per_1m_tokens")
        unknown_reasons.append("cached_input_price_missing")
    if reasoning_tokens is not None and pricing_known and pricing.get("reasoning_cost_per_1m_tokens") is None:
        missing_fields.append("reasoning_cost_per_1m_tokens")
        unknown_reasons.append("reasoning_price_missing")

    cost_known = bool(pricing_known and not missing_fields)
    input_cost = token_cost(input_tokens, pricing.get("input_cost_per_1m_tokens") if pricing else None) if cost_known else 0.0
    output_cost = token_cost(output_tokens, pricing.get("output_cost_per_1m_tokens") if pricing else None) if cost_known else 0.0
    cached_cost = token_cost(cached_input_tokens, pricing.get("cached_input_cost_per_1m_tokens") if pricing else None) if cost_known else 0.0
    reasoning_cost = token_cost(reasoning_tokens, pricing.get("reasoning_cost_per_1m_tokens") if pricing else None) if cost_known else 0.0
    total_cost = input_cost + output_cost + cached_cost + reasoning_cost

    return {
        "cost_known": cost_known,
        "provider": provider,
        "model": model,
        "input_cost_usd": round(input_cost, 12),
        "output_cost_usd": round(output_cost, 12),
        "cached_input_cost_usd": round(cached_cost, 12),
        "reasoning_cost_usd": round(reasoning_cost, 12),
        "total_cost_usd": round(total_cost, 12),
        "pricing_known": pricing_known,
        "missing_fields": sorted(set(missing_fields)),
        "unknown_reasons": sorted(set(unknown_reasons)),
        "currency": "USD",
        "pricing_table_version": PRICE_TABLE_VERSION,
        "pricing_source": pricing.get("pricing_source") if pricing else None,
        "last_updated": pricing.get("last_updated") if pricing else None,
    }


def aggregate_costs(
    input_path: str | Path = USAGE_EVENTS_JSONL,
    output_path: str | Path = COST_SUMMARY_JSON,
) -> dict[str, Any]:
    events = read_usage_events(input_path)
    cost_results = [calculate_event_cost(event) for event in events]
    summary = build_cost_summary(events, cost_results)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def build_cost_summary(events: list[dict[str, Any]], cost_results: list[dict[str, Any]]) -> dict[str, Any]:
    known_results = [result for result in cost_results if result["cost_known"]]
    unknown_results = [result for result in cost_results if not result["cost_known"]]
    return {
        "total_events": len(cost_results),
        "cost_known_events": len(known_results),
        "cost_unknown_events": len(unknown_results),
        "total_cost_usd": round(sum(float(result["total_cost_usd"]) for result in known_results), 12),
        "provider_cost_breakdown": build_cost_breakdown(cost_results, "provider"),
        "model_cost_breakdown": build_cost_breakdown(cost_results, "model"),
        "workflow_cost_breakdown": build_workflow_cost_breakdown(events, cost_results),
        "unknown_cost_reasons": build_unknown_reason_counts(unknown_results),
        "pricing_table_version": PRICE_TABLE_VERSION,
        "currency": "USD",
        "cost_calculation_scope": "observed_usage_only",
        "missing_tokens_inferred": False,
        "savings_calculation_included": False,
        "roi_calculation_included": False,
        "dollar_savings_calculation_included": False,
        "pricing_may_be_stale": True,
        "usage_events_path": str(Path(USAGE_EVENTS_JSONL)),
        "cost_summary_path": str(Path(COST_SUMMARY_JSON)),
    }


def build_cost_breakdown(results: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result.get(field) or "unknown")].append(result)
    return {
        key: {
            "events": len(items),
            "cost_known_events": sum(1 for item in items if item["cost_known"]),
            "cost_unknown_events": sum(1 for item in items if not item["cost_known"]),
            "total_cost_usd": round(sum(float(item["total_cost_usd"]) for item in items if item["cost_known"]), 12),
        }
        for key, items in sorted(grouped.items())
    }


def build_workflow_cost_breakdown(events: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event, result in zip(events, results):
        grouped[str(event.get("workflow") or "unknown")].append(result)
    return {
        key: {
            "events": len(items),
            "cost_known_events": sum(1 for item in items if item["cost_known"]),
            "cost_unknown_events": sum(1 for item in items if not item["cost_known"]),
            "total_cost_usd": round(sum(float(item["total_cost_usd"]) for item in items if item["cost_known"]), 12),
        }
        for key, items in sorted(grouped.items())
    }


def build_unknown_reason_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for result in results:
        reasons = result.get("unknown_reasons") or ["unknown"]
        for reason in reasons:
            counter[str(reason)] += 1
    return dict(sorted(counter.items()))


def get_model_pricing(provider: Any, model: Any) -> dict[str, Any] | None:
    provider_key = normalize_key(provider)
    model_key = normalize_key(model)
    if not provider_key or not model_key:
        return None
    provider_prices = PRICING_TABLE.get(provider_key)
    if not provider_prices:
        return None
    pricing = provider_prices.get(model_key)
    return dict(pricing) if pricing else None


def token_cost(tokens: Any, cost_per_1m_tokens: Any) -> float:
    if tokens is None or cost_per_1m_tokens is None:
        return 0.0
    return float(tokens) * float(cost_per_1m_tokens) / 1_000_000.0


def normalize_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def validation_events() -> list[dict[str, Any]]:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return [
        {
            "timestamp": timestamp,
            "provider": "openai",
            "model": "validation-openai-model",
            "workflow": "ValidationWorkflow",
            "task": "OpenAI-like event with input/output tokens",
            "input_tokens": 1_000_000,
            "output_tokens": 500_000,
            "cached_input_tokens": 100_000,
            "reasoning_tokens": None,
            "total_tokens": 1_600_000,
            "request_count": 1,
            "duration_ms": 10.0,
            "cost_known": True,
            "metadata": {"fixture": "openai_like"},
        },
        {
            "timestamp": timestamp,
            "provider": "anthropic",
            "model": "validation-anthropic-model",
            "workflow": "ValidationWorkflow",
            "task": "Anthropic-like event with input/output tokens",
            "input_tokens": 200_000,
            "output_tokens": 50_000,
            "cached_input_tokens": 25_000,
            "reasoning_tokens": None,
            "total_tokens": 275_000,
            "request_count": 1,
            "duration_ms": 12.0,
            "cost_known": True,
            "metadata": {"fixture": "anthropic_like"},
        },
        {
            "timestamp": timestamp,
            "provider": "openrouter",
            "model": "validation/openrouter-model",
            "workflow": "ValidationWorkflow",
            "task": "OpenRouter-like event with input/output tokens",
            "input_tokens": 300_000,
            "output_tokens": 70_000,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": 370_000,
            "request_count": 1,
            "duration_ms": 14.0,
            "cost_known": True,
            "metadata": {"fixture": "openrouter_like"},
        },
        {
            "timestamp": timestamp,
            "provider": "openai",
            "model": "unknown-model",
            "workflow": "ValidationWorkflow",
            "task": "Unknown model event",
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": 15,
            "request_count": 1,
            "duration_ms": 1.0,
            "cost_known": True,
            "metadata": {"fixture": "unknown_model"},
        },
        {
            "timestamp": timestamp,
            "provider": "openai",
            "model": "validation-openai-model",
            "workflow": "ValidationWorkflow",
            "task": "Missing token event",
            "input_tokens": 10,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "request_count": 1,
            "duration_ms": 1.0,
            "cost_known": False,
            "metadata": {"fixture": "missing_tokens"},
        },
    ]


def write_validation_fixtures(output_dir: str | Path = VALIDATION_DIR) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    events = validation_events()
    events_path = output / "validation_events.jsonl"
    with events_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    costs = [calculate_event_cost(event) for event in events]
    costs_path = output / "validation_costs.json"
    costs_path.write_text(json.dumps(costs, indent=2), encoding="utf-8")
    summary = build_cost_summary(events, costs)
    report = {
        "milestone": "Cost Calculator",
        "validation_event_count": len(events),
        "validation_costs": costs,
        "summary": summary,
        "pricing_table_version": PRICE_TABLE_VERSION,
        "cost_calculation_scope": "observed_usage_only",
        "savings_calculation_included": False,
        "roi_calculation_included": False,
        "dollar_savings_calculation_included": False,
        "outputs": {
            "validation_events_jsonl": str(events_path),
            "validation_costs_json": str(costs_path),
            "summary_json": str(output / "summary.json"),
            "report_json": str(output / "report.json"),
        },
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
