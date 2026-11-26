"""Synthetic lexical path decisions, not filesystem enforcement."""
import unittest
import json
import os
from unittest.mock import patch
from experiments.secure_context_path_policy import Decision, decide_path, evaluate_bundle, evaluate_file


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
        with patch("experiments.secure_context_path_policy.evaluate_file", side_effect=[(Decision.ALLOW, "one"), (Decision.ALLOW, "two")]):
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


if __name__ == "__main__":
    unittest.main()
