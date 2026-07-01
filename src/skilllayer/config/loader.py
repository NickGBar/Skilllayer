from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "router": {
        "mode": "cascade",
        "local_model": None,
        "backend": "mock",
    },
    "execution": {
        "dry_run": False,
        "run_tests": True,
    },
    "logging": {
        "log_dir": "runs/skilllayer_logs",
    },
    "browser_smoke": {
        "url": None,
        "selectors": "body",
        "wait_seconds": 1,
        "output_dir": "runs/browser_smoke",
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = merge_dicts(DEFAULT_CONFIG, {})
    config_path = Path(path) if path else Path("skilllayer.yaml")
    if not config_path.exists():
        return config
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        loaded = json.loads(text)
    else:
        loaded = parse_simple_yaml(text)
    return merge_dicts(config, loaded)


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small config shape used by SkillLayer.

    This deliberately avoids adding a PyYAML dependency.
    """

    root: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            root[section] = {}
            current_section = root[section]
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = parse_scalar(raw_value.strip())
        if raw_line.startswith(" ") and current_section is not None:
            current_section[key] = value
        else:
            root[key] = value
    return root


def parse_scalar(value: str) -> Any:
    if value in {"", "null", "None", "~"}:
        return None
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        merged[key] = merge_dicts(value, {}) if isinstance(value, dict) else value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged
