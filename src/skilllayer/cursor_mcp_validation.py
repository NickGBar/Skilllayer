from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .mcp_server import FastMCP, create_mcp_server, list_tool_schemas


OUTPUT_DIR = Path("runs/p7_5_cursor_validation")
EXPECTED_TOOLS = [
    "skilllayer_doctor",
    "skilllayer_inspect_repo",
    "skilllayer_list_skills",
    "skilllayer_list_workflows",
    "skilllayer_run",
]


def run_cursor_mcp_validation(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    started = time.perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[2]

    config_payload, config_process = generate_mcp_config(repo_root)
    cursor_config_validation = validate_cursor_config(config_payload, config_process)
    tool_inventory = build_tool_inventory()
    tool_registration_validation = validate_tool_registration(tool_inventory)
    mcp_health_report = build_mcp_health_report(repo_root, config_payload)
    onboarding_review = build_onboarding_review(cursor_config_validation, mcp_health_report)

    status = determine_cursor_status(cursor_config_validation, tool_registration_validation, mcp_health_report)
    report = {
        "milestone": "Cursor MCP Validation",
        "success": bool(
            cursor_config_validation["valid"]
            and tool_registration_validation["success"]
            and mcp_health_report["server_startup_validation"]["ok"]
        ),
        "cursor_support_status": status,
        "cursor_ui_tool_discovery_validated": False,
        "validation_scope": [
            "generated Cursor config",
            "local MCP server import/startup",
            "tool schema listing",
            "expected tool inventory",
        ],
        "not_validated": [
            "Cursor UI tool discovery",
            "real Cursor client invocation",
        ],
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "outputs": {
            "cursor_config_validation": str(output_dir / "cursor_config_validation.json"),
            "tool_registration_validation": str(output_dir / "tool_registration_validation.json"),
            "cursor_tool_inventory": str(output_dir / "cursor_tool_inventory.json"),
            "mcp_health_report": str(output_dir / "mcp_health_report.json"),
            "cursor_onboarding_review": str(output_dir / "cursor_onboarding_review.json"),
            "report": str(output_dir / "report.json"),
        },
        "interpretation": (
            "Cursor support is partially validated from the SkillLayer side: config JSON, "
            "local MCP server startup, and tool schemas are valid. Cursor UI discovery "
            "still needs a manual client-side check."
        ),
    }

    write_json(output_dir / "cursor_config_validation.json", cursor_config_validation)
    write_json(output_dir / "tool_registration_validation.json", tool_registration_validation)
    write_json(output_dir / "cursor_tool_inventory.json", tool_inventory)
    write_json(output_dir / "mcp_health_report.json", mcp_health_report)
    write_json(output_dir / "cursor_onboarding_review.json", onboarding_review)
    write_json(output_dir / "report.json", report)
    return report


def generate_mcp_config(repo_root: Path) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    command = [sys.executable, str(repo_root / "scripts" / "generate_mcp_config.py")]
    completed = subprocess.run(command, text=True, capture_output=True, cwd=repo_root, check=False)
    payload: dict[str, Any] = {}
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {}
    return payload, completed


def validate_cursor_config(
    config_payload: dict[str, Any],
    process: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    clients = config_payload.get("clients", {}) if isinstance(config_payload, dict) else {}
    cursor = clients.get("cursor", {}) if isinstance(clients, dict) else {}
    cursor_config = cursor.get("config", {}) if isinstance(cursor, dict) else {}
    mcp_servers = cursor_config.get("mcpServers", {}) if isinstance(cursor_config, dict) else {}
    skilllayer = mcp_servers.get("skilllayer", {}) if isinstance(mcp_servers, dict) else {}

    command = skilllayer.get("command")
    cwd = skilllayer.get("cwd")
    args = skilllayer.get("args", [])
    env = skilllayer.get("env", {})
    checks = {
        "generator_exit_zero": process.returncode == 0,
        "json_valid": bool(config_payload),
        "cursor_section_present": bool(cursor),
        "cursor_config_present": bool(cursor_config),
        "mcp_servers_present": bool(mcp_servers),
        "skilllayer_server_present": bool(skilllayer),
        "command_is_absolute": isinstance(command, str) and Path(command).is_absolute(),
        "command_exists": isinstance(command, str) and Path(command).exists(),
        "cwd_is_absolute": isinstance(cwd, str) and Path(cwd).is_absolute(),
        "cwd_exists": isinstance(cwd, str) and Path(cwd).exists(),
        "args_include_mcp_server": args == ["-m", "skilllayer.mcp_server"],
        "env_json_object": isinstance(env, dict),
    }
    return {
        "valid": all(checks.values()),
        "cursor_status_from_generator": cursor.get("status"),
        "checks": checks,
        "cursor_config": cursor_config,
        "generator": {
            "returncode": process.returncode,
            "stderr": process.stderr.strip(),
        },
        "notes": [
            "Cursor config is generated as a standard mcpServers object.",
            "Cursor UI tool discovery is not validated by this local check.",
        ],
    }


def build_tool_inventory() -> dict[str, Any]:
    schemas = list_tool_schemas()
    tools = list(schemas.get("tools", []))
    tool_names = sorted(str(tool.get("name")) for tool in tools)
    return {
        "source": "python -m skilllayer.mcp_server --list-tools equivalent",
        "mcp_sdk_available": bool(schemas.get("mcp_sdk_available")),
        "tool_count": len(tools),
        "tool_names": tool_names,
        "tools": tools,
    }


def validate_tool_registration(tool_inventory: dict[str, Any]) -> dict[str, Any]:
    registered_tools = sorted(tool_inventory.get("tool_names", []))
    missing_tools = sorted(set(EXPECTED_TOOLS) - set(registered_tools))
    unexpected_tools = sorted(set(registered_tools) - set(EXPECTED_TOOLS))
    return {
        "success": not missing_tools,
        "expected_tools": EXPECTED_TOOLS,
        "registered_tools": registered_tools,
        "missing_tools": missing_tools,
        "unexpected_tools": unexpected_tools,
        "cursor_visible_tools": None,
        "cursor_visible_tools_validated": False,
        "note": "Registered tools are validated from SkillLayer MCP schemas; Cursor UI visibility requires manual client validation.",
    }


def build_mcp_health_report(repo_root: Path, config_payload: dict[str, Any]) -> dict[str, Any]:
    list_tools_command = [sys.executable, "-m", "skilllayer.mcp_server", "--list-tools"]
    completed = subprocess.run(list_tools_command, text=True, capture_output=True, cwd=repo_root, check=False)
    list_tools_json_valid = False
    list_tools_payload: dict[str, Any] = {}
    if completed.stdout.strip():
        try:
            list_tools_payload = json.loads(completed.stdout)
            list_tools_json_valid = True
        except json.JSONDecodeError:
            list_tools_json_valid = False

    create_server_ok = False
    create_server_error = None
    if FastMCP is not None:
        try:
            create_mcp_server()
            create_server_ok = True
        except Exception as exc:  # pragma: no cover - defensive startup check.
            create_server_error = str(exc)

    generated_command = config_payload.get("validation", {}).get("list_tools_command", [])
    generated_completed = None
    generated_stdout_json_valid = False
    generated_tool_count = 0
    generated_stderr = ""
    generated_returncode = None
    if isinstance(generated_command, list) and generated_command:
        generated_completed = subprocess.run(
            [str(part) for part in generated_command],
            text=True,
            capture_output=True,
            cwd=repo_root,
            check=False,
        )
        generated_returncode = generated_completed.returncode
        generated_stderr = generated_completed.stderr.strip()
        if generated_completed.stdout.strip():
            try:
                generated_payload = json.loads(generated_completed.stdout)
                generated_stdout_json_valid = True
                generated_tool_count = len(generated_payload.get("tools", []))
            except json.JSONDecodeError:
                generated_stdout_json_valid = False

    generated_command_ok = bool(generated_command) and generated_returncode == 0 and generated_stdout_json_valid
    startup_ok = (
        FastMCP is not None
        and create_server_ok
        and completed.returncode == 0
        and list_tools_json_valid
        and generated_command_ok
    )
    return {
        "mcp_sdk_available": FastMCP is not None,
        "server_startup_validation": {
            "ok": startup_ok,
            "create_mcp_server_ok": create_server_ok,
            "create_mcp_server_error": create_server_error,
            "stdio_server_run_not_started": True,
            "reason": "server.run() blocks waiting for a client; startup is validated by constructing the server and listing schemas.",
        },
        "list_tools_command": {
            "command": list_tools_command,
            "returncode": completed.returncode,
            "stdout_json_valid": list_tools_json_valid,
            "stderr": completed.stderr.strip(),
            "tool_count": len(list_tools_payload.get("tools", [])) if list_tools_json_valid else 0,
        },
        "generated_config_list_tools_command": {
            "command": generated_command,
            "returncode": generated_returncode,
            "stdout_json_valid": generated_stdout_json_valid,
            "stderr": generated_stderr,
            "tool_count": generated_tool_count,
            "ok": generated_command_ok,
        },
        "schemas_load": list_tools_json_valid,
        "no_startup_exceptions": startup_ok,
    }


def build_onboarding_review(
    cursor_config_validation: dict[str, Any],
    mcp_health_report: dict[str, Any],
) -> dict[str, Any]:
    friction_points = [
        {
            "area": "Cursor config location",
            "severity": "medium",
            "detail": "Cursor settings may expose the MCP config file differently by version; docs should tell users to use Cursor's MCP settings UI.",
        },
        {
            "area": "Python path",
            "severity": "medium",
            "detail": "Generated config uses an absolute Python executable path. It is reliable locally but must be regenerated after moving the repo or recreating the venv.",
        },
        {
            "area": "Restart requirement",
            "severity": "low",
            "detail": "Cursor may need a restart or MCP reload before tools appear.",
        },
        {
            "area": "MCP SDK",
            "severity": "low" if mcp_health_report.get("mcp_sdk_available") else "high",
            "detail": "The MCP Python SDK must be installed in the same environment used by the generated config.",
        },
    ]
    return {
        "install_complexity": "low" if cursor_config_validation["checks"].get("command_exists") else "medium",
        "config_complexity": "medium",
        "restart_required": "likely",
        "common_failure_points": friction_points,
        "recommended_user_flow": [
            "Run python scripts/generate_mcp_config.py",
            "Copy clients.cursor.config into Cursor MCP settings",
            "Restart or reload Cursor MCP",
            "Verify SkillLayer tools appear",
            "Run skilllayer_doctor from Cursor if available",
        ],
    }


def determine_cursor_status(
    cursor_config_validation: dict[str, Any],
    tool_registration_validation: dict[str, Any],
    mcp_health_report: dict[str, Any],
) -> str:
    if not cursor_config_validation["valid"] or not tool_registration_validation["success"]:
        return "not_validated"
    if not mcp_health_report["server_startup_validation"]["ok"]:
        return "not_validated"
    return "partially_validated"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    report = run_cursor_mcp_validation()
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
