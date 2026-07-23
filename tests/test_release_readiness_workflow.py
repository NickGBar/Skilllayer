"""Tests for the ReleaseReadinessWorkflow professional skill
(build_release_readiness_artifacts).

Aggregates existing read-only inspections (repo, git, secrets, dependencies,
memory, packaging) into one bounded verdict. Never certifies a repository as
secure; every incomplete/skipped check must reduce confidence rather than
silently becoming a clean READY. Covers: a reasonably complete fixture,
incomplete secret scan, unknown dependency status, failing deep tests,
absent packaging metadata, dirty git state, no false READY, no hidden
writes, and MCP/CLI parity.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skilllayer.mcp_server import skilllayer_release_readiness
from skilllayer.runner.core import build_release_readiness_artifacts, build_save_context_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    _git(["init"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    return tmp_path


def _complete_fixture(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\ndependencies = ["requests==2.31.0"]\n'
    )
    (repo / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from app import greet\n\ndef test_greet():\n    assert greet('a') == 'hello a'\n"
    )
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


class TestBoundedDefaults:
    def test_complete_fixture_is_incomplete_until_tests_run(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        r = build_release_readiness_artifacts(repo)
        assert r["skill"] == "release_readiness"
        assert r["blockers"] == []
        assert r["verdict"] == "INCOMPLETE_ASSESSMENT"
        assert r["secret_scan_status"] == "clear_for_supported_patterns"
        assert r["package_status"]["pyproject_toml"] is True
        assert r["package_status"]["pyproject_parses"] is True

    def test_deep_mode_runs_and_passes(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        build_save_context_artifacts(repo, "healthy baseline", [])  # gives memory_integrity something to check
        r = build_release_readiness_artifacts(repo, deep=True)
        assert r["test_status"]["mode"] == "deep"
        assert r["test_status"]["tests_run"] is True
        assert r["test_status"]["tests_passed"] is True
        assert r["checks_incomplete"] == []
        assert r["verdict"] == "READY_FOR_CAREFUL_TESTERS"

    def test_bounded_mode_with_tests_not_started_is_incomplete(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        build_save_context_artifacts(repo, "healthy baseline", [])

        r = build_release_readiness_artifacts(repo)

        assert r["test_status"]["tests_run"] is False
        assert any(item["check"] == "test_status" for item in r["checks_incomplete"])
        assert r["verdict"] == "INCOMPLETE_ASSESSMENT"

    def test_deep_mode_failing_tests_not_ready(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        (repo / "tests" / "test_app.py").write_text(
            "from app import greet\n\ndef test_greet():\n    assert greet('a') == 'WRONG'\n"
        )
        r = build_release_readiness_artifacts(repo, deep=True)
        assert r["verdict"] == "NOT_READY"
        assert any("Test suite failed" in b for b in r["blockers"])

    def test_no_dependency_manifest_is_incomplete_not_clean(self, tmp_path):
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("print('hi')\n")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "initial"], cwd=repo)
        r = build_release_readiness_artifacts(repo)
        assert any(c["check"] == "dependency_inspection" for c in r["checks_incomplete"])
        assert r["dependency_status"]["total_dependencies"] == 0
        assert r["dependency_status"]["files_parsed"] == []

    def test_incomplete_secret_scan_reduces_confidence(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        big = repo / "big.py"
        big.write_text("x = 1\n" * 200_000)  # forces an oversized-file skip
        r = build_release_readiness_artifacts(repo)
        assert r["secret_scan_status"] == "incomplete"
        assert r["verdict"] == "INCOMPLETE_ASSESSMENT"
        assert any("incomplete" in w.lower() for w in r["warnings"])

    def test_no_packaging_metadata_is_incomplete(self, tmp_path):
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("print('hi')\n")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "initial"], cwd=repo)
        r = build_release_readiness_artifacts(repo)
        assert r["package_status"] == {"pyproject_toml": False, "setup_py": False}
        assert any(c["check"] == "packaging_metadata" for c in r["checks_incomplete"])

    def test_dirty_git_state_is_warned_not_blocked(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        (repo / "scratch.txt").write_text("wip")
        r = build_release_readiness_artifacts(repo)
        assert r["git_status"]["clean"] is False
        assert any("uncommitted" in w for w in r["warnings"])
        assert r["blockers"] == []

    def test_never_false_ready_when_blockers_present(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        (repo / "pyproject.toml").write_text("not valid toml [[[")
        r = build_release_readiness_artifacts(repo)
        assert r["blockers"]
        assert r["verdict"] == "NOT_READY"

    def test_no_hidden_writes_when_no_prior_store(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        assert not (repo / ".skilllayer").exists()
        build_release_readiness_artifacts(repo)
        assert not (repo / ".skilllayer").exists()

    def test_written_paths_always_empty(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        r = build_release_readiness_artifacts(repo)
        assert r["written_paths"] == []


class TestMcpCliParity:
    def test_mcp_handler_matches_builder(self, tmp_path):
        repo = _complete_fixture(tmp_path)
        direct = build_release_readiness_artifacts(repo)
        via_mcp = skilllayer_release_readiness(str(repo))
        for key in ("skill", "verdict", "blockers", "secret_scan_status"):
            assert direct[key] == via_mcp[key]
