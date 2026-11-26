"""Show the limits of literal blocking with synthetic names, no raw output."""
from experiments.secure_context_hybrid_poc import Decision, analyze_guarded


def main():
    # Restriction hits occur before Gitleaks; the deliberately missing binary
    # means clean-looking variants are also blocked by the next fail-closed layer.
    # Here a stub makes the rule matcher observable without claiming safety.
    from unittest.mock import patch

    examples = {
        "exact": "project-alpha",
        "uppercase": "PROJECT-ALPHA",
        "different_punctuation": "project_alpha",
        "inserted_space": "project alpha",
        "encoded_hyphen": "project%2Dalpha",
        "unicode_hyphen": "project\u2011alpha",
        "zero_width": "project\u200b-alpha",
    }
    with patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks", return_value=(Decision.ALLOW, "synthetic")):
        for label, value in examples.items():
            decision, _ = analyze_guarded(value, "/not-used", source_authorized=True,
                                          restricted_terms=("project-alpha",))
            print(f"{label}: literal_rule_blocked={decision == Decision.BLOCK}")


if __name__ == "__main__":
    main()
