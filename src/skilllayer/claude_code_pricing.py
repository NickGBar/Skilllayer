"""SkillLayer-maintained rate table for Claude Code model usage.

This table is deliberately kept SEPARATE from ``skilllayer.pricing`` (which
models SkillLayer's own LLM-router costs with internal validation fixtures).
The rates here are real, published per-million-token prices used to turn the
token counts recorded in Claude Code's local session logs into estimated
dollar costs.

MAINTENANCE COST (stated plainly, not hidden): this is a hand-maintained
constant. It drifts from reality whenever Anthropic changes pricing. Every
report stamps ``table_version`` / ``last_updated`` / ``source`` so a consumer
can always see how stale the basis is. There is deliberately NO network price
fetch — that would violate the zero-network / zero-dependency contract.

Unknown model -> the caller must record ``pricing_known: false`` and
``estimated_cost_usd: null``. Never guess a rate.
"""

from __future__ import annotations

from typing import Any

# Bump this whenever CLAUDE_CODE_MODEL_PRICING changes.
CLAUDE_CODE_PRICE_TABLE_VERSION = "2026-07-06"

CLAUDE_CODE_PRICE_TABLE_META: dict[str, str] = {
    "table_version": CLAUDE_CODE_PRICE_TABLE_VERSION,
    "last_updated": "2026-07-06",
    "source": "claude.com/pricing",
    "currency": "USD",
    "unit": "USD per 1,000,000 tokens",
}

# Rates are $ per 1,000,000 tokens. Four categories, matching the four token
# fields recorded in Claude Code's message.usage:
#   input       -> input_tokens
#   output      -> output_tokens
#   cache_write -> cache_creation_input_tokens (5-minute ephemeral standard rate)
#   cache_read  -> cache_read_input_tokens
CLAUDE_CODE_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.50, "cache_read": 1.00},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
}


def pricing_reference_meta() -> dict[str, Any]:
    """Return a copy of the price-table metadata for embedding in a report."""
    return dict(CLAUDE_CODE_PRICE_TABLE_META)


def price_known(model: str | None) -> bool:
    """True only when the model is present in the maintained table."""
    return bool(model) and model in CLAUDE_CODE_MODEL_PRICING


def estimate_cost_usd(tokens: dict[str, int], model: str | None) -> float | None:
    """Estimate the USD cost of one message's token usage.

    ``tokens`` must carry integer keys: input, output, cache_write, cache_read.
    Returns None when the model is unknown — never guesses a rate.
    """
    entry = CLAUDE_CODE_MODEL_PRICING.get(model) if model else None
    if entry is None:
        return None
    cost = (
        tokens.get("input", 0) * entry["input"]
        + tokens.get("output", 0) * entry["output"]
        + tokens.get("cache_write", 0) * entry["cache_write"]
        + tokens.get("cache_read", 0) * entry["cache_read"]
    ) / 1_000_000
    return round(cost, 6)
