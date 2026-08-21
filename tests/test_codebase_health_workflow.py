"""Tests for the CodebaseHealthWorkflow professional skill
(build_codebase_health_artifacts) — Skills v0.1, part B.

Aggregates existing read-only inspections (merge-conflict scan, dead-code scan,
dependency inspection/staleness) into one bounded verdict, mirroring
build_release_readiness_artifacts field-for-field. No new primitive: every
underlying builder this composes already exists and is tested elsewhere.
Covers: a clean repo, unresolved merge conflicts (blocker), dead code
(finding, not blocker), the bounded/deep dependency-staleness split, no false
HEALTHY, and MCP/direct parity.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skilllayer.mcp_server import skilllayer_codebase_health
from skilllayer.runner.core import build_codebase_health_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    _git(["init"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    return tmp_path


def _clean_fixture(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("def used():\n    return 2\n\nused()\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\ndependencies = ["requests==2.31.0"]\n'
    )
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


class TestBoundedDefaults:
    def test_clean_repo_is_incomplete_until_deep(self, tmp_path):
        # Mirrors release_readiness's own bounded-mode ceiling: dependency
        # staleness is skipped by default, so INCOMPLETE_ASSESSMENT is the
        # best reachable verdict without deep=True, never a bare HEALTHY.
        repo = _clean_fixture(tmp_path)
        r = build_codebase_health_artifacts(repo)
        assert r["skill"] == "codebase_health"
        assert r["blockers"] == []
        assert r["verdict"] == "INCOMPLETE_ASSESSMENT"
        assert {c["check"] for c in r["checks_incomplete"]} == {"dependency_staleness"}

    def test_clean_repo_is_healthy_when_deep(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        r = build_codebase_health_artifacts(repo, deep=True)
        assert r["verdict"] == "HEALTHY"
        assert r["checks_incomplete"] == []
        assert r["findings"] == []

    def test_no_manifest_is_incomplete_even_when_deep(self, tmp_path):
        repo = _init_repo(tmp_path)
        (repo / "app.py").write_text("def used():\n    return 2\n\nused()\n")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "initial"], cwd=repo)
        r = build_codebase_health_artifacts(repo, deep=True)
        assert r["verdict"] == "INCOMPLETE_ASSESSMENT"
        assert any(c["check"] == "dependency_inspection" for c in r["checks_incomplete"])


class TestMergeConflicts:
    def test_unresolved_conflict_markers_block(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        (repo / "app.py").write_text(
            "<<<<<<< HEAD\ndef used():\n=======\ndef used2():\n>>>>>>> branch\n    return 2\n"
        )
        r = build_codebase_health_artifacts(repo, deep=True)
        assert r["verdict"] == "NOT_HEALTHY"
        assert r["blockers"]
        assert r["conflict_status"]["clean"] is False

    def test_conflict_blocks_even_in_bounded_mode(self, tmp_path):
        # A blocker outranks "incomplete" in the verdict priority — bounded
        # mode never hides a real conflict behind a milder verdict.
        repo = _clean_fixture(tmp_path)
        (repo / "app.py").write_text("<<<<<<< HEAD\n=======\n>>>>>>> b\n")
        r = build_codebase_health_artifacts(repo)
        assert r["verdict"] == "NOT_HEALTHY"


class TestDeadCode:
    def test_dead_code_is_a_finding_not_a_blocker(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        (repo / "app.py").write_text(
            "def _unused_private():\n    return 1\n\ndef used():\n    return 2\n\nused()\n"
        )
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "add dead code"], cwd=repo)
        r = build_codebase_health_artifacts(repo, deep=True)
        assert r["verdict"] == "HEALTHY_WITH_FINDINGS"
        assert r["blockers"] == []
        assert r["dead_code_status"]["certain_count"] == 1
        assert any(f["check"] == "dead_code_scan" for f in r["findings"])


class TestNeverFalselyHealthy:
    def test_no_verdict_reaches_healthy_with_an_incomplete_check(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        r = build_codebase_health_artifacts(repo)  # bounded — always incomplete
        assert r["verdict"] != "HEALTHY"
        assert r["verdict"] != "HEALTHY_WITH_FINDINGS"

    def test_never_writes_anything(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        before = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())
        build_codebase_health_artifacts(repo, deep=True)
        after = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())
        assert before == after


class TestMcpParity:
    def test_mcp_wrapper_matches_direct_call(self, tmp_path):
        repo = _clean_fixture(tmp_path)
        direct = build_codebase_health_artifacts(repo, deep=True)
        via_mcp = skilllayer_codebase_health(str(repo), deep=True)
        assert via_mcp["verdict"] == direct["verdict"]
        assert via_mcp["findings"] == direct["findings"]

    def test_mcp_wrapper_reports_an_error_for_a_missing_repo_path(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        r = skilllayer_codebase_health(str(missing))
        assert r["success"] is False
        assert "error" in r
