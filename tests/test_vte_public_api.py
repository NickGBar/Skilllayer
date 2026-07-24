"""Verified Task Execution — public product surface (Milestone E).

Covers vte_start/vte_status/vte_checkpoint/vte_resume/vte_finalize/
vte_abandon (skilllayer.tasks.public_api), the six registered MCP tools,
the verification receipt, prevented-action records, the confirmation
token flow, and the professional skill catalog entry. All tests use
disposable Git repositories under tmp_path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skilllayer.config.defaults import VERIFIED_TASK_EXECUTION_SKILL
from skilllayer.tasks import public_api as api


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "SkillLayer Test")
    (repo / ".gitignore").write_text(".skilllayer/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
    (repo / "src" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_auth.py").write_text("def test_login(): pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _start(repo: Path, title: str = "fix auth", **kwargs):
    kwargs.setdefault("allowed_paths", ["src/auth.py", "tests/test_auth.py"])
    return api.vte_start(repo, title, persist_consent=True, **kwargs)


# ---------------------------------------------------------------------------
# vte_start
# ---------------------------------------------------------------------------


class TestVteStart:
    def test_requires_explicit_consent(self, tmp_path):
        repo = _repo(tmp_path)
        result = api.vte_start(repo, "fix auth", allowed_paths=["src/auth.py"])
        assert result["status"] == "ERROR"
        assert result["error_code"] == "persist_consent_required"
        assert not (repo / ".skilllayer").exists()

    def test_happy_path_reaches_ready(self, tmp_path):
        repo = _repo(tmp_path)
        result = _start(repo)
        assert result["status"] == "READY", result
        assert result["task_id"]
        assert result["scope"]["allowed_paths"] == ["src/auth.py", "tests/test_auth.py"]
        assert result["baseline"] == "BASELINE_CAPTURED"

    def test_blocked_by_preexisting_repository_state(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "auth.py").write_text("def login(): return True\n", encoding="utf-8")
        result = _start(repo)
        assert result["status"] == "ERROR"
        assert result["error_code"] == "forbidden_preexisting_state"
        assert "next_action" in result and result["next_action"]

    def test_second_start_produces_a_second_distinct_task(self, tmp_path):
        repo = _repo(tmp_path)
        first = _start(repo)
        second = _start(repo, title="a different task")
        assert first["task_id"] != second["task_id"]


# ---------------------------------------------------------------------------
# vte_status
# ---------------------------------------------------------------------------


class TestVteStatus:
    def test_unknown_task_id(self, tmp_path):
        repo = _repo(tmp_path)
        result = api.vte_status(repo, "not-a-real-task-id")
        assert result["status"] == "ERROR"

    def test_known_task_reports_state(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        result = api.vte_status(repo, started["task_id"])
        assert result["status"] == "OK"
        assert result["task_state"] == "RUNNING"
        assert "checkpoint_task" in result["safe_operations"]
        assert result["confirmations_required"] == []

    def test_status_never_writes(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_dir = repo / ".skilllayer" / "tasks" / started["task_id"]
        before = {p: p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()}
        api.vte_status(repo, started["task_id"])
        after = {p: p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()}
        assert before == after


# ---------------------------------------------------------------------------
# vte_checkpoint
# ---------------------------------------------------------------------------


class TestVteCheckpoint:
    def test_normal_checkpoint(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        result = api.vte_checkpoint(
            repo, started["task_id"], "implementing", completed_steps=["read the file"],
            active_step="writing the fix", remaining_steps=["run tests"], files_observed=["src/auth.py"],
        )
        assert result["status"] == "CHECKPOINTED", result
        assert result["sequence"] == 1

    def test_checkpoint_rejected_outside_running_or_validating(self, tmp_path):
        from skilllayer.tasks.persistence import generate_task_id

        repo = _repo(tmp_path)
        task_id = generate_task_id(repo, "no state yet")
        result = api.vte_checkpoint(repo, task_id, "phase")
        assert result["status"] == "ERROR"

    def test_interruption_moves_task_to_interrupted(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "working", completed_steps=["step one"])
        interrupted = api.vte_checkpoint(repo, task_id, "working", interruption_reason="UNKNOWN_INTERRUPTION")
        assert interrupted["status"] == "INTERRUPTED"
        assert api.vte_status(repo, task_id)["task_state"] == "INTERRUPTED"

    def test_invalid_interruption_reason_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        result = api.vte_checkpoint(repo, started["task_id"], "working", interruption_reason="NOT_A_REAL_REASON")
        assert result["status"] == "ERROR"
        assert result["error_code"] == "interruption_reason_invalid"


# ---------------------------------------------------------------------------
# vte_resume
# ---------------------------------------------------------------------------


class TestVteResume:
    def test_resume_requires_confirmation_then_succeeds_with_token(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "working", completed_steps=["step one"])
        api.vte_checkpoint(repo, task_id, "working", interruption_reason="UNKNOWN_INTERRUPTION")

        first = api.vte_resume(repo, task_id)
        assert first["status"] == "CONFIRMATION_REQUIRED", first
        token = first["confirmation_token"]
        assert token

        second = api.vte_resume(repo, task_id, confirmed=True, confirmation_token=token)
        assert second["status"] == "RUNNING", second
        assert api.vte_status(repo, task_id)["task_state"] == "RUNNING"

    def test_confirmation_token_is_single_use(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "working", completed_steps=["step one"])
        api.vte_checkpoint(repo, task_id, "working", interruption_reason="UNKNOWN_INTERRUPTION")
        first = api.vte_resume(repo, task_id)
        token = first["confirmation_token"]
        api.vte_resume(repo, task_id, confirmed=True, confirmation_token=token)

        # Interrupt again, then try to reuse the SAME (already-consumed) token.
        api.vte_checkpoint(repo, task_id, "working", interruption_reason="UNKNOWN_INTERRUPTION")
        reused = api.vte_resume(repo, task_id, confirmed=False, confirmation_token=token)
        # A consumed token must not silently confirm; it must fall back to
        # issuing a fresh confirmation requirement.
        assert reused["status"] == "CONFIRMATION_REQUIRED"
        assert reused["confirmation_token"] != token

    def test_confirmation_token_is_not_transferable_to_another_task(self, tmp_path):
        repo = _repo(tmp_path)
        first_task = _start(repo, title="task one")["task_id"]
        second_task = _start(repo, title="task two", allowed_paths=["src/other.py"])["task_id"]
        api.vte_checkpoint(repo, first_task, "working", interruption_reason="UNKNOWN_INTERRUPTION")
        issued = api.vte_resume(repo, first_task)
        token = issued["confirmation_token"]

        api.vte_checkpoint(repo, second_task, "working", interruption_reason="UNKNOWN_INTERRUPTION")
        cross_task_attempt = api.vte_resume(repo, second_task, confirmed=False, confirmation_token=token)
        assert cross_task_attempt["status"] == "CONFIRMATION_REQUIRED"
        assert cross_task_attempt["confirmation_token"] != token

    def test_stale_baseline_is_caught_at_finalize_even_if_resume_let_it_through(self, tmp_path):
        # Same-owner self-resumption is masked by ownership at the raw
        # assess_resume layer before it ever reaches the staleness check (see
        # VTE_FOUNDATION_D_ORCHESTRATOR.md's documented limitation), so
        # resume_task can legitimately reach RUNNING here. The safety
        # guarantee is that finalize_task re-checks scope/baseline freshness
        # live and independently — nothing is silently skipped end-to-end.
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "working", completed_steps=["step one"])
        api.vte_checkpoint(repo, task_id, "working", interruption_reason="UNKNOWN_INTERRUPTION")

        (repo / "extra.py").write_text("Z = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "unrelated commit advances HEAD")

        resumed = api.vte_resume(repo, task_id, confirmed=True)
        assert resumed["status"] == "RUNNING", resumed

        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        assert result["status"] == "BLOCKED", result
        assert "baseline_stale" in (result["receipt"].get("limitations") or []) or result["receipt"]["final_verdict"] == "TASK_BLOCKED"


# ---------------------------------------------------------------------------
# vte_finalize
# ---------------------------------------------------------------------------


class TestVteFinalize:
    def test_finalize_verified_complete(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        (repo / "src" / "auth.py").write_text("def login(): return True\n", encoding="utf-8")
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["fixed login"])
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True, tests_summary_label="3 passed")
        assert result["status"] == "COMPLETED", result
        assert result["receipt"]["final_verdict"] == "TASK_VERIFIED_COMPLETE"
        assert "Verdict: VERIFIED COMPLETE" in result["receipt_text"]

    def test_false_completion_is_prevented(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        (repo / "src" / "auth.py").write_text("def login(): return True\n", encoding="utf-8")
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["fixed login"])
        result = api.vte_finalize(repo, task_id, tests_recorded=False)
        assert result["status"] == "BLOCKED"
        assert result["receipt"]["final_verdict"] != "TASK_VERIFIED_COMPLETE"
        types = [item["intervention_type"] for item in result["receipt"]["prevented_actions"]]
        assert "FALSE_COMPLETION_PREVENTED" in types

    def test_scope_violation_is_blocked_and_recorded(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        (repo / "src" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")  # out of scope
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["edited"])
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        assert result["status"] == "BLOCKED"
        assert "src/other.py" in result["receipt"]["unexpected_changes"]
        types = [item["intervention_type"] for item in result["receipt"]["prevented_actions"]]
        assert "FORBIDDEN_PATH_CHANGE" in types

    def test_finalize_is_idempotent_on_retry(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        first = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        second = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        assert first["status"] == second["status"] == "COMPLETED"
        assert first["receipt"]["final_verdict"] == second["receipt"]["final_verdict"]


# ---------------------------------------------------------------------------
# vte_abandon
# ---------------------------------------------------------------------------


class TestVteAbandon:
    def test_abandon_from_running(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        result = api.vte_abandon(repo, started["task_id"], reason_label="user cancelled")
        assert result["status"] == "ABANDONED"
        assert api.vte_status(repo, started["task_id"])["task_state"] == "ABANDONED"

    def test_cannot_abandon_a_completed_task(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        result = api.vte_abandon(repo, task_id, reason_label="too late")
        assert result["status"] == "ERROR"


# ---------------------------------------------------------------------------
# MCP tool registration, schema stability, tool count
# ---------------------------------------------------------------------------


class TestMcpIntegration:
    def test_vte_tools_registered_and_counted(self):
        from skilllayer.mcp_server import MCP_TOOL_HANDLERS, mcp_tool_count

        names = {handler.__name__ for handler in MCP_TOOL_HANDLERS}
        expected = {
            "skilllayer_vte_start", "skilllayer_vte_status", "skilllayer_vte_checkpoint",
            "skilllayer_vte_resume", "skilllayer_vte_finalize", "skilllayer_vte_abandon",
        }
        assert expected.issubset(names)
        assert mcp_tool_count() == len(MCP_TOOL_HANDLERS)

    def test_tool_schemas_include_vte_tools_with_descriptions(self):
        from skilllayer.mcp_server import list_tool_schemas

        schemas = {tool["name"]: tool for tool in list_tool_schemas()["tools"]}
        for name in ("skilllayer_vte_start", "skilllayer_vte_status", "skilllayer_vte_checkpoint",
                     "skilllayer_vte_resume", "skilllayer_vte_finalize", "skilllayer_vte_abandon"):
            assert name in schemas
            assert schemas[name]["description"], f"{name} is missing a docstring/description"

    def test_malformed_arguments_return_structured_error_not_traceback(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_vte_start

        result = skilllayer_vte_start(str(tmp_path / "does-not-exist"), "title", persist_consent=True)
        assert result.get("success") is False or result.get("status") == "ERROR"
        assert "Traceback" not in str(result)

    def test_professional_skill_catalog_entry(self):
        from skilllayer.mcp_server import skilllayer_list_skills

        result = skilllayer_list_skills()
        names = [skill["name"] for skill in result["professional_skills"]]
        assert "verified_task_execution" in names
        entry = next(s for s in result["professional_skills"] if s["name"] == "verified_task_execution")
        for required_key in ("purpose", "activation_examples", "non_activation_examples",
                              "required_mcp_tools", "supported_lifecycle", "safety_guarantees",
                              "known_limitations"):
            assert required_key in entry

    def test_diagnostics_reports_verified_task_execution_truthfully(self):
        from skilllayer.diagnostics import build_diagnostics

        report = build_diagnostics(None, {"overall_status": "ok", "success": True}, tool_count=45)
        assert report["professional_skills"]["verified_task_execution"] is True


# ---------------------------------------------------------------------------
# Verification receipt
# ---------------------------------------------------------------------------


class TestReceipt:
    def test_receipt_is_derived_only_from_persisted_evidence(self, tmp_path):
        from skilllayer.tasks.receipt import build_receipt, render_receipt_text

        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)

        receipt = build_receipt(repo, task_id)
        assert receipt["receipt_version"] == 1
        assert receipt["task_id"] == task_id
        assert receipt["skill_name"] == VERIFIED_TASK_EXECUTION_SKILL["name"]
        text = render_receipt_text(receipt)
        assert "Verified Task Execution" in text
        assert "Verdict:" in text
