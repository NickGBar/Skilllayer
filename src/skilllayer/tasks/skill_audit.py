"""SkillLayer Product G — Skill Opportunity and Adoption Audit.

Local, deterministic observability over which SkillLayer capabilities a
session actually used, plausibly could have used, or had no capability for
at all. This module never inspects arbitrary agent activity, never invokes
a skill on the caller's behalf, never blocks an ordinary tool, and never
sends telemetry. It only classifies *explicit, bounded, normalized-enum*
observations the host chooses to report via ``record_operation``/
``record_skilllayer_call`` — it cannot see anything the host doesn't report.

Purely additive: no changes to persistence.py/baseline.py/checkpoint.py/
resume.py/scope.py/orchestrator.py/public_api.py/receipt.py/
interventions.py/human_report.py. Reuses Foundation A's consent/atomic-
write/lock/path-confinement/redaction primitives directly for the optional
persistence path.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .persistence import FieldPolicy, TaskConsent, _check_consent, _rel, sanitize_persisted_value

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

OPERATION_CLASSES = frozenset({
    "FILE_EDIT", "FILE_CREATE", "FILE_DELETE", "GIT_STATUS", "GIT_DIFF", "GIT_LOG",
    "TEST_RUN", "SECRET_REVIEW", "REMOTE_JOB_SUBMIT", "REMOTE_JOB_POLL",
    "CONTEXT_RESTORE", "DECISION_RECORD", "TODO_UPDATE", "RELEASE_ACTION",
})

CLASSIFICATIONS = frozenset({
    "USED", "APPLICABLE_AND_USED", "APPLICABLE_BUT_SKIPPED", "POSSIBLY_APPLICABLE",
    "NOT_APPLICABLE", "CAPABILITY_MISSING", "UNKNOWN",
})
# Only a HIGH-confidence rule may ever produce this classification.
_STRONG_SKIP_CLASSIFICATION = "APPLICABLE_BUT_SKIPPED"

CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW"})

MODES = frozenset({"NATURAL_DOGFOOD", "ASSISTED"})

_MAX_OPERATIONS = 500
_MAX_SKILLLAYER_CALLS = 200
_MAX_EVIDENCE_CODES = 16
_MAX_LABEL = 160
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REPO_RELATIVE_PATH_RE = re.compile(r"[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]{0,199}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillAuditError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def new_session_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise SkillAuditError("session_id_invalid")


# ---------------------------------------------------------------------------
# Capability registry — deterministic, no hypothetical functionality.
# ---------------------------------------------------------------------------

CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "VERIFIED_TASK_EXECUTION": {
        "display_name": "Verified Task Execution",
        "related_mcp_tools": [
            "skilllayer_vte_start", "skilllayer_vte_status", "skilllayer_vte_checkpoint",
            "skilllayer_vte_resume", "skilllayer_vte_finalize", "skilllayer_vte_abandon",
        ],
        "related_professional_skill": "verified_task_execution",
        "applicability_signals": [
            "one or more repository files were modified",
            "tests or validation were run",
            "more than one file was changed",
            "production logic was modified",
            "work crossed a session interruption",
        ],
        "exclusion_signals": ["no repository files were modified (read-only session)"],
        "manual_equivalents": ["FILE_EDIT + GIT_DIFF + TEST_RUN without any vte_* call"],
        "expected_unique_value": "Bounded scope, checkpointed progress, safe resume, evidence-derived completion verdict.",
        "maturity_status": "AVAILABLE",
    },
    "DECISION_TRACKING": {
        "display_name": "Decision Tracking",
        "related_mcp_tools": ["skilllayer_track_decision"],
        "related_professional_skill": None,
        "applicability_signals": ["always applicable when a durable decision is made"],
        "exclusion_signals": [],
        "manual_equivalents": ["decision only stated in conversation, never recorded"],
        "expected_unique_value": "Durable, searchable record of why a choice was made.",
        "maturity_status": "AVAILABLE",
    },
    "CONTEXT_SAVE_RESUME": {
        "display_name": "Context Save / Resume",
        "related_mcp_tools": ["skilllayer_save_context", "skilllayer_resume_work", "skilllayer_rehydrate_context"],
        "related_professional_skill": "resume_project_work",
        "applicability_signals": ["session ended or was interrupted with unfinished work"],
        "exclusion_signals": [],
        "manual_equivalents": ["rereading many files to reconstruct prior state"],
        "expected_unique_value": "Structured cross-session handoff instead of re-deriving context from scratch.",
        "maturity_status": "AVAILABLE",
    },
    "TODO_MANAGEMENT": {
        "display_name": "Todo Management",
        "related_mcp_tools": ["skilllayer_add_todo", "skilllayer_mark_todo_done", "skilllayer_list_todos"],
        "related_professional_skill": None,
        "applicability_signals": ["a discrete follow-up action was identified but not done immediately"],
        "exclusion_signals": [],
        "manual_equivalents": ["follow-up only mentioned in conversation, never tracked"],
        "expected_unique_value": "Durable, independently-tracked action items across sessions.",
        "maturity_status": "AVAILABLE",
    },
    "DECISION_SEARCH": {
        "display_name": "Decision Search",
        "related_mcp_tools": ["skilllayer_search_decisions"],
        "related_professional_skill": None,
        "applicability_signals": [
            "multiple decisions exist",
            "work resumed after context compaction/interruption",
            "a later decision references an earlier frozen constraint",
        ],
        "exclusion_signals": ["fewer than 2 decisions recorded"],
        "manual_equivalents": ["rereading decision history files manually"],
        "expected_unique_value": "Direct retrieval of a relevant prior decision instead of rereading everything.",
        "maturity_status": "AVAILABLE",
    },
    "CONTEXT_SNAPSHOT_COMPARE": {
        "display_name": "Context Snapshot Compare",
        "related_mcp_tools": ["skilllayer_compare_context_snapshots"],
        "related_professional_skill": None,
        "applicability_signals": ["at least two context snapshots exist", "a resume or drift investigation occurred"],
        "exclusion_signals": ["fewer than 2 context snapshots"],
        "manual_equivalents": ["manually diffing two saved context notes"],
        "expected_unique_value": "Structured diff of state/open-questions between two snapshots.",
        "maturity_status": "AVAILABLE",
    },
    "SCOPED_GIT_INSPECTION": {
        "display_name": "Scoped Git Inspection",
        "related_mcp_tools": [
            "skilllayer_git_diff", "skilllayer_git_log", "skilllayer_git_blame",
            "skilllayer_file_history", "skilllayer_find_conflicts", "skilllayer_list_branches", "skilllayer_get_commit",
        ],
        "related_professional_skill": None,
        "applicability_signals": [
            "git inspection occurred during an active VTE task",
            "the agent manually compared changed paths against an approved scope",
        ],
        "exclusion_signals": ["no git inspection observed"],
        "manual_equivalents": ["raw git diff/log/status instead of a SkillLayer git tool"],
        "expected_unique_value": "Repository-confined, structured git inspection instead of raw shell output.",
        "maturity_status": "AVAILABLE",
    },
    "SECRET_DETECTION": {
        "display_name": "Secret Detection",
        "related_mcp_tools": ["skilllayer_detect_secrets"],
        "related_professional_skill": None,
        "applicability_signals": ["publish/upload/release/package operations occurred", "files were prepared for external distribution"],
        "exclusion_signals": ["no release/publish/upload activity observed"],
        "manual_equivalents": ["manually enumerating or grepping files for secrets"],
        "expected_unique_value": "Deterministic, pattern-based secret scanning before external exposure.",
        "maturity_status": "AVAILABLE",
    },
    "RELEASE_READINESS": {
        "display_name": "Release Readiness",
        "related_mcp_tools": ["skilllayer_release_readiness"],
        "related_professional_skill": "release_readiness",
        "applicability_signals": ["a release/publish operation was under consideration"],
        "exclusion_signals": [],
        "manual_equivalents": ["ad hoc manual checklist before release"],
        "expected_unique_value": "Bounded, evidence-based readiness verdict.",
        "maturity_status": "AVAILABLE",
    },
    "SAFE_CODE_CHANGE": {
        "display_name": "Safe Code Change",
        "related_mcp_tools": ["skilllayer_safe_change"],
        "related_professional_skill": "safe_code_change",
        "applicability_signals": ["a code change was requested with a validation expectation"],
        "exclusion_signals": [],
        "manual_equivalents": ["manual edit-then-test loop with no bounded verdict"],
        "expected_unique_value": "Bounded, validated change with an explicit verdict.",
        "maturity_status": "AVAILABLE",
    },
    "EXTERNAL_ASYNC_JOB_ORCHESTRATION": {
        "display_name": "External Async Job Orchestration",
        "related_mcp_tools": [],
        "related_professional_skill": None,
        "applicability_signals": ["remote job submit/poll/download operations occurred"],
        "exclusion_signals": [],
        "manual_equivalents": ["custom hand-written polling loop for a remote job"],
        "expected_unique_value": "Durable orchestration of remote asynchronous jobs across interruptions.",
        "maturity_status": "MISSING",
    },
}


# ---------------------------------------------------------------------------
# In-memory session state (default; nothing persisted unless requested)
# ---------------------------------------------------------------------------


@dataclass
class _SessionState:
    session_id: str
    project_fingerprint: str | None
    mode: str
    created_at: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    skilllayer_calls: list[dict[str, Any]] = field(default_factory=list)
    truncated_operations: int = 0
    truncated_calls: int = 0


_SESSIONS: dict[str, _SessionState] = {}
_SESSIONS_LOCK = threading.Lock()


def start_session(
    session_id: str | None = None, *, project_fingerprint: str | None = None, mode: str = "NATURAL_DOGFOOD",
) -> dict[str, Any]:
    """Start (or reuse) an in-memory audit session. No filesystem write."""
    if mode not in MODES:
        return {"success": False, "error": "mode_invalid", "session_id": session_id}
    session_id = session_id or new_session_id()
    try:
        _validate_session_id(session_id)
    except SkillAuditError as exc:
        return {"success": False, "error": exc.reason, "session_id": session_id}
    with _SESSIONS_LOCK:
        if session_id not in _SESSIONS:
            _SESSIONS[session_id] = _SessionState(
                session_id=session_id, project_fingerprint=project_fingerprint, mode=mode, created_at=_now(),
            )
    return {"success": True, "error": None, "session_id": session_id, "mode": _SESSIONS[session_id].mode}


def reset_session(session_id: str) -> dict[str, Any]:
    with _SESSIONS_LOCK:
        existed = _SESSIONS.pop(session_id, None) is not None
    return {"success": True, "error": None, "session_id": session_id, "existed": existed}


def get_session_summary(session_id: str) -> dict[str, Any]:
    """Read-only bounded summary of one session's recorded state — never
    the raw operation/call records themselves. Never writes anything."""
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    return {
        "success": True, "error": None, "session_id": session_id, "mode": state.mode,
        "operations_recorded": len(state.operations), "skilllayer_calls_recorded": len(state.skilllayer_calls),
        "truncated_operations": state.truncated_operations, "truncated_calls": state.truncated_calls,
    }


def _get_session_or_error(session_id: str) -> tuple[_SessionState | None, dict[str, Any] | None]:
    with _SESSIONS_LOCK:
        state = _SESSIONS.get(session_id)
    if state is None:
        return None, {"success": False, "error": "session_not_found", "session_id": session_id}
    return state, None


# Bounded, allowlisted attribute keys per operation class. Every value is
# passed through sanitize_persisted_value with the policy shown — no
# free-form or unbounded string is ever accepted, so there is no untrusted
# text channel into which a secret, a raw path, or a prompt could hide.
_OPERATION_ATTRIBUTE_POLICY: dict[str, dict[str, tuple[FieldPolicy, Any]]] = {
    "FILE_EDIT": {
        "path": (FieldPolicy.SAFE_STRUCTURED, _REPO_RELATIVE_PATH_RE),
        "is_production_logic": (FieldPolicy.SAFE_STRUCTURED, None),
    },
    "FILE_CREATE": {"path": (FieldPolicy.SAFE_STRUCTURED, _REPO_RELATIVE_PATH_RE)},
    "FILE_DELETE": {"path": (FieldPolicy.SAFE_STRUCTURED, _REPO_RELATIVE_PATH_RE)},
    "GIT_STATUS": {},
    "GIT_DIFF": {"compared_against_scope": (FieldPolicy.SAFE_STRUCTURED, None)},
    "GIT_LOG": {},
    "TEST_RUN": {"completed": (FieldPolicy.SAFE_STRUCTURED, None), "exit_code_known": (FieldPolicy.SAFE_STRUCTURED, None)},
    "SECRET_REVIEW": {},
    "REMOTE_JOB_SUBMIT": {"job_kind": (FieldPolicy.REDACTABLE_TEXT, None)},
    "REMOTE_JOB_POLL": {"job_kind": (FieldPolicy.REDACTABLE_TEXT, None)},
    "CONTEXT_RESTORE": {},
    "DECISION_RECORD": {},
    "TODO_UPDATE": {},
    "RELEASE_ACTION": {"action_kind": (FieldPolicy.REDACTABLE_TEXT, None)},
}

_KNOWN_MCP_TOOLS: frozenset[str] = frozenset(
    tool for entry in CAPABILITY_REGISTRY.values() for tool in entry["related_mcp_tools"]
)


def _sanitize_attributes(operation: str, attributes: dict[str, Any] | None, allowed: dict[str, tuple[FieldPolicy, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    attributes = attributes or {}
    if not isinstance(attributes, dict) or len(attributes) > 8:
        return None, "attributes_invalid"
    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in allowed:
            return None, f"attribute_not_allowed:{key}"
        policy, shape = allowed[key]
        if isinstance(value, bool):
            sanitized[key] = value
            continue
        if not isinstance(value, str):
            return None, f"attribute_must_be_bool_or_str:{key}"
        result = sanitize_persisted_value(
            value, policy, max_length=_MAX_LABEL, shape_pattern=shape if isinstance(shape, re.Pattern) else None,
            field_name=key,
        )
        if not result.accepted:
            return None, result.rejection_reason
        sanitized[key] = result.sanitized_value
    return sanitized, None


def record_operation(session_id: str, operation: str, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record one bounded, normalized-enum operation observation.

    Never accepts unrestricted shell transcripts: ``operation`` must be one
    of ``OPERATION_CLASSES`` and ``attributes`` must be drawn from that
    operation's small allowlist, each value passed through Foundation A's
    own redaction/rejection gate."""
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    if operation not in OPERATION_CLASSES:
        return {"success": False, "error": "operation_class_invalid", "session_id": session_id}
    sanitized, err = _sanitize_attributes(operation, attributes, _OPERATION_ATTRIBUTE_POLICY.get(operation, {}))
    if err is not None:
        return {"success": False, "error": err, "session_id": session_id}
    with _SESSIONS_LOCK:
        if len(state.operations) >= _MAX_OPERATIONS:
            state.truncated_operations += 1
            return {"success": True, "error": None, "session_id": session_id, "recorded": False, "reason": "operation_limit_reached"}
        state.operations.append({"operation": operation, "attributes": sanitized, "recorded_at": _now()})
    return {"success": True, "error": None, "session_id": session_id, "recorded": True}


def record_skilllayer_call(session_id: str, tool_name: str, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record that a real SkillLayer MCP tool was called. ``tool_name`` must
    be one of the tools referenced by ``CAPABILITY_REGISTRY`` — an unknown
    name is rejected rather than silently accepted as evidence of adoption."""
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    if tool_name not in _KNOWN_MCP_TOOLS:
        return {"success": False, "error": "tool_name_unknown", "session_id": session_id}
    sanitized, err = _sanitize_attributes(tool_name, attributes, {"count": (FieldPolicy.SAFE_STRUCTURED, re.compile(r"\d{1,6}"))})
    if err is not None:
        return {"success": False, "error": err, "session_id": session_id}
    with _SESSIONS_LOCK:
        if len(state.skilllayer_calls) >= _MAX_SKILLLAYER_CALLS:
            state.truncated_calls += 1
            return {"success": True, "error": None, "session_id": session_id, "recorded": False, "reason": "call_limit_reached"}
        state.skilllayer_calls.append({"tool_name": tool_name, "attributes": sanitized, "recorded_at": _now()})
    return {"success": True, "error": None, "session_id": session_id, "recorded": True}


# ---------------------------------------------------------------------------
# Deterministic applicability rules
# ---------------------------------------------------------------------------


def _count_ops(state: _SessionState, *classes: str) -> int:
    return sum(1 for op in state.operations if op["operation"] in classes)


def _calls_for_tools(state: _SessionState, tools: list[str]) -> int:
    return sum(1 for call in state.skilllayer_calls if call["tool_name"] in tools)


def _file_change_count(state: _SessionState) -> int:
    return _count_ops(state, "FILE_EDIT", "FILE_CREATE", "FILE_DELETE")


def _production_logic_modified(state: _SessionState) -> bool:
    return any(
        op["operation"] == "FILE_EDIT" and op["attributes"].get("is_production_logic") is True
        for op in state.operations
    )


def _has_interruption_recovery(state: _SessionState) -> bool:
    return _count_ops(state, "CONTEXT_RESTORE") > 0


def _make_event(
    capability_id: str, classification: str, confidence: str, *, evidence_codes: list[str],
    observed_operations: list[str], related_calls: list[str], manual_equivalents: list[str],
    reason: str, limitations: list[str] | None = None,
) -> dict[str, Any]:
    assert classification in CLASSIFICATIONS
    assert confidence in CONFIDENCE_LEVELS
    if classification == _STRONG_SKIP_CLASSIFICATION:
        assert confidence == "HIGH", "APPLICABLE_BUT_SKIPPED requires HIGH confidence"
    return {
        "capability_id": capability_id,
        "capability_category": CAPABILITY_REGISTRY[capability_id]["display_name"],
        "classification": classification,
        "confidence": confidence,
        "evidence_codes": evidence_codes[:_MAX_EVIDENCE_CODES],
        "observed_operations": observed_operations,
        "related_skilllayer_calls": related_calls,
        "manual_equivalent_operations": manual_equivalents,
        "reason": reason[:_MAX_LABEL],
        "limitations": limitations or [],
    }


def _classify_vte(state: _SessionState) -> dict[str, Any]:
    tools = CAPABILITY_REGISTRY["VERIFIED_TASK_EXECUTION"]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    file_changes = _file_change_count(state)
    tests_run = _count_ops(state, "TEST_RUN") > 0
    production = _production_logic_modified(state)
    interrupted = _has_interruption_recovery(state)

    if used:
        return _make_event(
            "VERIFIED_TASK_EXECUTION", "APPLICABLE_AND_USED", "HIGH",
            evidence_codes=["vte_call_observed"], observed_operations=["FILE_EDIT", "TEST_RUN"],
            related_calls=tools, manual_equivalents=[],
            reason="Real Verified Task Execution MCP calls were observed.",
        )
    if file_changes == 0:
        return _make_event(
            "VERIFIED_TASK_EXECUTION", "NOT_APPLICABLE", "HIGH",
            evidence_codes=["no_file_changes"], observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="No repository files were modified this session.",
        )

    applicable = tests_run or file_changes > 1 or production or interrupted
    if not applicable:
        return _make_event(
            "VERIFIED_TASK_EXECUTION", "POSSIBLY_APPLICABLE", "LOW",
            evidence_codes=["single_file_change_no_tests"], observed_operations=["FILE_EDIT"],
            related_calls=[], manual_equivalents=["FILE_EDIT without VTE"],
            reason="A single file changed with no other strong applicability signal.",
        )

    confidence = "HIGH" if tests_run else ("MEDIUM" if (file_changes > 1 or production) else "LOW")
    classification = _STRONG_SKIP_CLASSIFICATION if confidence == "HIGH" else "POSSIBLY_APPLICABLE"
    evidence = []
    if tests_run:
        evidence.append("tests_run_without_vte")
    if file_changes > 1:
        evidence.append("multiple_files_changed")
    if production:
        evidence.append("production_logic_modified")
    if interrupted:
        evidence.append("session_interruption_recovered")
    return _make_event(
        "VERIFIED_TASK_EXECUTION", classification, confidence,
        evidence_codes=evidence, observed_operations=["FILE_EDIT", "TEST_RUN"] if tests_run else ["FILE_EDIT"],
        related_calls=[], manual_equivalents=["FILE_EDIT + GIT_DIFF + TEST_RUN without any vte_* call"],
        reason="Repository files changed with strong completion signals but no VTE call was observed."
        if classification == _STRONG_SKIP_CLASSIFICATION else
        "Repository files changed with a weaker applicability signal for Verified Task Execution.",
    )


def _classify_scoped_git(state: _SessionState) -> dict[str, Any]:
    tools = CAPABILITY_REGISTRY["SCOPED_GIT_INSPECTION"]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    raw_git_ops = _count_ops(state, "GIT_STATUS", "GIT_DIFF", "GIT_LOG")
    vte_used = _calls_for_tools(state, CAPABILITY_REGISTRY["VERIFIED_TASK_EXECUTION"]["related_mcp_tools"]) > 0
    scope_compare = any(op["operation"] == "GIT_DIFF" and op["attributes"].get("compared_against_scope") is True for op in state.operations)

    if used:
        return _make_event(
            "SCOPED_GIT_INSPECTION", "APPLICABLE_AND_USED", "HIGH",
            evidence_codes=["scoped_git_tool_called"], observed_operations=["GIT_DIFF"], related_calls=tools,
            manual_equivalents=[], reason="A SkillLayer scoped git tool was called.",
        )
    if raw_git_ops == 0:
        return _make_event(
            "SCOPED_GIT_INSPECTION", "NOT_APPLICABLE", "HIGH",
            evidence_codes=["no_git_inspection"], observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="No git inspection was observed this session.",
        )
    if vte_used or scope_compare:
        return _make_event(
            "SCOPED_GIT_INSPECTION", _STRONG_SKIP_CLASSIFICATION, "HIGH",
            evidence_codes=["raw_git_during_active_vte" if vte_used else "manual_scope_comparison"],
            observed_operations=["GIT_DIFF", "GIT_STATUS", "GIT_LOG"], related_calls=[],
            manual_equivalents=["raw git diff/log/status instead of a SkillLayer git tool"],
            reason="Raw git inspection occurred where a scoped SkillLayer git tool was directly applicable.",
        )
    return _make_event(
        "SCOPED_GIT_INSPECTION", "POSSIBLY_APPLICABLE", "LOW",
        evidence_codes=["raw_git_ops_observed"], observed_operations=["GIT_DIFF", "GIT_STATUS", "GIT_LOG"],
        related_calls=[], manual_equivalents=["raw git diff/log/status instead of a SkillLayer git tool"],
        reason="Raw git inspection occurred without a strong scope-related signal.",
    )


def _classify_secret_detection(state: _SessionState) -> dict[str, Any]:
    tools = CAPABILITY_REGISTRY["SECRET_DETECTION"]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    release_ops = _count_ops(state, "RELEASE_ACTION")
    manual_review = _count_ops(state, "SECRET_REVIEW")

    if used:
        return _make_event(
            "SECRET_DETECTION", "APPLICABLE_AND_USED", "HIGH", evidence_codes=["secret_detection_called"],
            observed_operations=["RELEASE_ACTION"], related_calls=tools, manual_equivalents=[],
            reason="Secret detection was run before a release/publish operation.",
        )
    if release_ops == 0:
        return _make_event(
            "SECRET_DETECTION", "NOT_APPLICABLE", "HIGH", evidence_codes=["no_release_activity"],
            observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="No release, publish, upload, or package operation was observed.",
        )
    if manual_review == 0:
        return _make_event(
            "SECRET_DETECTION", _STRONG_SKIP_CLASSIFICATION, "HIGH",
            evidence_codes=["release_without_any_secret_review"], observed_operations=["RELEASE_ACTION"],
            related_calls=[], manual_equivalents=["manually enumerating or grepping files for secrets"],
            reason="A release/publish/upload operation occurred with no secret review evidence at all.",
        )
    return _make_event(
        "SECRET_DETECTION", "POSSIBLY_APPLICABLE", "MEDIUM",
        evidence_codes=["release_with_manual_review"], observed_operations=["RELEASE_ACTION", "SECRET_REVIEW"],
        related_calls=[], manual_equivalents=["manually enumerating or grepping files for secrets"],
        reason="A release operation occurred with some manual secret review, but not the SkillLayer scanner.",
    )


_DECISION_SEARCH_HIGH_THRESHOLD = 8
_DECISION_SEARCH_MEDIUM_THRESHOLD = 3


def _classify_decision_search(state: _SessionState) -> dict[str, Any]:
    tools = CAPABILITY_REGISTRY["DECISION_SEARCH"]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    decisions = _count_ops(state, "DECISION_RECORD") + _calls_for_tools(state, CAPABILITY_REGISTRY["DECISION_TRACKING"]["related_mcp_tools"])
    interrupted = _has_interruption_recovery(state)

    if used:
        return _make_event(
            "DECISION_SEARCH", "APPLICABLE_AND_USED", "HIGH", evidence_codes=["decision_search_called"],
            observed_operations=["DECISION_RECORD"], related_calls=tools, manual_equivalents=[],
            reason="Decision search was called.",
        )
    if decisions < 2:
        return _make_event(
            "DECISION_SEARCH", "NOT_APPLICABLE", "HIGH", evidence_codes=["fewer_than_two_decisions"],
            observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="Fewer than two decisions were recorded this session.",
        )
    if decisions >= _DECISION_SEARCH_HIGH_THRESHOLD and interrupted:
        return _make_event(
            "DECISION_SEARCH", _STRONG_SKIP_CLASSIFICATION, "HIGH",
            evidence_codes=["many_decisions_with_resume", f"decision_count:{decisions}"],
            observed_operations=["DECISION_RECORD", "CONTEXT_RESTORE"], related_calls=[],
            manual_equivalents=["rereading decision history files manually"],
            reason="Many decisions existed and work resumed after an interruption, but decision search was never called.",
        )
    confidence = "MEDIUM" if decisions >= _DECISION_SEARCH_MEDIUM_THRESHOLD else "LOW"
    return _make_event(
        "DECISION_SEARCH", "POSSIBLY_APPLICABLE", confidence,
        evidence_codes=[f"decision_count:{decisions}"], observed_operations=["DECISION_RECORD"], related_calls=[],
        manual_equivalents=["rereading decision history files manually"],
        reason="Multiple decisions exist; searching them may have been useful.",
    )


def _classify_context_snapshot_compare(state: _SessionState) -> dict[str, Any]:
    tools = CAPABILITY_REGISTRY["CONTEXT_SNAPSHOT_COMPARE"]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    snapshots = _calls_for_tools(state, CAPABILITY_REGISTRY["CONTEXT_SAVE_RESUME"]["related_mcp_tools"])
    interrupted = _has_interruption_recovery(state)

    if used:
        return _make_event(
            "CONTEXT_SNAPSHOT_COMPARE", "APPLICABLE_AND_USED", "HIGH", evidence_codes=["snapshot_compare_called"],
            observed_operations=["CONTEXT_RESTORE"], related_calls=tools, manual_equivalents=[],
            reason="Context snapshot comparison was called.",
        )
    if snapshots < 2:
        return _make_event(
            "CONTEXT_SNAPSHOT_COMPARE", "NOT_APPLICABLE", "HIGH", evidence_codes=["fewer_than_two_snapshots"],
            observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="Fewer than two context snapshots exist this session.",
        )
    if interrupted:
        return _make_event(
            "CONTEXT_SNAPSHOT_COMPARE", _STRONG_SKIP_CLASSIFICATION, "HIGH",
            evidence_codes=["two_plus_snapshots_with_restore"], observed_operations=["CONTEXT_RESTORE"],
            related_calls=[], manual_equivalents=["manually diffing two saved context notes"],
            reason="At least two context snapshots exist and a resume occurred, but they were never compared.",
        )
    return _make_event(
        "CONTEXT_SNAPSHOT_COMPARE", "POSSIBLY_APPLICABLE", "MEDIUM",
        evidence_codes=["two_plus_snapshots_no_restore_signal"], observed_operations=[], related_calls=[],
        manual_equivalents=["manually diffing two saved context notes"],
        reason="At least two context snapshots exist, without a clear resume/drift signal.",
    )


def _classify_simple_usage(state: _SessionState, capability_id: str, operation_signal: str) -> dict[str, Any]:
    """For capabilities with no defined applicability rule (this milestone
    only specifies rules for the five above): report USED when called,
    otherwise UNKNOWN — never a fabricated APPLICABLE_BUT_SKIPPED claim
    without a real rule behind it."""
    tools = CAPABILITY_REGISTRY[capability_id]["related_mcp_tools"]
    used = _calls_for_tools(state, tools) > 0
    if used:
        return _make_event(
            capability_id, "USED", "HIGH", evidence_codes=["tool_called"], observed_operations=[operation_signal],
            related_calls=tools, manual_equivalents=[], reason=f"{CAPABILITY_REGISTRY[capability_id]['display_name']} was called.",
        )
    return _make_event(
        capability_id, "UNKNOWN", "LOW", evidence_codes=["no_applicability_rule_defined"], observed_operations=[],
        related_calls=[], manual_equivalents=[],
        reason=f"No defined applicability rule for {CAPABILITY_REGISTRY[capability_id]['display_name']}; not called this session.",
        limitations=["no_deterministic_applicability_rule_defined_for_this_capability"],
    )


def _classify_missing_capability(state: _SessionState) -> dict[str, Any]:
    remote_ops = _count_ops(state, "REMOTE_JOB_SUBMIT", "REMOTE_JOB_POLL")
    if remote_ops == 0:
        return _make_event(
            "EXTERNAL_ASYNC_JOB_ORCHESTRATION", "NOT_APPLICABLE", "HIGH",
            evidence_codes=["no_remote_job_activity"], observed_operations=[], related_calls=[], manual_equivalents=[],
            reason="No remote asynchronous job activity was observed.",
        )
    return _make_event(
        "EXTERNAL_ASYNC_JOB_ORCHESTRATION", "CAPABILITY_MISSING", "HIGH",
        evidence_codes=[f"remote_job_operations:{remote_ops}"], observed_operations=["REMOTE_JOB_SUBMIT", "REMOTE_JOB_POLL"],
        related_calls=[], manual_equivalents=["custom hand-written polling loop for a remote job"],
        reason="Remote asynchronous job operations were observed, but SkillLayer has no capability for this yet.",
    )


def classify_opportunities(session_id: str) -> dict[str, Any]:
    """Classify every registered capability for one session. Read-only."""
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    if not state.operations and not state.skilllayer_calls:
        events = [
            _make_event(
                capability_id, "UNKNOWN", "LOW", evidence_codes=["no_observations_recorded"],
                observed_operations=[], related_calls=[], manual_equivalents=[],
                reason="No operations were recorded this session; applicability cannot be judged.",
                limitations=["no_observations_recorded"],
            )
            for capability_id in CAPABILITY_REGISTRY
        ]
        return {"success": True, "error": None, "session_id": session_id, "events": events}

    events = [
        _classify_vte(state),
        _classify_scoped_git(state),
        _classify_secret_detection(state),
        _classify_decision_search(state),
        _classify_context_snapshot_compare(state),
        _classify_simple_usage(state, "DECISION_TRACKING", "DECISION_RECORD"),
        _classify_simple_usage(state, "CONTEXT_SAVE_RESUME", "CONTEXT_RESTORE"),
        _classify_simple_usage(state, "TODO_MANAGEMENT", "TODO_UPDATE"),
        _classify_simple_usage(state, "RELEASE_READINESS", "RELEASE_ACTION"),
        _classify_simple_usage(state, "SAFE_CODE_CHANGE", "FILE_EDIT"),
        _classify_missing_capability(state),
    ]
    return {"success": True, "error": None, "session_id": session_id, "events": events}


# ---------------------------------------------------------------------------
# Manual reinvention detection
# ---------------------------------------------------------------------------


def detect_manual_reinventions(session_id: str) -> dict[str, Any]:
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    reinventions: list[dict[str, Any]] = []

    vte_tools = CAPABILITY_REGISTRY["VERIFIED_TASK_EXECUTION"]["related_mcp_tools"]
    if (_file_change_count(state) > 0 and _count_ops(state, "GIT_DIFF") > 0 and _count_ops(state, "TEST_RUN") > 0
            and _calls_for_tools(state, vte_tools) == 0):
        reinventions.append({
            "capability_duplicated": "VERIFIED_TASK_EXECUTION",
            "observed_manual_steps": ["FILE_EDIT", "GIT_DIFF", "TEST_RUN"],
            "missing_skilllayer_evidence": "no vte_* MCP call observed",
            "likely_extra_work": "Scope checking, checkpointing, and completion verification were done manually.",
            "confidence": "HIGH",
            "limitations": ["extra work is qualitative, not measured against a timed baseline"],
        })

    git_tools = CAPABILITY_REGISTRY["SCOPED_GIT_INSPECTION"]["related_mcp_tools"]
    if _count_ops(state, "GIT_DIFF", "GIT_STATUS", "GIT_LOG") > 0 and _calls_for_tools(state, git_tools) == 0:
        reinventions.append({
            "capability_duplicated": "SCOPED_GIT_INSPECTION",
            "observed_manual_steps": ["GIT_DIFF", "GIT_STATUS", "GIT_LOG"],
            "missing_skilllayer_evidence": "no scoped SkillLayer git tool called",
            "likely_extra_work": "Raw git output was inspected instead of a repository-confined, structured result.",
            "confidence": "MEDIUM",
            "limitations": ["extra work is qualitative, not measured against a timed baseline"],
        })

    secret_tools = CAPABILITY_REGISTRY["SECRET_DETECTION"]["related_mcp_tools"]
    if (_count_ops(state, "RELEASE_ACTION") > 0 and _count_ops(state, "SECRET_REVIEW") > 0
            and _calls_for_tools(state, secret_tools) == 0):
        reinventions.append({
            "capability_duplicated": "SECRET_DETECTION",
            "observed_manual_steps": ["RELEASE_ACTION", "SECRET_REVIEW"],
            "missing_skilllayer_evidence": "no skilllayer_detect_secrets call observed",
            "likely_extra_work": "Files were manually reviewed for secrets before release instead of scanned deterministically.",
            "confidence": "MEDIUM",
            "limitations": ["extra work is qualitative, not measured against a timed baseline"],
        })

    context_tools = CAPABILITY_REGISTRY["CONTEXT_SAVE_RESUME"]["related_mcp_tools"]
    if _has_interruption_recovery(state) and _calls_for_tools(state, context_tools) == 0 and _file_change_count(state) > 2:
        reinventions.append({
            "capability_duplicated": "CONTEXT_SAVE_RESUME",
            "observed_manual_steps": ["CONTEXT_RESTORE", "FILE_EDIT (repeated)"],
            "missing_skilllayer_evidence": "no saved context resume was available",
            "likely_extra_work": "Many files were reread after an interruption instead of resuming from a saved snapshot.",
            "confidence": "MEDIUM",
            "limitations": ["extra work is qualitative, not measured against a timed baseline"],
        })

    return {"success": True, "error": None, "session_id": session_id, "reinventions": reinventions}


# ---------------------------------------------------------------------------
# Recommendation logic — exactly one, or none
# ---------------------------------------------------------------------------


def _build_recommendation(events: list[dict[str, Any]], reinventions: list[dict[str, Any]]) -> str | None:
    missing = [e for e in events if e["classification"] == "CAPABILITY_MISSING"]
    if missing:
        remote_ops = next((c for c in missing[0]["evidence_codes"] if c.startswith("remote_job_operations:")), None)
        count = remote_ops.split(":")[1] if remote_ops else "multiple"
        return (
            f"Add external async-job orchestration; {count} remote job operation(s) required custom polling code."
        )

    high_skipped = [e for e in events if e["classification"] == _STRONG_SKIP_CLASSIFICATION]
    if high_skipped:
        top = high_skipped[0]
        if top["capability_id"] == "VERIFIED_TASK_EXECUTION":
            return "Reduce VTE entry friction; a high-confidence Verified Task Execution opportunity was handled manually."
        if top["capability_id"] == "DECISION_SEARCH":
            return "Surface decision search after resume; many decisions existed but none were queried."
        return f"Reduce entry friction for {top['capability_category']}; a high-confidence opportunity was handled manually."

    if reinventions:
        top = reinventions[0]
        return f"Reduce manual reinvention of {CAPABILITY_REGISTRY[top['capability_duplicated']]['display_name']}; it was reproduced by hand this session."

    possibly = [e for e in events if e["classification"] == "POSSIBLY_APPLICABLE"]
    used = [e for e in events if e["classification"] in {"USED", "APPLICABLE_AND_USED"}]
    if possibly and not used:
        return "Improve capability discoverability; several plausibly-applicable capabilities were never called this session."

    return None


# ---------------------------------------------------------------------------
# Session adoption report
# ---------------------------------------------------------------------------


def build_session_adoption_report(session_id: str) -> dict[str, Any]:
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    classified = classify_opportunities(session_id)
    reinvented = detect_manual_reinventions(session_id)
    events = classified["events"]
    reinventions = reinvented["reinventions"]

    used = [e for e in events if e["classification"] in {"USED", "APPLICABLE_AND_USED"}]
    skipped = [e for e in events if e["classification"] == _STRONG_SKIP_CLASSIFICATION]
    possibly = [e for e in events if e["classification"] == "POSSIBLY_APPLICABLE"]
    missing = [e for e in events if e["classification"] == "CAPABILITY_MISSING"]

    recommendation = _build_recommendation(events, reinventions)
    evidence_complete = bool(state.operations or state.skilllayer_calls) and state.truncated_operations == 0 and state.truncated_calls == 0

    false_positive_risks: list[str] = []
    if any(e["confidence"] != "HIGH" for e in skipped):
        false_positive_risks.append("A skipped-opportunity claim was based on less than HIGH confidence evidence.")
    if not state.operations and not state.skilllayer_calls:
        false_positive_risks.append("No operations were recorded; all classifications are UNKNOWN.")
    if state.truncated_operations or state.truncated_calls:
        false_positive_risks.append("Some observations were dropped after the session's bounded limit was reached.")

    report = {
        "report_version": SCHEMA_VERSION,
        "session_id": session_id,
        "project_fingerprint": state.project_fingerprint,
        "observed_period": {"started_at": state.created_at, "reported_at": _now()},
        "capabilities_used": [{"capability_id": e["capability_id"], "classification": e["classification"]} for e in used],
        "opportunities_detected": events,
        "applicable_but_skipped": skipped,
        "possibly_applicable": possibly,
        "manual_reinventions": reinventions,
        "missing_capabilities": missing,
        "false_positive_risks": false_positive_risks,
        "recommendations": [recommendation] if recommendation else [],
        "evidence_complete": evidence_complete,
        "created_at": _now(),
    }
    return {"success": True, "error": None, "report": report}


_STATUS_LABEL = {
    "USED": "Used naturally",
    "APPLICABLE_AND_USED": "Used naturally",
    _STRONG_SKIP_CLASSIFICATION: "Applicable but skipped",
    "POSSIBLY_APPLICABLE": "Possibly applicable",
}


def render_session_adoption_report(report: dict[str, Any]) -> str:
    """Deterministic Markdown rendering. Sections omitted when empty."""
    lines = ["# SkillLayer Session Audit", ""]

    if report["capabilities_used"]:
        lines.append("## Used naturally")
        lines.append("")
        for item in report["capabilities_used"]:
            lines.append(f"- {CAPABILITY_REGISTRY[item['capability_id']]['display_name']}")
        lines.append("")

    if report["applicable_but_skipped"]:
        lines.append("## Applicable but skipped")
        lines.append("")
        for item in report["applicable_but_skipped"]:
            lines.append(f"- {item['capability_category']}: {item['reason']}")
        lines.append("")

    if report["possibly_applicable"]:
        lines.append("## Possibly applicable")
        lines.append("")
        for item in report["possibly_applicable"]:
            lines.append(f"- {item['capability_category']} ({item['confidence']} confidence): {item['reason']}")
        lines.append("")

    if report["manual_reinventions"]:
        lines.append("## Manual reinvention")
        lines.append("")
        for item in report["manual_reinventions"]:
            display = CAPABILITY_REGISTRY[item["capability_duplicated"]]["display_name"]
            lines.append(f"- {display}: {item['likely_extra_work']}")
        lines.append("")

    if report["missing_capabilities"]:
        lines.append("## Missing capability")
        lines.append("")
        for item in report["missing_capabilities"]:
            lines.append(f"- {item['capability_category']}: {item['reason']}")
        lines.append("")

    if report["false_positive_risks"]:
        lines.append("## False-positive risks")
        lines.append("")
        lines.extend(f"- {risk}" for risk in report["false_positive_risks"])
        lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(report["recommendations"][0] if report["recommendations"] else "No recommendation; evidence is insufficient.")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Assisted mode — at most one concise suggestion, never auto-invoked
# ---------------------------------------------------------------------------


def get_assisted_suggestion(session_id: str) -> dict[str, Any]:
    state, error = _get_session_or_error(session_id)
    if error is not None:
        return error
    if state.mode != "ASSISTED":
        return {"success": True, "error": None, "session_id": session_id, "suggestion": None, "reason": "not_in_assisted_mode"}
    classified = classify_opportunities(session_id)
    high_skipped = [e for e in classified["events"] if e["classification"] == _STRONG_SKIP_CLASSIFICATION]
    if not high_skipped:
        return {"success": True, "error": None, "session_id": session_id, "suggestion": None}
    top = high_skipped[0]
    return {
        "success": True, "error": None, "session_id": session_id,
        "suggestion": {"capability_id": top["capability_id"], "capability_category": top["capability_category"], "reason": top["reason"]},
    }


# ---------------------------------------------------------------------------
# Optional persistence — explicit consent required; reuses Foundation A.
# ---------------------------------------------------------------------------

_AUDIT_ID_RE = _SESSION_ID_RE
_MAX_JSONL_LINES = 500
_MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class _AuditPaths:
    audits_root: Path
    audit_dir: Path
    operations_path: Path
    opportunities_path: Path
    report_json_path: Path
    report_md_path: Path


class AuditPathSafetyError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _audit_paths(project_root: Path, session_id: str) -> _AuditPaths:
    if not _AUDIT_ID_RE.fullmatch(session_id):
        raise AuditPathSafetyError("session_id_invalid")
    root = project_root.resolve()
    skilllayer_dir = root / ".skilllayer"
    audits_root = skilllayer_dir / "session-audits"
    audit_dir = audits_root / session_id
    for ancestor in (skilllayer_dir, audits_root, audit_dir):
        if ancestor.is_symlink():
            raise AuditPathSafetyError("symlink_escape")
    try:
        audit_dir.resolve().relative_to(audits_root.resolve())
    except ValueError as exc:
        raise AuditPathSafetyError("path_escape") from exc
    return _AuditPaths(
        audits_root=audits_root, audit_dir=audit_dir,
        operations_path=audit_dir / "operations.jsonl", opportunities_path=audit_dir / "opportunities.json",
        report_json_path=audit_dir / "adoption-report.json", report_md_path=audit_dir / "adoption-report.md",
    )


def write_session_audit(
    project_root: Path, session_id: str, *, consent: TaskConsent | None, report: dict[str, Any], markdown: str,
    opportunities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist the session audit under consent. Idempotent on identical
    content; explicit rejection on a conflicting rewrite. Never modifies
    ``operations.jsonl`` once written for a given session (append-only,
    bounded)."""
    if report.get("session_id") != session_id:
        return {"success": False, "error": "cross_session_report_reference", "written_paths": []}
    try:
        paths = _audit_paths(project_root, session_id)
    except AuditPathSafetyError as exc:
        return {"success": False, "error": exc.reason, "written_paths": []}

    consent_state = _check_consent(consent, task_id=session_id, project_root=project_root, record_type="result")
    if consent_state is not None:
        return {"success": False, "error": "consent_required_or_invalid", "written_paths": []}

    root = project_root.resolve()
    encoded_len = len(json.dumps(report, ensure_ascii=False))
    if encoded_len > _MAX_FILE_BYTES or len(markdown) > _MAX_FILE_BYTES:
        return {"success": False, "error": "report_too_large", "written_paths": []}

    written: list[str] = []
    try:
        with memory_lock(root / ".skilllayer"):
            for path in (paths.report_json_path, paths.report_md_path, paths.opportunities_path, paths.operations_path):
                if path.is_symlink():
                    return {"success": False, "error": "symlink_not_permitted", "written_paths": []}

            existing_json = paths.report_json_path.read_text(encoding="utf-8") if paths.report_json_path.exists() else None
            existing_md = paths.report_md_path.read_text(encoding="utf-8") if paths.report_md_path.exists() else None
            if existing_json is not None or existing_md is not None:
                same = (existing_json is not None and json.loads(existing_json) == report) and existing_md == markdown
                if same:
                    return {"success": True, "error": None, "written_paths": [], "idempotent": True}
                return {"success": False, "error": "conflicting_report_rewrite", "written_paths": []}

            paths.audit_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(paths.report_json_path, report)
            _atomic_write_text(paths.report_md_path, markdown)
            written.extend([_rel(paths.report_json_path, root), _rel(paths.report_md_path, root)])

            if opportunities is not None:
                atomic_write_json(paths.opportunities_path, {"schema_version": SCHEMA_VERSION, "session_id": session_id, "events": opportunities})
                written.append(_rel(paths.opportunities_path, root))
    except MemoryLockTimeoutError as exc:
        return {"success": False, "error": "memory_lock_timeout", "written_paths": [], "detail": str(exc)}
    except OSError as exc:
        return {"success": False, "error": "io_error", "written_paths": [], "detail": str(exc)}

    return {"success": True, "error": None, "written_paths": written, "idempotent": False}


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_session_audit(project_root: Path, session_id: str) -> dict[str, Any] | None:
    try:
        paths = _audit_paths(project_root, session_id)
    except AuditPathSafetyError:
        return None
    if paths.report_json_path.is_symlink() or not paths.report_json_path.exists():
        return None
    try:
        data = json.loads(paths.report_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("session_id") == session_id else None
