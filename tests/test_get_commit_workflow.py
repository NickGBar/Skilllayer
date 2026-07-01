"""Tests for GetCommitDetailsWorkflow (build_get_commit_artifacts)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_get_commit_artifacts


def _git(args: list[str], cwd: Path, capture: bool = True) -> str:
    r = subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=capture, text=True)
    return r.stdout.strip() if capture else ""


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello world")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit\n\nLonger description here."], repo)
    commit_hash = _git(["rev-parse", "HEAD"], repo)
    return repo, commit_hash


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestNotFound:
    def test_invalid_hash_returns_explicit_error(self, tmp_path):
        repo, _ = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, "deadbeef123")
        assert result["error_code"] == "commit_not_found"
        assert result["workflow"] == "GetCommitDetailsWorkflow"
        assert result["hash"] is None

    def test_not_git_repo_returns_error(self, tmp_path):
        result = build_get_commit_artifacts(tmp_path, "abc1234")
        assert result["error_code"] == "commit_not_found"

    def test_error_message_contains_hash(self, tmp_path):
        repo, _ = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, "badhash99")
        assert "badhash99" in result["error"]


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestCommitStructure:
    def test_returns_full_hash(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40[:7])
        assert result["hash"] == hash40
        assert len(result["hash"]) == 40

    def test_short_hash_is_7(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40[:7])
        assert len(result["short_hash"]) == 7

    def test_message_is_full_body(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert "initial commit" in result["message"]
        assert "Longer description" in result["message"]

    def test_author_name_present(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert result["author_name"] == "Test User"

    def test_no_email_in_result(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert "@" not in result.get("author_name", "")
        assert "author_email" not in result
        # Confirm email not in files_changed either
        for f in result.get("files_changed", []):
            for v in f.values():
                if isinstance(v, str):
                    assert "@" not in v

    def test_date_is_iso8601(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert "T" in result["date"]

    def test_files_changed_list(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert isinstance(result["files_changed"], list)
        assert len(result["files_changed"]) >= 1
        fc = result["files_changed"][0]
        assert "path" in fc
        assert "status" in fc
        assert "insertions" in fc
        assert "deletions" in fc

    def test_parent_hashes_for_initial_is_empty(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert result["parent_hashes"] == []

    def test_parent_hash_for_second_commit(self, tmp_path):
        repo, first_hash = _make_repo(tmp_path)
        (repo / "extra.txt").write_text("x")
        _git(["add", "."], repo)
        _git(["commit", "-m", "second"], repo)
        second_hash = _git(["rev-parse", "HEAD"], repo)
        result = build_get_commit_artifacts(repo, second_hash)
        assert len(result["parent_hashes"]) == 1
        assert result["parent_hashes"][0] == first_hash[:7]

    def test_insertions_nonnegative(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert result["total_insertions"] >= 0
        assert result["total_deletions"] >= 0

    def test_workflow_name(self, tmp_path):
        repo, hash40 = _make_repo(tmp_path)
        result = build_get_commit_artifacts(repo, hash40)
        assert result["workflow"] == "GetCommitDetailsWorkflow"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_show_commit_hash(self):
        assert self._route("show commit abc1234") == "get_commit"

    def test_what_changed_in_commit(self):
        assert self._route("what changed in commit abc1234f") == "get_commit"

    def test_get_commit_details(self):
        assert self._route("get commit details") == "get_commit"

    def test_show_me_commit(self):
        assert self._route("show me commit deadbeef") == "get_commit"

    def test_what_did_commit_do(self):
        assert self._route("what did commit abc1234 do") == "get_commit"
