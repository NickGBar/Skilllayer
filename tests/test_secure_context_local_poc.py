"""Synthetic regression cases for the local, non-production policy probe."""
import builtins
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from experiments.secure_context_local_poc import Decision, analyze
from experiments.secure_context_policy_demo import decide
from experiments.secure_context_route_probe import route


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
                       {"success": True, "scan_complete": True, "findings": None},
                       {"success": True, "scan_complete": True, "findings": [], "scanned_files": 0, "scanned_bytes": 0}):
            with self.subTest(result=result), patch(
                "experiments.secure_context_local_poc.build_detect_secrets_artifacts", return_value=result
            ):
                self.assertEqual(analyze("clean"), (Decision.BLOCK, None))

    def test_second_scan_failure_blocks_transformed_output(self):
        responses = [{"success": True, "scan_complete": True, "findings": [], "scanned_files": 1,
                      "scanned_bytes": len("Email irina.smirnova@example.com".encode("utf-8"))},
                     {"success": False, "scan_complete": False, "findings": []}]
        with patch("experiments.secure_context_local_poc.build_detect_secrets_artifacts", side_effect=responses):
            self.assertEqual(analyze("Email irina.smirnova@example.com"), (Decision.BLOCK, None))

    def test_same_identity_keeps_alias_and_distinct_identities_differ(self):
        decision, output = analyze("a.person@example.com b.person@example.com a.person@example.com")
        self.assertEqual(decision, Decision.TRANSFORM)
        self.assertEqual(output.count("<EMAIL_ADDRESS_1>"), 2)
        self.assertEqual(output.count("<EMAIL_ADDRESS_2>"), 1)
        self.assertNotIn("a.person@example.com", output)
        self.assertNotIn("b.person@example.com", output)

    def test_transformed_python_string_remains_parseable(self):
        import ast
        decision, output = analyze('contact = "a.person@example.com"')
        self.assertEqual(decision, Decision.TRANSFORM)
        ast.parse(output)

    def test_existing_placeholder_collision_blocks(self):
        self.assertEqual(analyze("<EMAIL_ADDRESS_1> a.person@example.com"), (Decision.BLOCK, None))

    def test_overlapping_detector_findings_block(self):
        findings = [SimpleNamespace(start=0, end=10, entity_type="EMAIL_ADDRESS"),
                    SimpleNamespace(start=5, end=15, entity_type="PHONE_NUMBER")]
        with patch("presidio_analyzer.AnalyzerEngine.analyze", return_value=findings):
            self.assertEqual(analyze("a.person@example.com"), (Decision.BLOCK, None))

    def test_oversized_and_binary_inputs_block(self):
        self.assertEqual(analyze("x" * 1_048_577), (Decision.BLOCK, None))
        self.assertEqual(analyze("text\x00hidden"), (Decision.BLOCK, None))

    def test_fake_transport_sends_only_valid_allowed_output(self):
        for decision, output, expected in ((Decision.ALLOW, "safe", ["safe"]),
                                           (Decision.TRANSFORM, "masked", ["masked"]),
                                           (Decision.BLOCK, None, []),
                                           (Decision.LOCAL_ONLY, None, []),
                                           (Decision.ALLOW, None, [])):
            sent = []
            with self.subTest(decision=decision, output=output), patch(
                "experiments.secure_context_route_probe.analyze", return_value=(decision, output)
            ):
                result = route("synthetic", False, sent.append)
                self.assertEqual(sent, expected)
                if decision == Decision.ALLOW and output is None:
                    self.assertEqual(result, Decision.BLOCK)


if __name__ == "__main__":
    unittest.main()
