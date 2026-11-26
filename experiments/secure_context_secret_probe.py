"""Show synthetic secret coverage by layer without emitting secret values."""
import os
from experiments.secure_context_gitleaks import scan
from experiments.secure_context_hybrid_poc import Decision, analyze_with_gitleaks


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    # Values are intentionally fake; variations expose supported-pattern limits.
    cases = {
        "github_token": "TOKEN=ghp_" + "A1b2C3d4" * 5,
        "generic_api_key": 'api_key = "' + "abcXYZ0123456789" * 3 + '"',
        "plain_bearer": "Authorization: Bearer " + "abcXYZ0123456789" * 3,
        "aws_access_id": "AWS_ACCESS_KEY_ID=AKIA" + "ABCDEF234567GHJK",
        "fake_key_assigned_unusual_name": 'private_setting = "' + "abcXYZ0123456789" * 3 + '"',
        "encoded_fake_key": "encoded_key = YWJjWFlaMDEyMzQ1Njc4OQ==",
    }
    for label, value in cases.items():
        complete, found = scan(value, binary)
        decision, _ = analyze_with_gitleaks(value, binary, restricted=False)
        print(f"{label}: gitleaks_complete={complete} gitleaks_found={found} final={decision.value}")
        if not complete:
            raise SystemExit("Gitleaks failed to complete")
    print("Note: unknown opaque strings cannot be classified as secrets from shape alone.")


if __name__ == "__main__":
    main()
