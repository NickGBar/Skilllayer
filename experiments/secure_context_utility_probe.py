"""Bounded syntactic utility probe; all examples are synthetic and local."""
import ast
import os

from experiments.secure_context_hybrid_poc import Decision, analyze_with_gitleaks


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    cases = {
        "string_literal": ('contact = "a.person@example.com"\n', 'contact = "<EMAIL_ADDRESS_1>"\n'),
        "function_default": ('def contact(email="a.person@example.com"):\n    return email\n', 'def contact(email="<EMAIL_ADDRESS_1>"):\n    return email\n'),
        "dictionary": ('recipients = {"primary": "a.person@example.com", "backup": "b.person@example.com"}\n', 'recipients = {"primary": "<EMAIL_ADDRESS_1>", "backup": "<EMAIL_ADDRESS_2>"}\n'),
        "comment": ('# Contact a.person@example.com\nvalue = 42\n', '# Contact <EMAIL_ADDRESS_1>\nvalue = 42\n'),
        "repeated": ('first = "a.person@example.com"\nsecond = "a.person@example.com"\n', 'first = "<EMAIL_ADDRESS_1>"\nsecond = "<EMAIL_ADDRESS_1>"\n'),
    }
    successes = 0
    for name, (original, expected) in cases.items():
        ast.parse(original)
        decision, output = analyze_with_gitleaks(original, binary)
        parseable = False
        if output is not None:
            try:
                ast.parse(output)
                parseable = True
            except SyntaxError:
                pass
        ok = decision == Decision.TRANSFORM and parseable and output == expected
        successes += ok
        print(f"{name}: decision={decision.value} parseable={parseable} exact_expected={ok}")
    print(f"syntactic_cases_passed={successes}/{len(cases)}")
    return int(successes != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
