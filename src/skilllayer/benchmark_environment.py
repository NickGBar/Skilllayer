from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ENVIRONMENT_OK = "ENVIRONMENT_OK"
ENVIRONMENT_MISSING_DEPENDENCY = "ENVIRONMENT_MISSING_DEPENDENCY"
ENVIRONMENT_UNSUPPORTED = "ENVIRONMENT_UNSUPPORTED"
ENVIRONMENT_STATUSES = (
    ENVIRONMENT_OK,
    ENVIRONMENT_MISSING_DEPENDENCY,
    ENVIRONMENT_UNSUPPORTED,
)
DEPENDENCIES = ("python", "pytest", "node", "npm", "git")


def probe_benchmark_environment() -> dict[str, Any]:
    tools = {
        "python": probe_python(),
        "pytest": probe_command("pytest", [sys.executable, "-m", "pytest", "--version"]),
        "node": probe_command("node", ["node", "--version"]),
        "npm": probe_command("npm", ["npm", "--version"]),
        "git": probe_command("git", ["git", "--version"]),
    }
    missing = [name for name, info in tools.items() if not info["available"]]
    return {
        "tools": tools,
        "available": [name for name, info in tools.items() if info["available"]],
        "missing": missing,
        "environment_status": ENVIRONMENT_OK if not missing else ENVIRONMENT_MISSING_DEPENDENCY,
        "network_access": "not used",
        "no_install_attempted": True,
    }


def probe_python() -> dict[str, Any]:
    return {
        "available": True,
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "executable": sys.executable,
        "command": [sys.executable, "--version"],
        "error": None,
    }


def probe_command(name: str, command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0]) if command else None
    if command and command[0] == sys.executable:
        executable = sys.executable
    if not executable:
        return {
            "available": False,
            "version": None,
            "executable": None,
            "command": command,
            "error": f"{name} executable not found",
        }
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=8, check=False)
    except Exception as exc:  # noqa: BLE001 - environment probing is best effort.
        return {
            "available": False,
            "version": None,
            "executable": executable,
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return {
        "available": completed.returncode == 0,
        "version": output[0] if output else None,
        "executable": executable,
        "command": command,
        "returncode": completed.returncode,
        "error": None if completed.returncode == 0 else (completed.stderr or completed.stdout).strip()[:500],
    }


def annotate_scenario_environment(
    scenario_name: str,
    workflow_name: str,
    repo: Path,
    result: dict[str, Any],
    environment_probe: dict[str, Any],
) -> dict[str, Any]:
    requirements = infer_environment_requirements(scenario_name, workflow_name, repo, result)
    missing = [
        dependency
        for dependency in requirements
        if not environment_probe.get("tools", {}).get(dependency, {}).get("available", False)
    ]
    unsupported = has_unsupported_environment_result(result)
    if unsupported:
        environment_status = ENVIRONMENT_UNSUPPORTED
    elif missing:
        environment_status = ENVIRONMENT_MISSING_DEPENDENCY
    else:
        environment_status = ENVIRONMENT_OK
    attribution = classify_issue_attribution(environment_status, result)
    return {
        "environment_status": environment_status,
        "environment_requirements": requirements,
        "missing_dependencies": missing,
        "environment_issue_attribution": attribution,
    }


def infer_environment_requirements(
    scenario_name: str,
    workflow_name: str,
    repo: Path,
    result: dict[str, Any],
) -> list[str]:
    if workflow_name == "GitStatusWorkflow" or scenario_name == "Git Status Summary":
        return ["git"]
    if workflow_name in {"RunTestsWorkflow", "ExplainFailureWorkflow"} or scenario_name in {"Run Tests", "Explain Failure"}:
        return infer_test_environment_requirements(repo, result)
    return ["python"]


def infer_test_environment_requirements(repo: Path, result: dict[str, Any]) -> list[str]:
    artifacts = result.get("artifacts") or {}
    error_code = str(result.get("error_code") or artifacts.get("error_code") or "").lower()
    command_items = artifacts.get("command_argv") or []
    test_command = str(artifacts.get("test_command") or "")
    command_blob = " ".join(str(item) for item in command_items)
    command_blob = f"{command_blob} {test_command}".lower()
    requirements: set[str] = {"python"}
    if error_code == "no_test_command_detected":
        return sorted(requirements)
    if "npm" in command_blob:
        requirements.update({"node", "npm"})
    if "node" in command_blob and "npm" not in command_blob:
        requirements.add("node")
    if "pytest" in command_blob:
        requirements.add("pytest")

    if not command_blob.strip():
        if (repo / "package.json").exists():
            requirements.update({"node", "npm"})
        if looks_like_python_test_repo(repo):
            requirements.add("pytest")
    return sorted(requirements)


def looks_like_python_test_repo(repo: Path) -> bool:
    if (repo / "pytest.ini").exists() or (repo / "tox.ini").exists():
        return True
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        return True
    tests_dir = repo / "tests"
    if tests_dir.exists() and any(tests_dir.rglob("test*.py")):
        return True
    return any(repo.glob("test*.py"))


def has_unsupported_environment_result(result: dict[str, Any]) -> bool:
    artifacts = result.get("artifacts") or {}
    error_code = str(result.get("error_code") or artifacts.get("error_code") or "").lower()
    return error_code in {"unsupported_environment", "unsupported_test_runner", "single_test_not_supported"}


def classify_issue_attribution(environment_status: str, result: dict[str, Any]) -> str:
    artifacts = result.get("artifacts") or {}
    error_code = str(result.get("error_code") or artifacts.get("error_code") or "").lower()
    benchmark_outcome = str(result.get("benchmark_outcome") or "").upper()
    if environment_status in {ENVIRONMENT_MISSING_DEPENDENCY, ENVIRONMENT_UNSUPPORTED}:
        return "environment_issue"
    if error_code in {"no_test_command_detected", "no_tests_discovered"} or benchmark_outcome in {
        "NO_TEST_COMMAND",
        "NO_TESTS_DISCOVERED",
    }:
        return "repository_issue"
    if result.get("counts_as_benchmark_failure") or benchmark_outcome in {"COMMAND_ERROR", "UNKNOWN"}:
        return "workflow_issue"
    return "none"


def summarize_environment_status(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {status: 0 for status in ENVIRONMENT_STATUSES}
    dependency_distribution = {dependency: 0 for dependency in DEPENDENCIES}
    attribution_counts = {
        "none": 0,
        "environment_issue": 0,
        "repository_issue": 0,
        "workflow_issue": 0,
    }
    for scenario in scenarios:
        status = scenario.get("environment_status") or ENVIRONMENT_OK
        if status in status_counts:
            status_counts[status] += 1
        for dependency in scenario.get("missing_dependencies") or []:
            if dependency in dependency_distribution:
                dependency_distribution[dependency] += 1
        attribution = scenario.get("environment_issue_attribution") or "none"
        if attribution in attribution_counts:
            attribution_counts[attribution] += 1
    total = len(scenarios)
    blocked = status_counts[ENVIRONMENT_MISSING_DEPENDENCY] + status_counts[ENVIRONMENT_UNSUPPORTED]
    ready = total - blocked
    return {
        "environment_ready_scenarios": ready,
        "environment_blocked_scenarios": blocked,
        "environment_status_distribution": status_counts,
        "environment_dependency_distribution": dependency_distribution,
        "environment_issue_attribution_distribution": attribution_counts,
        "environment_readiness_score": round(ready / total, 3) if total else 0.0,
    }


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)
