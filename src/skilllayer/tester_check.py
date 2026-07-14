from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .version import product_version


OUTPUT_DIR = Path("runs/tester_check")


def run_tester_check() -> dict[str, Any]:
    started = time.perf_counter()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    checks = {
        "package_import": check_package_import(),
        "tester_files": check_tester_files(),
        "cli_help": run_cli_check([sys.executable, "-m", "skilllayer", "--help"]),
        "doctor_command": run_cli_check([sys.executable, "-m", "skilllayer", "doctor", "--json"]),
        "workflows_command": run_cli_check([sys.executable, "-m", "skilllayer", "workflows", "--json"]),
        "telemetry_export_command": run_cli_check(
            [
                sys.executable,
                "-m",
                "skilllayer",
                "telemetry-export",
                "--json",
                "--output",
                str(OUTPUT_DIR / "tester_check_telemetry_export.json"),
            ]
        ),
    }

    summary = build_tester_kit_summary(checks, started)
    report = {
        "version": package_version(),
        "summary": summary,
        "checks": checks,
        "outputs": {
            "tester_kit_summary": str(OUTPUT_DIR / "tester_kit_summary.json"),
            "tester_check_report": str(OUTPUT_DIR / "tester_check_report.json"),
            "report": str(OUTPUT_DIR / "report.json"),
        },
        "documentation": {
            "tester_guide": "TESTER_GUIDE.md",
            "feedback_template": "FEEDBACK_TEMPLATE.md",
            "telemetry_privacy": "docs/TELEMETRY_PRIVACY.md",
            "tester_checklist": "docs/TESTER_CHECKLIST.md",
            "bug_report_template": ".github/ISSUE_TEMPLATE/bug_report.md",
            "workflow_request_template": ".github/ISSUE_TEMPLATE/workflow_request.md",
            "tester_feedback_template": ".github/ISSUE_TEMPLATE/tester_feedback.md",
        },
        "no_network_requests": True,
        "automatic_upload": False,
        "automatic_telemetry": False,
        "new_workflows_added": False,
    }
    write_json(OUTPUT_DIR / "tester_kit_summary.json", summary)
    write_json(OUTPUT_DIR / "tester_check_report.json", {"checks": checks})
    write_json(OUTPUT_DIR / "report.json", report)
    return report


def check_package_import() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        import skilllayer  # noqa: F401

        version = package_version()
        return {
            "ok": True,
            "duration_ms": elapsed_ms(started),
            "version": version,
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "duration_ms": elapsed_ms(started),
            "version": None,
            "error": str(exc),
        }


def check_tester_files() -> dict[str, Any]:
    started = time.perf_counter()
    required_files = [
        "TESTER_GUIDE.md",
        "FEEDBACK_TEMPLATE.md",
        "docs/TELEMETRY_PRIVACY.md",
        "docs/TESTER_CHECKLIST.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/workflow_request.md",
        ".github/ISSUE_TEMPLATE/tester_feedback.md",
    ]
    missing = [path for path in required_files if not Path(path).exists()]
    return {
        "ok": not missing,
        "duration_ms": elapsed_ms(started),
        "required_files": required_files,
        "missing_files": missing,
        "error": None if not missing else "missing_tester_files",
    }


def package_version() -> str:
    return product_version()


def run_cli_check(command: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        parsed_json = parse_json_output(completed.stdout)
        return {
            "ok": completed.returncode == 0,
            "command": redact_python_command(command),
            "returncode": completed.returncode,
            "duration_ms": elapsed_ms(started),
            "stdout_preview": preview(completed.stdout),
            "stderr_preview": preview(completed.stderr),
            "json_output_valid": parsed_json is not None,
            "json_output": parsed_json if isinstance(parsed_json, dict) else None,
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": redact_python_command(command),
            "returncode": None,
            "duration_ms": elapsed_ms(started),
            "stdout_preview": preview(exc.stdout or ""),
            "stderr_preview": preview(exc.stderr or ""),
            "json_output_valid": False,
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": redact_python_command(command),
            "returncode": None,
            "duration_ms": elapsed_ms(started),
            "stdout_preview": "",
            "stderr_preview": "",
            "json_output_valid": False,
            "error": str(exc),
        }


def parse_json_output(value: str) -> Any | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def redact_python_command(command: list[str]) -> list[str]:
    if not command:
        return []
    redacted = list(command)
    if redacted[0] == sys.executable:
        redacted[0] = "python"
    return redacted


def preview(value: str, limit: int = 500) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def build_tester_kit_summary(checks: dict[str, dict[str, Any]], started: float) -> dict[str, Any]:
    doctor_json = checks.get("doctor_command", {}).get("json_output") or {}
    doctor_warnings = list(doctor_json.get("warnings", []) or []) if isinstance(doctor_json, dict) else []
    return {
        "ok": all(check.get("ok") for check in checks.values()),
        "check_count": len(checks),
        "passed_count": sum(1 for check in checks.values() if check.get("ok")),
        "failed_count": sum(1 for check in checks.values() if not check.get("ok")),
        "duration_ms": elapsed_ms(started),
        "package_import_ok": bool(checks["package_import"]["ok"]),
        "tester_files_exist": bool(checks["tester_files"]["ok"]),
        "cli_available": bool(checks["cli_help"]["ok"]),
        "doctor_command_works": bool(checks["doctor_command"]["ok"]),
        "doctor_warning_count": len(doctor_warnings),
        "doctor_warnings": doctor_warnings,
        "pytest_missing_is_optional": any("pytest is optional" in str(warning).lower() for warning in doctor_warnings)
        or bool((doctor_json.get("optional_checks", {}).get("pytest_available", {}) if isinstance(doctor_json, dict) else {}).get("ok", True)),
        "workflows_command_works": bool(checks["workflows_command"]["ok"]),
        "telemetry_export_command_works": bool(checks["telemetry_export_command"]["ok"]),
        "no_network_requests": True,
        "automatic_upload": False,
        "automatic_telemetry": False,
        "new_workflows_added": False,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
