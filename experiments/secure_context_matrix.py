"""Measure a small, fixed synthetic set; no request is sent anywhere."""
from time import perf_counter
from experiments.secure_context_local_poc import analyze


def main():
    cases = [
        ("clean_summary", "Summarize this public README.", False, "ALLOW"),
        ("clean_function", "def add(a, b): return a + b", False, "ALLOW"),
        ("clean_error", "ValueError: invalid input", False, "ALLOW"),
        ("clean_list", "Sort values in ascending order", False, "ALLOW"),
        ("clean_date", "Run date: 2026-11-26", False, "ALLOW"),
        ("email", "Email irina.smirnova@example.com", False, "TRANSFORM"),
        ("email_twice", "a.person@example.com then a.person@example.com", False, "TRANSFORM"),
        ("email_and_phone", "a.person@example.com +1-202-555-0147", False, "TRANSFORM"),
        ("phone", "Phone: +1-202-555-0147", False, "TRANSFORM"),
        ("mixed_code", 'contact = "a.person@example.com"', False, "TRANSFORM"),
        ("generic_secret", 'api_key = "' + "abcXYZ0123456789" * 3 + '"', False, "BLOCK"),
        ("github_secret", 'token = "ghp_' + "A1b2C3d4" * 5 + '"', False, "BLOCK"),
        ("secret_and_pii", 'api_key = "' + "abcXYZ0123456789" * 3 + '" email a.person@example.com', False, "BLOCK"),
        ("restricted_code", "def private_business_logic(): pass", True, "LOCAL_ONLY"),
        ("restricted_clean", "Describe internal service", True, "LOCAL_ONLY"),
        ("restricted_pii", "a.person@example.com", True, "LOCAL_ONLY"),
        ("restricted_secret", 'api_key = "' + "abcXYZ0123456789" * 3 + '"', True, "BLOCK"),
        ("email_domain", "Support: service.team@example.com", False, "TRANSFORM"),
        ("clean_math", "return sum(values) / len(values)", False, "ALLOW"),
        ("clean_text", "Explain how a Python list works", False, "ALLOW"),
    ]
    matches = 0
    start = perf_counter()
    for name, value, restricted, expected in cases:
        started = perf_counter()
        actual, _ = analyze(value, restricted=restricted)
        matches += actual.value == expected
        print(f"{name}: expected={expected} actual={actual.value} duration_ms={(perf_counter()-started)*1000:.1f}")
    print(f"matches={matches}/{len(cases)} total_seconds={perf_counter()-start:.2f}")
    return 0 if matches == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
