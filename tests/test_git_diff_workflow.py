"""Tests for GitDiffWorkflow (build_git_diff_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_git_diff_artifacts


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello world\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_not_git_repo(self, tmp_path):
        result = build_git_diff_artifacts(tmp_path)
        assert result["error_code"] == "not_git_repo"
        assert result["files"] == []

    def test_invalid_base_ref(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_diff_artifacts(repo, base="nonexistent-ref")
        assert result["error_code"] == "ref_not_found"
        assert "nonexistent-ref" in result["error"]

    def test_invalid_target_ref(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_diff_artifacts(repo, base="HEAD", target="nonexistent-ref")
        assert result["error_code"] == "ref_not_found"

    def test_workflow_name(self, tmp_path):
        result = build_git_diff_artifacts(tmp_path)
        assert result["workflow"] == "GitDiffWorkflow"


# ---------------------------------------------------------------------------
# Working tree diff
# ---------------------------------------------------------------------------

class TestWorkingTreeDiff:
    def test_empty_diff_when_clean(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_diff_artifacts(repo, base="HEAD")
        assert result["total_files"] == 0
        assert result["files"] == []

    def test_detects_modified_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("hello world\nmodified\n")
        result = build_git_diff_artifacts(repo, base="HEAD")
        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "README.md"
        assert result["files"][0]["status"] in ("modified", "M")

    def test_detects_new_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "newfile.txt").write_text("new content\n")
        _git(["add", "newfile.txt"], repo)
        result = build_git_diff_artifacts(repo, base="HEAD")
        paths = [f["path"] for f in result["files"]]
        assert "newfile.txt" in paths


# ---------------------------------------------------------------------------
# Ref-to-ref diff
# ---------------------------------------------------------------------------

class TestRefToRefDiff:
    def test_diff_between_two_commits(self, tmp_path):
        repo = _make_repo(tmp_path)
        first_hash = _git(["rev-parse", "HEAD"], repo)
        (repo / "second.txt").write_text("second file\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "second"], repo)
        result = build_git_diff_artifacts(repo, base=first_hash, target="HEAD")
        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "second.txt"

    def test_insertions_counted(self, tmp_path):
        repo = _make_repo(tmp_path)
        first_hash = _git(["rev-parse", "HEAD"], repo)
        (repo / "second.txt").write_text("line1\nline2\nline3\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "second"], repo)
        result = build_git_diff_artifacts(repo, base=first_hash, target="HEAD")
        assert result["total_insertions"] == 3

    def test_deletions_counted(self, tmp_path):
        repo = _make_repo(tmp_path)
        first_hash = _git(["rev-parse", "HEAD"], repo)
        (repo / "README.md").write_text("")
        _git(["add", "."], repo)
        _git(["commit", "-m", "empty readme"], repo)
        result = build_git_diff_artifacts(repo, base=first_hash, target="HEAD")
        assert result["total_deletions"] >= 1


# ---------------------------------------------------------------------------
# diff_preview cap
# ---------------------------------------------------------------------------

class TestPreviewCap:
    def test_diff_preview_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("hello world\nmodified\n")
        result = build_git_diff_artifacts(repo, base="HEAD")
        if result["total_files"] > 0:
            assert "diff_preview" in result["files"][0]

    def test_diff_preview_capped_at_500(self, tmp_path):
        repo = _make_repo(tmp_path)
        big_content = "\n".join(f"line {i}" for i in range(200))
        (repo / "README.md").write_text(big_content)
        result = build_git_diff_artifacts(repo, base="HEAD")
        if result["files"]:
            assert len(result["files"][0]["diff_preview"]) <= 500


# ---------------------------------------------------------------------------
# Path filter
# ---------------------------------------------------------------------------

class TestPathFilter:
    def test_path_filter_limits_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        first_hash = _git(["rev-parse", "HEAD"], repo)
        (repo / "a.txt").write_text("aaa\n")
        (repo / "b.txt").write_text("bbb\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "two files"], repo)
        result = build_git_diff_artifacts(repo, base=first_hash, target="HEAD", path="a.txt")
        paths = [f["path"] for f in result["files"]]
        assert "a.txt" in paths
        assert "b.txt" not in paths


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

class TestStructure:
    def test_truncated_false_for_few_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_diff_artifacts(repo, base="HEAD")
        assert result["truncated"] is False

    def test_checked_at_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = build_git_diff_artifacts(repo, base="HEAD")
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_show_diff(self):
        assert self._route("show diff") == "git_diff"

    def test_git_diff(self):
        assert self._route("git diff") == "git_diff"

    def test_what_files_changed(self):
        assert self._route("what files changed") == "git_diff"

    def test_what_changed_between(self):
        assert self._route("what changed between main and feature") == "git_diff"

    def test_show_me_the_diff(self):
        assert self._route("show me the diff") == "git_diff"

    def test_what_changed_since_routes_to_activity_not_diff(self):
        assert self._route("what changed since last week") != "git_diff"
