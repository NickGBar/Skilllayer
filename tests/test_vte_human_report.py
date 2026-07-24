"""Verified Task Execution — deterministic human-readable task report
(Milestone F: skilllayer.tasks.human_report).

Covers the report schema/mapping in isolation (synthetic receipts, for
precise control over each verdict/blocker combination), persistence
semantics, consistency validation, security, and integration with
vte_finalize/vte_status end-to-end against disposable git repositories.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skilllayer.tasks import public_api as api
from skilllayer.tasks.human_report import (
    MAX_MARKDOWN_CHARS,
    HumanReportError,
    build_human_report,
    read_human_report,
    render_human_report_markdown,
    render_human_report_text,
    validate_report_consistency,
    write_human_report,
)
from skilllayer.tasks.persistence import grant_task_consent


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


def _base_receipt(**overrides) -> dict:
    receipt = {
        "receipt_version": 1, "task_id": "20260724T000000Z-demo-task-aaaaaaaa",
        "objective_label": "demo task", "final_verdict": "TASK_VERIFIED_COMPLETE",
        "task_state": "COMPLETED", "baseline_status": "BASELINE_CAPTURED", "scope_status": "SCOPE_CLEAN",
        "resume_status": None, "changed_paths": ["src/auth.py"], "allowed_changes": ["src/auth.py"],
        "unexpected_changes": [], "tests_summary": {"reported_recorded": True, "passed": True, "summary_label": "3 passed"},
        "checkpoints_created": 1, "interruptions_recovered": 0, "prevented_actions": [], "confirmations_required": [],
        "evidence_complete": True, "limitations": [], "created_at": "2026-07-24T00:00:00Z", "blockers": [],
    }
    receipt.update(overrides)
    return receipt


# ---------------------------------------------------------------------------
# Deterministic mapping (synthetic receipts)
# ---------------------------------------------------------------------------


class TestDeterministicMapping:
    def test_verified_success(self):
        report = build_human_report(_base_receipt())
        assert report["overall_status"] == "SUCCESS"
        assert report["failed_items"] == []
        assert report["blocked_items"] == []
        assert report["unknown_items"] == []
        assert report["succeeded_items"]

    def test_success_with_limitations(self):
        receipt = _base_receipt(final_verdict="TASK_COMPLETE_WITH_LIMITATIONS", limitations=["collection_limitations_present"])
        report = build_human_report(receipt)
        assert report["overall_status"] == "SUCCESS_WITH_LIMITATIONS"
        assert report["limitations"]

    def test_partial_completion(self):
        receipt = _base_receipt(final_verdict="TASK_INCOMPLETE", evidence_complete=False)
        report = build_human_report(receipt)
        assert report["overall_status"] == "PARTIAL"
        assert report["problem_summary"]

    def test_blocked_scope(self):
        receipt = _base_receipt(
            final_verdict="TASK_BLOCKED", scope_status="SCOPE_VIOLATED",
            unexpected_changes=["src/other.py"], changed_paths=["src/other.py"], allowed_changes=[],
            blockers=["unexpected_path:src/other.py"],
            prevented_actions=[{
                "intervention_id": "iv-1", "intervention_type": "FORBIDDEN_PATH_CHANGE",
                "prevented_outcome": "Finalization was blocked; the task was not marked complete.",
            }],
        )
        report = build_human_report(receipt)
        assert report["overall_status"] == "BLOCKED"
        assert "outside the approved task scope" in report["problem_summary"]
        assert report["next_action"]

    def test_failed_tests(self):
        receipt = _base_receipt(
            final_verdict="TASK_BLOCKED", tests_summary={"reported_recorded": True, "passed": False, "summary_label": "2 failed"},
        )
        report = build_human_report(receipt)
        assert report["overall_status"] == "FAILED"
        assert report["failed_items"]
        assert "Fix the failing test" in report["next_action"]

    def test_unknown_test_result(self):
        receipt = _base_receipt(
            final_verdict="TASK_BLOCKED", tests_summary={"reported_recorded": True, "passed": None},
            prevented_actions=[{
                "intervention_id": "iv-2", "intervention_type": "UNKNOWN_TEST_RESULT",
                "prevented_outcome": "Finalization was blocked; the task was not marked complete.",
            }],
        )
        report = build_human_report(receipt)
        assert report["overall_status"] == "BLOCKED"
        assert any("no completed result was recorded" in item for item in report["unknown_items"])
        assert not any("no completed result" in item for item in report["failed_items"])

    def test_stale_baseline(self):
        receipt = _base_receipt(final_verdict="TASK_BLOCKED", blockers=["baseline_stale"])
        report = build_human_report(receipt)
        assert report["overall_status"] == "BLOCKED"
        assert "changed after" in report["problem_summary"]

    def test_ownership_conflict(self):
        receipt = _base_receipt(
            final_verdict="TASK_BLOCKED",
            prevented_actions=[{
                "intervention_id": "iv-3", "intervention_type": "OWNERSHIP_CONFLICT",
                "prevented_outcome": "The task was not resumed into RUNNING.",
            }],
        )
        report = build_human_report(receipt)
        assert "active task owner" in report["problem_summary"]

    def test_failed_verdict(self):
        report = build_human_report(_base_receipt(final_verdict="TASK_FAILED"))
        assert report["overall_status"] == "FAILED"

    def test_abandoned_verdict(self):
        report = build_human_report(_base_receipt(final_verdict="TASK_ABANDONED"))
        assert report["overall_status"] == "ABANDONED"

    def test_not_yet_finalized_is_unknown(self):
        report = build_human_report(_base_receipt(final_verdict=None, task_state="RUNNING"))
        assert report["overall_status"] == "UNKNOWN"

    def test_never_maps_incomplete_evidence_to_success(self):
        receipt = _base_receipt(final_verdict="TASK_INCOMPLETE", evidence_complete=False)
        report = build_human_report(receipt)
        assert report["overall_status"] != "SUCCESS"


# ---------------------------------------------------------------------------
# What succeeded / did not succeed
# ---------------------------------------------------------------------------


class TestSucceededAndNotSucceeded:
    def test_unknown_never_labeled_failed(self):
        receipt = _base_receipt(final_verdict="TASK_BLOCKED", tests_summary={"reported_recorded": True, "passed": None})
        report = build_human_report(receipt)
        assert not report["failed_items"]
        assert report["unknown_items"]

    def test_not_run_never_labeled_passed(self):
        receipt = _base_receipt(final_verdict="TASK_BLOCKED", tests_summary={"reported_recorded": False})
        report = build_human_report(receipt)
        assert not any("test" in item.lower() and "passed" in item.lower() for item in report["succeeded_items"])

    def test_success_has_no_not_succeeded_items(self):
        report = build_human_report(_base_receipt())
        assert not report["failed_items"] and not report["blocked_items"] and not report["unknown_items"]


# ---------------------------------------------------------------------------
# Next action
# ---------------------------------------------------------------------------


class TestNextAction:
    def test_success_next_action_is_fixed_sentence(self):
        report = build_human_report(_base_receipt())
        assert report["next_action"] == "No further verified action is required."

    @pytest.mark.parametrize("verdict", ["TASK_INCOMPLETE", "TASK_BLOCKED", "TASK_FAILED", "TASK_ABANDONED"])
    def test_non_success_always_has_exactly_one_next_action(self, verdict):
        report = build_human_report(_base_receipt(final_verdict=verdict, evidence_complete=False))
        assert isinstance(report["next_action"], str) and report["next_action"]

    def test_never_recommends_unsafe_reset_actions(self):
        for verdict in ("TASK_BLOCKED", "TASK_INCOMPLETE", "TASK_FAILED", "TASK_ABANDONED"):
            report = build_human_report(_base_receipt(final_verdict=verdict, evidence_complete=False))
            lowered = report["next_action"].lower()
            for forbidden in ("git reset", "git checkout", "git stash", "rollback"):
                assert forbidden not in lowered


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def test_empty_sections_omitted(self):
        report = build_human_report(_base_receipt())
        md = render_human_report_markdown(report)
        assert "## Problem" not in md
        assert "## Prevented by SkillLayer" not in md
        assert "## Limitations" not in md
        assert "## What did not succeed" not in md

    def test_required_sections_always_present(self):
        report = build_human_report(_base_receipt())
        md = render_human_report_markdown(report)
        for required in ("## Result", "## Next action", "## Verdict"):
            assert required in md

    def test_deterministic_repeated_rendering_is_byte_identical(self):
        report = build_human_report(_base_receipt())
        md1 = render_human_report_markdown(report)
        md2 = render_human_report_markdown(report)
        assert md1 == md2

    def test_text_rendering_has_no_markdown_syntax(self):
        report = build_human_report(_base_receipt())
        text = render_human_report_text(report)
        assert "##" not in text and "**" not in text

    def test_bounded_output_within_max_chars(self):
        huge = _base_receipt(changed_paths=[f"file_{i}.py" for i in range(500)], allowed_changes=[f"file_{i}.py" for i in range(500)])
        report = build_human_report(huge)
        md = render_human_report_markdown(report)
        assert len(md) <= MAX_MARKDOWN_CHARS
        assert "## Next action" in md and "## Verdict" in md

    def test_unbounded_prose_fields_are_still_truncated(self):
        receipt = _base_receipt(limitations=["x" * 5000 for _ in range(10)])
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        assert len(md) <= MAX_MARKDOWN_CHARS
        assert "## Verdict" in md


# ---------------------------------------------------------------------------
# Consistency validation — fail closed
# ---------------------------------------------------------------------------


class TestConsistencyValidation:
    def test_valid_report_passes(self):
        receipt = _base_receipt()
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        assert validate_report_consistency(report, receipt, md)["valid"]

    def test_task_id_mismatch_fails_closed(self):
        receipt = _base_receipt()
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        report = {**report, "task_id": "different-task-id"}
        result = validate_report_consistency(report, receipt, md)
        assert not result["valid"] and "task_id_mismatch" in result["errors"]

    def test_verdict_mismatch_fails_closed(self):
        receipt = _base_receipt()
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        report = {**report, "final_verdict": "TASK_FAILED"}
        result = validate_report_consistency(report, receipt, md)
        assert not result["valid"]

    def test_success_cannot_coexist_with_blocked_items(self):
        receipt = _base_receipt()
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        report = {**report, "blocked_items": ["fabricated blocker"]}
        result = validate_report_consistency(report, receipt, md)
        assert not result["valid"] and "success_coexists_with_non_success_items" in result["errors"]

    def test_blocked_without_problem_fails_closed(self):
        receipt = _base_receipt(final_verdict="TASK_BLOCKED", blockers=["baseline_stale"])
        report = build_human_report(receipt)
        md = render_human_report_markdown(report)
        report = {**report, "overall_status": "BLOCKED", "problem_summary": None}
        result = validate_report_consistency(report, receipt, md)
        assert not result["valid"] and "blocked_missing_problem_or_next_action" in result["errors"]


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_unsupported_receipt_version_rejected(self):
        with pytest.raises(HumanReportError):
            build_human_report(_base_receipt(receipt_version=2))

    def test_unsupported_locale_rejected(self):
        with pytest.raises(HumanReportError):
            build_human_report(_base_receipt(), locale="fr")

    def test_symlink_report_path_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        task_dir = repo / ".skilllayer" / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "report.json").symlink_to(tmp_path / "elsewhere.json")
        consent = grant_task_consent(repo, task_id)
        result = write_human_report(repo, task_id, consent=consent, report={"task_id": task_id}, markdown="x\n")
        assert not result["success"]
        assert result["error"] == "symlink_not_permitted"

    def test_cross_task_report_reference_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        consent = grant_task_consent(repo, task_id)
        other_report = {"report_version": 1, "task_id": "some-other-task"}
        result = write_human_report(repo, task_id, consent=consent, report=other_report, markdown="x\n")
        assert not result["success"] and result["error"] == "cross_task_report_reference"

    def test_no_consent_no_write(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        result = write_human_report(repo, task_id, consent=None, report={"task_id": task_id}, markdown="x\n")
        assert not result["success"]
        assert not (repo / ".skilllayer" / "tasks" / task_id / "report.json").exists()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_idempotent_identical_rewrite(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        consent = grant_task_consent(repo, task_id)
        report = {"report_version": 1, "task_id": task_id, "x": 1}
        first = write_human_report(repo, task_id, consent=consent, report=report, markdown="md\n")
        second = write_human_report(repo, task_id, consent=consent, report=report, markdown="md\n")
        assert first["success"] and first["written_paths"]
        assert second["success"] and second.get("idempotent") is True and second["written_paths"] == []

    def test_conflicting_rewrite_fails_explicitly(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        consent = grant_task_consent(repo, task_id)
        write_human_report(repo, task_id, consent=consent, report={"report_version": 1, "task_id": task_id, "x": 1}, markdown="md1\n")
        result = write_human_report(repo, task_id, consent=consent, report={"report_version": 1, "task_id": task_id, "x": 2}, markdown="md2\n")
        assert not result["success"] and result["error"] == "conflicting_report_rewrite"

    def test_read_human_report_roundtrip(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        consent = grant_task_consent(repo, task_id)
        report = {"report_version": 1, "task_id": task_id, "overall_status": "SUCCESS"}
        write_human_report(repo, task_id, consent=consent, report=report, markdown="md\n")
        assert read_human_report(repo, task_id) == report

    def test_read_missing_report_returns_none(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        assert read_human_report(repo, started["task_id"]) is None


# ---------------------------------------------------------------------------
# Integration: vte_finalize / vte_status (end-to-end)
# ---------------------------------------------------------------------------


class TestFinalizeIntegration:
    def test_finalize_returns_report_and_persists_it(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        (repo / "src" / "auth.py").write_text("def login(): return True\n", encoding="utf-8")
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["fixed"])
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True, tests_summary_label="3 passed")
        assert result["status"] == "COMPLETED"
        assert result["human_report"]["overall_status"] == "SUCCESS"
        assert "Verified Task Report" in result["human_report_markdown"]
        assert result["report_paths"]
        for rel in result["report_paths"]:
            assert (repo / rel).exists()

    def test_finalize_report_present_even_when_blocked(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        result = api.vte_finalize(repo, task_id, tests_recorded=False)
        assert result["status"] == "BLOCKED"
        assert result["human_report"] is not None
        assert result["human_report"]["overall_status"] != "SUCCESS"
        assert result["human_report"]["next_action"]

    def test_repeated_finalize_report_persistence_is_idempotent(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        first = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        second = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        assert first["report_paths"]
        assert second["report_paths"] == []  # idempotent: nothing rewritten

    def test_unsupported_locale_rejected_explicitly(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True, locale="fr")
        assert result["status"] == "ERROR" and result["error_code"] == "unsupported_locale"

    def test_persist_report_false_does_not_write(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True, persist_report=False)
        assert result["human_report"] is not None
        assert result["report_paths"] == []
        assert not (repo / ".skilllayer" / "tasks" / task_id / "report.json").exists()

    def test_interrupted_resumed_task_shows_recovery(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "work", completed_steps=["step1"])
        api.vte_checkpoint(repo, task_id, "work", interruption_reason="UNKNOWN_INTERRUPTION")
        resumed = api.vte_resume(repo, task_id)
        api.vte_resume(repo, task_id, confirmed=True, confirmation_token=resumed["confirmation_token"])
        api.vte_checkpoint(repo, task_id, "work", completed_steps=["step2"])
        result = api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        assert result["human_report"]["overall_status"] == "SUCCESS"
        assert any("safely resumed" in item for item in result["human_report"]["succeeded_items"])


class TestStatusIntegration:
    def test_status_never_writes_a_report(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_status(repo, task_id)
        assert not (repo / ".skilllayer" / "tasks" / task_id / "report.json").exists()

    def test_status_preview_none_before_finalize(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        result = api.vte_status(repo, started["task_id"])
        assert result["report_preview"] is None

    def test_status_preview_reflects_persisted_report(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        status = api.vte_status(repo, task_id)
        assert status["report_preview"]["overall_status"] == "SUCCESS"
        assert status["report_preview"]["final_verdict"] == "TASK_VERIFIED_COMPLETE"

    def test_status_never_mutates_files(self, tmp_path):
        repo = _repo(tmp_path)
        started = _start(repo)
        task_id = started["task_id"]
        api.vte_checkpoint(repo, task_id, "done", completed_steps=["noop"])
        api.vte_finalize(repo, task_id, tests_recorded=True, tests_passed=True)
        task_dir = repo / ".skilllayer" / "tasks" / task_id
        before = {p: p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()}
        api.vte_status(repo, task_id)
        after = {p: p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()}
        assert before == after


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


class TestMcpIntegration:
    def test_finalize_handler_returns_human_report_markdown(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_vte_finalize, skilllayer_vte_start, skilllayer_vte_checkpoint

        repo = _repo(tmp_path)
        started = skilllayer_vte_start(str(repo), "mcp test", persist_consent=True, allowed_paths=["src/auth.py"])
        task_id = started["task_id"]
        skilllayer_vte_checkpoint(str(repo), task_id, "done", completed_steps=["noop"])
        result = skilllayer_vte_finalize(str(repo), task_id, tests_recorded=True, tests_passed=True)
        assert "human_report_markdown" in result
        assert "Verified Task Report" in result["human_report_markdown"]

    def test_tool_count_unchanged_by_milestone_f(self):
        from skilllayer.mcp_server import mcp_tool_count

        assert mcp_tool_count() == 45
