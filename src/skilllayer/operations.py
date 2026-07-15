"""Bounded installation detection and explicit uninstall planning."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def detect_installation_type() -> str:
    module_path = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "skilllayer").is_dir() and module_path.is_relative_to(cwd):
        return "SOURCE_CHECKOUT"
    prefix = Path(sys.prefix).resolve()
    if ".local/pipx/venvs/skilllayer" in prefix.as_posix():
        return "PIPX"
    try:
        import importlib.metadata as metadata
        dist = metadata.distribution("skilllayer")
        direct_url = dist.read_text("direct_url.json")
        if direct_url and '"editable": true' in direct_url.lower():
            return "EDITABLE"
    except Exception:
        pass
    if sys.prefix != sys.base_prefix:
        return "VENV"
    try:
        import importlib.metadata as metadata
        metadata.version("skilllayer")
        return "PIP"
    except Exception:
        return "UNKNOWN"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def build_uninstall_plan(repo: str | os.PathLike[str] | None, *, remove_venv: bool = False, remove_project_state: bool = False) -> dict[str, Any]:
    root = Path(repo or Path.cwd()).expanduser().resolve()
    plan: dict[str, Any] = {"repo": str(root), "status": "READY", "changes": [], "preserved": [], "error_code": None}
    config_path = root / ".mcp.json"
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            plan.update(status="BLOCKED", error_code="malformed_mcp_config")
            return plan
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        if isinstance(servers, dict) and "skilllayer" in servers:
            plan["changes"].append({"action": "remove_mcp_entry", "path": ".mcp.json", "entry": "skilllayer"})
        else:
            plan["preserved"].append(".mcp.json (no SkillLayer entry)")
    else:
        plan["preserved"].append(".mcp.json (absent)")
    if remove_venv:
        candidate = root / ".venv"
        if candidate.is_dir() and (candidate / "pyvenv.cfg").is_file():
            plan["changes"].append({"action": "remove_owned_venv", "path": ".venv"})
        else:
            plan["preserved"].append(".venv (not a recognizable repository-local environment)")
    else:
        plan["preserved"].append(".venv (preserved by default)")
    if remove_project_state:
        if (root / ".skilllayer").exists():
            plan["changes"].append({"action": "remove_project_state", "path": ".skilllayer"})
        else:
            plan["preserved"].append(".skilllayer (absent)")
    else:
        plan["preserved"].append(".skilllayer (preserved by default)")
    return plan


def apply_uninstall_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("status") != "READY":
        return plan
    root = Path(plan["repo"])
    removed: list[str] = []
    for change in plan.get("changes", []):
        action, relative = change["action"], change["path"]
        if action == "remove_mcp_entry":
            path = root / relative
            payload = json.loads(path.read_text(encoding="utf-8"))
            servers = payload.get("mcpServers", {})
            servers.pop("skilllayer", None)
            if servers:
                payload["mcpServers"] = servers
            else:
                payload.pop("mcpServers", None)
            _atomic_json(path, payload)
        elif action in {"remove_owned_venv", "remove_project_state"}:
            shutil.rmtree(root / relative)
        removed.append(relative)
    plan["removed"] = removed
    return plan
