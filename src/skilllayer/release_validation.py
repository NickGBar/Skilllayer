from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .security_check import run_security_check


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOC_REQUIRED_FILES = [
    "README.md",
    "INSTALL.md",
    "TESTER_GUIDE.md",
    "SECURITY_REVIEW.md",
]

HYGIENE_GENERATED_PATTERNS = (
    "runs/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "dist/",
    "build/",
)

PUBLIC_EXPORT_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "runs",
    "node_modules",
    "dist",
    "build",
}

LOCAL_REFERENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "mac_user_home": re.compile(r"/Users/[A-Za-z0-9._-]+"),
    "linux_user_home": re.compile(r"/home/[A-Za-z0-9._-]+"),
    "windows_user_home": re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", re.IGNORECASE),
    "local_machine_name": re.compile(r"\b(?:MacBook|DESKTOP-[A-Za-z0-9-]+|LAPTOP-[A-Za-z0-9-]+)\b", re.IGNORECASE),
}


def run_release_check(repo_path: str | Path | None = None) -> dict[str, Any]:
    """Run read-only public export readiness checks.

    This function intentionally avoids writing report artifacts. The validation module writes milestone outputs separately after it verifies that
    the release-check command itself is non-mutating.
    """

    started = time.perf_counter()
    root = Path(repo_path or PROJECT_ROOT).resolve()
    code_validation = run_code_validation(root)
    environment_validation = run_environment_validation(root)
    documentation_validation = run_documentation_validation(root)
    repository_hygiene = run_repository_hygiene(root)
    public_export_validation = run_public_export_validation(root)

    errors: list[str] = []
    warnings: list[str] = []
    collect_category_messages("code_validation", code_validation, errors, warnings)
    collect_category_messages("environment_validation", environment_validation, errors, warnings)
    collect_category_messages("documentation_validation", documentation_validation, errors, warnings)
    collect_category_messages("repository_hygiene", repository_hygiene, errors, warnings)
    collect_category_messages("public_export_validation", public_export_validation, errors, warnings)

    ready = not errors
    return {
        "ready_for_public_export": ready,
        "repo_path": str(root),
        "duration_ms": elapsed_ms(started),
        "code_validation": code_validation,
        "environment_validation": environment_validation,
        "documentation_validation": documentation_validation,
        "repository_hygiene": repository_hygiene,
        "public_export_validation": public_export_validation,
        "warnings": warnings,
        "errors": errors,
        "read_only": True,
        "push_performed": False,
        "commit_performed": False,
        "release_created": False,
    }


def run_code_validation(root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    source_files = sorted((root / "src" / "skilllayer").rglob("*.py")) + sorted((root / "skilllayer").glob("*.py"))
    compile_failures: list[dict[str, Any]] = []
    for path in source_files:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            compile_failures.append(
                {
                    "file": relative_path(path, root),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    import_validation = run_import_validation(root)
    ok = not compile_failures and import_validation["ok"]
    return {
        "ok": ok,
        "status": status_label(ok),
        "compileall": {
            "ok": not compile_failures,
            "method": "read_only_source_compile",
            "source_file_count": len(source_files),
            "failure_count": len(compile_failures),
            "failures": compile_failures[:20],
        },
        "import_validation": import_validation,
        "duration_ms": elapsed_ms(started),
        "errors": build_errors("code_validation", compile_failures, import_validation),
        "warnings": [],
    }


def run_import_validation(root: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pythonpath = env.get("PYTHONPATH")
    src_path = str(root / "src")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    command = [
        sys.executable,
        "-B",
        "-c",
        "from skilllayer import SkillLayer; import skilllayer.cli; print('import-ok')",
    ]
    return run_command(command, cwd=root, env=env, timeout=30)


def run_environment_validation(root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    tester_check = run_read_only_tester_probe(root)
    doctor = run_read_only_doctor_probe(root)
    security = run_security_check(write_outputs=False)
    security_check = {
        "ok": bool(security.get("success")),
        "summary": security.get("summary", {}),
        "network_upload_enabled": security.get("security_check", {}).get("network_upload_enabled"),
        "automatic_telemetry_upload": security.get("security_check", {}).get("automatic_telemetry_upload"),
        "mcp_server_auto_start": security.get("security_check", {}).get("mcp_server_auto_start"),
    }
    ok = tester_check["ok"] and doctor["ok"] and security_check["ok"]
    warnings = []
    warnings.extend(tester_check.get("warnings", []))
    warnings.extend(doctor.get("warnings", []))
    return {
        "ok": ok,
        "status": status_label(ok),
        "tester_check": tester_check,
        "doctor": doctor,
        "security_check": security_check,
        "duration_ms": elapsed_ms(started),
        "errors": collect_probe_errors("environment_validation", {"tester-check": tester_check, "doctor": doctor, "security-check": security_check}),
        "warnings": warnings,
    }


def run_read_only_tester_probe(root: Path) -> dict[str, Any]:
    required_files = [
        "TESTER_GUIDE.md",
        "FEEDBACK_TEMPLATE.md",
        "docs/TELEMETRY_PRIVACY.md",
        "docs/TESTER_CHECKLIST.md",
    ]
    missing = [path for path in required_files if not (root / path).exists()]
    import_check = run_import_validation(root)
    ok = not missing and import_check["ok"]
    return {
        "ok": ok,
        "method": "read_only_tester_probe",
        "package_import_ok": import_check["ok"],
        "tester_files_exist": not missing,
        "missing_files": missing,
        "telemetry_export_not_run": True,
        "writes_artifacts": False,
        "warnings": [],
        "errors": [] if ok else ["tester-check probe failed"],
    }


def run_read_only_doctor_probe(root: Path) -> dict[str, Any]:
    config_ok = True
    config_error = None
    try:
        load_config(None)
    except Exception as exc:
        config_ok = False
        config_error = str(exc)
    checks = {
        "python_version": {
            "ok": sys.version_info >= (3, 10),
            "value": ".".join(str(part) for part in sys.version_info[:3]),
            "required": True,
        },
        "repo_path_exists": {"ok": root.exists(), "value": str(root), "required": True},
        "config_valid": {"ok": config_ok, "value": "default", "error": config_error, "required": True},
        "pytest_available": {
            "ok": module_available("pytest") or bool(shutil_which("pytest")),
            "value": bool(module_available("pytest") or shutil_which("pytest")),
            "required": False,
        },
        "mcp_sdk_available": {"ok": module_available("mcp"), "value": module_available("mcp"), "required": False},
    }
    warnings = []
    if not checks["pytest_available"]["ok"]:
        warnings.append("pytest is optional unless running pytest-based smoke tests.")
    if not checks["mcp_sdk_available"]["ok"]:
        warnings.append("MCP SDK is optional for CLI-only release validation.")
    required_ok = all(check["ok"] for check in checks.values() if check.get("required"))
    return {
        "ok": required_ok,
        "method": "read_only_doctor_probe",
        "checks": checks,
        "warnings": warnings,
        "errors": [] if required_ok else ["required doctor probe check failed"],
        "writes_artifacts": False,
    }


def run_documentation_validation(root: Path) -> dict[str, Any]:
    required = {
        path: {
            "exists": (root / path).exists(),
            "path": path,
        }
        for path in DOC_REQUIRED_FILES
    }
    missing = [path for path, item in required.items() if not item["exists"]]
    ok = not missing
    return {
        "ok": ok,
        "status": status_label(ok),
        "required_files": required,
        "missing_files": missing,
        "errors": [f"missing required doc: {path}" for path in missing],
        "warnings": [],
    }


def run_repository_hygiene(root: Path) -> dict[str, Any]:
    tracked = git_tracked_files(root)
    git_available = tracked is not None
    tracked_paths = tracked or []
    tracked_issues = {
        ".venv_tracked": [path for path in tracked_paths if path == ".venv" or path.startswith(".venv/")],
        "pycache_tracked": [path for path in tracked_paths if "__pycache__/" in path or path.endswith(".pyc") or path.endswith(".pyo")],
        "ds_store_tracked": [path for path in tracked_paths if path.endswith(".DS_Store")],
        "runs_tracked": [path for path in tracked_paths if path == "runs" or path.startswith("runs/")],
        "generated_artifacts_tracked": [path for path in tracked_paths if is_generated_artifact_path(path)],
    }
    issue_count = sum(len(paths) for paths in tracked_issues.values())
    ok = git_available and issue_count == 0
    warnings = [] if git_available else ["git metadata is unavailable; tracked-file hygiene could not be fully verified."]
    return {
        "ok": ok,
        "status": status_label(ok),
        "git_available": git_available,
        "tracked_file_count": len(tracked_paths),
        "tracked_issues": tracked_issues,
        "issue_count": issue_count,
        "errors": flatten_tracked_issue_errors(tracked_issues),
        "warnings": warnings,
    }


def run_public_export_validation(root: Path) -> dict[str, Any]:
    scanned_files = public_scan_files(root)
    findings: list[dict[str, Any]] = []
    for path in scanned_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if is_detector_definition_line(path, line):
                continue
            for pattern_type, pattern in LOCAL_REFERENCE_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        {
                            "file_path": relative_path(path, root),
                            "line_number": line_number,
                            "pattern_type": pattern_type,
                            "redacted_excerpt": redact_line(line),
                        }
                    )
    ok = not findings
    return {
        "ok": ok,
        "status": status_label(ok),
        "scanned_file_count": len(scanned_files),
        "finding_count": len(findings),
        "findings": findings[:50],
        "patterns_checked": sorted(LOCAL_REFERENCE_PATTERNS),
        "errors": [f"local reference detected in {item['file_path']}:{item['line_number']}" for item in findings[:20]],
        "warnings": [],
    }


def public_scan_files(root: Path) -> list[Path]:
    allowed_suffixes = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".sh", ".ps1", ".txt"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in PUBLIC_EXPORT_EXCLUDE_DIRS for part in relative_parts):
            continue
        if path.suffix.lower() in allowed_suffixes or path.name in {"LICENSE", ".gitignore"}:
            files.append(path)
    return sorted(files)


def is_detector_definition_line(path: Path, line: str) -> bool:
    if path.name != "release_validation.py" or "re.compile" not in line:
        return False
    return "_home" in line or "local_machine_name" in line


def git_tracked_files(root: Path) -> list[str] | None:
    result = run_command(["git", "ls-files"], cwd=root, timeout=15)
    if not result["ok"]:
        return None
    return [line.strip() for line in str(result["stdout"]).splitlines() if line.strip()]


def run_command(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": completed.returncode == 0,
            "command": redact_command(command),
            "returncode": completed.returncode,
            "stdout": preview(completed.stdout),
            "stderr": preview(completed.stderr),
            "duration_ms": elapsed_ms(started),
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": redact_command(command),
            "returncode": None,
            "stdout": preview(exc.stdout or ""),
            "stderr": preview(exc.stderr or ""),
            "duration_ms": elapsed_ms(started),
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": redact_command(command),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": elapsed_ms(started),
            "error": str(exc),
        }


def is_generated_artifact_path(path: str) -> bool:
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in HYGIENE_GENERATED_PATTERNS):
        return True
    if path.endswith((".pyc", ".pyo", ".pt", ".pth", ".ckpt", ".onnx")):
        return True
    if ".egg-info/" in path or path.endswith(".egg-info"):
        return True
    return False


def flatten_tracked_issue_errors(issues: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    for issue, paths in issues.items():
        for path in paths[:20]:
            errors.append(f"{issue}: {path}")
    return errors


def build_errors(prefix: str, compile_failures: list[dict[str, Any]], import_validation: dict[str, Any]) -> list[str]:
    errors = [f"{prefix}.compileall failed for {item['file']}: {item['error']}" for item in compile_failures[:20]]
    if not import_validation["ok"]:
        errors.append(f"{prefix}.import_validation failed: {import_validation.get('stderr') or import_validation.get('error')}")
    return errors


def collect_probe_errors(prefix: str, probes: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for name, probe in probes.items():
        if probe.get("ok"):
            continue
        probe_errors = probe.get("errors") or [probe.get("error") or "failed"]
        for error in probe_errors:
            errors.append(f"{prefix}.{name}: {error}")
    return errors


def collect_category_messages(category: str, result: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    if not result.get("ok", False):
        category_errors = result.get("errors") or [f"{category} failed"]
        errors.extend(str(error) for error in category_errors)
    warnings.extend(str(warning) for warning in result.get("warnings", []) or [])


def redact_line(line: str) -> str:
    redacted = line
    redacted = re.sub(r"/Users/[A-Za-z0-9._-]+", "/Users/[REDACTED]", redacted)
    redacted = re.sub(r"/home/[A-Za-z0-9._-]+", "/home/[REDACTED]", redacted)
    redacted = re.sub(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", lambda _: r"C:\Users\[REDACTED]", redacted)
    return redacted.strip()[:240]


def redact_command(command: list[str]) -> list[str]:
    redacted = list(command)
    if redacted and redacted[0] == sys.executable:
        redacted[0] = "python"
    return redacted


def preview(value: str, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def status_label(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def main() -> int:
    result = run_release_check()
    print(json.dumps(result, indent=2))
    return 0 if result["ready_for_public_export"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
