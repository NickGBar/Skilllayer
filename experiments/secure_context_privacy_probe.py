"""Explore privacy recognizer coverage without printing synthetic inputs."""
import os
from experiments.secure_context_hybrid_poc import analyze_with_gitleaks


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    cases = {
        "english_name": "Contact: Alice Smith",
        "russian_name": "Контакт: Иван Петров",
        "email_com": "Contact: alice@example.com",
        "email_test": "Contact: alice@example.test",
        "email_subdomain": "Contact: alice@support.example.com",
        "russian_mobile": "Телефон +7 999 123-45-67",
        "us_mobile": "Phone +1-202-555-0147",
        "fictional_address": "Address: 123 Example Street, Sampleville",
        "birth_date": "Дата рождения: 12.03.1990",
        "internal_user_id": "Internal customer identifier: CUST-538421",
    }
    for label, value in cases.items():
        decision, output = analyze_with_gitleaks(value, binary)
        print(f"{label}: decision={decision.value} changed={output is not None and output != value}")


if __name__ == "__main__":
    main()
