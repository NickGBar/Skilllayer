"""Tests for FindMergeConflictsWorkflow (build_find_conflicts_artifacts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import build_find_conflicts_artifacts


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    return p


CONFLICT_TEXT = """\
<<<<<<< HEAD
print("ours")
=======
print("theirs")
>>>>>>> feature-branch
"""


# ---------------------------------------------------------------------------
# Clean repo
# ---------------------------------------------------------------------------

class TestCleanRepo:
    def test_clean_true_when_no_conflicts(self, tmp_path):
        _write(tmp_path, "main.py", "x = 1\ny = 2\n")
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["clean"] is True
        assert result["total_files_with_conflicts"] == 0
        assert result["total_conflict_sections"] == 0
        assert result["conflicts"] == []

    def test_workflow_name(self, tmp_path):
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["workflow"] == "FindMergeConflictsWorkflow"

    def test_checked_at_present(self, tmp_path):
        result = build_find_conflicts_artifacts(tmp_path)
        assert "T" in result["checked_at"]

    def test_empty_directory(self, tmp_path):
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["clean"] is True


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

class TestConflictDetection:
    def test_detects_one_conflict(self, tmp_path):
        _write(tmp_path, "main.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["clean"] is False
        assert result["total_files_with_conflicts"] == 1
        assert result["total_conflict_sections"] == 1

    def test_conflict_entry_has_required_fields(self, tmp_path):
        _write(tmp_path, "main.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        conflict = result["conflicts"][0]
        assert "file" in conflict
        assert "conflict_count" in conflict
        assert "sections" in conflict

    def test_section_has_line_numbers(self, tmp_path):
        _write(tmp_path, "main.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        section = result["conflicts"][0]["sections"][0]
        assert "start_line" in section
        assert "separator_line" in section
        assert "end_line" in section
        assert section["start_line"] == 1
        assert section["end_line"] == 5

    def test_multiple_conflicts_in_one_file(self, tmp_path):
        content = CONFLICT_TEXT + "\n" + CONFLICT_TEXT
        _write(tmp_path, "main.py", content)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["conflicts"][0]["conflict_count"] == 2
        assert result["total_conflict_sections"] == 2

    def test_multiple_files_with_conflicts(self, tmp_path):
        _write(tmp_path, "a.py", CONFLICT_TEXT)
        _write(tmp_path, "b.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["total_files_with_conflicts"] == 2
        assert result["total_conflict_sections"] == 2

    def test_conflict_file_path_relative(self, tmp_path):
        _write(tmp_path, "src/main.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["conflicts"][0]["file"] == "src/main.py"


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------

class TestIgnoreRules:
    def test_skips_git_directory(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        _write(tmp_path / ".git", "MERGE_HEAD", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["total_files_with_conflicts"] == 0

    def test_skips_venv_directory(self, tmp_path):
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        _write(tmp_path / ".venv", "conflict.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["total_files_with_conflicts"] == 0

    def test_skips_node_modules(self, tmp_path):
        nm_dir = tmp_path / "node_modules"
        nm_dir.mkdir()
        _write(tmp_path / "node_modules", "conflict.js", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["total_files_with_conflicts"] == 0

    def test_clean_file_with_similar_text(self, tmp_path):
        _write(tmp_path, "main.py", "# <<<<<< not a conflict marker\n")
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["clean"] is True

    def test_counts_conflict_correctly_not_false_positive(self, tmp_path):
        # Only "<<<<<<< " with trailing space is a conflict marker
        _write(tmp_path, "a.py", "x = '<<<<<<< this is a string'\n")
        _write(tmp_path, "b.py", CONFLICT_TEXT)
        result = build_find_conflicts_artifacts(tmp_path)
        assert result["total_files_with_conflicts"] == 1


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_find_merge_conflicts(self):
        assert self._route("find merge conflicts") == "find_conflicts"

    def test_are_there_conflicts(self):
        assert self._route("are there any conflicts") == "find_conflicts"

    def test_check_for_merge_conflicts(self):
        assert self._route("check for merge conflicts") == "find_conflicts"

    def test_do_i_have_conflicts(self):
        assert self._route("do I have conflicts") == "find_conflicts"

    def test_show_conflicts(self):
        assert self._route("show conflicts") == "find_conflicts"

    def test_merge_conflicts(self):
        assert self._route("merge conflicts") == "find_conflicts"
