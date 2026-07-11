from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


OUTPUT_DIR = Path("runs/p8_1_1_run_tests_real_repo_smoke")


def run_smoke(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    repos_root = output_dir / "repos"
    if repos_root.exists():
        shutil.rmtree(repos_root)
    repos_root.mkdir(parents=True)

    repo_specs = build_repo_specs(repos_root)
    repo_matrix: list[dict[str, Any]] = []
    smoke_results: list[dict[str, Any]] = []
    artifact_quality: list[dict[str, Any]] = []
    read_only: list[dict[str, Any]] = []
    human_samples: list[str] = []

    for spec in repo_specs:
        repo_matrix.append(
            {
                "repo_id": spec["repo_id"],
                "kind": spec["kind"],
                "path": str(spec["path"]),
                "expected": spec["expected"],
                "skipped": bool(spec.get("skipped", False)),
                "skip_reason": spec.get("skip_reason"),
            }
        )
        if spec.get("skipped"):
            smoke_results.append(
                {
                    "repo_id": spec["repo_id"],
                    "skipped": True,
                    "skip_reason": spec.get("skip_reason"),
                }
            )
            continue

        before = snapshot_repo_files(spec["path"])
        completed = run_skilllayer_json(spec["path"])
        after = snapshot_repo_files(spec["path"])
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {"error": completed.stderr.strip()}
        extracted = extract_result_fields(payload)
        extracted.update(
            {
                "repo_id": spec["repo_id"],
                "kind": spec["kind"],
                "returncode": completed.returncode,
                "skipped": False,
            }
        )
        smoke_results.append(extracted)

        read_only.append(
            {
                "repo_id": spec["repo_id"],
                "read_only": before == after,
                "changed_files": sorted(set(before) ^ set(after))
                + sorted(path for path in before if path in after and before[path] != after[path]),
            }
        )

        artifact_quality.append(check_artifact_quality(spec, payload))

        if spec["repo_id"] in {"python_pytest_failing", "node_npm_failing"}:
            human = run_skilllayer_human(spec["path"])
            human_samples.append(
                "\n".join(
                    [
                        f"### {spec['repo_id']}",
                        f"returncode={human.returncode}",
                        human.stdout.strip(),
                        human.stderr.strip(),
                    ]
                ).strip()
            )

    read_only_report = {
        "all_read_only": all(item["read_only"] for item in read_only),
        "repos": read_only,
    }
    artifact_quality_report = {
        "all_passed": all(item["passed"] for item in artifact_quality),
        "checks": artifact_quality,
    }
    report = {
        "milestone": "RunTestsWorkflow Real Repo Smoke",
        "success": bool(read_only_report["all_read_only"] and artifact_quality_report["all_passed"]),
        "summary": {
            "repo_count": len(repo_matrix),
            "executed_repo_count": sum(1 for item in smoke_results if not item.get("skipped")),
            "skipped_repo_count": sum(1 for item in smoke_results if item.get("skipped")),
            "node_available": node_available(),
            "npm_available": npm_available(),
            "pytest_passing_ok": result_ok(artifact_quality, "python_pytest_passing"),
            "pytest_failing_ok": result_ok(artifact_quality, "python_pytest_failing"),
            "no_test_repo_ok": result_ok(artifact_quality, "python_no_tests"),
            "node_passing_ok": result_ok(artifact_quality, "node_npm_passing") if npm_available() else None,
            "node_failing_ok": result_ok(artifact_quality, "node_npm_failing") if npm_available() else None,
            "read_only_confirmed": read_only_report["all_read_only"],
            "human_output_smoke_passed": validate_human_samples(human_samples),
        },
        "limitations": build_limitations(),
        "outputs": {
            "repo_matrix": str(output_dir / "repo_matrix.json"),
            "smoke_results": str(output_dir / "smoke_results.json"),
            "artifact_quality_report": str(output_dir / "artifact_quality_report.json"),
            "read_only_report": str(output_dir / "read_only_report.json"),
            "human_output_samples": str(output_dir / "human_output_samples.txt"),
            "report": str(output_dir / "report.json"),
        },
    }
    report["success"] = bool(report["success"] and report["summary"]["human_output_smoke_passed"])

    write_json(output_dir / "repo_matrix.json", {"repositories": repo_matrix})
    write_json(output_dir / "smoke_results.json", {"results": smoke_results})
    write_json(output_dir / "artifact_quality_report.json", artifact_quality_report)
    write_json(output_dir / "read_only_report.json", read_only_report)
    (output_dir / "human_output_samples.txt").write_text("\n\n".join(human_samples) + "\n", encoding="utf-8")
    write_json(output_dir / "report.json", report)
    return report


def build_repo_specs(root: Path) -> list[dict[str, Any]]:
    specs = [
        {
            "repo_id": "python_pytest_passing",
            "kind": "python_pytest",
            "path": create_pytest_repo(root / "python_pytest_passing", failing=False),
            "expected": "passed",
        },
        {
            "repo_id": "python_pytest_failing",
            "kind": "python_pytest",
            "path": create_pytest_repo(root / "python_pytest_failing", failing=True),
            "expected": "failed",
        },
        {
            "repo_id": "python_no_tests",
            "kind": "python_no_tests",
            "path": create_no_tests_repo(root / "python_no_tests"),
            "expected": "not_applicable",
        },
    ]
    if npm_available():
        specs.extend(
            [
                {
                    "repo_id": "node_npm_passing",
                    "kind": "node_npm",
                    "path": create_node_repo(root / "node_npm_passing", failing=False),
                    "expected": "passed",
                },
                {
                    "repo_id": "node_npm_failing",
                    "kind": "node_npm",
                    "path": create_node_repo(root / "node_npm_failing", failing=True),
                    "expected": "failed",
                },
            ]
        )
    else:
        specs.extend(
            [
                {
                    "repo_id": "node_npm_passing",
                    "kind": "node_npm",
                    "path": str(root / "node_npm_passing"),
                    "expected": "passed",
                    "skipped": True,
                    "skip_reason": "npm_not_available",
                },
                {
                    "repo_id": "node_npm_failing",
                    "kind": "node_npm",
                    "path": str(root / "node_npm_failing"),
                    "expected": "failed",
                    "skipped": True,
                    "skip_reason": "npm_not_available",
                },
            ]
        )
    return specs


def create_pytest_repo(path: Path, *, failing: bool) -> Path:
    path.mkdir(parents=True)
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_calc.py").write_text(
        "\n".join(
            [
                "from calc import add",
                "",
                "",
                "def test_add():",
                "    assert add(1, 1) == " + ("3" if failing else "2"),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def create_no_tests_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return path


def create_node_repo(path: Path, *, failing: bool) -> Path:
    path.mkdir(parents=True)
    (path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node test.js"}}, indent=2),
        encoding="utf-8",
    )
    (path / "calc.js").write_text("exports.add = (a, b) => a + b;\n", encoding="utf-8")
    expected = "3" if failing else "2"
    (path / "test.js").write_text(
        "\n".join(
            [
                "const assert = require('assert');",
                "const { add } = require('./calc');",
                f"assert.strictEqual(add(1, 1), {expected});",
                "console.log('node test complete');",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def run_skilllayer_json(repo: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(["run", "--repo", str(repo), "--task", "Run tests", "--json"])


def run_skilllayer_human(repo: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(["run", "--repo", str(repo), "--task", "Run tests"])


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path + (os.pathsep + existing if existing else "")
    return subprocess.run([preferred_python(), "-m", "skilllayer", *args], text=True, capture_output=True, check=False, env=env)


def preferred_python() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def extract_result_fields(payload: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "workflow",
        "success",
        "tests_run",
        "tests_passed",
        "validation_status",
        "test_command",
        "exit_code",
        "duration_ms",
        "failure_count",
        "failed_tests",
        "stdout_snippet",
        "stderr_snippet",
        "error_code",
    ]
    return {key: payload.get(key) for key in keys}


def check_artifact_quality(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    expected = spec["expected"]
    checks = {
        "workflow_is_run_tests": payload.get("workflow") == "RunTestsWorkflow",
        "has_artifact_keys": all(
            key in payload
            for key in [
                "test_command",
                "tests_run",
                "tests_passed",
                "validation_status",
                "exit_code",
                "duration_ms",
                "failure_count",
                "failed_tests",
                "stdout_snippet",
                "stderr_snippet",
            ]
        ),
    }
    if expected == "passed":
        checks.update(
            {
                "tests_run_true": payload.get("tests_run") is True,
                "tests_passed_true": payload.get("tests_passed") is True,
                "validation_passed": payload.get("validation_status") == "passed",
                "failure_count_zero": int(payload.get("failure_count", -1) or 0) == 0,
            }
        )
    elif expected == "failed":
        checks.update(
            {
                "tests_run_true": payload.get("tests_run") is True,
                "tests_passed_false": payload.get("tests_passed") is False,
                "validation_failed": payload.get("validation_status") == "failed",
                "failure_count_present": int(payload.get("failure_count", 0) or 0) >= 1,
                "failed_tests_present_if_parseable": bool(payload.get("failed_tests")) or spec["kind"] == "node_npm",
            }
        )
    else:
        checks.update(
            {
                "tests_run_false": payload.get("tests_run") is False,
                "tests_passed_null": payload.get("tests_passed") is None,
                "validation_not_applicable": payload.get("validation_status") == "not_applicable",
                "error_code_no_test_command": payload.get("error_code") == "no_test_command_detected",
            }
        )
    return {
        "repo_id": spec["repo_id"],
        "kind": spec["kind"],
        "expected": expected,
        "passed": all(checks.values()),
        "checks": checks,
    }


def validate_human_samples(samples: list[str]) -> bool:
    if not samples:
        return False
    text = "\n".join(samples)
    return "test_command:" in text and "validation:" in text and "failure_count:" in text


def snapshot_repo_files(repo: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(item for item in repo.rglob("*") if item.is_file()):
        if should_ignore_generated(path):
            continue
        relative = str(path.relative_to(repo))
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def should_ignore_generated(path: Path) -> bool:
    return any(part in {"__pycache__", ".pytest_cache", "node_modules"} for part in path.parts) or path.suffix == ".pyc"


def node_available() -> bool:
    return shutil.which("node") is not None


def npm_available() -> bool:
    return shutil.which("npm") is not None


def result_ok(items: list[dict[str, Any]], repo_id: str) -> bool | None:
    for item in items:
        if item.get("repo_id") == repo_id:
            return bool(item.get("passed"))
    return None


def build_limitations() -> list[str]:
    limitations: list[str] = []
    if not npm_available():
        limitations.append("Node/npm smoke fixtures were skipped because npm is unavailable.")
    limitations.append("Smoke fixtures are local realistic shapes, not external repositories.")
    limitations.append("RunTestsWorkflow remains read-only and does not repair failures.")
    return limitations


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    report = run_smoke()
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
