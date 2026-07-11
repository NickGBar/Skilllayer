"""Tests for GetFileHistoryWorkflow (build_file_history_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_file_history_artifacts


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "main.py").write_text("x = 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "add main.py"], repo)
    (repo / "main.py").write_text("x = 1\ny = 2\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "update main.py"], repo)
    (repo / "other.py").write_text("z = 3\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "add other.py"], repo)
    return repo


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_not_git_repo(self, tmp_path):
        result = build_file_history_artifacts(tmp_path, "main.py")
        assert result["error_code"] == "not_git_repo"
        assert result["commits"] == []

    def test_file_not_in_history_explicit_error(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "nonexistent.py")
        assert result["error_code"] == "file_not_in_history"
        assert result["commits"] == []
        assert "nonexistent.py" in result["error"]

    def test_workflow_name(self, tmp_path):
        result = build_file_history_artifacts(tmp_path, "x.py")
        assert result["workflow"] == "GetFileHistoryWorkflow"


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestStructure:
    def test_returns_commits_for_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        assert result["total_shown"] == 2
        assert len(result["commits"]) == 2

    def test_file_path_in_result(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        assert result["file"] == "main.py"

    def test_commit_has_required_fields(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        c = result["commits"][0]
        assert "hash" in c
        assert "message" in c
        assert "author_name" in c
        assert "date" in c
        assert "insertions" in c
        assert "deletions" in c

    def test_no_email_in_commits(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        for c in result["commits"]:
            assert "@" not in c.get("author_name", "")
            assert "author_email" not in c

    def test_other_file_has_one_commit(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "other.py")
        assert result["total_shown"] == 1

    def test_last_modified_date_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        assert result["last_modified_date"] is not None
        assert "T" in result["last_modified_date"]

    def test_first_commit_date_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        assert result["first_commit_date"] is not None

    def test_checked_at_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py")
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_respected(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py", limit=1)
        assert result["total_shown"] == 1

    def test_limit_capped_at_50(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py", limit=999)
        assert result["commits"] is not None  # capped — doesn't error

    def test_limit_minimum_1(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_file_history_artifacts(repo, "main.py", limit=0)
        assert len(result["commits"]) >= 1


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_history_of_file(self):
        assert self._route("history of file main.py") == "file_history"

    def test_who_changed_file(self):
        assert self._route("who changed main.py") == "file_history"

    def test_when_was_modified(self):
        assert self._route("when was main.py last modified") == "file_history"

    def test_show_history_for(self):
        assert self._route("show history for utils.py") == "file_history"

    def test_what_commits_touched(self):
        assert self._route("what commits touched this file") == "file_history"
