"""The single, installed-package contract for SkillLayer's stdio MCP server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "skilllayer"
TRANSPORT = "stdio"
SERVER_ARGS = ("-m", "skilllayer.mcp_server")
DEFAULT_ENV = {"PYTHONUNBUFFERED": "1"}


def installed_interpreter_path() -> Path:
    """Return this environment's launcher path without resolving venv symlinks."""
    candidates = (
        Path(sys.prefix) / "bin" / "python",
        Path(sys.prefix) / "Scripts" / "python.exe",
        Path(sys.executable),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.absolute()
    return Path(sys.executable).absolute()


def build_server_config(executable: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Build the portable server block used by all supported MCP clients.

    A working directory is intentionally omitted: all repository tools receive
    an explicit ``repo_path`` and the installed module has no checkout-relative
    runtime dependency.
    """
    command = Path(executable).expanduser().absolute() if executable else installed_interpreter_path()
    return {"command": str(command), "args": list(SERVER_ARGS), "env": dict(DEFAULT_ENV)}


def build_config(
    executable: str | os.PathLike[str] | None = None,
    *,
    tool_count: int | None = None,
) -> dict[str, Any]:
    server = build_server_config(executable)
    return {
        "skilllayer_mcp_config_version": "2.0",
        "server_name": SERVER_NAME,
        "transport": TRANSPORT,
        "expected_tool_count": tool_count,
        "mcpServers": {SERVER_NAME: server},
    }


def render_config(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2) + "\n"


def validate_config(
    payload: dict[str, Any],
    *,
    tool_count_provider: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Validate the generated contract without starting an MCP server."""
    servers = payload.get("mcpServers")
    server = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    command = server.get("command") if isinstance(server, dict) else None
    args = server.get("args") if isinstance(server, dict) else None
    env = server.get("env") if isinstance(server, dict) else None
    expected_count = payload.get("expected_tool_count")
    actual_count = tool_count_provider() if tool_count_provider else None
    executable_exists = isinstance(command, str) and Path(command).is_file() and os.access(command, os.X_OK)
    checks = {
        "server_name": payload.get("server_name") == SERVER_NAME,
        "transport": payload.get("transport") == TRANSPORT,
        "server_block": isinstance(server, dict),
        "executable_exists": executable_exists,
        "arguments": args == list(SERVER_ARGS),
        "environment": isinstance(env, dict) and env.get("PYTHONUNBUFFERED") == "1",
        "tool_count": (
            isinstance(expected_count, int)
            and expected_count >= 0
            and (actual_count is None or expected_count == actual_count)
        ),
    }
    remediation = None
    if not executable_exists:
        remediation = "Regenerate MCP config with: skilllayer mcp-config --output <config.json>"
    elif actual_count is not None and expected_count != actual_count:
        remediation = "Regenerate MCP config after upgrading SkillLayer."
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "server_name": SERVER_NAME,
        "transport": TRANSPORT,
        "configured_executable": command,
        "expected_tool_count": expected_count,
        "actual_tool_count": actual_count,
        "remediation": remediation,
    }
