"""Policy config guards for the synthetic local experiment."""
import json
import os
import unittest
from unittest.mock import patch

from experiments.secure_context_policy_config import Decision, evaluate_with_policy, parse_policy


class PolicyConfigTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.dumps({"version": "v1", "sources": {"public": "allow", "internal": "local_only", "secret": "block"},
                                  "restricted_terms": ["project-alpha"]})

    def test_unknown_source_and_explicit_source_actions(self):
        with patch("experiments.secure_context_policy_config.analyze_guarded") as detector:
            for source, expected in (("missing", Decision.BLOCK), ("secret", Decision.BLOCK), ("internal", Decision.LOCAL_ONLY)):
                self.assertEqual(evaluate_with_policy("input", "binary", source, self.policy), (expected, None))
            detector.assert_not_called()

    def test_authorized_source_goes_through_guarded_checks(self):
        with patch("experiments.secure_context_policy_config.analyze_guarded", return_value=(Decision.BLOCK, None)) as detector:
            self.assertEqual(evaluate_with_policy("input", "binary", "public", self.policy), (Decision.BLOCK, None))
            detector.assert_called_once_with("input", "binary", source_authorized=True, restricted_terms=("project-alpha",))

    def test_malformed_policies_block(self):
        bad = ["{", "{}", '{"version":"v1","version":"v2","sources":{},"restricted_terms":[]}',
               json.dumps({"version": "v1", "sources": {"public": "approve"}, "restricted_terms": []}),
               json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": [""]}),
               json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": [], "extra": True})]
        with patch("experiments.secure_context_policy_config.analyze_guarded") as detector:
            for policy in bad:
                with self.subTest(policy=policy):
                    self.assertEqual(evaluate_with_policy("input", "binary", "public", policy), (Decision.BLOCK, None))
            detector.assert_not_called()

    def test_parsed_policy_is_immutable(self):
        policy = parse_policy(self.policy)
        with self.assertRaises(TypeError):
            policy.sources["public"] = "block"

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_real_detectors_after_policy_decision(self):
        binary = os.environ["SECURE_CONTEXT_GITLEAKS"]
        self.assertEqual(evaluate_with_policy("project-ALPHA", binary, "public", self.policy), (Decision.BLOCK, None))
        decision, output = evaluate_with_policy("email a.person@example.com", binary, "public", self.policy)
        self.assertEqual(decision, Decision.TRANSFORM)
        self.assertNotIn("a.person@example.com", output)
        secret = "TOKEN=ghp_" + "A1b2C3d4" * 5
        self.assertEqual(evaluate_with_policy(secret, binary, "public", self.policy), (Decision.BLOCK, None))


if __name__ == "__main__":
    unittest.main()
