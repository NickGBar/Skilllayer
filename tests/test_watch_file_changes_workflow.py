"""Tests for WatchFileChangesWorkflow (build_watch_file_changes_artifacts)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import build_watch_file_changes_artifacts as _build_watch_file_changes_artifacts


def build_watch_file_changes_artifacts(repo: Path, scope: str | None = None) -> dict:
    """Existing snapshot-diff cases opt in explicitly to snapshot persistence."""
    return _build_watch_file_changes_artifacts(repo, scope=scope, persist_snapshot=True)


# ---------------------------------------------------------------------------
# First watch
# ---------------------------------------------------------------------------

class TestFirstWatch:
    def test_first_watch_flag_is_true(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["first_watch"] is True

    def test_first_watch_diff_lists_empty(self, tmp_path):
        # No prior baseline exists yet, so there is nothing to diff against —
        # everything found is the new baseline, not a reported "change".
        (tmp_path / "foo.py").write_text("print(1)\n")
        (tmp_path / "bar.py").write_text("print(2)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["added"] == []
        assert result["modified"] == []
        assert result["deleted"] == []

    def test_first_watch_scanned_file_count(self, tmp_path):
        # No .gitignore is silently created; only the two user files are scanned.
        (tmp_path / "foo.py").write_text("print(1)\n")
        (tmp_path / "bar.py").write_text("print(2)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["summary"]["scanned_file_count"] == 2

    def test_first_watch_snapshot_saved(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        snap = tmp_path / ".skilllayer" / "file_watch_snapshot.json"
        assert snap.exists()
        data = json.loads(snap.read_text())
        assert "files" in data
        assert "foo.py" in data["files"]
        assert data["files"]["foo.py"]["hash"].startswith("sha256:")
        assert "size" in data["files"]["foo.py"]
        assert "mtime" in data["files"]["foo.py"]

    def test_previous_recorded_at_none_on_first_watch(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["previous_recorded_at"] is None


# ---------------------------------------------------------------------------
# Second watch — added / modified / deleted
# ---------------------------------------------------------------------------

class TestSecondWatchAdded:
    def test_new_file_detected(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        (tmp_path / "new_module.py").write_text("print(2)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["first_watch"] is False
        assert [a["path"] for a in result["added"]] == ["new_module.py"]
        assert result["added"][0]["size"] == len("print(2)\n")
        assert result["summary"]["added_count"] == 1


class TestSecondWatchModified:
    def test_content_change_detected(self, tmp_path):
        f = tmp_path / "foo.py"
        f.write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        f.write_text("print(1)\nprint(2)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert [m["path"] for m in result["modified"]] == ["foo.py"]
        entry = result["modified"][0]
        assert entry["size_before"] == len("print(1)\n")
        assert entry["size_after"] == len("print(1)\nprint(2)\n")
        assert entry["size_delta"] == entry["size_after"] - entry["size_before"]
        assert "mtime" in entry

    def test_unchanged_file_not_reported(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        (tmp_path / "bar.py").write_text("print(2)\n")
        build_watch_file_changes_artifacts(tmp_path)
        # bar.py untouched between watches
        (tmp_path / "foo.py").write_text("print(99)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        modified_paths = {m["path"] for m in result["modified"]}
        assert "bar.py" not in modified_paths
        assert "foo.py" in modified_paths


class TestSecondWatchDeleted:
    def test_removed_file_detected(self, tmp_path):
        f = tmp_path / "gone.py"
        f.write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        f.unlink()
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["deleted"] == [{"path": "gone.py"}]
        assert result["summary"]["deleted_count"] == 1

    def test_no_false_positive_when_nothing_removed(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["deleted"] == []


# ---------------------------------------------------------------------------
# Dual-filter scope: should_ignore_search_path + repo .gitignore
# ---------------------------------------------------------------------------

class TestDualFilterScope:
    def test_should_ignore_search_path_dirs_excluded(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "foo.cpython-311.pyc").write_bytes(b"junk")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "pkg.js").write_text("module.exports = {}\n")

        result = build_watch_file_changes_artifacts(tmp_path)
        # Only foo.py is scanned; no .gitignore is created.
        assert result["summary"]["scanned_file_count"] == 1

        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        assert set(snap["files"].keys()) == {"foo.py"}

    def test_gitignore_patterns_excluded(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        (tmp_path / "secret.log").write_text("do not track me\n")
        (tmp_path / ".gitignore").write_text("*.log\n")

        result = build_watch_file_changes_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        assert "secret.log" not in snap["files"]
        assert "foo.py" in snap["files"]
        # .gitignore itself is ordinary tracked content, not excluded by its own rules.
        assert ".gitignore" in snap["files"]

    def test_new_gitignore_entry_causes_apparent_deletion(self, tmp_path):
        # A file that stops being watched because it was newly gitignored is
        # reported as "deleted" from the watch's perspective, even though it
        # still physically exists on disk — this is expected, not a bug: the
        # snapshot only ever reflects what's currently in scope.
        (tmp_path / "foo.py").write_text("print(1)\n")
        (tmp_path / "build_artifact.log").write_text("output\n")
        build_watch_file_changes_artifacts(tmp_path)
        (tmp_path / ".gitignore").write_text("*.log\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        deleted_paths = {d["path"] for d in result["deleted"]}
        assert "build_artifact.log" in deleted_paths
        assert (tmp_path / "build_artifact.log").exists()  # still on disk

    def test_both_filters_combined_on_whole_repo_scan(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('app')\n")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "app.pyc").write_bytes(b"junk")
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "bundle.js").write_text("bundled\n")
        (tmp_path / ".gitignore").write_text("dist/\n")

        result = build_watch_file_changes_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        # src/app.py and .gitignore survive both filters; __pycache__ is
        # caught by should_ignore_search_path, dist/ by the repo's own
        # .gitignore.
        assert set(snap["files"].keys()) == {"src/app.py", ".gitignore"}
        assert result["summary"]["scanned_file_count"] == 2


# ---------------------------------------------------------------------------
# 1MB size cutoff
# ---------------------------------------------------------------------------

class TestSizeCutoff:
    def test_large_file_skipped(self, tmp_path):
        small = tmp_path / "small.py"
        small.write_text("print(1)\n")
        large = tmp_path / "large.bin"
        large.write_bytes(b"x" * (1_048_576 + 1))
        result = build_watch_file_changes_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        assert "large.bin" not in snap["files"]
        assert "small.py" in snap["files"]

    def test_file_at_exact_limit_included(self, tmp_path):
        at_limit = tmp_path / "at_limit.bin"
        at_limit.write_bytes(b"x" * 1_048_576)
        result = build_watch_file_changes_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        assert "at_limit.bin" in snap["files"]


# ---------------------------------------------------------------------------
# sha256 content hashing correctness
# ---------------------------------------------------------------------------

class TestHashing:
    def test_hash_matches_real_sha256(self, tmp_path):
        import hashlib
        content = b"exact content to hash\n"
        (tmp_path / "foo.py").write_bytes(content)
        result = build_watch_file_changes_artifacts(tmp_path)
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
        assert snap["files"]["foo.py"]["hash"] == expected

    def test_same_content_same_hash_not_modified(self, tmp_path):
        # Rewriting a file with byte-identical content must not show up as
        # "modified" — only a real content change should.
        f = tmp_path / "foo.py"
        f.write_text("print(1)\n")
        build_watch_file_changes_artifacts(tmp_path)
        f.write_text("print(1)\n")  # identical content, new mtime
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["modified"] == []


# ---------------------------------------------------------------------------
# Scope parameter
# ---------------------------------------------------------------------------

class TestScope:
    def test_scope_narrows_to_subdirectory(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('app')\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("docs\n")
        result = build_watch_file_changes_artifacts(tmp_path, scope="src")
        assert result["scope"] == "src"
        snap = json.loads((tmp_path / ".skilllayer" / "file_watch_snapshot.json").read_text())
        assert set(snap["files"].keys()) == {"src/app.py"}

    def test_scope_escape_guard(self, tmp_path):
        result = build_watch_file_changes_artifacts(tmp_path, scope="../../etc")
        assert result["error_code"] == "scope_escapes_repo"

    def test_scope_not_found(self, tmp_path):
        result = build_watch_file_changes_artifacts(tmp_path, scope="does_not_exist")
        assert result["error_code"] == "scope_not_found"

    def test_default_scope_is_whole_repo(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert result["scope"] == "."


# ---------------------------------------------------------------------------
# Snapshot persistence / self-gitignoring
# ---------------------------------------------------------------------------

class TestSnapshotPersistence:
    def test_gitignore_not_created(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        assert not (tmp_path / ".gitignore").exists()
        assert result["gitignore_suggestions"] == [".skilllayer/file_watch_snapshot.json"]

    def test_existing_gitignore_not_modified(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.tmp\n", "utf-8")
        build_watch_file_changes_artifacts(tmp_path)
        build_watch_file_changes_artifacts(tmp_path)
        assert gitignore.read_text("utf-8") == "*.tmp\n"

    def test_recorded_at_advances_between_watches(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        r1 = build_watch_file_changes_artifacts(tmp_path)
        r2 = build_watch_file_changes_artifacts(tmp_path)
        assert r2["previous_recorded_at"] == r1["recorded_at"]


# ---------------------------------------------------------------------------
# Output never includes file content
# ---------------------------------------------------------------------------

class TestNoContentLeakage:
    def test_added_entry_has_no_content_field(self, tmp_path):
        build_watch_file_changes_artifacts(tmp_path)
        (tmp_path / "secret.py").write_text("PASSWORD = 'hunter2'\n")
        result = build_watch_file_changes_artifacts(tmp_path)
        added = result["added"][0]
        assert set(added.keys()) == {"path", "size"}
        assert "hunter2" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        (tmp_path / "foo.py").write_text("print(1)\n")
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_watch_file_changes_artifacts(tmp_path)
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_watch_for_file_changes(self):
        assert self._route("watch for file changes") == "watch_file_changes"

    def test_uncommitted_changes(self):
        assert self._route("show me uncommitted changes") == "watch_file_changes"

    def test_untracked_files(self):
        assert self._route("list untracked files") == "watch_file_changes"

    def test_detect_file_changes(self):
        assert self._route("detect file changes") == "watch_file_changes"

    def test_does_not_shadow_git_diff(self):
        # "what files changed" (no "uncommitted"/"untracked"/"watch"/"detect"
        # framing) is deliberately left to GitDiffWorkflow, which already
        # claimed this exact phrasing before this workflow existed.
        assert self._route("what files changed since I last looked") == "git_diff"

    def test_does_not_shadow_detect_activity(self):
        assert self._route("what changed since last commit") == "detect_activity"
        assert self._route("has anything changed in this repo") == "detect_activity"


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

class TestMCPTool:
    def test_tool_registered_in_schema_list(self):
        from skilllayer.mcp_server import list_tool_schemas
        names = [t["name"] for t in list_tool_schemas()["tools"]]
        assert "skilllayer_watch_file_changes" in names

    def test_tool_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_watch_file_changes
        (tmp_path / "foo.py").write_text("print(1)\n")
        result = skilllayer_watch_file_changes(str(tmp_path))
        assert result["success"] is True
        assert result["workflow"] == "WatchFileChangesWorkflow"
        assert result["llm_calls"] == 0


# ---------------------------------------------------------------------------
# Workflow stability / metadata
# ---------------------------------------------------------------------------

class TestWorkflowMetadata:
    def test_stability_is_stable(self):
        from skilllayer.config.defaults import WORKFLOW_METADATA
        assert WORKFLOW_METADATA["WatchFileChangesWorkflow"]["stability"] == "stable"

    def test_macro_sequence_registered(self):
        from skilllayer.config.defaults import WORKFLOWS
        assert "WatchFileChangesWorkflow" in WORKFLOWS
        assert len(WORKFLOWS["WatchFileChangesWorkflow"]) > 0
