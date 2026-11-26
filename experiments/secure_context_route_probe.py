"""Exercise one fake local transport with synthetic input, never network I/O."""
from experiments.secure_context_local_poc import Decision, analyze


def route(text, restricted, send):
    decision, output = analyze(text, restricted=restricted)
    if decision in (Decision.ALLOW, Decision.TRANSFORM):
        send(output)
    return decision


def main():
    sent = []
    token = "abcXYZ0123456789" * 3
    cases = [
        ("clean", "Explain a list", False, Decision.ALLOW),
        ("pii", "Email irina.smirnova@example.com", False, Decision.TRANSFORM),
        ("secret", 'api_key = "' + token + '"', False, Decision.BLOCK),
        ("restricted", "Internal implementation", True, Decision.LOCAL_ONLY),
    ]
    for name, text, restricted, expected in cases:
        before = len(sent)
        decision = route(text, restricted, sent.append)
        expected_sends = int(expected in (Decision.ALLOW, Decision.TRANSFORM))
        assert decision == expected and len(sent) - before == expected_sends
        if decision == Decision.TRANSFORM:
            assert "irina.smirnova@example.com" not in sent[-1]
        print(f"{name}: {decision.value} fake_sends={len(sent)-before}")
    print(f"fake_sends_total={len(sent)}; real_network_calls=0")


if __name__ == "__main__":
    main()
