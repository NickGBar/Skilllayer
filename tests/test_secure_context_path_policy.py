"""Synthetic lexical path decisions, not filesystem enforcement."""
import unittest
import json
import os
from unittest.mock import patch
from experiments.secure_context_path_policy import Decision, decide_path, evaluate_bundle, evaluate_central_bundle, evaluate_central_file, evaluate_file


class PathPolicyTests(unittest.TestCase):
    def test_allow_and_denial_boundaries(self):
        allowed = ("public", "docs/readme.md")
        for path, expected in (("public/a.py", Decision.ALLOW), ("docs/readme.md", Decision.ALLOW),
                               ("publicity/a.py", Decision.BLOCK), ("secret/a.py", Decision.BLOCK)):
            with self.subTest(path=path):
                self.assertEqual(decide_path(path, allowed=allowed, forbidden=()), expected)

    def test_forbidden_takes_priority(self):
        self.assertEqual(decide_path("public/customer/a.py", allowed=("public",),
                                     forbidden=("public/customer",)), Decision.BLOCK)
        self.assertEqual(decide_path("public/customer2/a.py", allowed=("public",),
                                     forbidden=("public/customer",)), Decision.ALLOW)

    def test_invalid_and_escape_paths_are_blocked(self):
        for path in ("", "/etc/passwd", "public/../secret", "public/./file", "public//file",
                     "public\\secret", "public/file\x00name", "C:/private/file", "public/file\nname"):
            with self.subTest(path=path):
                self.assertEqual(decide_path(path, allowed=("public",), forbidden=()), Decision.BLOCK)

    def test_invalid_rules_fail_closed(self):
        for allowed, forbidden in ((["public"], ()), (("",), ()), (("public",), ("../secret",)),
                                   (("public",), (None,))):
            with self.subTest(allowed=allowed, forbidden=forbidden):
                self.assertEqual(decide_path("public/a.py", allowed=allowed, forbidden=forbidden), Decision.BLOCK)

    def test_denied_path_never_runs_content_policy(self):
        with patch("experiments.secure_context_policy_config.evaluate_with_policy") as content:
            self.assertEqual(evaluate_file("sensitive", "binary", path="private/file.py", source="public",
                                           raw_policy="{}", allowed=("public",), forbidden=()), (Decision.BLOCK, None))
            content.assert_not_called()

    def test_allowed_path_still_requires_content_checks(self):
        with patch("experiments.secure_context_policy_config.evaluate_with_policy", return_value=(Decision.BLOCK, None)) as content:
            self.assertEqual(evaluate_file("sensitive", "binary", path="public/file.py", source="public",
                                           raw_policy="{}", allowed=("public",), forbidden=()), (Decision.BLOCK, None))
            content.assert_called_once()

    def test_content_policy_exception_blocks(self):
        with patch("experiments.secure_context_policy_config.evaluate_with_policy", side_effect=RuntimeError("sensitive error")):
            self.assertEqual(evaluate_file("sensitive", "binary", path="public/file.py", source="public",
                                           raw_policy="{}", allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_central_policy_combines_source_and_path_rules(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": [],
                             "allowed_paths": ["public"], "forbidden_paths": ["public/private"]})
        with patch("experiments.secure_context_policy_config.evaluate_with_policy", return_value=(Decision.ALLOW, "ok")) as content:
            self.assertEqual(evaluate_central_file("ok", "binary", path="public/file.py", source="public", raw_policy=policy),
                             (Decision.ALLOW, "ok"))
            self.assertEqual(evaluate_central_file("secret", "binary", path="public/private/file.py", source="public", raw_policy=policy),
                             (Decision.BLOCK, None))
            self.assertEqual(content.call_count, 1)

    def test_central_policy_without_path_rules_blocks(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": []})
        self.assertEqual(evaluate_central_file("ok", "binary", path="public/file.py", source="public", raw_policy=policy),
                         (Decision.BLOCK, None))

    def test_central_bundle_uses_path_policy_atomically(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": [],
                             "allowed_paths": ["public"], "forbidden_paths": ["public/private"]})
        files = (("public/ok.py", "clean"), ("public/private/x.py", "sensitive"))
        with patch("experiments.secure_context_path_policy.evaluate_file") as content:
            self.assertEqual(evaluate_central_bundle(files, "binary", source="public", raw_policy=policy),
                             (Decision.BLOCK, None))
            content.assert_not_called()
        self.assertEqual(evaluate_central_bundle(files, "binary", source="public", raw_policy="bad"), (Decision.BLOCK, None))

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_real_central_bundle_blocks_split_secret(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": [],
                             "allowed_paths": ["public"], "forbidden_paths": []})
        files = (("public/a.py", "TOKEN=ghp_"), ("public/b.py", "A1b2C3d4" * 5))
        self.assertEqual(evaluate_central_bundle(files, os.environ["SECURE_CONTEXT_GITLEAKS"], source="public", raw_policy=policy),
                         (Decision.BLOCK, None))

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_real_central_file_policy_and_detectors(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": ["project-alpha"],
                             "allowed_paths": ["public"], "forbidden_paths": ["public/private"]})
        binary = os.environ["SECURE_CONTEXT_GITLEAKS"]
        result = evaluate_central_file("contact a.person@example.com", binary, path="public/file.py", source="public", raw_policy=policy)
        self.assertEqual(result[0], Decision.TRANSFORM)
        self.assertNotIn("a.person@example.com", result[1])
        self.assertEqual(evaluate_central_file("clean", binary, path="public/private/file.py", source="public", raw_policy=policy),
                         (Decision.BLOCK, None))
        self.assertEqual(evaluate_central_file("PROJECT-ALPHA", binary, path="public/file.py", source="public", raw_policy=policy),
                         (Decision.BLOCK, None))

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_allowed_file_with_secret_is_blocked_by_real_detectors(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": []})
        text = "TOKEN=ghp_" + "A1b2C3d4" * 5
        decision, output = evaluate_file(text, os.environ["SECURE_CONTEXT_GITLEAKS"], path="public/file.py",
                                         source="public", raw_policy=policy, allowed=("public",), forbidden=())
        self.assertEqual((decision, output), (Decision.BLOCK, None))

    def test_bundle_denied_path_never_returns_partial_content(self):
        files = (("public/a.py", "one"), ("private/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file") as evaluator:
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))
            evaluator.assert_not_called()

    def test_bundle_late_failure_discards_earlier_output(self):
        files = (("public/a.py", "one"), ("public/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.ALLOW, "one"), (Decision.BLOCK, None)]):
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_bundle_exception_discards_earlier_output(self):
        files = (("public/a.py", "one"), ("public/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.ALLOW, "one"), RuntimeError("sensitive error")]):
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_bundle_blocks_allow_with_unexpected_modified_content(self):
        with patch("experiments.secure_context_path_policy.evaluate_file", return_value=(Decision.ALLOW, "changed")):
            self.assertEqual(evaluate_bundle((("public/a.py", "original"),), "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_multifile_transform_blocks_until_alias_scope_is_shared(self):
        files = (("public/a.py", "one"), ("public/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.TRANSFORM, "masked"), (Decision.ALLOW, "two")]):
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_bundle_success_and_single_transform(self):
        files = (("public/a.py", "one"), ("public/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.ALLOW, "one"), (Decision.ALLOW, "two")]), \
             patch("experiments.secure_context_policy_config.evaluate_with_policy", return_value=(Decision.ALLOW, "onetwo")):
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.ALLOW, ("one", "two")))
        with patch("experiments.secure_context_path_policy.evaluate_file", return_value=(Decision.TRANSFORM, "masked")):
            self.assertEqual(evaluate_bundle(files[:1], "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.TRANSFORM, ("masked",)))

    def test_bundle_duplicate_or_malformed_input_blocks(self):
        for files in ((), (("public/a.py", "one"), ("public/a.py", "two")), (("public/a.py",),), (("public/a.py", 5),)):
            with self.subTest(files=files):
                self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                                 allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_combined_context_failure_discards_all_outputs(self):
        files = (("public/a.py", "one"), ("public/b.py", "two"))
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.ALLOW, "one"), (Decision.ALLOW, "two")]), \
             patch("experiments.secure_context_policy_config.evaluate_with_policy", return_value=(Decision.BLOCK, None)):
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))

    def test_oversized_bundle_blocks_before_combined_scan(self):
        files = (("public/a.py", "x" * 600_000), ("public/b.py", "y" * 600_000))
        with patch("experiments.secure_context_path_policy.evaluate_file") as each_file, \
             patch("experiments.secure_context_policy_config.evaluate_with_policy") as combined:
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))
            each_file.assert_not_called()
            combined.assert_not_called()

    def test_multibyte_bundle_size_is_counted_in_utf8_bytes(self):
        files = (("public/a.py", "я" * 524_289),)
        with patch("experiments.secure_context_path_policy.evaluate_file") as each_file:
            self.assertEqual(evaluate_bundle(files, "binary", source="public", raw_policy="{}",
                                             allowed=("public",), forbidden=()), (Decision.BLOCK, None))
            each_file.assert_not_called()

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_real_detector_blocks_secret_split_across_files(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": []})
        files = (("public/a.py", "TOKEN=ghp_"), ("public/b.py", "A1b2C3d4" * 5))
        for path, value in files:
            self.assertEqual(evaluate_file(value, os.environ["SECURE_CONTEXT_GITLEAKS"], path=path,
                                           source="public", raw_policy=policy, allowed=("public",), forbidden=())[0], Decision.ALLOW)
        decision, output = evaluate_bundle(files, os.environ["SECURE_CONTEXT_GITLEAKS"], source="public",
                                           raw_policy=policy, allowed=("public",), forbidden=())
        self.assertEqual((decision, output), (Decision.BLOCK, None))

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_combined_context_with_split_email_blocks(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": []})
        files = (("public/a.py", "a.person@"), ("public/b.py", "example.com"))
        decision, output = evaluate_bundle(files, os.environ["SECURE_CONTEXT_GITLEAKS"], source="public",
                                           raw_policy=policy, allowed=("public",), forbidden=())
        self.assertEqual((decision, output), (Decision.BLOCK, None))

    @unittest.skipUnless(os.environ.get("SECURE_CONTEXT_GITLEAKS"), "optional Gitleaks binary not supplied")
    def test_combined_context_with_split_restricted_term_blocks(self):
        policy = json.dumps({"version": "v1", "sources": {"public": "allow"}, "restricted_terms": ["project-alpha"]})
        files = (("public/a.py", "project-"), ("public/b.py", "alpha"))
        decision, output = evaluate_bundle(files, os.environ["SECURE_CONTEXT_GITLEAKS"], source="public",
                                           raw_policy=policy, allowed=("public",), forbidden=())
        self.assertEqual((decision, output), (Decision.BLOCK, None))


if __name__ == "__main__":
    unittest.main()
