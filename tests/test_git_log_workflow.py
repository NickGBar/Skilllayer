"""Tests for GitLogWorkflow (build_git_log_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_git_log_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path, n_commits: int = 3) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    for i in range(n_commits):
        (repo / f"file{i}.txt").write_text(f"content {i}")
        _git(["add", "."], repo)
        _git(["commit", "-m", f"commit {i}"], repo)
    return repo


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestNotGitRepo:
    def test_returns_explicit_error(self, tmp_path):
        result = build_git_log_artifacts(tmp_path)
        assert result["error_code"] == "not_git_repo"
        assert result["commits"] == []
        assert result["total_shown"] == 0

    def test_workflow_name(self, tmp_path):
        result = build_git_log_artifacts(tmp_path)
        assert result["workflow"] == "GitLogWorkflow"


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestLogStructure:
    def test_returns_commits(self, tmp_path):
        repo = _make_repo(tmp_path, n_commits=3)
        result = build_git_log_artifacts(repo)
        assert result["total_shown"] == 3
        assert len(result["commits"]) == 3

    def test_commit_has_required_fields(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo)
        c = result["commits"][0]
        assert "hash" in c
        assert "full_hash" in c
        assert "message" in c
        assert "author_name" in c
        assert "date" in c
        assert "files_changed" in c
        assert "insertions" in c
        assert "deletions" in c

    def test_no_email_in_commit(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo)
        for c in result["commits"]:
            assert "@" not in c.get("author_name", "")
            assert "author_email" not in c

    def test_hash_is_7_chars(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo)
        assert len(result["commits"][0]["hash"]) == 7

    def test_full_hash_is_40_chars(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo)
        assert len(result["commits"][0]["full_hash"]) == 40

    def test_checked_at_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo)
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_respected(self, tmp_path):
        repo = _make_repo(tmp_path, n_commits=5)
        result = build_git_log_artifacts(repo, limit=2)
        assert result["total_shown"] == 2

    def test_limit_capped_at_100(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, limit=999)
        assert result["filters_applied"]["limit"] == 100

    def test_limit_minimum_1(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, limit=0)
        assert result["filters_applied"]["limit"] == 1

    def test_filters_applied_includes_limit(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, limit=5)
        assert result["filters_applied"]["limit"] == 5


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_author_filter_recorded(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, author="Test User")
        assert result["filters_applied"].get("author") == "Test User"

    def test_since_filter_recorded(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, since="2000-01-01")
        assert result["filters_applied"].get("since") == "2000-01-01"

    def test_path_filter_recorded(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_log_artifacts(repo, path="file0.txt")
        assert result["filters_applied"].get("path") == "file0.txt"

    def test_path_filter_limits_results(self, tmp_path):
        repo = _make_repo(tmp_path, n_commits=3)
        result = build_git_log_artifacts(repo, path="file0.txt")
        assert result["total_shown"] == 1


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_show_git_log(self):
        assert self._route("show git log") == "git_log"

    def test_recent_commits(self):
        assert self._route("recent commits") == "git_log"

    def test_commit_history(self):
        assert self._route("commit history") == "git_log"

    def test_who_committed_recently(self):
        assert self._route("who committed recently") == "git_log"

    def test_last_5_commits(self):
        assert self._route("last 5 commits") == "git_log"
