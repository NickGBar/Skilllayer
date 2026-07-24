"""Local-only sanitized diagnostics report."""
from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mcp_config import validate_config
from .operations import detect_installation_type
from .sanitization import sanitize
from .version import product_version


def build_diagnostics(repo: str | None, doctor: dict[str, Any], *, tool_count: int | None) -> dict[str, Any]:
    root = Path(repo).resolve() if repo else None
    config = root / ".mcp.json" if root else None
    mcp_status: dict[str, Any] = {"status": "INCOMPLETE", "observed": "no project MCP config selected"}
    if config and config.exists():
        try:
            import json
            payload = json.loads(config.read_text(encoding="utf-8"))
            checked = validate_config(payload, tool_count_provider=(lambda: tool_count) if tool_count is not None else None)
            mcp_status = {"status": "PASS" if checked["ok"] else "WARNING", "observed": "project MCP config checked", "tool_count": checked.get("actual_tool_count")}
        except Exception as exc:
            mcp_status = {"status": "INCOMPLETE", "observed": f"project MCP config could not be parsed: {exc}"}
    project_python = None
    if root:
        for name in (".venv", "venv", "env"):
            candidate = root / name / "bin" / "python"
            if candidate.exists():
                project_python = {"status": "PASS", "location": f"{name}/bin/python"}
                break
        if project_python is None:
            project_python = {"status": "INCOMPLETE", "observed": "no supported project-local Python environment detected"}
    return sanitize({
        "product_version": product_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operating_system": platform.system(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "installation_type": detect_installation_type(),
        "doctor_summary": {"overall_status": doctor.get("overall_status"), "success": doctor.get("success")},
        "mcp_configuration": mcp_status,
        "mcp_handshake": {"status": "INCOMPLETE", "observed": "not started by diagnostics"},
        "mcp_tool_count": tool_count,
        "professional_skills": {
            "safe_code_change": True, "release_readiness": True, "resume_project_work": True,
            "verified_task_execution": True,
        },
        "selected_project_python": project_python,
        "recent_error_codes": [],
        "uploaded": False,
        "privacy_warning": "Review this report before sharing. It is generated locally and is not uploaded automatically.",
    })
