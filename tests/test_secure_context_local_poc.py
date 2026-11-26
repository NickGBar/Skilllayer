"""Synthetic regression cases for the local, non-production policy probe."""
import builtins
import unittest
from unittest.mock import patch

from experiments.secure_context_local_poc import Decision, analyze
from experiments.secure_context_policy_demo import decide


class LocalPolicyProbeTests(unittest.TestCase):
    def test_clean_and_restricted(self):
        self.assertEqual(analyze("sum two numbers")[0], Decision.ALLOW)
        self.assertEqual(analyze("sum two numbers", restricted=True), (Decision.LOCAL_ONLY, None))

    def test_pii_is_replaced_and_secret_is_blocked(self):
        original = "Contact irina.smirnova@example.com or +1-202-555-0147."
        result, output = analyze(original)
        self.assertEqual(result, Decision.TRANSFORM)
        self.assertNotIn("irina.smirnova@example.com", output)
        self.assertNotIn("+1-202-555-0147", output)
        secret = 'api_key = "' + "abcXYZ0123456789" * 3 + '"'
        self.assertEqual(analyze(secret), (Decision.BLOCK, None))

    def test_dependency_failure_blocks_without_echoing_input(self):
        original_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "tldextract":
                raise ImportError("synthetic failure")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=failing_import):
            self.assertEqual(analyze("sensitive input"), (Decision.BLOCK, None))

    def test_incomplete_detector_and_secret_take_precedence(self):
        self.assertEqual(decide(detector_complete=False, secret=False, pii=False,
                                restricted=False, transformable=True), Decision.BLOCK)
        self.assertEqual(decide(detector_complete=True, secret=True, pii=True,
                                restricted=True, transformable=True), Decision.BLOCK)

    def test_incomplete_and_malformed_scanner_results_block(self):
        for result in ({"success": True, "scan_complete": False, "findings": []},
                       {"success": True, "scan_complete": True},
                       {"success": True, "scan_complete": True, "findings": None}):
            with self.subTest(result=result), patch(
                "experiments.secure_context_local_poc.build_detect_secrets_artifacts", return_value=result
            ):
                self.assertEqual(analyze("clean"), (Decision.BLOCK, None))

    def test_second_scan_failure_blocks_transformed_output(self):
        responses = [{"success": True, "scan_complete": True, "findings": []},
                     {"success": False, "scan_complete": False, "findings": []}]
        with patch("experiments.secure_context_local_poc.build_detect_secrets_artifacts", side_effect=responses):
            self.assertEqual(analyze("Email irina.smirnova@example.com"), (Decision.BLOCK, None))


if __name__ == "__main__":
    unittest.main()
