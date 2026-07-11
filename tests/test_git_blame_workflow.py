"""Tests for GitBlameWorkflow (build_git_blame_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_git_blame_artifacts


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    content = "\n".join(f"line {i}" for i in range(1, 11))  # 10 lines
    (repo / "main.py").write_text(content + "\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "add main.py"], repo)
    return repo


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_not_git_repo(self, tmp_path):
        result = build_git_blame_artifacts(tmp_path, "main.py")
        assert result["error_code"] == "not_git_repo"
        assert result["lines"] == []

    def test_file_not_found_explicit_error(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "nonexistent.py")
        assert result["error_code"] == "file_not_found"
        assert result["lines"] == []

    def test_workflow_name(self, tmp_path):
        result = build_git_blame_artifacts(tmp_path, "x.py")
        assert result["workflow"] == "GitBlameWorkflow"

    def test_file_in_result_even_on_error(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "nonexistent.py")
        assert result["file"] == "nonexistent.py"


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestBlameStructure:
    def test_returns_lines(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        assert result["total_lines"] == 10
        assert len(result["lines"]) == 10

    def test_line_has_required_fields(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        ln = result["lines"][0]
        assert "line_number" in ln
        assert "content" in ln
        assert "commit_hash" in ln
        assert "author_name" in ln
        assert "date" in ln
        assert "summary" in ln

    def test_no_email_in_any_field(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        for ln in result["lines"]:
            for k, v in ln.items():
                if isinstance(v, str):
                    assert "@" not in v, f"Email found in field {k}: {v}"
        assert "unique_authors" in result
        for a in result["unique_authors"]:
            assert "@" not in a

    def test_commit_hash_is_7_chars(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        assert len(result["lines"][0]["commit_hash"]) == 7

    def test_line_numbers_are_sequential(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        line_nums = [ln["line_number"] for ln in result["lines"]]
        assert line_nums == list(range(1, 11))

    def test_unique_authors_list(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        assert isinstance(result["unique_authors"], list)
        assert "Test User" in result["unique_authors"]

    def test_date_range_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        dr = result["date_range"]
        assert "earliest" in dr
        assert "latest" in dr
        assert dr["earliest"] is not None

    def test_truncated_false_for_10_lines(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        assert result["truncated"] is False

    def test_checked_at_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py")
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Line range
# ---------------------------------------------------------------------------

class TestLineRange:
    def test_start_and_end_line(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py", start_line=2, end_line=5)
        assert result["total_lines"] == 4
        nums = [ln["line_number"] for ln in result["lines"]]
        assert nums == [2, 3, 4, 5]

    def test_start_line_only(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_blame_artifacts(repo, "main.py", start_line=8)
        # start=8 with +200 cap — returns lines 8-10
        nums = [ln["line_number"] for ln in result["lines"]]
        assert 8 in nums


# ---------------------------------------------------------------------------
# 200-line cap
# ---------------------------------------------------------------------------

class TestLineCap:
    def test_cap_at_200_lines(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Create a 250-line file
        content = "\n".join(f"line {i}" for i in range(1, 251))
        (repo / "big.py").write_text(content + "\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "big file"], repo)
        result = build_git_blame_artifacts(repo, "big.py")
        assert result["total_lines"] <= 200
        assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_git_blame(self):
        assert self._route("git blame main.py") == "git_blame"

    def test_blame(self):
        assert self._route("blame main.py") == "git_blame"

    def test_who_wrote(self):
        assert self._route("who wrote this file") == "git_blame"

    def test_who_is_responsible(self):
        assert self._route("who is responsible for this code") == "git_blame"

    def test_who_wrote_line(self):
        assert self._route("who wrote line 42 in main.py") == "git_blame"
