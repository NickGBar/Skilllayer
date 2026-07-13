"""Tests for the SafeCodeChangeWorkflow professional skill (build_safe_change_artifacts).

Two-phase orchestration: phase="plan" inspects/searches/plans without writing
anything; phase="validate" inspects the diff the host agent produced and runs
tests. SkillLayer never edits files itself here — executed_by is always
"host_agent". Covers: clean vs. dirty repo, symbol found vs. not found,
passing vs. failing tests, incomplete validation, no hidden writes, non-git
repo, and MCP/CLI parity.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skilllayer.mcp_server import skilllayer_safe_change
from skilllayer.runner.core import build_safe_change_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    _git(["init"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    return tmp_path


def _passing_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from app import greet\n\ndef test_greet():\n    assert greet('a') == 'hello a'\n"
    )
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


class TestPlanPhase:
    def test_clean_repo_ready_to_implement(self, tmp_path):
        repo = _passing_repo(tmp_path)
        r = build_safe_change_artifacts(repo, "add a farewell function")
        assert r["skill"] == "safe_code_change"
        assert r["phase"] == "plan"
        assert r["verdict"] == "READY_TO_IMPLEMENT"
        assert r["uncommitted_work_present"] is False
        assert r["executed_by"] == "host_agent"
        assert r["changed_files"] == []
        assert r["written_paths"] == []

    def test_dirty_repo_flags_risk(self, tmp_path):
        repo = _passing_repo(tmp_path)
        (repo / "scratch.txt").write_text("wip")
        r = build_safe_change_artifacts(repo, "add a farewell function")
        assert r["uncommitted_work_present"] is True
        assert any("uncommitted" in risk for risk in r["risks"])

    def test_relevant_symbol_found(self, tmp_path):
        repo = _passing_repo(tmp_path)
        r = build_safe_change_artifacts(repo, "modify the `greet` function to accept a title")
        assert "greet" in r["relevant_symbols"]
        assert "app.py" in r["relevant_files"]

    def test_no_relevant_symbol_found_still_produces_plan(self, tmp_path):
        repo = _passing_repo(tmp_path)
        r = build_safe_change_artifacts(repo, "xyzzynonexistentsymbolabc")
        assert r["relevant_files"] == []
        assert r["relevant_symbols"] == []
        assert r["verdict"] == "READY_TO_IMPLEMENT"
        assert r["proposed_plan"]

    def test_not_git_repo_is_blocked(self, tmp_path):
        r = build_safe_change_artifacts(tmp_path, "add a farewell function")
        assert r["verdict"] == "BLOCKED_BY_REPOSITORY_STATE"
        assert r["success"] is False

    def test_plan_phase_creates_no_files(self, tmp_path):
        repo = _passing_repo(tmp_path)
        build_safe_change_artifacts(repo, "add a farewell function")
        assert not (repo / ".skilllayer").exists()


class TestValidatePhase:
    def test_passing_change_is_validated(self, tmp_path):
        repo = _passing_repo(tmp_path)
        (repo / "app.py").write_text(
            "def greet(name):\n    return f'hello {name}'\n\n\ndef farewell(name):\n    return f'goodbye {name}'\n"
        )
        (repo / "tests" / "test_app.py").write_text(
            "from app import greet, farewell\n\n"
            "def test_greet():\n    assert greet('a') == 'hello a'\n\n"
            "def test_farewell():\n    assert farewell('a') == 'goodbye a'\n"
        )
        r = build_safe_change_artifacts(repo, "add a farewell function", phase="validate")
        assert r["verdict"] == "CHANGE_VALIDATED"
        assert r["tests_run"] is True
        assert r["tests_passed"] is True
        assert set(r["changed_files"]) == {"app.py", "tests/test_app.py"}
        assert r["validation_complete"] is True
        # The dirt at validate time IS the change being validated, not a risk.
        assert r["risks"] == []

    def test_failing_tests_are_validation_failed(self, tmp_path):
        repo = _passing_repo(tmp_path)
        (repo / "app.py").write_text("def greet(name):\n    return f'WRONG {name}'\n")
        r = build_safe_change_artifacts(repo, "break greet", phase="validate")
        assert r["verdict"] == "VALIDATION_FAILED"
        assert r["success"] is False
        assert r["tests_run"] is True
        assert r["tests_passed"] is False

    def test_no_changed_files_is_incomplete(self, tmp_path):
        repo = _passing_repo(tmp_path)
        r = build_safe_change_artifacts(repo, "add a farewell function", phase="validate")
        assert r["verdict"] == "CHANGE_INCOMPLETE"
        assert r["success"] is False

    def test_save_context_discloses_written_paths(self, tmp_path):
        repo = _passing_repo(tmp_path)
        (repo / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n# comment\n")
        r = build_safe_change_artifacts(repo, "add a comment", phase="validate", save_context=True)
        assert r["written_paths"]
        for rel in r["written_paths"]:
            assert (repo / rel).exists()

    def test_validate_without_save_context_writes_nothing(self, tmp_path):
        repo = _passing_repo(tmp_path)
        (repo / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n# comment\n")
        build_safe_change_artifacts(repo, "add a comment", phase="validate")
        assert not (repo / ".skilllayer").exists()

    def test_unsupported_phase(self, tmp_path):
        repo = _passing_repo(tmp_path)
        r = build_safe_change_artifacts(repo, "x", phase="bogus")
        assert r["verdict"] == "UNSUPPORTED"


class TestMcpCliParity:
    def test_mcp_handler_matches_builder(self, tmp_path):
        repo = _passing_repo(tmp_path)
        direct = build_safe_change_artifacts(repo, "add a farewell function")
        via_mcp = skilllayer_safe_change(str(repo), "add a farewell function")
        for key in ("skill", "phase", "verdict", "uncommitted_work_present", "executed_by"):
            assert direct[key] == via_mcp[key]
