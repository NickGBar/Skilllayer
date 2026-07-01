#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate ready-to-copy SkillLayer MCP config snippets.")
    parser.add_argument("--output", type=Path, default=None, help="Optional path to write the generated JSON.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    python_path = preferred_python_path(repo_root)
    config = build_config(repo_root, python_path)
    text = json.dumps(config, indent=2)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def preferred_python_path(repo_root: Path) -> str:
    candidates = [
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def build_config(repo_root: Path, python_path: str) -> dict[str, Any]:
    server = {
        "command": python_path,
        "args": ["-m", "skilllayer.mcp_server"],
        "cwd": str(repo_root),
        "env": {
            "PYTHONUNBUFFERED": "1",
        },
    }
    return {
        "skilllayer_mcp_config_version": "1.0",
        "generated_from_repo": str(repo_root),
        "privacy_note": "SkillLayer MCP runs locally. It does not upload telemetry automatically.",
        "clients": {
            "codex": {
                "status": "tested",
                "config": {
                    "mcpServers": {
                        "skilllayer": server,
                    }
                },
            },
            "generic_mcp_client": {
                "status": "expected",
                "config": {
                    "mcpServers": {
                        "skilllayer": server,
                    }
                },
            },
            "claude_code": {
                "status": "prepared_validation_pending",
                "note": (
                    "SkillLayer has prepared a local MCP server block for Claude Code, "
                    "but real Claude Code client discovery/invocation has not yet been validated. "
                    "Adapt this snippet to the Claude Code MCP configuration location used on your machine."
                ),
                "server_name": "skilllayer",
                "server": server,
                "config": {
                    "mcpServers": {
                        "skilllayer": server,
                    }
                },
                "validation_commands": {
                    "list_tools": [python_path, "-m", "skilllayer.mcp_server", "--list-tools"],
                    "doctor": [python_path, "-m", "skilllayer", "doctor", "--json"],
                    "tester_check": [python_path, "-m", "skilllayer", "tester-check", "--json"],
                },
            },
            "cursor": {
                "status": "partially_validated_local_config",
                "note": (
                    "Copy this mcpServers object into Cursor's MCP configuration. "
                    "SkillLayer validates the generated config and local MCP schemas; "
                    "Cursor UI tool visibility still requires a manual client check."
                ),
                "config": {
                    "mcpServers": {
                        "skilllayer": server,
                    }
                },
            },
        },
        "validation": {
            "list_tools_command": [python_path, "-m", "skilllayer.mcp_server", "--list-tools"],
            "doctor_command": [python_path, "-m", "skilllayer", "doctor", "--json"],
            "tester_check_command": [python_path, "-m", "skilllayer", "tester-check", "--json"],
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
