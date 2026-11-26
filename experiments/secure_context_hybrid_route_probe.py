"""Synthetic-only local policy and fake transport; no gateway or network."""
from experiments.secure_context_hybrid_poc import Decision, analyze_guarded


def route(text: str, binary: str, sent: list[str], *, source_authorized: bool = False,
          restricted: bool = False, restricted_terms: tuple[str, ...] = ()) -> Decision:
    decision, output = analyze_guarded(text, binary, source_authorized=source_authorized,
                                       restricted=restricted, restricted_terms=restricted_terms)
    if decision in (Decision.ALLOW, Decision.TRANSFORM):
        if not isinstance(output, str):
            return Decision.BLOCK
        sent.append(output)
    return decision


def main():
    import os
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    sent: list[str] = []
    cases = [
        ("unknown", "Describe a list", False, False, (), Decision.BLOCK),
        ("clean", "Describe a list", True, False, (), Decision.ALLOW),
        ("pii", "Email a.person@example.com", True, False, (), Decision.TRANSFORM),
        ("named_service", "project-alpha service", True, False, ("project-alpha",), Decision.BLOCK),
        ("restricted", "Private logic", True, True, (), Decision.LOCAL_ONLY),
        ("secret", "TOKEN=ghp_" + "A1b2C3d4" * 5, True, False, (), Decision.BLOCK),
    ]
    for label, text, authorized, restricted, terms, expected in cases:
        before = len(sent)
        decision = route(text, binary, sent, source_authorized=authorized,
                         restricted=restricted, restricted_terms=terms)
        count = len(sent) - before
        assert decision == expected and count == int(expected in (Decision.ALLOW, Decision.TRANSFORM))
        if decision == Decision.TRANSFORM:
            assert "a.person@example.com" not in sent[-1]
        print(f"{label}: decision={decision.value} fake_sends={count}")
    print(f"fake_sends_total={len(sent)} real_network_calls=0")


if __name__ == "__main__":
    main()
