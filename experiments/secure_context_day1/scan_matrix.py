"""Reproduce a small synthetic profile of SkillLayer's built-in secret scanner.

This is an experiment driver, not a security test suite. It generates only
synthetic values in a temporary directory and prints aggregate results without
printing any matched content.

Run from the repository root with:
    PYTHONPATH=src python experiments/secure_context_day1/scan_matrix.py
"""

from __future__ import annotations

import tempfile
import argparse
import json
import random
import shutil
import string
import subprocess
from pathlib import Path

from skilllayer.runner.core import build_detect_secrets_artifacts


def _fixtures() -> tuple[dict[str, str], dict[str, str]]:
    rng = random.Random(20261124)

    def token(length: int, alphabet: str = string.ascii_letters + string.digits) -> str:
        return "".join(rng.choice(alphabet) for _ in range(length))

    positive = {
        "anthropic.py": f'TOKEN = "sk-ant-api03-{token(93, string.ascii_letters + string.digits + "_-")}AA"\n',
        "openai.py": f'TOKEN = "sk-{token(48)}"\n',
        "aws_access.py": f'AWS_ACCESS_KEY_ID = "AKIA{token(16, string.ascii_uppercase + "234567")}"\n',
        "aws_secret.py": f'aws_secret_access_key = "{token(40, string.ascii_letters + string.digits + "/+=")}"\n',
        "github.py": f'TOKEN = "ghp_{token(36)}"\n',
        "generic_api.py": f'api_key = "{token(32)}"\n',
        "bearer_curl.py": f'curl -H "Authorization: Bearer {token(32)}" https://api.example.test/\n',
        "bearer_plain.py": f'Authorization: Bearer {token(32)}\n',
        "private_key.py": (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + "\n".join(token(64, string.ascii_letters + string.digits + "/+") for _ in range(8))
            + "\n-----END RSA PRIVATE KEY-----\n"
        ),
    }
    negative = {
        "pii_only.py": (
            "email=irina.smirnova@example.test "
            "phone=+1-202-555-0147\n"
        ),
        "clean.py": (
            "from datetime import datetime\n"
            'EXAMPLE = datetime.strptime("2026-11-25", "%Y-%m-%d")\n'
        ),
    }
    return positive, negative


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitleaks", help="path to a pinned Gitleaks binary")
    args = parser.parse_args()
    positives, negatives = _fixtures()
    with tempfile.TemporaryDirectory(prefix="skilllayer-secctx-") as temp_dir:
        temp_root = Path(temp_dir)
        root = temp_root / "fixtures"
        root.mkdir()
        for name, content in {**positives, **negatives}.items():
            (root / name).write_text(content, encoding="utf-8")

        result = build_detect_secrets_artifacts(root)
        findings_by_file: dict[str, list[dict[str, object]]] = {}
        for finding in result.get("findings", []):
            findings_by_file.setdefault(str(finding["file"]), []).append(finding)

        missed = [name for name in positives if name not in findings_by_file]
        negative_hits = [name for name in negatives if name in findings_by_file]
        expected_positive_files = len(positives)
        observed_positive_files = expected_positive_files - len(missed)

        print(f"workflow={result.get('workflow')}")
        print(f"scan_complete={result.get('scan_complete')}")
        print(f"files_scanned={result.get('scanned_files')}")
        print(f"positive_files_detected={observed_positive_files}/{expected_positive_files}")
        print(f"negative_files_flagged={len(negative_hits)}/{len(negatives)}")
        print(f"total_findings={result.get('findings_count')}")
        core_rules = sorted({str(finding.get("pattern_name", "unknown")) for finding in result.get("findings", [])})
        print(f"core_rule_ids={','.join(core_rules) or 'none'}")
        core_file_rules = [
            f"{name}:{','.join(sorted({str(item.get('pattern_name', 'unknown')) for item in findings_by_file.get(name, [])})) or 'none'}"
            for name in [*positives, *negatives]
        ]
        print(f"core_rules_by_file={';'.join(core_file_rules)}")
        print(f"missed_positive_files={','.join(missed) or 'none'}")
        print(f"flagged_negative_files={','.join(negative_hits) or 'none'}")
        print("fixture_classification=all values are generated synthetic; tool likely_test_fixture is not a reliable label source")

        gitleaks = args.gitleaks or shutil.which("gitleaks")
        if gitleaks:
            report_path = temp_root / "gitleaks-report.json"
            proc = subprocess.run(
                [
                    gitleaks,
                    "dir",
                    "--no-banner",
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report_path),
                    "--redact=100",
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else []
            gitleaks_by_file: dict[str, list[dict[str, object]]] = {}
            for finding in report:
                gitleaks_by_file.setdefault(Path(str(finding.get("File", ""))).name, []).append(finding)
            gitleaks_missed = [name for name in positives if name not in gitleaks_by_file]
            gitleaks_negative_hits = [name for name in negatives if name in gitleaks_by_file]
            gitleaks_detected = [name for name in positives if name in gitleaks_by_file]
            print(f"gitleaks_exit_code={proc.returncode}")
            print(f"gitleaks_positive_files_detected={len(positives) - len(gitleaks_missed)}/{len(positives)}")
            print(f"gitleaks_detected_positive_files={','.join(gitleaks_detected) or 'none'}")
            print(f"gitleaks_negative_files_flagged={len(gitleaks_negative_hits)}/{len(negatives)}")
            print(f"gitleaks_total_findings={len(report)}")
            print(f"gitleaks_missed_positive_files={','.join(gitleaks_missed) or 'none'}")
            print(f"gitleaks_flagged_negative_files={','.join(gitleaks_negative_hits) or 'none'}")
            safe_rules = sorted(
                {
                    str(finding.get("RuleID", "unknown"))
                    for finding in report
                }
            )
            print(f"gitleaks_rule_ids={','.join(safe_rules) or 'none'}")
            file_rules = [
                f"{name}:{','.join(sorted({str(item.get('RuleID', 'unknown')) for item in gitleaks_by_file.get(name, [])})) or 'none'}"
                for name in [*positives, *negatives]
            ]
            print(f"gitleaks_rules_by_file={';'.join(file_rules)}")
            if proc.returncode not in (0, 1) or not report_path.exists():
                return 3
        else:
            print("gitleaks=unavailable; pass --gitleaks PATH to compare")

        if not result.get("success") or not result.get("scan_complete"):
            return 2
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
