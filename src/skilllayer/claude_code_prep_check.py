from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .mcp_server import create_mcp_server, list_tool_schemas


OUTPUT_DIR = Path("runs/claude_code_tester_prep")
EXPECTED_TOOLS = {
    "skilllayer_run",
    "skilllayer_inspect_repo",
    "skilllayer_list_workflows",
    "skilllayer_list_skills",
    "skilllayer_doctor",
}


def run_claude_code_prep_check(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    started = time.perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[2]

    doc_check = check_claude_setup_doc(repo_root)
    config_check = check_claude_mcp_config(repo_root)
    tool_inventory = build_mcp_tool_inventory()
    tester_check = run_tester_check_command()
    prep_report = {
        "status": "prepared_validation_pending",
        "claude_code_validated": False,
        "checks": {
            "claude_setup_doc": doc_check["ok"],
            "claude_mcp_config": config_check["ok"],
            "mcp_tools_listed": tool_inventory["ok"],
            "tester_check_passed": tester_check["ok"],
        },
        "duration_ms": elapsed_ms(started),
        "interpretation": (
            "SkillLayer is prepared for Claude Code testing from the local side. "
            "Real Claude Code tool discovery and invocation remain pending."
        ),
    }
    report = {
        "success": bool(all(prep_report["checks"].values())),
        "claude_code_status": "prepared_validation_pending",
        "claude_code_validated": False,
        "summary": {
            "setup_doc_exists": doc_check["ok"],
            "config_generator_has_claude_section": config_check["has_claude_section"],
            "config_has_mcp_server_block": config_check["has_mcp_server_block"],
            "mcp_tool_listing_works": tool_inventory["ok"],
            "registered_tool_count": len(tool_inventory["registered_tools"]),
            "tester_check_passes": tester_check["ok"],
            "new_workflows_added": False,
            "network_services_added": False,
        },
        "outputs": {
            "claude_setup_doc_check": str(output_dir / "claude_setup_doc_check.json"),
            "claude_mcp_config_check": str(output_dir / "claude_mcp_config_check.json"),
            "mcp_tool_inventory": str(output_dir / "mcp_tool_inventory.json"),
            "prep_report": str(output_dir / "prep_report.json"),
            "report": str(output_dir / "report.json"),
        },
        "limitations": [
            "This check does not launch a real Claude Code client.",
            "Claude Code MCP tool visibility and invocation still require manual tester confirmation.",
            "The generated config includes placeholders for user-specific Claude Code configuration location.",
        ],
    }

    write_json(output_dir / "claude_setup_doc_check.json", doc_check)
    write_json(output_dir / "claude_mcp_config_check.json", config_check)
    write_json(output_dir / "mcp_tool_inventory.json", tool_inventory)
    write_json(output_dir / "prep_report.json", prep_report)
    write_json(output_dir / "report.json", report)
    return report


def check_claude_setup_doc(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "docs" / "CLAUDE_CODE_SETUP.md"
    required_phrases = [
        "not yet validated",
        "tester-check",
        "python -m skilllayer.mcp_server --list-tools",
        "clients.claude_code",
        "skilllayer_doctor",
        "skilllayer_inspect_repo",
        "dry_run=true",
    ]
    exists = path.exists()
    text = path.read_text(encoding="utf-8") if exists else ""
    missing_phrases = [phrase for phrase in required_phrases if phrase not in text]
    return {
        "ok": exists and not missing_phrases,
        "path": str(path),
        "exists": exists,
        "required_phrases": required_phrases,
        "missing_phrases": missing_phrases,
        "status_wording_conservative": "not yet validated" in text and "Prepared, validation pending" in text,
    }


def check_claude_mcp_config(repo_root: Path) -> dict[str, Any]:
    command = [sys.executable, str(repo_root / "scripts" / "generate_mcp_config.py")]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    payload = parse_json(completed.stdout) or {}
    clients = payload.get("clients", {}) if isinstance(payload, dict) else {}
    claude = clients.get("claude_code", {}) if isinstance(clients, dict) else {}
    server = claude.get("server", {}) if isinstance(claude, dict) else {}
    config = claude.get("config", {}) if isinstance(claude, dict) else {}
    mcp_servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
    skilllayer = mcp_servers.get("skilllayer", {}) if isinstance(mcp_servers, dict) else {}
    checks = {
        "command_ok": completed.returncode == 0,
        "json_valid": bool(payload),
        "has_claude_section": bool(claude),
        "status_prepared_pending": claude.get("status") == "prepared_validation_pending",
        "has_server": bool(server),
        "has_mcp_server_block": bool(skilllayer),
        "command_path_present": bool(server.get("command")),
        "args_include_mcp_server": server.get("args") == ["-m", "skilllayer.mcp_server"],
        "cwd_present": bool(server.get("cwd")),
        "env_present": isinstance(server.get("env"), dict),
        "placeholder_location_present": "config_location" in claude,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "has_claude_section": checks["has_claude_section"],
        "has_mcp_server_block": checks["has_mcp_server_block"],
        "status": claude.get("status"),
        "server": server,
        "config": config,
        "stdout_preview": preview(completed.stdout),
        "stderr_preview": preview(completed.stderr),
    }


def build_mcp_tool_inventory() -> dict[str, Any]:
    schemas = list_tool_schemas()
    registered = sorted(str(item.get("name")) for item in schemas.get("tools", []) if item.get("name"))
    missing = sorted(EXPECTED_TOOLS - set(registered))
    unexpected = sorted(set(registered) - EXPECTED_TOOLS)
    create_server_ok = False
    create_server_error = None
    try:
        create_mcp_server()
        create_server_ok = True
    except Exception as exc:
        create_server_error = str(exc)
    return {
        "ok": not missing and bool(registered),
        "expected_tools": sorted(EXPECTED_TOOLS),
        "registered_tools": registered,
        "missing_tools": missing,
        "unexpected_tools": unexpected,
        "mcp_sdk_available": bool(schemas.get("mcp_sdk_available")),
        "create_mcp_server_ok": create_server_ok,
        "create_mcp_server_error": create_server_error,
        "source": "skilllayer.mcp_server.list_tool_schemas",
    }


def run_tester_check_command() -> dict[str, Any]:
    command = [preferred_python(), "-m", "skilllayer", "tester-check", "--json"]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=60, env=python_env())
        payload = parse_json(completed.stdout) or {}
        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        return {
            "ok": completed.returncode == 0 and bool(summary.get("ok", payload.get("success", False))),
            "command": ["python", "-m", "skilllayer", "tester-check", "--json"],
            "returncode": completed.returncode,
            "json_valid": bool(payload),
            "stdout_preview": preview(completed.stdout),
            "stderr_preview": preview(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": ["python", "-m", "skilllayer", "tester-check", "--json"],
            "returncode": None,
            "json_valid": False,
            "stdout_preview": preview(exc.stdout or ""),
            "stderr_preview": preview(exc.stderr or ""),
            "error": "timeout",
        }


def parse_json(value: str) -> Any | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def preview(value: str, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def preferred_python() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    for candidate in [repo_root / ".venv" / "bin" / "python", repo_root / ".venv" / "Scripts" / "python.exe"]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def python_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path + (os.pathsep + existing if existing else "")
    return env


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    report = run_claude_code_prep_check()
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
