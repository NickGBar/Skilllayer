"""Bounded syntactic utility probe; all examples are synthetic and local."""
import ast
import os

from experiments.secure_context_hybrid_poc import Decision, analyze_with_gitleaks


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    cases = {
        "string_literal": 'contact = "a.person@example.com"\n',
        "function_default": 'def contact(email="a.person@example.com"):\n    return email\n',
        "dictionary": 'recipients = {"primary": "a.person@example.com", "backup": "b.person@example.com"}\n',
        "comment": '# Contact a.person@example.com\nvalue = 42\n',
        "repeated": 'first = "a.person@example.com"\nsecond = "a.person@example.com"\n',
    }
    successes = 0
    for name, original in cases.items():
        ast.parse(original)
        decision, output = analyze_with_gitleaks(original, binary)
        parseable = False
        if output is not None:
            try:
                ast.parse(output)
                parseable = True
            except SyntaxError:
                pass
        ok = decision == Decision.TRANSFORM and parseable and "@example.com" not in output
        successes += ok
        print(f"{name}: decision={decision.value} parseable={parseable} expected_met={ok}")
    print(f"syntactic_cases_passed={successes}/{len(cases)}")
    return int(successes != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
