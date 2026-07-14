"""Authoritative SkillLayer product-version lookup."""
from __future__ import annotations

from importlib import metadata


PACKAGE_NAME = "skilllayer"


def product_version() -> str:
    """Return the installed distribution version, or ``unknown`` if uninstalled."""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"
