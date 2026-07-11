from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


COST_DIR = Path("runs/skilllayer_cost")
USAGE_EVENTS_JSONL = COST_DIR / "usage_events.jsonl"
USAGE_SUMMARY_JSON = COST_DIR / "usage_summary.json"

USAGE_FIELDS = [
    "timestamp",
    "provider",
    "model",
    "workflow",
    "task",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
    "request_count",
    "duration_ms",
    "cost_known",
    "metadata",
]


def record_usage_event(
    *,
    timestamp: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    workflow: str | None = None,
    task: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    request_count: int | None = None,
    duration_ms: float | None = None,
    cost_known: bool | None = None,
    metadata: dict[str, Any] | None = None,
    output_path: str | Path = USAGE_EVENTS_JSONL,
) -> dict[str, Any]:
    event = normalize_usage_event(
        {
            "timestamp": timestamp or current_timestamp(),
            "provider": provider,
            "model": model,
            "workflow": workflow,
            "task": task,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "request_count": request_count,
            "duration_ms": duration_ms,
            "cost_known": cost_known,
            "metadata": metadata or {},
        }
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def aggregate_usage(
    input_path: str | Path = USAGE_EVENTS_JSONL,
    output_path: str | Path = USAGE_SUMMARY_JSON,
) -> dict[str, Any]:
    events = read_usage_events(input_path)
    summary = build_usage_summary(events)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def read_usage_events(path: str | Path = USAGE_EVENTS_JSONL) -> list[dict[str, Any]]:
    event_path = Path(path)
    if not event_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in event_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(normalize_usage_event(parsed))
    return events


def build_usage_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    provider_breakdown = build_breakdown(events, "provider")
    model_breakdown = build_breakdown(events, "model")
    workflow_breakdown = build_breakdown(events, "workflow")
    return {
        "total_events": len(events),
        "total_input_tokens": sum_tokens(events, "input_tokens"),
        "total_output_tokens": sum_tokens(events, "output_tokens"),
        "total_cached_tokens": sum_tokens(events, "cached_input_tokens"),
        "total_reasoning_tokens": sum_tokens(events, "reasoning_tokens"),
        "total_tokens": sum_tokens(events, "total_tokens"),
        "provider_breakdown": provider_breakdown,
        "model_breakdown": model_breakdown,
        "workflow_breakdown": workflow_breakdown,
        "unknown_usage_events": sum(1 for event in events if not bool(event.get("cost_known"))),
        "usage_events_path": str(Path(USAGE_EVENTS_JSONL)),
        "usage_summary_path": str(Path(USAGE_SUMMARY_JSON)),
        "cost_calculation_included": False,
        "roi_calculation_included": False,
        "dollar_calculation_included": False,
    }


def build_breakdown(events: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = str(event.get(field) or "unknown")
        grouped[key].append(event)
    return {
        key: {
            "events": len(items),
            "request_count": sum_tokens(items, "request_count"),
            "input_tokens": sum_tokens(items, "input_tokens"),
            "output_tokens": sum_tokens(items, "output_tokens"),
            "cached_input_tokens": sum_tokens(items, "cached_input_tokens"),
            "reasoning_tokens": sum_tokens(items, "reasoning_tokens"),
            "total_tokens": sum_tokens(items, "total_tokens"),
            "unknown_usage_events": sum(1 for item in items if not bool(item.get("cost_known"))),
        }
        for key, items in sorted(grouped.items())
    }


def sum_tokens(events: list[dict[str, Any]], field: str) -> int:
    return sum(int(event[field]) for event in events if event.get(field) is not None)


def normalize_usage_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = {field: event.get(field) for field in USAGE_FIELDS}
    normalized["timestamp"] = str(normalized.get("timestamp") or current_timestamp())
    normalized["metadata"] = dict(normalized.get("metadata") or {})
    for field in [
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "total_tokens",
        "request_count",
    ]:
        normalized[field] = as_int_or_none(normalized.get(field))
    normalized["duration_ms"] = as_float_or_none(normalized.get("duration_ms"))
    if normalized["total_tokens"] is None:
        pieces = [
            normalized.get("input_tokens"),
            normalized.get("output_tokens"),
            normalized.get("cached_input_tokens"),
            normalized.get("reasoning_tokens"),
        ]
        if any(piece is not None for piece in pieces):
            normalized["total_tokens"] = sum(int(piece or 0) for piece in pieces)
    if normalized["cost_known"] is None:
        token_fields = ["input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"]
        normalized["cost_known"] = any(normalized.get(field) is not None for field in token_fields)
    else:
        normalized["cost_known"] = bool(normalized["cost_known"])
    return normalized


class BaseUsageAdapter:
    provider = "unknown"

    def normalize(
        self,
        response: Any,
        *,
        workflow: str | None = None,
        task: str | None = None,
        model: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return normalize_usage_event(
            {
                "timestamp": current_timestamp(),
                "provider": self.provider,
                "model": model or read_field(response, "model"),
                "workflow": workflow,
                "task": task,
                "duration_ms": duration_ms,
                "request_count": 1,
                "cost_known": False,
                "metadata": metadata or {},
            }
        )


class OpenAIUsageAdapter(BaseUsageAdapter):
    provider = "openai"

    def normalize(
        self,
        response: Any,
        *,
        workflow: str | None = None,
        task: str | None = None,
        model: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        usage = read_field(response, "usage") or {}
        input_tokens = first_int(usage, ["input_tokens", "prompt_tokens"])
        output_tokens = first_int(usage, ["output_tokens", "completion_tokens"])
        total_tokens = first_int(usage, ["total_tokens"])
        input_details = read_field(usage, "input_tokens_details") or read_field(usage, "prompt_tokens_details") or {}
        output_details = read_field(usage, "output_tokens_details") or read_field(usage, "completion_tokens_details") or {}
        cached_input_tokens = first_int(input_details, ["cached_tokens", "cached_input_tokens"])
        reasoning_tokens = first_int(output_details, ["reasoning_tokens"])
        return normalize_usage_event(
            {
                "timestamp": current_timestamp(),
                "provider": self.provider,
                "model": model or read_field(response, "model"),
                "workflow": workflow,
                "task": task,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
                "request_count": 1,
                "duration_ms": duration_ms,
                "cost_known": any(value is not None for value in [input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens]),
                "metadata": metadata or {},
            }
        )


class AnthropicUsageAdapter(BaseUsageAdapter):
    provider = "anthropic"

    def normalize(
        self,
        response: Any,
        *,
        workflow: str | None = None,
        task: str | None = None,
        model: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        usage = read_field(response, "usage") or {}
        input_tokens = first_int(usage, ["input_tokens"])
        output_tokens = first_int(usage, ["output_tokens"])
        cache_creation = first_int(usage, ["cache_creation_input_tokens"])
        cache_read = first_int(usage, ["cache_read_input_tokens"])
        cached_input_tokens = None
        if cache_creation is not None or cache_read is not None:
            cached_input_tokens = int(cache_creation or 0) + int(cache_read or 0)
        return normalize_usage_event(
            {
                "timestamp": current_timestamp(),
                "provider": self.provider,
                "model": model or read_field(response, "model"),
                "workflow": workflow,
                "task": task,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "request_count": 1,
                "duration_ms": duration_ms,
                "cost_known": any(value is not None for value in [input_tokens, output_tokens, cached_input_tokens]),
                "metadata": metadata or {},
            }
        )


class OpenRouterUsageAdapter(OpenAIUsageAdapter):
    provider = "openrouter"


class GenericUsageAdapter(BaseUsageAdapter):
    provider = "generic"

    def normalize(
        self,
        response: Any,
        *,
        workflow: str | None = None,
        task: str | None = None,
        model: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        usage = read_field(response, "usage") or response or {}
        input_tokens = first_int(usage, ["input_tokens", "prompt_tokens"])
        output_tokens = first_int(usage, ["output_tokens", "completion_tokens"])
        cached_input_tokens = first_int(usage, ["cached_input_tokens", "cached_tokens"])
        reasoning_tokens = first_int(usage, ["reasoning_tokens"])
        total_tokens = first_int(usage, ["total_tokens"])
        return normalize_usage_event(
            {
                "timestamp": current_timestamp(),
                "provider": read_field(response, "provider") or self.provider,
                "model": model or read_field(response, "model") or read_field(usage, "model"),
                "workflow": workflow,
                "task": task,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
                "request_count": first_int(usage, ["request_count"]) or 1,
                "duration_ms": duration_ms,
                "cost_known": any(value is not None for value in [input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens]),
                "metadata": metadata or {},
            }
        )


def adapter_for_provider(provider: str | None) -> BaseUsageAdapter:
    provider_name = (provider or "").lower()
    if provider_name == "openai":
        return OpenAIUsageAdapter()
    if provider_name == "anthropic":
        return AnthropicUsageAdapter()
    if provider_name == "openrouter":
        return OpenRouterUsageAdapter()
    if provider_name == "generic":
        return GenericUsageAdapter()
    return GenericUsageAdapter()


def event_from_llm_client(
    llm: Any,
    *,
    workflow: str | None,
    task: str | None,
    duration_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = getattr(llm, "provider", None) or getattr(llm, "mode", None) or "unknown"
    model = getattr(llm, "model", None)
    call_count = as_int_or_none(getattr(llm, "call_count", None))
    input_tokens = as_int_or_none(getattr(llm, "prompt_tokens", None))
    output_tokens = as_int_or_none(getattr(llm, "completion_tokens", None))
    cached_input_tokens = as_int_or_none(getattr(llm, "cached_input_tokens", None))
    reasoning_tokens = as_int_or_none(getattr(llm, "reasoning_tokens", None))
    total_tokens = None
    if any(value is not None for value in [input_tokens, output_tokens, cached_input_tokens, reasoning_tokens]):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0) + int(cached_input_tokens or 0) + int(reasoning_tokens or 0)
    return normalize_usage_event(
        {
            "timestamp": current_timestamp(),
            "provider": provider,
            "model": model,
            "workflow": workflow,
            "task": task,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "request_count": call_count,
            "duration_ms": duration_ms,
            "cost_known": bool(call_count and total_tokens is not None),
            "metadata": metadata or {},
        }
    )


def read_field(value: Any, field: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def first_int(value: Any, fields: list[str]) -> int | None:
    for field in fields:
        parsed = as_int_or_none(read_field(value, field))
        if parsed is not None:
            return parsed
    return None


def as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
