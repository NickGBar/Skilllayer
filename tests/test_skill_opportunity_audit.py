"""SkillLayer Product G — Skill Opportunity and Adoption Audit.

Covers skilllayer.tasks.skill_audit (classification rules, manual
reinvention detection, recommendation logic, persistence, security) and the
four registered MCP tools. Each test uses a fresh, uniquely-generated
session_id (via new_session_id()) since sessions live in module-level
in-memory state shared across the whole test run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skilllayer.tasks import skill_audit as sa
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
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _new_session(**kwargs) -> str:
    started = sa.start_session(**kwargs)
    assert started["success"], started
    return started["session_id"]


def _event(events: list[dict], capability_id: str) -> dict:
    return next(e for e in events if e["capability_id"] == capability_id)


# ---------------------------------------------------------------------------
# Classification correctness
# ---------------------------------------------------------------------------


class TestClassifications:
    def test_read_only_work_never_claims_vte_opportunity(self):
        # A session with SOME recorded activity (so it isn't the empty/
        # UNKNOWN case) but no file changes — e.g. inspecting history only.
        sid = _new_session()
        sa.record_operation(sid, "GIT_LOG")
        report = sa.build_session_adoption_report(sid)["report"]
        vte = _event(report["opportunities_detected"], "VERIFIED_TASK_EXECUTION")
        assert vte["classification"] == "NOT_APPLICABLE"

    def test_no_observations_is_unknown_not_fabricated(self):
        sid = _new_session()
        result = sa.classify_opportunities(sid)
        assert all(e["classification"] == "UNKNOWN" for e in result["events"])

    def test_vte_used_produces_applicable_and_used(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT", {"is_production_logic": True})
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        sa.record_skilllayer_call(sid, "skilllayer_vte_start")
        report = sa.build_session_adoption_report(sid)["report"]
        vte = _event(report["opportunities_detected"], "VERIFIED_TASK_EXECUTION")
        assert vte["classification"] == "APPLICABLE_AND_USED"

    def test_vte_used_never_also_claims_skipped(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT", {"is_production_logic": True})
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        sa.record_skilllayer_call(sid, "skilllayer_vte_finalize")
        report = sa.build_session_adoption_report(sid)["report"]
        assert not any(e["capability_id"] == "VERIFIED_TASK_EXECUTION" for e in report["applicable_but_skipped"])

    def test_vte_high_confidence_skip_requires_strong_signal(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        report = sa.build_session_adoption_report(sid)["report"]
        vte = _event(report["opportunities_detected"], "VERIFIED_TASK_EXECUTION")
        assert vte["classification"] == "APPLICABLE_BUT_SKIPPED"
        assert vte["confidence"] == "HIGH"

    def test_vte_weak_signal_is_possibly_applicable_not_skipped(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        report = sa.build_session_adoption_report(sid)["report"]
        vte = _event(report["opportunities_detected"], "VERIFIED_TASK_EXECUTION")
        assert vte["classification"] == "POSSIBLY_APPLICABLE"
        assert vte["confidence"] in {"LOW", "MEDIUM"}

    def test_only_high_confidence_may_be_applicable_but_skipped(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "FILE_EDIT")
        report = sa.build_session_adoption_report(sid)["report"]
        for event in report["opportunities_detected"]:
            if event["classification"] == "APPLICABLE_BUT_SKIPPED":
                assert event["confidence"] == "HIGH"

    def test_secret_detection_applicable_on_release_without_review(self):
        sid = _new_session()
        sa.record_operation(sid, "RELEASE_ACTION", {"action_kind": "publish"})
        report = sa.build_session_adoption_report(sid)["report"]
        secret = _event(report["opportunities_detected"], "SECRET_DETECTION")
        assert secret["classification"] == "APPLICABLE_BUT_SKIPPED"

    def test_secret_detection_not_applicable_without_release_activity(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        report = sa.build_session_adoption_report(sid)["report"]
        secret = _event(report["opportunities_detected"], "SECRET_DETECTION")
        assert secret["classification"] == "NOT_APPLICABLE"

    def test_secret_detection_used_when_actually_called(self):
        sid = _new_session()
        sa.record_operation(sid, "RELEASE_ACTION")
        sa.record_skilllayer_call(sid, "skilllayer_detect_secrets")
        report = sa.build_session_adoption_report(sid)["report"]
        secret = _event(report["opportunities_detected"], "SECRET_DETECTION")
        assert secret["classification"] == "APPLICABLE_AND_USED"

    def test_decision_search_applicable_with_many_decisions_and_resume(self):
        sid = _new_session()
        for _ in range(9):
            sa.record_skilllayer_call(sid, "skilllayer_track_decision")
        sa.record_operation(sid, "CONTEXT_RESTORE")
        report = sa.build_session_adoption_report(sid)["report"]
        ds = _event(report["opportunities_detected"], "DECISION_SEARCH")
        assert ds["classification"] == "APPLICABLE_BUT_SKIPPED"
        assert ds["confidence"] == "HIGH"

    def test_decision_search_not_applicable_with_few_decisions(self):
        sid = _new_session()
        sa.record_skilllayer_call(sid, "skilllayer_track_decision")
        report = sa.build_session_adoption_report(sid)["report"]
        ds = _event(report["opportunities_detected"], "DECISION_SEARCH")
        assert ds["classification"] == "NOT_APPLICABLE"

    def test_context_snapshot_compare_applicable(self):
        sid = _new_session()
        sa.record_skilllayer_call(sid, "skilllayer_save_context")
        sa.record_skilllayer_call(sid, "skilllayer_save_context")
        sa.record_operation(sid, "CONTEXT_RESTORE")
        report = sa.build_session_adoption_report(sid)["report"]
        compare = _event(report["opportunities_detected"], "CONTEXT_SNAPSHOT_COMPARE")
        assert compare["classification"] == "APPLICABLE_BUT_SKIPPED"

    def test_missing_capability_classification(self):
        sid = _new_session()
        sa.record_operation(sid, "REMOTE_JOB_SUBMIT")
        sa.record_operation(sid, "REMOTE_JOB_POLL")
        report = sa.build_session_adoption_report(sid)["report"]
        missing = _event(report["opportunities_detected"], "EXTERNAL_ASYNC_JOB_ORCHESTRATION")
        assert missing["classification"] == "CAPABILITY_MISSING"

    def test_missing_capability_not_applicable_without_remote_jobs(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        report = sa.build_session_adoption_report(sid)["report"]
        missing = _event(report["opportunities_detected"], "EXTERNAL_ASYNC_JOB_ORCHESTRATION")
        assert missing["classification"] == "NOT_APPLICABLE"

    def test_simple_capability_used_when_called(self):
        sid = _new_session()
        sa.record_skilllayer_call(sid, "skilllayer_add_todo")
        report = sa.build_session_adoption_report(sid)["report"]
        todo = _event(report["opportunities_detected"], "TODO_MANAGEMENT")
        assert todo["classification"] == "USED"


# ---------------------------------------------------------------------------
# Manual reinvention detection
# ---------------------------------------------------------------------------


class TestManualReinvention:
    def test_vte_manual_reinvention_grouping(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "GIT_DIFF")
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        result = sa.detect_manual_reinventions(sid)
        assert any(r["capability_duplicated"] == "VERIFIED_TASK_EXECUTION" for r in result["reinventions"])

    def test_no_reinvention_claim_when_vte_used(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "GIT_DIFF")
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        sa.record_skilllayer_call(sid, "skilllayer_vte_start")
        result = sa.detect_manual_reinventions(sid)
        assert not any(r["capability_duplicated"] == "VERIFIED_TASK_EXECUTION" for r in result["reinventions"])

    def test_reinvention_entries_have_required_fields(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "GIT_DIFF")
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        result = sa.detect_manual_reinventions(sid)
        for item in result["reinventions"]:
            for key in ("capability_duplicated", "observed_manual_steps", "missing_skilllayer_evidence", "likely_extra_work", "confidence", "limitations"):
                assert key in item


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------


class TestRecommendation:
    def test_exactly_one_or_zero_recommendations(self):
        for setup in (
            lambda sid: None,
            lambda sid: sa.record_operation(sid, "FILE_EDIT"),
            lambda sid: (sa.record_operation(sid, "REMOTE_JOB_SUBMIT"), sa.record_operation(sid, "REMOTE_JOB_POLL")),
        ):
            sid = _new_session()
            setup(sid)
            report = sa.build_session_adoption_report(sid)["report"]
            assert len(report["recommendations"]) in {0, 1}

    def test_missing_capability_takes_priority(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        sa.record_operation(sid, "REMOTE_JOB_SUBMIT")
        sa.record_operation(sid, "REMOTE_JOB_POLL")
        report = sa.build_session_adoption_report(sid)["report"]
        assert "async" in report["recommendations"][0].lower()

    def test_no_recommendation_when_insufficient_evidence(self):
        sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        assert report["recommendations"] == []


# ---------------------------------------------------------------------------
# Natural dogfood / assisted mode
# ---------------------------------------------------------------------------


class TestModes:
    def test_natural_dogfood_never_suggests(self):
        sid = _new_session(mode="NATURAL_DOGFOOD")
        sa.record_operation(sid, "FILE_EDIT", {"is_production_logic": True})
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        result = sa.get_assisted_suggestion(sid)
        assert result["suggestion"] is None
        assert result["reason"] == "not_in_assisted_mode"

    def test_assisted_mode_bounded_single_suggestion(self):
        sid = _new_session(mode="ASSISTED")
        sa.record_operation(sid, "FILE_EDIT", {"is_production_logic": True})
        sa.record_operation(sid, "TEST_RUN", {"completed": True})
        result = sa.get_assisted_suggestion(sid)
        assert result["suggestion"] is not None
        assert set(result["suggestion"]) == {"capability_id", "capability_category", "reason"}

    def test_assisted_mode_no_suggestion_without_high_confidence_skip(self):
        sid = _new_session(mode="ASSISTED")
        result = sa.get_assisted_suggestion(sid)
        assert result["suggestion"] is None

    def test_natural_dogfood_never_writes_or_blocks(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session(mode="NATURAL_DOGFOOD")
        sa.record_operation(sid, "FILE_EDIT")
        assert not (repo / ".skilllayer").exists()


# ---------------------------------------------------------------------------
# Privacy, persistence, security
# ---------------------------------------------------------------------------


class TestPrivacyAndPersistence:
    def test_in_memory_by_default(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        sa.build_session_adoption_report(sid)
        assert not (repo / ".skilllayer").exists()

    def test_persistence_requires_explicit_consent(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        result = sa.write_session_audit(repo, sid, consent=None, report=report, markdown=md)
        assert not result["success"]
        assert not (repo / ".skilllayer" / "session-audits").exists()

    def test_idempotent_persistence(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        consent = grant_task_consent(repo, sid)
        first = sa.write_session_audit(repo, sid, consent=consent, report=report, markdown=md)
        second = sa.write_session_audit(repo, sid, consent=consent, report=report, markdown=md)
        assert first["success"] and first["written_paths"]
        assert second["success"] and second["idempotent"] and second["written_paths"] == []

    def test_conflicting_rewrite_fails_explicitly(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        consent = grant_task_consent(repo, sid)
        sa.write_session_audit(repo, sid, consent=consent, report=report, markdown=md)
        result = sa.write_session_audit(repo, sid, consent=consent, report={**report, "evidence_complete": not report["evidence_complete"]}, markdown=md)
        assert not result["success"] and result["error"] == "conflicting_report_rewrite"

    def test_cross_session_reference_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        other_sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        consent = grant_task_consent(repo, other_sid)
        result = sa.write_session_audit(repo, other_sid, consent=consent, report=report, markdown=md)
        assert not result["success"] and result["error"] == "cross_session_report_reference"

    def test_symlink_escape_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        sid = _new_session()
        audit_dir = repo / ".skilllayer" / "session-audits" / sid
        audit_dir.mkdir(parents=True)
        (audit_dir / "adoption-report.json").symlink_to(tmp_path / "elsewhere.json")
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        consent = grant_task_consent(repo, sid)
        result = sa.write_session_audit(repo, sid, consent=consent, report=report, markdown=md)
        assert not result["success"] and result["error"] == "symlink_not_permitted"

    def test_no_raw_logs_accepted(self):
        sid = _new_session()
        result = sa.record_operation(sid, "FILE_EDIT", {"unrestricted_shell_transcript": "rm -rf /; cat secrets.env"})
        assert not result["success"]
        assert "attribute_not_allowed" in result["error"]

    def test_secret_rejected_in_attributes(self):
        sid = _new_session()
        result = sa.record_operation(sid, "RELEASE_ACTION", {"action_kind": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"})
        assert not result["success"]
        assert "secret" in result["error"]

    def test_private_absolute_path_rejected(self):
        sid = _new_session()
        result = sa.record_operation(sid, "FILE_EDIT", {"path": "/Users/nikolai/private/file.py"})
        assert not result["success"]

    def test_bounded_operation_count(self):
        sid = _new_session()
        for _ in range(sa._MAX_OPERATIONS + 5):
            sa.record_operation(sid, "FILE_EDIT")
        summary = sa.get_session_summary(sid)
        assert summary["operations_recorded"] == sa._MAX_OPERATIONS
        assert summary["truncated_operations"] > 0

    def test_invalid_operation_class_rejected(self):
        sid = _new_session()
        result = sa.record_operation(sid, "NOT_A_REAL_OPERATION")
        assert not result["success"] and result["error"] == "operation_class_invalid"

    def test_unknown_tool_name_rejected(self):
        sid = _new_session()
        result = sa.record_skilllayer_call(sid, "not_a_real_tool")
        assert not result["success"] and result["error"] == "tool_name_unknown"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_deterministic_rendering_byte_identical(self):
        sid = _new_session()
        sa.record_operation(sid, "FILE_EDIT")
        report = sa.build_session_adoption_report(sid)["report"]
        md1 = sa.render_session_adoption_report(report)
        md2 = sa.render_session_adoption_report(report)
        assert md1 == md2

    def test_recommendation_section_always_present(self):
        sid = _new_session()
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        assert "## Recommendation" in md

    def test_bounded_report_size(self):
        sid = _new_session()
        for i in range(50):
            sa.record_operation(sid, "FILE_CREATE", {"path": f"src/file_{i}.py"})
        report = sa.build_session_adoption_report(sid)["report"]
        md = sa.render_session_adoption_report(report)
        assert len(md) < 20_000  # deterministic templates keep this small regardless of op count


# ---------------------------------------------------------------------------
# MCP integration
# ---------------------------------------------------------------------------


class TestMcpIntegration:
    def test_audit_tools_registered_and_counted(self):
        from skilllayer.mcp_server import MCP_TOOL_HANDLERS, mcp_tool_count

        names = {h.__name__ for h in MCP_TOOL_HANDLERS}
        expected = {"skilllayer_audit_record_operation", "skilllayer_audit_session", "skilllayer_audit_status", "skilllayer_audit_reset"}
        assert expected.issubset(names)
        assert mcp_tool_count() == len(MCP_TOOL_HANDLERS)

    def test_tool_schemas_include_audit_tools_with_descriptions(self):
        from skilllayer.mcp_server import list_tool_schemas

        schemas = {tool["name"]: tool for tool in list_tool_schemas()["tools"]}
        for name in ("skilllayer_audit_record_operation", "skilllayer_audit_session", "skilllayer_audit_status", "skilllayer_audit_reset"):
            assert name in schemas and schemas[name]["description"]

    def test_status_never_writes(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_audit_record_operation, skilllayer_audit_status

        repo = _repo(tmp_path)
        sid = _new_session()
        skilllayer_audit_record_operation(str(repo), sid, operation="FILE_EDIT")
        skilllayer_audit_status(str(repo), sid)
        assert not (repo / ".skilllayer").exists()

    def test_exactly_one_of_operation_or_tool_name_enforced(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_audit_record_operation

        repo = _repo(tmp_path)
        sid = _new_session()
        neither = skilllayer_audit_record_operation(str(repo), sid)
        assert not neither["success"]
        both = skilllayer_audit_record_operation(str(repo), sid, operation="FILE_EDIT", skilllayer_tool_name="skilllayer_add_todo")
        assert not both["success"]

    def test_reset_scoped_to_one_session(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_audit_record_operation, skilllayer_audit_reset, skilllayer_audit_status

        repo = _repo(tmp_path)
        sid1, sid2 = _new_session(), _new_session()
        skilllayer_audit_record_operation(str(repo), sid1, operation="FILE_EDIT")
        skilllayer_audit_record_operation(str(repo), sid2, operation="FILE_EDIT")
        skilllayer_audit_reset(str(repo), sid1)
        assert not skilllayer_audit_status(str(repo), sid1)["success"]
        assert skilllayer_audit_status(str(repo), sid2)["success"]
