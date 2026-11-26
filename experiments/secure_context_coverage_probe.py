"""Probe known unsupported data classes; output labels, never input strings."""
import os
from experiments.secure_context_hybrid_poc import analyze_with_gitleaks


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    cases = {
        "person_name": "Customer contact: Иван Петров",
        "company_name": "Customer is Example Holdings LLC",
        "internal_host": "Connect to internal-payments.example.internal",
        "business_logic": 'def authorize(user): return user.role == "admin"',
        "fictional_email_domain": "Email dev@example.test",
        "email_real_domain": "Email dev@example.com",
    }
    for label, value in cases.items():
        decision, output = analyze_with_gitleaks(value, binary)
        print(f"{label}: decision={decision.value} transformed={output is not None and output != value}")


if __name__ == "__main__":
    main()
