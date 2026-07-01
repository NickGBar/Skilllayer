"""Tests for ListBranchesWorkflow (build_list_branches_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_list_branches_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestNotGitRepo:
    def test_returns_explicit_error(self, tmp_path):
        result = build_list_branches_artifacts(tmp_path)
        assert result["error_code"] == "not_git_repo"
        assert result["workflow"] == "ListBranchesWorkflow"
        assert result["branches"] == []
        assert result["total_count"] == 0

    def test_checked_at_present(self, tmp_path):
        result = build_list_branches_artifacts(tmp_path)
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestBranchStructure:
    def test_returns_correct_keys(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        assert "branches" in result
        assert "current_branch" in result
        assert "total_count" in result
        assert "checked_at" in result
        assert result["workflow"] == "ListBranchesWorkflow"

    def test_current_branch_detected(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        assert result["current_branch"] == "main"

    def test_total_count_matches_branches(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        assert result["total_count"] == len(result["branches"])

    def test_branch_has_required_fields(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        branch = result["branches"][0]
        assert "name" in branch
        assert "current" in branch
        assert "remote" in branch
        assert "last_commit_hash" in branch
        assert "last_commit_message" in branch
        assert "last_commit_date" in branch
        assert "ahead" in branch
        assert "behind" in branch

    def test_current_flag_set_on_checked_out_branch(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        current = [b for b in result["branches"] if b["current"]]
        assert len(current) == 1
        assert current[0]["name"] == "main"

    def test_local_branch_not_marked_remote(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        local = [b for b in result["branches"] if not b["remote"]]
        assert len(local) >= 1
        assert local[0]["remote"] is False


# ---------------------------------------------------------------------------
# Multiple branches
# ---------------------------------------------------------------------------

class TestMultipleBranches:
    def test_new_branch_appears(self, tmp_path):
        repo = _make_repo(tmp_path)
        _git(["checkout", "-b", "feature-x"], repo)
        result = build_list_branches_artifacts(repo)
        names = [b["name"] for b in result["branches"]]
        assert "main" in names
        assert "feature-x" in names

    def test_ahead_behind_are_ints_for_non_default_branch(self, tmp_path):
        repo = _make_repo(tmp_path)
        _git(["checkout", "-b", "feature-y"], repo)
        (repo / "extra.txt").write_text("extra")
        _git(["add", "."], repo)
        _git(["commit", "-m", "extra commit"], repo)
        result = build_list_branches_artifacts(repo)
        feature = next(b for b in result["branches"] if b["name"] == "feature-y")
        assert feature["ahead"] == 1
        assert feature["behind"] == 0

    def test_default_branch_ahead_behind_null(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_list_branches_artifacts(repo)
        main_b = next(b for b in result["branches"] if b["name"] == "main")
        assert main_b["ahead"] is None
        assert main_b["behind"] is None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_list_branches(self):
        assert self._route("list branches") == "list_branches"

    def test_what_branches_exist(self):
        assert self._route("what branches exist") == "list_branches"

    def test_show_all_branches(self):
        assert self._route("show all branches") == "list_branches"

    def test_what_branch_am_i_on(self):
        assert self._route("what branch am I on") == "list_branches"

    def test_list_all_git_branches(self):
        assert self._route("list all git branches") == "list_branches"
