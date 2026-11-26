"""Strict synthetic policy config for the local Secure Context probe."""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from experiments.secure_context_hybrid_poc import Decision, analyze_guarded


@dataclass(frozen=True)
class Policy:
    version: str
    sources: Mapping[str, str]
    restricted_terms: tuple[str, ...]


def parse_policy(raw: str) -> Policy:
    """Reject unexpected, missing or ambiguous fields rather than guessing."""
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate policy key")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid policy JSON") from exc
    if not isinstance(data, dict) or set(data) != {"version", "sources", "restricted_terms"}:
        raise ValueError("invalid policy fields")
    version, sources, terms = data["version"], data["sources"], data["restricted_terms"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("invalid policy version")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("invalid source policy")
    if any(not isinstance(k, str) or not k.strip() or v not in ("allow", "block", "local_only")
           for k, v in sources.items()):
        raise ValueError("invalid source entry")
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError("invalid restricted terms")
    return Policy(version, MappingProxyType(dict(sources)), tuple(terms))


def evaluate_with_reason(text: str, binary: str, source: str, raw_policy: str) -> tuple[Decision, str | None, str]:
    """Return a bounded reason code; never include input, source or policy text."""
    try:
        policy = parse_policy(raw_policy)
        action = policy.sources.get(source)
    except (TypeError, ValueError):
        return Decision.BLOCK, None, "invalid_policy_or_source"
    if action is None:
        return Decision.BLOCK, None, "unknown_source"
    if action == "block":
        return Decision.BLOCK, None, "source_blocked"
    if action == "local_only":
        return Decision.LOCAL_ONLY, None, "source_local_only"
    try:
        decision, output = analyze_guarded(text, binary, source_authorized=True, restricted_terms=policy.restricted_terms)
    except Exception:
        return Decision.BLOCK, None, "content_or_detector_blocked"
    reasons = {Decision.BLOCK: "content_or_detector_blocked", Decision.LOCAL_ONLY: "local_only",
               Decision.TRANSFORM: "content_transformed", Decision.ALLOW: "narrow_checks_clear"}
    if not isinstance(decision, Decision):
        return Decision.BLOCK, None, "content_or_detector_blocked"
    if decision in (Decision.BLOCK, Decision.LOCAL_ONLY):
        output = None
    elif not isinstance(output, str) or (decision == Decision.ALLOW and output != text):
        return Decision.BLOCK, None, "content_or_detector_blocked"
    return decision, output, reasons[decision]


def evaluate_with_policy(text: str, binary: str, source: str, raw_policy: str) -> tuple[Decision, str | None]:
    decision, output, _ = evaluate_with_reason(text, binary, source, raw_policy)
    return decision, output
