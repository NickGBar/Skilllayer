"""Synthetic policy-to-detectors-to-in-memory-sink demonstration."""
import os
from pathlib import Path

from experiments.secure_context_policy_config import Decision, evaluate_with_policy


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    policy = Path(__file__).with_name("secure_context_policy_example.json").read_text(encoding="utf-8")
    cases = [
        ("unknown", "Describe a list", "unknown", Decision.BLOCK),
        ("clean", "Describe a list", "synthetic-public", Decision.ALLOW),
        ("pii", "Email a.person@example.com", "synthetic-public", Decision.TRANSFORM),
        ("named_service", "PROJECT-ALPHA service", "synthetic-public", Decision.BLOCK),
        ("internal", "Private logic", "synthetic-internal", Decision.LOCAL_ONLY),
        ("restricted_source", "Anything", "synthetic-secret", Decision.BLOCK),
        ("secret", "TOKEN=ghp_" + "A1b2C3d4" * 5, "synthetic-public", Decision.BLOCK),
    ]
    sent = []
    for label, text, source, expected in cases:
        decision, output = evaluate_with_policy(text, binary, source, policy)
        if decision in (Decision.ALLOW, Decision.TRANSFORM) and isinstance(output, str):
            sent.append(output)
        assert decision == expected
        if decision == Decision.TRANSFORM:
            assert "a.person@example.com" not in output
        print(f"{label}: decision={decision.value}")
    print(f"fake_sends_total={len(sent)} real_network_calls=0")
    assert len(sent) == 2


if __name__ == "__main__":
    main()
