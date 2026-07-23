"""Tests for the ResumeProjectWorkWorkflow professional skill
(build_resume_work_artifacts).

Reconstructs project state for a brand-new session purely from saved
.skilllayer/ memory: rehydrate + validate_memory + git status + activity
drift. Always read-only unless confirm_update=True is passed with new_state.
Covers: healthy saved context, no context, corrupt memory, repository drift,
uncommitted files, unknown unsaved-work status, no automatic overwrite, and
MCP/CLI parity.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skilllayer.mcp_server import skilllayer_resume_work
from skilllayer.memory.skilllayer_memory import track_decision
from skilllayer.runner.core import (
    build_detect_activity_artifacts,
    build_resume_work_artifacts,
    build_save_context_artifacts,
)

_STRUCTURED_STATE = (
    "PURPOSE: A sample project.\n"
    "OBJECTIVE: Ship the thing.\n"
    "CONSTRAINTS: Don't break prod.\n"
    "COMPLETED: Wrote the code.\n"
    "NEXT STEP: Write the tests."
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    _git(["init"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "readme.txt").write_text("hello")
    _git(["add", "-A"], cwd=tmp_path)
    _git(["commit", "-m", "initial"], cwd=tmp_path)
    return tmp_path


class TestResumeWork:
    def test_no_saved_context(self, tmp_path):
        repo = _init_repo(tmp_path)
        r = build_resume_work_artifacts(repo)
        assert r["verdict"] == "NO_SAVED_CONTEXT"
        assert r["written_paths"] == []
        assert r["uncertainty"]

    def test_healthy_structured_context_ready_to_continue(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        r = build_resume_work_artifacts(repo)
        assert r["verdict"] == "READY_TO_CONTINUE"
        assert r["last_objective"] == "Ship the thing."
        assert r["constraints"] == "Don't break prod."
        assert r["completed_work"] == "Wrote the code."
        assert r["remembered_next_action"] == "Write the tests."
        assert r["memory_health"]["status"] == "healthy"

    def test_unstructured_context_is_incomplete_but_returns_raw_text(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, "just some free text about the project", [])
        r = build_resume_work_artifacts(repo)
        assert r["verdict"] == "CONTEXT_INCOMPLETE"
        assert r["project_summary"] == "just some free text about the project"
        assert any("not in the" in u for u in r["uncertainty"])

    def test_corrupt_memory_is_unhealthy(self, tmp_path):
        repo = _init_repo(tmp_path)
        sk = repo / ".skilllayer"
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="9999")  # dangling -> broken
        r = build_resume_work_artifacts(repo)
        assert r["verdict"] == "MEMORY_UNHEALTHY"
        assert r["success"] is False

    def test_repository_drift_detected(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        build_detect_activity_artifacts(repo, persist_snapshot=True)  # baseline
        (repo / "new_file.txt").write_text("new")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "second commit"], cwd=repo)
        r = build_resume_work_artifacts(repo)
        assert r["verdict"] == "READY_WITH_REPOSITORY_DRIFT"
        assert r["detected_drift"]["first_run"] is False
        assert r["detected_drift"]["commit_count"] >= 1

    def test_commit_after_context_save_is_not_treated_as_current_without_activity_baseline(
        self,
        tmp_path,
        monkeypatch,
    ):
        repo = _init_repo(tmp_path)
        monkeypatch.setattr(
            "skilllayer.memory.skilllayer_memory._utc_iso",
            lambda: "2000-01-01T00:00:00Z",
        )
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        (repo / "new_file.txt").write_text("new")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "second commit"], cwd=repo)

        r = build_resume_work_artifacts(repo)

        assert r["verdict"] == "READY_WITH_REPOSITORY_DRIFT"
        assert r["detected_drift"]["commit_after_context_save"] is True

    def test_uncommitted_files_reflected_in_unfinished_work(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        (repo / "scratch.txt").write_text("wip")
        r = build_resume_work_artifacts(repo)
        assert r["current_repository_state"]["untracked_count"] >= 1
        assert r["unfinished_work"]["untracked_git_files"] >= 1

    def test_unknown_unsaved_work_status_is_disclosed(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        r = build_resume_work_artifacts(repo)
        assert r["unfinished_work"]["needs_wrap_status"] == "unknown"
        assert any("unknown" in u.lower() for u in r["uncertainty"])

    def test_no_automatic_overwrite_without_confirm(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        r = build_resume_work_artifacts(repo, new_state="SHOULD NOT BE SAVED")
        assert r["written_paths"] == []
        latest = (repo / ".skilllayer" / "context" / "latest.md").read_text()
        assert "SHOULD NOT BE SAVED" not in latest

    def test_explicit_confirm_update_writes(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        r = build_resume_work_artifacts(repo, confirm_update=True, new_state="UPDATED STATE")
        assert r["written_paths"]
        latest = (repo / ".skilllayer" / "context" / "latest.md").read_text()
        assert "UPDATED STATE" in latest


class TestMcpCliParity:
    def test_mcp_handler_matches_builder(self, tmp_path):
        repo = _init_repo(tmp_path)
        build_save_context_artifacts(repo, _STRUCTURED_STATE, [])
        direct = build_resume_work_artifacts(repo)
        via_mcp = skilllayer_resume_work(str(repo))
        for key in ("skill", "verdict", "last_objective", "remembered_next_action"):
            assert direct[key] == via_mcp[key]
