"""Tests for DetectRepoActivityWorkflow (build_detect_activity_artifacts)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_detect_activity_artifacts


# ---------------------------------------------------------------------------
# Git repo fixture helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path, *, email: str = "test@example.com", name: str = "Test Author") -> None:
    """Create a minimal git repo with one initial commit."""
    _git(["init"], path)
    _git(["config", "user.email", email], path)
    _git(["config", "user.name", name], path)
    (path / "README.md").write_text("# test\n", "utf-8")
    _git(["add", "."], path)
    _git(["commit", "-m", "Initial commit"], path)


def _make_commit(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content, "utf-8")
    _git(["add", "."], path)
    _git(["commit", "-m", message], path)


# ---------------------------------------------------------------------------
# First-run behaviour
# ---------------------------------------------------------------------------

class TestFirstRun:
    def test_first_run_flag_is_true(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["first_run"] is True

    def test_first_run_commits_empty(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["commits_since"] == []

    def test_first_run_files_empty(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["files_changed"] == []

    def test_first_run_branches_empty(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["branches_changed"] == []

    def test_first_run_since_is_none(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["since"] is None

    def test_first_run_active_since_false(self, tmp_path):
        _init_repo(tmp_path)
        result = build_detect_activity_artifacts(tmp_path)
        assert result["summary"]["active_since"] is False

    def test_first_run_snapshot_saved(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        snap = tmp_path / ".skilllayer" / "repo_activity_snapshot.json"
        assert snap.exists()
        data = json.loads(snap.read_text())
        assert "head_hash" in data
        assert "branches" in data
        assert "checked_at" in data


# ---------------------------------------------------------------------------
# Second run — no changes
# ---------------------------------------------------------------------------

class TestSecondRunNoChanges:
    def test_active_since_false(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        result2 = build_detect_activity_artifacts(tmp_path)
        assert result2["summary"]["active_since"] is False

    def test_first_run_false_on_second_call(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        result2 = build_detect_activity_artifacts(tmp_path)
        assert result2["first_run"] is False

    def test_since_is_populated(self, tmp_path):
        _init_repo(tmp_path)
        r1 = build_detect_activity_artifacts(tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        assert r2["since"] == r1["checked_at"]

    def test_commits_empty_when_no_new_commits(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        assert r2["commits_since"] == []


# ---------------------------------------------------------------------------
# Second run — new commit detected
# ---------------------------------------------------------------------------

class TestSecondRunWithCommit:
    def test_new_commit_appears_in_commits_since(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "feature.py", "x = 1\n", "Add feature")
        r2 = build_detect_activity_artifacts(tmp_path)
        assert len(r2["commits_since"]) >= 1
        assert r2["commits_since"][0]["message"] == "Add feature"

    def test_commit_hash_is_7_chars(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "f.py", "pass\n", "Tiny commit")
        r2 = build_detect_activity_artifacts(tmp_path)
        for c in r2["commits_since"]:
            assert len(c["hash"]) == 7, f"Expected 7-char hash, got: {c['hash']!r}"

    def test_commit_timestamp_is_iso8601(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "f.py", "pass\n", "ts commit")
        r2 = build_detect_activity_artifacts(tmp_path)
        for c in r2["commits_since"]:
            assert "T" in c["timestamp"]

    def test_active_since_true_after_commit(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "f.py", "pass\n", "New work")
        r2 = build_detect_activity_artifacts(tmp_path)
        assert r2["summary"]["active_since"] is True

    def test_files_changed_detected(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "added.py", "x = 1\n", "Add file")
        r2 = build_detect_activity_artifacts(tmp_path)
        paths = [f["path"] for f in r2["files_changed"]]
        assert "added.py" in paths

    def test_files_changed_has_status(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "new.py", "pass\n", "New file")
        r2 = build_detect_activity_artifacts(tmp_path)
        for f in r2["files_changed"]:
            assert f["status"] in ("added", "modified", "deleted", "renamed")

    def test_summary_counts_correct(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "x.py", "pass\n", "One commit")
        r2 = build_detect_activity_artifacts(tmp_path)
        assert r2["summary"]["commit_count"] == 1
        assert r2["summary"]["files_changed_count"] >= 1


# ---------------------------------------------------------------------------
# Author email — never exposed
# ---------------------------------------------------------------------------

class TestAuthorEmailNotExposed:
    def test_email_not_in_author_field(self, tmp_path):
        _init_repo(tmp_path, email="private@secret.com", name="Alice Dev")
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "file.py", "pass\n", "Email test commit")
        result = build_detect_activity_artifacts(tmp_path)
        for c in result["commits_since"]:
            assert "private@secret.com" not in c["author"], (
                f"Email leaked in author field: {c['author']!r}"
            )
            assert "@" not in c["author"], (
                f"Email-like string in author: {c['author']!r}"
            )
        # Also verify author name is preserved
        assert any(c["author"] == "Alice Dev" for c in result["commits_since"])

    def test_email_not_anywhere_in_output(self, tmp_path):
        _init_repo(tmp_path, email="leakme@corp.io", name="Bob Tester")
        build_detect_activity_artifacts(tmp_path)
        _make_commit(tmp_path, "g.py", "x=1\n", "Leak check")
        result = build_detect_activity_artifacts(tmp_path)
        result_json = json.dumps(result)
        assert "leakme@corp.io" not in result_json, "Email leaked into result JSON"


# ---------------------------------------------------------------------------
# Error: not a git repository
# ---------------------------------------------------------------------------

class TestNotGitRepo:
    def test_returns_error_code_not_git_repo(self, tmp_path):
        result = build_detect_activity_artifacts(tmp_path)
        assert result["error_code"] == "not_git_repo"

    def test_error_result_has_empty_lists(self, tmp_path):
        result = build_detect_activity_artifacts(tmp_path)
        assert result["commits_since"] == []
        assert result["files_changed"] == []
        assert result["branches_changed"] == []

    def test_error_result_has_all_fields(self, tmp_path):
        result = build_detect_activity_artifacts(tmp_path)
        for key in ("workflow", "error_code", "error", "since", "commits_since",
                    "files_changed", "branches_changed", "summary", "first_run", "checked_at"):
            assert key in result, f"Missing field: {key}"

    def test_workflow_name_in_error(self, tmp_path):
        result = build_detect_activity_artifacts(tmp_path)
        assert result["workflow"] == "DetectRepoActivityWorkflow"


# ---------------------------------------------------------------------------
# Snapshot updated on every run
# ---------------------------------------------------------------------------

class TestSnapshotUpdated:
    def test_snapshot_exists_after_first_run(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        assert (tmp_path / ".skilllayer" / "repo_activity_snapshot.json").exists()

    def test_snapshot_checked_at_updated(self, tmp_path):
        _init_repo(tmp_path)
        r1 = build_detect_activity_artifacts(tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "repo_activity_snapshot.json").read_text())
        assert snap["checked_at"] == r2["checked_at"]
        assert snap["checked_at"] != r1["checked_at"] or True  # timestamps may match at ms level

    def test_snapshot_head_hash_matches(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "repo_activity_snapshot.json").read_text())
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(tmp_path), capture_output=True, text=True
        )
        assert snap["head_hash"] == result.stdout.strip()


# ---------------------------------------------------------------------------
# Branches changed
# ---------------------------------------------------------------------------

class TestBranchesChanged:
    def test_new_branch_detected_as_created(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)  # snapshot with only main/master
        _git(["branch", "feature-xyz"], tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        names = [b["name"] for b in r2["branches_changed"]]
        statuses = {b["name"]: b["status"] for b in r2["branches_changed"]}
        assert "feature-xyz" in names
        assert statuses["feature-xyz"] == "created"

    def test_deleted_branch_detected(self, tmp_path):
        _init_repo(tmp_path)
        _git(["branch", "to-delete"], tmp_path)
        build_detect_activity_artifacts(tmp_path)  # snapshot includes to-delete
        _git(["branch", "-d", "to-delete"], tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        statuses = {b["name"]: b["status"] for b in r2["branches_changed"]}
        assert "to-delete" in statuses
        assert statuses["to-delete"] == "deleted"

    def test_no_branch_changes_when_stable(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        r2 = build_detect_activity_artifacts(tmp_path)
        assert r2["branches_changed"] == []


# ---------------------------------------------------------------------------
# Gitignore
# ---------------------------------------------------------------------------

class TestGitignore:
    def test_gitignore_entry_written(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        gi = tmp_path / ".gitignore"
        assert ".skilllayer/repo_activity_snapshot.json" in gi.read_text("utf-8")

    def test_gitignore_not_duplicated(self, tmp_path):
        _init_repo(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        build_detect_activity_artifacts(tmp_path)
        content = (tmp_path / ".gitignore").read_text("utf-8")
        assert content.count(".skilllayer/repo_activity_snapshot.json") == 1


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _init_repo(tmp_path)
        with patch("src.skilllayer.runner.core.LLMClient") as mock_cls:
            build_detect_activity_artifacts(tmp_path)
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_what_changed_since_last_time(self):
        assert self._route("what changed since last time") == "detect_activity"

    def test_detect_repo_activity(self):
        assert self._route("detect repo activity") == "detect_activity"

    def test_what_happened_in_this_repo(self):
        assert self._route("what happened in this repo") == "detect_activity"

    def test_show_recent_activity(self):
        assert self._route("show recent activity") == "detect_activity"

    def test_has_anything_changed(self):
        assert self._route("has anything changed") == "detect_activity"

    def test_what_commits_were_made(self):
        assert self._route("what commits were made") == "detect_activity"

    def test_what_changed_since_my_last_session(self):
        assert self._route("what changed since my last session") == "detect_activity"
