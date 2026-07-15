"""Read-only public release update checks."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from .version import product_version

RELEASES_URL = "https://api.github.com/repos/NickGBar/Skilllayer/releases/latest"
_VERSION = re.compile(r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def _parse(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(value.strip())
    return tuple(int(part) for part in match.groups()[:3]) if match else None


def update_command(installation_type: str) -> str:
    if installation_type == "SOURCE_CHECKOUT":
        return "git pull --ff-only"
    if installation_type == "EDITABLE":
        return "python -m pip install --upgrade -e ."
    return "python -m pip install --upgrade git+https://github.com/NickGBar/Skilllayer.git"


def check_for_update(*, timeout: float = 3.0, latest_url: str = RELEASES_URL, installation_type: str = "UNKNOWN") -> dict[str, Any]:
    installed = product_version()
    result: dict[str, Any] = {
        "installed_version": installed,
        "latest_known_public_release": None,
        "status": "UPDATE_STATUS_UNKNOWN",
        "checked_source": latest_url,
        "recommended_update_command": update_command(installation_type),
        "installation_type": installation_type,
        "mutated_environment": False,
        "timestamp": datetime.now(UTC).isoformat(),
        "error_code": None,
    }
    try:
        request = urllib.request.Request(latest_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "SkillLayer-update-check"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = payload.get("tag_name") if isinstance(payload, dict) else None
        latest = _parse(tag) if isinstance(tag, str) else None
        if latest is None:
            result["error_code"] = "invalid_release_version"
            return result
        result["latest_known_public_release"] = tag.lstrip("v")
        current = _parse(installed)
        if current is None:
            result["error_code"] = "invalid_installed_version"
        elif current < latest:
            result["status"] = "UPDATE_AVAILABLE"
        elif current == latest:
            result["status"] = "UP_TO_DATE"
        else:
            result["status"] = "UP_TO_DATE"
    except urllib.error.HTTPError as exc:
        result["error_code"] = "release_endpoint_http_error"
        result["http_status"] = exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        result["error_code"] = "release_endpoint_unavailable"
    return result
