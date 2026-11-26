"""Bounded synthetic-only audit of a local policy decision."""
from experiments.secure_context_hybrid_poc import analyze_guarded
from experiments.secure_context_policy_config import evaluate_with_reason


def evaluate(text: str, binary: str, *, source_authorized: bool = False, restricted: bool = False) -> dict[str, str | bool]:
    decision, _ = analyze_guarded(text, binary, source_authorized=source_authorized, restricted=restricted)
    # This decision-only event deliberately makes no claim about which
    # detector matched, whether input is safe, or what values were present.
    return {
        "decision": decision.value,
        "network_attempted": False,
        "scope": "synthetic_local_probe",
        "policy_version": "experimental-v1",
        "safety_verified": False,
    }


def evaluate_policy_event(text: str, binary: str, source: str, raw_policy: str) -> dict[str, str | bool]:
    decision, _, reason = evaluate_with_reason(text, binary, source, raw_policy)
    return {"decision": decision.value, "reason": reason, "network_attempted": False,
            "scope": "synthetic_local_probe", "safety_verified": False}


def evaluate_file_event(text: str, binary: str, path: str, source: str,
                        raw_policy: str) -> dict[str, str | bool]:
    """Emit bounded policy/path reason without storing raw metadata or text."""
    from experiments.secure_context_path_policy import Decision, decide_path
    from experiments.secure_context_policy_config import parse_policy

    try:
        policy = parse_policy(raw_policy)
    except (TypeError, ValueError):
        decision, reason = Decision.BLOCK, "invalid_policy"
    else:
        if decide_path(path, allowed=policy.allowed_paths, forbidden=policy.forbidden_paths) != Decision.ALLOW:
            decision, reason = Decision.BLOCK, "path_policy_blocked"
        else:
            decision, _, reason = evaluate_with_reason(text, binary, source, raw_policy)
    return {"decision": decision.value, "reason": reason, "network_attempted": False,
            "scope": "synthetic_local_probe", "safety_verified": False}


if __name__ == "__main__":
    import json
    import os

    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    print(json.dumps(evaluate("Contact alice@example.com", binary, source_authorized=True), sort_keys=True))
