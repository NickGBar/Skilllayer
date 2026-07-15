"""Shared, conservative redaction for locally generated support material."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PATTERNS = (
    (re.compile(r"(?:sk-proj-|sk-)[A-Za-z0-9_-]+"), "<REDACTED_TOKEN>"),
    (re.compile(r"(?:ghp_|github_pat_|glpat-)[A-Za-z0-9_-]+"), "<REDACTED_TOKEN>"),
    (re.compile(r"AIza[A-Za-z0-9_-]+|(?:AKIA|ASIA)[A-Z0-9]{12,}"), "<REDACTED_TOKEN>"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]+"), "<REDACTED_TOKEN>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.I), "Bearer <REDACTED_TOKEN>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<REDACTED_TOKEN>"),
    (re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@:]+:[^\s/@]+@[^\s]+", re.I), "<REDACTED_CREDENTIAL_URL>"),
    (re.compile(r"(?:https?://[^\s/@]+@[^\s]+|git@[^\s:]+:[^\s]+)"), "<PRIVATE_REMOTE_REDACTED>"),
    (re.compile(r"(?im)^\s*[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)\s*=\s*.+$"), "<REDACTED_ENV_ASSIGNMENT>"),
    (re.compile(r"(?im)^\s*authorization\s*:\s*.+$"), "authorization: <REDACTED_TOKEN>"),
)


def sanitize_text(value: str) -> str:
    """Redact credentials and home paths without retaining token fragments."""
    text = value
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    home = str(Path.home())
    if home and home != ".":
        text = text.replace(home, "~")
    text = re.sub(r"/(?:Users|home)/[^/\s]+", "~", text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, BaseException):
        return sanitize_text(str(value))
    return value
