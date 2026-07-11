"""CLI/MCP parity and cross-surface checks for WatchDependencyUpdatesWorkflow
(Runtime Safety Closure Stage 3). Complements test_watch_deps_workflow.py."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from skilllayer import SkillLayer
from skilllayer.mcp_server import skilllayer_watch_deps


def _patch_pypi(version, error_code=None):
    if version is None and error_code is None:
        error_code = "registry_lookup_failed"
    return patch(
        "skilllayer.runner.core._fetch_pypi_latest",
        return_value=(version, error_code),
    )


def test_cli_and_mcp_agree_on_counts_and_dependency_status(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.20.0\nflask==2.0.0\n")

    def side_effect(pkg):
        return ("2.28.0", None) if pkg == "requests" else (None, "registry_timeout")

    with patch("skilllayer.runner.core._fetch_pypi_latest", side_effect=side_effect):
        direct = skilllayer_watch_deps(str(tmp_path))
        generic = SkillLayer().run(tmp_path, "check for dependency updates")

    assert direct["outdated_count"] == generic.get("outdated_count")
    assert direct["unknown_count"] == generic.get("unknown_count")
    assert direct["up_to_date_count"] == generic.get("up_to_date_count")
    assert direct["complete"] == generic.get("complete")

    direct_by_name = {d["name"]: d for d in direct["dependencies"]}
    generic_by_name = {d["name"]: d for d in generic.get("dependencies", [])}
    for name in ("requests", "flask"):
        assert direct_by_name[name]["status"] == generic_by_name[name]["status"]
        assert direct_by_name[name]["outdated"] == generic_by_name[name]["outdated"]


def test_no_repository_writes_via_direct_mcp(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\n")
    before = set(tmp_path.iterdir())
    with _patch_pypi("2.28.0"):
        skilllayer_watch_deps(str(tmp_path))
    after = set(tmp_path.iterdir())
    assert before == after
