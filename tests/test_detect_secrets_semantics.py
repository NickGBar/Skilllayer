"""Focused tests for DetectSecrets honest scan-completeness semantics
(Runtime Safety Closure Stage 2). Complements the exhaustive pattern-matching
coverage in test_detect_secrets_workflow.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.mcp_server import skilllayer_detect_secrets
from skilllayer.runner.core import build_detect_secrets_artifacts


def test_clear_for_supported_patterns_on_fully_scanned_clean_fixture(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def hello():\n    return 'world'\n")
    result = build_detect_secrets_artifacts(tmp_path)
    assert result["status"] == "clear_for_supported_patterns"
    assert result["clean"] is True
    assert result["scan_complete"] is True
    assert result["success"] is True
    assert result["skipped_reasons"] == {}


def test_supported_token_pattern_produces_findings_status(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text('KEY = "sk-ant-' + "a" * 24 + '"\n')
    result = build_detect_secrets_artifacts(tmp_path)
    assert result["status"] == "findings"
    assert result["clean"] is False
    assert result["success"] is True
    assert result["findings_count"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_permission_denied_file_is_explicit_skip_not_silent_clean(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked.py"
    blocked.write_text('KEY = "sk-ant-' + "a" * 24 + '"\n')
    blocked.chmod(0o000)
    try:
        result = build_detect_secrets_artifacts(tmp_path)
    finally:
        blocked.chmod(0o644)
    assert result["skipped_reasons"].get("permission_denied", 0) >= 1
    assert result["scan_complete"] is False
    assert result["status"] == "incomplete"
    assert result["clean"] is False
    assert result["success"] is True


def test_cli_and_mcp_agree_on_status_and_counts(tmp_path: Path) -> None:
    (tmp_path / "secrets.env").write_text("KEY=" + "sk-ant-" + "a" * 24 + "\n")
    gi = tmp_path / ".gitignore"
    gi.write_text("secrets.env\n")

    direct = skilllayer_detect_secrets(str(tmp_path))
    generic = SkillLayer().run(tmp_path, "scan for secrets")

    assert direct["success"] == generic["success"]
    assert direct["status"] == generic.get("status")
    assert direct["clean"] == generic.get("clean")
    assert direct["scan_complete"] == generic.get("scan_complete")
    assert direct["scanned_files"] == generic.get("scanned_files")
    assert direct["skipped_files"] == generic.get("skipped_files")
    assert direct["findings_count"] == generic.get("findings_count")


def test_scan_does_not_modify_working_tree(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    (tmp_path / "secrets.env").write_text('KEY = "sk-ant-' + "a" * 24 + '"\n')
    (tmp_path / ".gitignore").write_text("secrets.env\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout

    build_detect_secrets_artifacts(tmp_path)

    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert before == after == ""
