"""Synthetic checks for the optional Gitleaks composition."""
import os
import subprocess
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from experiments.secure_context_gitleaks import scan
from experiments.secure_context_hybrid_poc import Decision, analyze_guarded, analyze_with_gitleaks
from experiments.secure_context_audit_probe import evaluate
from experiments.secure_context_hybrid_route_probe import route


class GitleaksProbeTests(unittest.TestCase):
    def test_missing_binary_and_timeout_fail_closed(self):
        self.assertEqual(scan("clean", "/missing/gitleaks"), (False, False))
        with patch("experiments.secure_context_gitleaks.subprocess.run", side_effect=subprocess.TimeoutExpired("gitleaks", 1)):
            self.assertEqual(scan("clean", "gitleaks"), (False, False))

    def test_invalid_exit_and_missing_report_fail_closed(self):
        for code in (0, 1, 2):
            with self.subTest(code=code), patch("experiments.secure_context_gitleaks.subprocess.run", return_value=SimpleNamespace(returncode=code)):
                self.assertEqual(scan("clean", "gitleaks"), (False, False))

    def test_malformed_report_and_mismatched_exit_fail_closed(self):
        for data, code in (("invalid", 0), (json.dumps({}), 0), ("[]", 1), ("[{}]", 0)):
            def fake_run(args, **kwargs):
                Path(args[args.index("--report-path") + 1]).write_text(data)
                return SimpleNamespace(returncode=code)

            with self.subTest(data=data, code=code), patch("experiments.secure_context_gitleaks.subprocess.run", side_effect=fake_run):
                self.assertEqual(scan("clean", "gitleaks"), (False, False))

    def test_input_is_stdin_and_untrusted_config_is_removed(self):
        observed = {}

        def fake_run(args, **kwargs):
            observed.update(kwargs)
            observed["args"] = args
            Path(args[args.index("--report-path") + 1]).write_text("[]")
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {"GITLEAKS_CONFIG": "/bad/config", "GITLEAKS_CONFIG_TOML": "bad", "TEST_SECRET": "synthetic-private"}), \
             patch("experiments.secure_context_gitleaks.subprocess.run", side_effect=fake_run):
            self.assertEqual(scan("sensitive test value", "/tmp/gitleaks"), (True, False))
        self.assertEqual(observed["input"], "sensitive test value")
        self.assertNotIn("sensitive test value", observed["args"])
        self.assertNotIn("GITLEAKS_CONFIG", observed["env"])
        self.assertNotIn("GITLEAKS_CONFIG_TOML", observed["env"])
        self.assertNotIn("TEST_SECRET", observed["env"])
        self.assertIn("--ignore-gitleaks-allow", observed["args"])

    def test_second_scan_failure_never_returns_input(self):
        with patch("experiments.secure_context_hybrid_poc.scan", side_effect=[(True, False), (False, False)]), \
             patch("experiments.secure_context_hybrid_poc.analyze", return_value=(Decision.TRANSFORM, "masked")):
            self.assertEqual(analyze_with_gitleaks("sensitive", "gitleaks"), (Decision.BLOCK, None))

    def test_secret_short_circuits_other_processing(self):
        with patch("experiments.secure_context_hybrid_poc.scan", return_value=(True, True)), \
             patch("experiments.secure_context_hybrid_poc.analyze") as local:
            self.assertEqual(analyze_with_gitleaks("sensitive", "gitleaks"), (Decision.BLOCK, None))
            local.assert_not_called()

    def test_unchanged_allow_uses_one_scan_but_unexpected_change_blocks(self):
        with patch("experiments.secure_context_hybrid_poc.scan", return_value=(True, False)) as detector, \
             patch("experiments.secure_context_hybrid_poc.analyze", return_value=(Decision.ALLOW, "clean")):
            self.assertEqual(analyze_with_gitleaks("clean", "gitleaks"), (Decision.ALLOW, "clean"))
            self.assertEqual(detector.call_count, 1)
        with patch("experiments.secure_context_hybrid_poc.scan", return_value=(True, False)) as detector, \
             patch("experiments.secure_context_hybrid_poc.analyze", return_value=(Decision.ALLOW, "changed")):
            self.assertEqual(analyze_with_gitleaks("clean", "gitleaks"), (Decision.BLOCK, None))
            self.assertEqual(detector.call_count, 1)

    def test_audit_only_exposes_fixed_metadata(self):
        with patch("experiments.secure_context_audit_probe.analyze_guarded", return_value=(Decision.TRANSFORM, "masked secret")):
            event = evaluate("synthetic sensitive input", "gitleaks")
        self.assertEqual(set(event), {"decision", "network_attempted", "scope", "policy_version", "safety_verified"})
        self.assertEqual(event["decision"], "TRANSFORM")
        self.assertIs(event["safety_verified"], False)
        self.assertNotIn("sensitive", str(event))
        self.assertNotIn("masked secret", str(event))

    def test_unknown_sources_block_before_detectors(self):
        with patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks") as inner:
            self.assertEqual(analyze_guarded("clean", "gitleaks"), (Decision.BLOCK, None))
            self.assertEqual(analyze_guarded("private", "gitleaks", restricted=True), (Decision.LOCAL_ONLY, None))
            inner.assert_not_called()

    def test_explicitly_authorized_source_still_requires_detectors(self):
        with patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks", return_value=(Decision.BLOCK, None)) as inner:
            self.assertEqual(analyze_guarded("clean", "gitleaks", source_authorized=True), (Decision.BLOCK, None))
            inner.assert_called_once_with("clean", "gitleaks")

    def test_restricted_terms_block_without_exposing_match(self):
        with patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks") as inner:
            self.assertEqual(analyze_guarded("Internal PROJECT-ALPHA service", "gitleaks",
                                             source_authorized=True, restricted_terms=("project-alpha",)),
                             (Decision.BLOCK, None))
            inner.assert_not_called()

    def test_invalid_term_policy_blocks(self):
        for terms in (("",), ("   ",), (None,), ["valid"]):
            with self.subTest(terms=terms), patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks") as inner:
                self.assertEqual(analyze_guarded("clean", "gitleaks", source_authorized=True,
                                                 restricted_terms=terms), (Decision.BLOCK, None))
                inner.assert_not_called()

    def test_nonmatching_term_still_runs_detectors(self):
        with patch("experiments.secure_context_hybrid_poc.analyze_with_gitleaks", return_value=(Decision.ALLOW, "public example")) as inner:
            self.assertEqual(analyze_guarded("public example", "gitleaks", source_authorized=True,
                                             restricted_terms=("private-service",)),
                             (Decision.ALLOW, "public example"))
            inner.assert_called_once()

    def test_fake_hybrid_route_blocks_unknown_and_malformed_output(self):
        sent = []
        self.assertEqual(route("clean", "/unused", sent), Decision.BLOCK)
        with patch("experiments.secure_context_hybrid_route_probe.analyze_guarded", return_value=(Decision.ALLOW, None)):
            self.assertEqual(route("clean", "/unused", sent, source_authorized=True), Decision.BLOCK)
        self.assertEqual(sent, [])

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_real_gitleaks_and_presidio(self):
        binary = os.environ["SECURE_CONTEXT_GITLEAKS"]
        self.assertEqual(analyze_with_gitleaks("sum two numbers", binary)[0], Decision.ALLOW)
        self.assertEqual(analyze_with_gitleaks("TOKEN=ghp_" + "A1b2C3d4" * 5, binary), (Decision.BLOCK, None))
        self.assertEqual(scan("TOKEN=ghp_" + "A1b2C3d4" * 5 + " # gitleaks:allow", binary), (True, True))
        decision, output = analyze_with_gitleaks("email irina.smirnova@example.com", binary)
        self.assertEqual(decision, Decision.TRANSFORM)
        self.assertNotIn("irina.smirnova@example.com", output)
        sent = []
        self.assertEqual(route("TOKEN=ghp_" + "A1b2C3d4" * 5, binary, sent, source_authorized=True), Decision.BLOCK)
        self.assertEqual(route("email irina.smirnova@example.com", binary, sent, source_authorized=True), Decision.TRANSFORM)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("irina.smirnova@example.com", sent[0])


if __name__ == "__main__":
    unittest.main()
