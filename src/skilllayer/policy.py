"""Small, local, non-executable repository policy contract."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

POLICY_FILES = (".skilllayer-policy.yml", ".skilllayer-policy.yaml")
ALLOWED_CHECKS = {"tests", "secrets"}
ALLOWED_APPROVALS = {"dependency_install", "destructive_command"}
DEFAULT_POLICY = {
    "version": 1,
    "required_checks": ["tests", "secrets"],
    "approval_required_for": ["dependency_install", "destructive_command"],
    "safe_change": {"require_clean_or_acknowledged_worktree": True, "require_validation": True},
    "release": {"allow_incomplete": False, "require_tests": True, "require_secret_check": True},
}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _parse_scalar(text: str) -> Any:
    value = text.strip()
    if not value:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"-?(0|[1-9]\d*)", value):
        return int(value)
    if value.startswith(("'", '"')):
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("unterminated quoted scalar")
        return value[1:-1]
    if value.startswith(("!", "&", "*", "[", "{", "${")) or "{{" in value:
        raise ValueError("unsafe YAML scalar is not supported")
    if re.search(r"[;&|<>`$]", value):
        raise ValueError("executable or interpolated scalar is not supported")
    return value


def _parse_policy_text(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("policy exceeds 64 KiB limit")
    if "\x00" in text or re.search(r"(?m)^\s*(?:---|\.\.\.)\s*$", text):
        raise ValueError("document markers or NUL bytes are not supported")
    lines: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise ValueError(f"line {number}: tabs are not supported")
        content = raw.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        lines.append((indent, content.strip(), number))
    if not lines:
        raise ValueError("policy is empty")
    result: dict[str, Any] = {}
    current: str | None = None
    seen: set[str] = set()
    for indent, content, number in lines:
        if indent == 0:
            if ":" not in content or content.startswith("-"):
                raise ValueError(f"line {number}: expected top-level key")
            key, raw = (part.strip() for part in content.split(":", 1))
            if key in seen:
                raise ValueError(f"duplicate key: {key}")
            seen.add(key)
            if key in {"required_checks", "approval_required_for", "safe_change", "release"} and not raw:
                result[key] = [] if key in {"required_checks", "approval_required_for"} else {}
                current = key
            else:
                result[key] = _parse_scalar(raw)
                current = None
        elif indent == 2 and current in {"required_checks", "approval_required_for"}:
            if not content.startswith("- "):
                raise ValueError(f"line {number}: expected list item")
            result[current].append(_parse_scalar(content[2:]))
        elif indent == 2 and current in {"safe_change", "release"}:
            if ":" not in content or content.startswith("-"):
                raise ValueError(f"line {number}: expected nested key")
            key, raw = (part.strip() for part in content.split(":", 1))
            if key in result[current]:
                raise ValueError(f"duplicate key: {current}.{key}")
            result[current][key] = _parse_scalar(raw)
        else:
            raise ValueError(f"line {number}: unsupported indentation or structure")
    return result


def _validate(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    allowed = {"version", "required_checks", "approval_required_for", "safe_change", "release"}
    unknown = sorted(set(raw) - allowed)
    errors.extend(_error("unknown_field", f"unsupported policy field: {key}") for key in unknown)
    if raw.get("version") != 1:
        errors.append(_error("unsupported_version", "policy version must be integer 1"))
    checks = raw.get("required_checks", DEFAULT_POLICY["required_checks"])
    approvals = raw.get("approval_required_for", DEFAULT_POLICY["approval_required_for"])
    if not isinstance(checks, list) or any(not isinstance(x, str) or x not in ALLOWED_CHECKS for x in checks):
        errors.append(_error("unknown_check", "required_checks may contain only tests and secrets"))
    if not isinstance(approvals, list) or any(not isinstance(x, str) or x not in ALLOWED_APPROVALS for x in approvals):
        errors.append(_error("unknown_approval", "approval_required_for contains an unsupported approval"))
    safe = raw.get("safe_change", {})
    release = raw.get("release", {})
    if not isinstance(safe, dict) or not isinstance(release, dict):
        errors.append(_error("invalid_type", "safe_change and release must be mappings"))
        safe, release = {}, {}
    for section, allowed_keys in (("safe_change", {"require_clean_or_acknowledged_worktree", "require_validation"}), ("release", {"allow_incomplete", "require_tests", "require_secret_check"})):
        value = raw.get(section, {})
        if isinstance(value, dict):
            for key in set(value) - allowed_keys:
                errors.append(_error("unknown_field", f"unsupported policy field: {section}.{key}"))
            for key, item in value.items():
                if not isinstance(item, bool):
                    errors.append(_error("invalid_type", f"{section}.{key} must be boolean"))
    if errors:
        return None, errors
    normalized = {
        "version": 1,
        "required_checks": list(checks),
        "approval_required_for": list(approvals),
        "safe_change": {**DEFAULT_POLICY["safe_change"], **safe},
        "release": {**DEFAULT_POLICY["release"], **release},
    }
    return normalized, []


def load_policy(repo: Path) -> dict[str, Any]:
    root = repo.expanduser().resolve()
    existing = [root / name for name in POLICY_FILES if (root / name).exists()]
    base = {"status": "POLICY_NOT_PRESENT", "policy_path": None, "version": None, "normalized_policy": None, "errors": [], "warnings": [], "effective_rules": None}
    if len(existing) > 1:
        base.update(status="POLICY_CONFLICT", errors=[_error("policy_conflict", "both .skilllayer-policy.yml and .skilllayer-policy.yaml exist")])
        return base
    if not existing:
        return base
    path = existing[0]
    if path.is_symlink() and not path.resolve().is_relative_to(root):
        base.update(status="POLICY_UNSAFE_PATH", errors=[_error("policy_symlink_escape", "policy symlink resolves outside the selected repository")])
        return base
    try:
        raw = _parse_policy_text(path.read_text(encoding="utf-8"))
        normalized, errors = _validate(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        base.update(status="POLICY_INVALID", policy_path=path.name, errors=[_error("policy_parse_error", str(exc))])
        return base
    if errors:
        status = "POLICY_UNSUPPORTED_VERSION" if any(e["code"] == "unsupported_version" for e in errors) else "POLICY_INVALID"
        base.update(status=status, policy_path=path.name, version=raw.get("version"), errors=errors)
        return base
    base.update(status="POLICY_VALID", policy_path=path.name, version=1, normalized_policy=normalized, effective_rules=normalized)
    return base


def policy_dry_run(repo: Path, workflow: str) -> dict[str, Any]:
    result = load_policy(repo)
    if result["status"] == "POLICY_INVALID" or result["status"] in {"POLICY_CONFLICT", "POLICY_UNSAFE_PATH", "POLICY_UNSUPPORTED_VERSION"}:
        verdict = "POLICY_WOULD_BLOCK"
    elif result["status"] == "POLICY_NOT_PRESENT":
        verdict = "POLICY_WOULD_ALLOW"
    else:
        policy = result["normalized_policy"]
        required = policy["required_checks"] if workflow == "release-readiness" else ["tests"] if policy["safe_change"]["require_validation"] else []
        approvals = policy["approval_required_for"]
        verdict = "POLICY_WOULD_REQUIRE_APPROVAL" if approvals else "POLICY_WOULD_ALLOW"
        result["required_checks"] = required
        result["approval_requirements"] = approvals
    return {"schema_version": 1, "workflow": workflow, "verdict": verdict, "policy": result, "execution_performed": False, "writes_performed": False}
