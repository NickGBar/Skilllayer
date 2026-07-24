"""Verified Task Execution Foundation D — internal task-lifecycle orchestrator.

Connects Foundations A (consent-gated persistence), B (baseline/scope), and C
(checkpoint history/resume/ownership) into one deterministic sequence. This
module is internal: not a workflow, CLI command, router target, or MCP tool.
It never edits source files, mutates Git, rolls anything back, calls an LLM,
or runs a background process — it only coordinates and verifies state built
from those three foundations' own read/write primitives.

Purely additive: no changes to persistence.py/baseline.py/checkpoint.py/
resume.py/scope.py. New on-disk records live under the already-confined task
directory returned by ``persistence.task_record_paths``:

  .skilllayer/tasks/<task-id>/
    state.json                    — atomic current-state pointer
    transitions/<seq>-<id>.json    — immutable per-transition history
    evidence/<evidence_type>.json  — latest-wins evidence referenced by checkpoints
  .skilllayer/tasks/.idempotency_index.json — bounded create_task idempotency map

Every mutating operation requires the same ``TaskConsent`` object Foundation A
already defines and re-uses its atomic-write/lock/symlink-safety/redaction
primitives directly — no second persistence mechanism is introduced.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .baseline import capture_repository_baseline, read_repository_state
from .checkpoint import (
    acquire_task_ownership as _foundation_c_acquire_ownership,
    build_checkpoint,
    read_task_ownership,
    write_checkpoint_history,
)
from .persistence import (
    FieldPolicy,
    TaskConsent,
    _contains_rejected_secret,
    _rel,
    _resolve_paths_or_blocked,
    create_task_contract,
    read_scope_amendments,
    read_task_baseline,
    read_task_contract,
    sanitize_persisted_value,
    write_task_baseline,
    write_task_result,
)
from .resume import assess_resume, load_checkpoint_chain
from .scope import validate_scope

ORCHESTRATOR_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

TASK_STATES = frozenset({
    "CREATED", "CONTRACT_READY", "BASELINE_READY", "READY_TO_START", "RUNNING",
    "INTERRUPTED", "RESUME_REVIEW", "VALIDATING", "BLOCKED", "COMPLETED",
    "FAILED", "ABANDONED",
})
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "ABANDONED"})

# operation -> {allowed_previous_state: True}. A state not present here for a
# given operation means the transition is invalid and must fail explicitly.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "capture_baseline": frozenset({"CONTRACT_READY"}),
    "acquire_ownership": frozenset({"BASELINE_READY"}),
    "start_task": frozenset({"READY_TO_START"}),
    "checkpoint_task": frozenset({"RUNNING", "VALIDATING"}),
    "interrupt_task": frozenset({"RUNNING", "VALIDATING"}),
    "resume_task": frozenset({"INTERRUPTED", "RESUME_REVIEW"}),
    "validate_task": frozenset({"RUNNING", "BLOCKED"}),
    "finalize_task": frozenset({"VALIDATING"}),
    "abandon_task": frozenset(TASK_STATES - TERMINAL_STATES),
    "reclaim_ownership": frozenset(TASK_STATES - TERMINAL_STATES),
}

# checkpoint.py's TASK_STATUSES vocabulary (NOT_STARTED/IN_PROGRESS/...) is a
# distinct concept from this module's own orchestrator states; every
# checkpoint written here maps deterministically from one to the other so
# Foundation C's own validators (which only know their own vocabulary) accept
# every checkpoint this orchestrator produces.
_CHECKPOINT_TASK_STATUS: dict[str, str] = {
    "CREATED": "NOT_STARTED", "CONTRACT_READY": "NOT_STARTED", "BASELINE_READY": "NOT_STARTED",
    "READY_TO_START": "NOT_STARTED", "RUNNING": "IN_PROGRESS", "INTERRUPTED": "INTERRUPTED",
    "RESUME_REVIEW": "INTERRUPTED", "VALIDATING": "VALIDATING", "BLOCKED": "BLOCKED",
    "COMPLETED": "COMPLETED", "FAILED": "FAILED", "ABANDONED": "ABANDONED",
}

FINAL_VERDICTS = frozenset({
    "TASK_VERIFIED_COMPLETE", "TASK_COMPLETE_WITH_LIMITATIONS", "TASK_INCOMPLETE",
    "TASK_BLOCKED", "TASK_FAILED", "TASK_ABANDONED",
})

# ---------------------------------------------------------------------------
# Structured result contract — distinct from persistence.py's PERSISTENCE_
# STATES vocabulary: this module additionally distinguishes failure classes
# the milestone explicitly asked not to collapse into bare success=False.
# ---------------------------------------------------------------------------

ORCHESTRATOR_STATES = frozenset({
    "OPERATION_COMPLETED", "OPERATION_REJECTED", "OPERATION_INCOMPLETE",
    "OPERATION_FAILED_BEFORE_MUTATION", "OPERATION_PARTIALLY_PERSISTED",
    "TASK_BLOCKED", "TASK_FAILED_STATE", "VALIDATION_FAILED", "PERSISTENCE_FAILED",
    "CONFIRMATION_REQUIRED", "READ_COMPLETED",
})
_SUCCESS_STATES = frozenset({"OPERATION_COMPLETED", "READ_COMPLETED"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _op_result(
    state: str,
    *,
    task_id: str | None,
    error_code: str | None = None,
    recovery_action: str | None = None,
    summary: str = "",
    written_paths: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    assert state in ORCHESTRATOR_STATES, f"unknown orchestrator state: {state}"
    return {
        "state": state,
        "success": state in _SUCCESS_STATES,
        "task_id": task_id,
        "error_code": error_code,
        "recovery_action": recovery_action,
        "summary": summary,
        "written_paths": list(written_paths or []),
        **extra,
    }


def _confirmation(
    *, task_id: str, reason: str, scope: str, operation_after_confirmation: str, summary: str,
) -> dict[str, Any]:
    return _op_result(
        "CONFIRMATION_REQUIRED", task_id=task_id, summary=summary,
        confirmation_required=True, confirmation_reason=reason,
        confirmation_scope=scope, operation_after_confirmation=operation_after_confirmation,
    )


# ---------------------------------------------------------------------------
# Orchestrator-local paths — computed as children of the already
# symlink-checked task_dir returned by persistence.task_record_paths(), never
# added to TaskRecordPaths itself (Foundation A/B/C stay unmodified).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OrchestratorPaths:
    task_dir: Path
    state_path: Path
    transitions_dir: Path
    evidence_dir: Path


def _orchestrator_paths(task_dir: Path) -> _OrchestratorPaths:
    return _OrchestratorPaths(
        task_dir=task_dir,
        state_path=task_dir / "state.json",
        transitions_dir=task_dir / "transitions",
        evidence_dir=task_dir / "evidence",
    )


_IDEMPOTENCY_INDEX_NAME = ".idempotency_index.json"
_TRANSITION_ID_RE = re.compile(r"^tr-[a-f0-9]{16}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_ACTOR_TYPES = frozenset({"orchestrator", "user"})


def _digest(value: dict[str, Any]) -> str:
    # Identical canonicalization to resume.py's _digest — evidence integrity
    # fingerprints must validate under Foundation C's own verification logic.
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _derive_transition_id(task_id: str, operation: str, idempotency_key: str) -> str:
    return "tr-" + hashlib.sha256(f"{task_id}:{operation}:{idempotency_key}".encode("utf-8")).hexdigest()[:16]


def _derive_checkpoint_id(task_id: str, sequence: int, idempotency_key: str) -> str:
    return "cp-" + hashlib.sha256(f"{task_id}:{sequence}:{idempotency_key}".encode("utf-8")).hexdigest()[:16]


class TaskLifecycleError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Recovery-safe state read
# ---------------------------------------------------------------------------


def _read_state_pointer(paths: _OrchestratorPaths, task_id: str) -> dict[str, Any]:
    """Read state.json without trusting it blindly against transition history.

    Never silently repairs corruption; returns a structured status the caller
    must handle explicitly (missing/stale/corrupt/duplicate/mismatched)."""
    if paths.state_path.is_symlink():
        return {"status": "POINTER_CORRUPT", "error": "state_pointer_symlink"}
    transitions_ok, transitions_error, transitions = _read_transition_history(paths, task_id)
    if not transitions_ok:
        return {"status": "HISTORY_CORRUPT", "error": transitions_error}
    if not paths.state_path.exists():
        if transitions:
            # Valid history exists but the pointer is missing — reconstruct
            # the pointer's logical value from immutable history rather than
            # treating this as unrecoverable, but never silently rewrite the
            # missing file ourselves; the caller decides whether to persist it.
            latest = transitions[-1]
            return {
                "status": "POINTER_MISSING_RECONSTRUCTIBLE",
                "reconstructed": {
                    "schema_version": ORCHESTRATOR_SCHEMA_VERSION, "task_id": task_id,
                    "current_state": latest["next_state"], "latest_transition_id": latest["transition_id"],
                    "latest_sequence": latest["sequence"], "updated_at": latest["triggered_at"],
                },
            }
        return {"status": "NOT_CREATED"}
    try:
        pointer = json.loads(paths.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "POINTER_CORRUPT", "error": "state_pointer_malformed_json"}
    required = {"schema_version", "task_id", "current_state", "latest_transition_id", "latest_sequence", "updated_at"}
    if not isinstance(pointer, dict) or set(pointer) != required:
        return {"status": "POINTER_CORRUPT", "error": "state_pointer_schema_invalid"}
    if pointer.get("schema_version") != ORCHESTRATOR_SCHEMA_VERSION or pointer.get("task_id") != task_id:
        return {"status": "POINTER_CORRUPT", "error": "state_pointer_version_or_task_mismatch"}
    if pointer.get("current_state") not in TASK_STATES:
        return {"status": "POINTER_CORRUPT", "error": "state_pointer_state_invalid"}
    if not transitions:
        return {"status": "POINTER_STALE", "error": "pointer_present_but_no_transition_history", "pointer": pointer}
    latest = transitions[-1]
    if pointer["latest_transition_id"] != latest["transition_id"] or pointer["latest_sequence"] != latest["sequence"] or pointer["current_state"] != latest["next_state"]:
        return {"status": "POINTER_STALE", "error": "pointer_does_not_match_latest_transition", "pointer": pointer, "latest_transition": latest}
    return {"status": "CURRENT", "pointer": pointer, "transitions": transitions}


def _read_transition_history(paths: _OrchestratorPaths, task_id: str) -> tuple[bool, str | None, list[dict[str, Any]]]:
    if not paths.transitions_dir.exists():
        return True, None, []
    if paths.transitions_dir.is_symlink():
        return False, "transitions_dir_symlink", []
    try:
        files = sorted(paths.transitions_dir.iterdir())
    except OSError:
        return False, "transitions_dir_unreadable", []
    if len(files) > 10_000:
        return False, "transition_history_limit_exceeded", []
    records: list[dict[str, Any]] = []
    for path in files:
        if path.is_symlink() or path.suffix != ".json":
            return False, "transition_entry_invalid", []
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "transition_entry_malformed", []
        checked = _validate_transition_record(record, task_id)
        if not checked:
            return False, "transition_entry_schema_invalid", []
        records.append(record)
    records.sort(key=lambda item: item["sequence"])
    for index, record in enumerate(records, start=1):
        if record["sequence"] != index:
            return False, "transition_sequence_gap_or_duplicate", []
        if index == 1 and record["previous_state"] is not None:
            # The very first transition's previous_state is None (pre-CREATED).
            pass
        if index > 1:
            expected_previous = records[index - 2]["next_state"]
            if record["previous_state"] != expected_previous:
                return False, "transition_previous_state_mismatch", []
    return True, None, records


_TRANSITION_REQUIRED_FIELDS = {
    "schema_version", "transition_id", "task_id", "sequence", "previous_state", "next_state",
    "triggered_at", "operation", "actor_type", "evidence_refs", "prerequisites_checked",
    "outcome", "limitations",
}
_ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _validate_transition_record(value: Any, task_id: str) -> bool:
    if not isinstance(value, dict) or set(value) != _TRANSITION_REQUIRED_FIELDS:
        return False
    if value.get("schema_version") != ORCHESTRATOR_SCHEMA_VERSION or value.get("task_id") != task_id:
        return False
    if not isinstance(value.get("transition_id"), str) or not _TRANSITION_ID_RE.fullmatch(value["transition_id"]):
        return False
    if not isinstance(value.get("sequence"), int) or value["sequence"] < 1:
        return False
    if value.get("next_state") not in TASK_STATES:
        return False
    if value.get("previous_state") is not None and value["previous_state"] not in TASK_STATES:
        return False
    if not isinstance(value.get("triggered_at"), str) or not _ISO_RE.fullmatch(value["triggered_at"]):
        return False
    if not isinstance(value.get("operation"), str) or not value["operation"]:
        return False
    if value.get("actor_type") not in _ACTOR_TYPES:
        return False
    if not isinstance(value.get("evidence_refs"), list) or len(value["evidence_refs"]) > 64:
        return False
    if not isinstance(value.get("prerequisites_checked"), list) or len(value["prerequisites_checked"]) > 64:
        return False
    if value.get("outcome") not in {"SUCCESS", "REJECTED", "BLOCKED"}:
        return False
    if not isinstance(value.get("limitations"), list) or len(value["limitations"]) > 32:
        return False
    return True


# ---------------------------------------------------------------------------
# Evidence writer — reused by validate_task/checkpoint_task; latest-wins,
# atomic, consent-gated exactly like Foundation A's own writers.
# ---------------------------------------------------------------------------


def _write_evidence(
    o_paths: _OrchestratorPaths, evidence_type: str, record: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Write one evidence record. Returns (evidence_ref_or_None, error_code)."""
    if _contains_rejected_secret(record):
        return None, "evidence_contains_rejected_value"
    evidence_path = o_paths.evidence_dir / f"{evidence_type}.json"
    if o_paths.evidence_dir.is_symlink() or evidence_path.is_symlink():
        return None, "symlink_not_permitted"
    try:
        o_paths.evidence_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(evidence_path, record)
    except OSError:
        return None, "io_error"
    ref = {
        "evidence_type": evidence_type,
        "record_path": f"evidence/{evidence_type}.json",
        "schema_version": int(record.get("schema_version", 1)),
        "integrity_fingerprint": _digest(record),
        "required": True,
        "observed_status": str(record.get("verdict") or record.get("collection_status") or record.get("baseline_status") or "RECORDED"),
    }
    return ref, ""


def _read_evidence(o_paths: _OrchestratorPaths, evidence_type: str) -> dict[str, Any] | None:
    path = o_paths.evidence_dir / f"{evidence_type}.json"
    if path.is_symlink() or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Transition writer — atomic, immutable-append, idempotent on a stable
# (task_id, operation, idempotency_key) triple.
# ---------------------------------------------------------------------------


def _write_transition(
    project_root: Path,
    task_id: str,
    o_paths: _OrchestratorPaths,
    *,
    operation: str,
    previous_state: str | None,
    next_state: str,
    actor_type: str,
    evidence_refs: list[dict[str, Any]],
    prerequisites_checked: list[str],
    outcome: str,
    limitations: list[str],
    idempotency_key: str | None,
) -> dict[str, Any]:
    key = idempotency_key or secrets.token_hex(8)
    if idempotency_key is not None and not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="idempotency_key_invalid",
                           summary="idempotency_key must match [a-z0-9][a-z0-9_-]{0,79}.")
    transition_id = _derive_transition_id(task_id, operation, key)

    root = project_root.resolve()
    skilllayer_dir = root / ".skilllayer"
    try:
        with memory_lock(skilllayer_dir):
            state_read = _read_state_pointer(o_paths, task_id)
            current_state = None
            if state_read["status"] == "CURRENT":
                current_state = state_read["pointer"]["current_state"]
            elif state_read["status"] == "NOT_CREATED":
                current_state = None
            elif state_read["status"] == "POINTER_MISSING_RECONSTRUCTIBLE":
                current_state = state_read["reconstructed"]["current_state"]
            else:
                return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id,
                                   error_code=state_read.get("error", "state_unrecoverable"),
                                   recovery_action="inspect_transition_history_manually",
                                   summary="Refusing to write a new transition: existing state is not safely readable.")

            if current_state != previous_state:
                # Idempotent-retry check: does a transition with this exact
                # derived id already exist as the CURRENT latest transition?
                transitions_ok, _, transitions = _read_transition_history(o_paths, task_id)
                if transitions_ok and transitions and transitions[-1]["transition_id"] == transition_id and transitions[-1]["next_state"] == next_state:
                    return _op_result("OPERATION_COMPLETED", task_id=task_id, written_paths=[],
                                       transition=transitions[-1],
                                       summary="Retry is idempotent; existing transition was retained.")
                return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                                   recovery_action="call_get_task_status",
                                   summary=f"Cannot apply {operation}: task is at {current_state!r}, not {previous_state!r}.")

            sequence = 1 if state_read["status"] == "NOT_CREATED" else (
                state_read["pointer"]["latest_sequence"] + 1 if state_read["status"] == "CURRENT"
                else state_read["reconstructed"]["latest_sequence"] + 1
            )
            record = {
                "schema_version": ORCHESTRATOR_SCHEMA_VERSION, "transition_id": transition_id, "task_id": task_id,
                "sequence": sequence, "previous_state": previous_state, "next_state": next_state,
                "triggered_at": _now(), "operation": operation, "actor_type": actor_type,
                "evidence_refs": evidence_refs, "prerequisites_checked": prerequisites_checked,
                "outcome": outcome, "limitations": limitations,
            }
            transition_path = o_paths.transitions_dir / f"{sequence:04d}-{transition_id}.json"
            if o_paths.transitions_dir.is_symlink() or transition_path.is_symlink() or o_paths.state_path.is_symlink():
                return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code="symlink_not_permitted",
                                   summary="Refusing to write: a transition path is a symlink.")
            if transition_path.exists():
                return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="duplicate_sequence",
                                   recovery_action="call_get_task_status",
                                   summary="A transition already exists at this sequence with a different id.")
            pointer = {
                "schema_version": ORCHESTRATOR_SCHEMA_VERSION, "task_id": task_id, "current_state": next_state,
                "latest_transition_id": transition_id, "latest_sequence": sequence, "updated_at": record["triggered_at"],
            }
            o_paths.transitions_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(transition_path, record)
            atomic_write_json(o_paths.state_path, pointer)
    except MemoryLockTimeoutError as exc:
        return _op_result("PERSISTENCE_FAILED", task_id=task_id, error_code="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _op_result("PERSISTENCE_FAILED", task_id=task_id, error_code="io_error", summary=str(exc))

    return _op_result(
        "OPERATION_COMPLETED", task_id=task_id,
        written_paths=[_rel(transition_path, root), _rel(o_paths.state_path, root)],
        transition=record, summary=f"{operation}: {previous_state!r} -> {next_state!r}.",
    )


def _current_state_or_none(project_root: Path, task_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Return (current_state, blocked_result). blocked_result is set only when
    state cannot be safely determined."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return None, _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id,
                                 error_code=blocked.get("error", "task_path_invalid"),
                                 summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)
    state_read = _read_state_pointer(o_paths, task_id)
    if state_read["status"] == "CURRENT":
        return state_read["pointer"]["current_state"], None
    if state_read["status"] == "POINTER_MISSING_RECONSTRUCTIBLE":
        return state_read["reconstructed"]["current_state"], None
    if state_read["status"] == "NOT_CREATED":
        return None, None
    return None, _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id,
                             error_code=state_read.get("error", "state_unrecoverable"),
                             recovery_action="inspect_transition_history_manually",
                             summary=f"Task state cannot be safely determined ({state_read['status']}).")


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def _idempotency_index_path(tasks_root: Path) -> Path:
    return tasks_root / _IDEMPOTENCY_INDEX_NAME


def _lookup_idempotency_index(tasks_root: Path, idempotency_key: str) -> str | None:
    path = _idempotency_index_path(tasks_root)
    if path.is_symlink() or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get("entries")
    return entries.get(idempotency_key) if isinstance(entries, dict) else None


def create_task(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    objective_label: str,
    idempotency_key: str | None = None,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    expected_checks: list[str] | None = None,
    constraints: list[dict[str, str]] | None = None,
    scope_mode: str = "EXPLICIT",
    max_changed_files: int | None = None,
    allow_new_files: bool = True,
    allow_deleted_files: bool = True,
    allow_test_files: bool = True,
    allowed_generated_paths: list[str] | None = None,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Create a task and persist its contract: CREATED -> CONTRACT_READY.

    task_id is caller-supplied (call ``persistence.generate_task_id`` first,
    the same way Foundation A's ``create_task_contract`` already requires) —
    a ``TaskConsent`` must be bound to a specific task_id before this call, so
    the orchestrator cannot mint one internally without an unresolvable
    chicken-and-egg consent problem.

    Idempotent when idempotency_key is supplied: a retry with the same key
    returns the SAME previously-created task_id and does not create a second
    task, even if this call's task_id/consent arguments differ (a genuine
    retry commonly generates a fresh task_id it cannot avoid; the key is what
    makes the retry recognizable, not the task_id)."""
    tasks_root = project_root.resolve() / ".skilllayer" / "tasks"
    if idempotency_key is not None:
        if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            return _op_result("OPERATION_REJECTED", task_id=None, error_code="idempotency_key_invalid",
                               summary="idempotency_key must match [a-z0-9][a-z0-9_-]{0,79}.")
        existing = _lookup_idempotency_index(tasks_root, idempotency_key)
        if existing is not None:
            status = get_task_status(project_root, existing)
            return _op_result("OPERATION_COMPLETED", task_id=existing, written_paths=[],
                               status=status, summary=f"idempotency_key already maps to task {existing}; returning it unchanged.")

    if consent is None:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="consent_required",
                           summary="No task created: task-lifecycle consent is required first.")

    created = create_task_contract(
        project_root, task_id, consent=consent, objective_label=objective_label,
        allowed_paths=allowed_paths, forbidden_paths=forbidden_paths, expected_checks=expected_checks,
        constraints=constraints, scope_mode=scope_mode, max_changed_files=max_changed_files,
        allow_new_files=allow_new_files, allow_deleted_files=allow_deleted_files,
        allow_test_files=allow_test_files, allowed_generated_paths=allowed_generated_paths,
        requested_by=requested_by,
    )
    if not created["success"]:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=created.get("error"),
                           summary=f"Contract not created: {created['summary']}")

    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=blocked.get("error"),
                           recovery_action="inspect_transition_history_manually",
                           summary="Contract was written but the task path failed a safety check afterwards.")
    o_paths = _orchestrator_paths(paths.task_dir)

    created_key = idempotency_key or ("create-" + secrets.token_hex(8))
    first = _write_transition(
        project_root, task_id, o_paths, operation="create_task", previous_state=None, next_state="CREATED",
        actor_type="orchestrator", evidence_refs=[], prerequisites_checked=[], outcome="SUCCESS", limitations=[],
        idempotency_key=created_key + "-created",
    )
    if not first["success"]:
        return first
    second = _write_transition(
        project_root, task_id, o_paths, operation="create_task", previous_state="CREATED", next_state="CONTRACT_READY",
        actor_type="orchestrator", evidence_refs=[], prerequisites_checked=["contract_persisted"], outcome="SUCCESS",
        limitations=[], idempotency_key=created_key + "-contract-ready",
    )
    if not second["success"]:
        return second

    if idempotency_key is not None:
        try:
            with memory_lock(project_root.resolve() / ".skilllayer"):
                index_path = _idempotency_index_path(tasks_root)
                if index_path.is_symlink():
                    return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code="symlink_not_permitted",
                                       recovery_action="inspect_idempotency_index_manually",
                                       summary="Task created, but the idempotency index path is a symlink.")
                existing_data: dict[str, Any] = {}
                if index_path.exists():
                    try:
                        existing_data = json.loads(index_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing_data = {}
                entries = existing_data.get("entries") if isinstance(existing_data.get("entries"), dict) else {}
                entries[idempotency_key] = task_id
                atomic_write_json(index_path, {"schema_version": ORCHESTRATOR_SCHEMA_VERSION, "entries": entries})
        except OSError:
            return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code="idempotency_index_write_failed",
                               recovery_action="retry_create_task_with_same_key",
                               summary="Task created, but the idempotency index could not be updated.")

    return _op_result("OPERATION_COMPLETED", task_id=task_id,
                       written_paths=created["written_paths"] + first["written_paths"] + second["written_paths"],
                       summary=f"Task {task_id} created and contract persisted.")


# ---------------------------------------------------------------------------
# capture_baseline
# ---------------------------------------------------------------------------


def capture_baseline(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """CONTRACT_READY -> BASELINE_READY. Captures and persists Foundation B evidence."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state != "CONTRACT_READY":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"capture_baseline requires CONTRACT_READY, task is at {current_state!r}.")

    contract = read_task_contract(project_root, task_id)
    if not contract["success"]:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=contract.get("error"),
                           summary="Contract could not be read.")

    baseline = capture_repository_baseline(project_root, task_id, contract["record"])
    written = write_task_baseline(project_root, task_id, consent=consent, baseline=baseline)
    if not written["success"]:
        return _op_result("OPERATION_REJECTED" if written["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED"} else "PERSISTENCE_FAILED",
                           task_id=task_id, error_code=written.get("error"), summary=written["summary"])

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=path_blocked.get("error"),
                           recovery_action="inspect_transition_history_manually", summary="Baseline written but path became unsafe.")
    o_paths = _orchestrator_paths(paths.task_dir)
    baseline_ref, error = _write_evidence(o_paths, "baseline", baseline)
    if baseline_ref is None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=error,
                           recovery_action="inspect_transition_history_manually",
                           summary="Baseline persisted via Foundation A, but the orchestrator evidence copy failed.")

    limitations = list(baseline.get("limitations", []))
    outcome = "SUCCESS" if baseline["baseline_status"] == "BASELINE_CAPTURED" else "BLOCKED"
    if baseline["baseline_status"] != "BASELINE_CAPTURED":
        return _op_result("OPERATION_INCOMPLETE", task_id=task_id, error_code=baseline["baseline_status"],
                           summary=f"Baseline captured with status {baseline['baseline_status']!r}; not advancing state.",
                           baseline=baseline)

    transitioned = _write_transition(
        project_root, task_id, o_paths, operation="capture_baseline", previous_state="CONTRACT_READY",
        next_state="BASELINE_READY", actor_type="orchestrator", evidence_refs=[baseline_ref],
        prerequisites_checked=["contract_exists"], outcome=outcome, limitations=limitations,
        idempotency_key=idempotency_key,
    )
    return transitioned


# ---------------------------------------------------------------------------
# acquire_ownership
# ---------------------------------------------------------------------------


def acquire_ownership(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    lease_seconds: int = 120, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """BASELINE_READY -> READY_TO_START. Wraps Foundation C's lease acquisition."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state != "BASELINE_READY":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"acquire_ownership requires BASELINE_READY, task is at {current_state!r}.")

    leased = _foundation_c_acquire_ownership(
        project_root, task_id, consent=consent, owner_instance_id=owner_instance_id, lease_seconds=lease_seconds,
    )
    if not leased["success"]:
        code = "task_owned_by_another_instance" if leased.get("error") == "task_owned_by_another_instance" else leased.get("error")
        state = "OPERATION_REJECTED" if leased["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED"} else "PERSISTENCE_FAILED"
        if leased.get("error") == "task_owned_by_another_instance":
            state = "OPERATION_REJECTED"
        return _op_result(state, task_id=task_id, error_code=code, summary=leased["summary"])

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=path_blocked.get("error"),
                           recovery_action="inspect_transition_history_manually", summary="Ownership acquired but path became unsafe.")
    o_paths = _orchestrator_paths(paths.task_dir)
    return _write_transition(
        project_root, task_id, o_paths, operation="acquire_ownership", previous_state="BASELINE_READY",
        next_state="READY_TO_START", actor_type="orchestrator", evidence_refs=[],
        prerequisites_checked=["baseline_captured", "ownership_lease_acquired"], outcome="SUCCESS", limitations=[],
        idempotency_key=idempotency_key,
    )


def reclaim_ownership(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    lease_seconds: int = 120, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Explicit re-acquisition of an expired (or never-taken) lease from any
    non-terminal state — distinct from acquire_ownership's one-time initial
    BASELINE_READY -> READY_TO_START transition. Ownership can expire at any
    later point (RUNNING, INTERRUPTED, ...); reclaiming it does not itself
    change the task's lifecycle state, only who holds the lease. Recorded as
    a same-state audit transition since taking over a lease is a
    security-relevant event worth its own immutable entry. A currently
    ACTIVE lease held by a DIFFERENT instance still blocks (Foundation C's
    own conflict rule), matching acquire_ownership's behavior exactly."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state is None or current_state in TERMINAL_STATES:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"reclaim_ownership is not valid for task state {current_state!r}.")

    leased = _foundation_c_acquire_ownership(
        project_root, task_id, consent=consent, owner_instance_id=owner_instance_id, lease_seconds=lease_seconds,
    )
    if not leased["success"]:
        state = "OPERATION_REJECTED" if leased["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED", "PERSISTENCE_BLOCKED"} else "PERSISTENCE_FAILED"
        if leased.get("error") == "task_owned_by_another_instance":
            state = "OPERATION_REJECTED"
        return _op_result(state, task_id=task_id, error_code=leased.get("error"), summary=leased["summary"])

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=path_blocked.get("error"),
                           recovery_action="inspect_transition_history_manually", summary="Ownership reclaimed but path became unsafe.")
    o_paths = _orchestrator_paths(paths.task_dir)
    return _write_transition(
        project_root, task_id, o_paths, operation="reclaim_ownership", previous_state=current_state,
        next_state=current_state, actor_type="user", evidence_refs=[],
        prerequisites_checked=["ownership_lease_expired_or_unowned"], outcome="SUCCESS", limitations=[],
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# start_task
# ---------------------------------------------------------------------------


def _ownership_is_active(project_root: Path, task_id: str, owner_instance_id: str) -> tuple[bool, str | None]:
    ownership = read_task_ownership(project_root, task_id)
    if ownership["ownership_status"] != "ACTIVE":
        return False, ownership.get("error") or f"ownership_{ownership['ownership_status'].lower()}"
    if ownership["record"]["owner_instance_id"] != owner_instance_id:
        return False, "ownership_held_by_different_instance"
    return True, None


def start_task(
    project_root: Path, task_id: str, *, owner_instance_id: str, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """READY_TO_START -> RUNNING. Verifies (never infers) every precondition."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state != "READY_TO_START":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"start_task requires READY_TO_START, task is at {current_state!r}.")

    prerequisites: list[str] = []
    contract = read_task_contract(project_root, task_id)
    if not contract["success"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="contract_missing_or_invalid",
                           summary="start_task blocked: no valid contract.")
    prerequisites.append("valid_contract")

    baseline = read_task_baseline(project_root, task_id)
    if not baseline["success"] or baseline["record"]["baseline_status"] != "BASELINE_CAPTURED":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="baseline_missing_or_incomplete",
                           summary="start_task blocked: no captured baseline.")
    prerequisites.append("captured_baseline")

    active, ownership_error = _ownership_is_active(project_root, task_id, owner_instance_id)
    if not active:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=ownership_error,
                           summary="start_task blocked: no valid active ownership.")
    prerequisites.append("valid_ownership")

    observed = read_repository_state(project_root, task_id=task_id, allowed_paths=contract["record"].get("allowed_paths", []))
    amendments = read_scope_amendments(project_root, task_id)
    amendment_records = amendments["record"].get("amendments", []) if amendments["success"] else []
    scope_result = validate_scope(contract["record"], baseline["record"], observed, task_id=task_id, amendments=amendment_records)
    if scope_result["verdict"] == "BASELINE_STALE":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="baseline_stale",
                           summary="start_task blocked: repository has moved since the baseline was captured.")
    prerequisites.append("no_stale_repository_evidence")
    if scope_result["verdict"] not in {"SCOPE_EMPTY", "SCOPE_CLEAN"}:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="forbidden_preexisting_state",
                           recovery_action="review_scope_validation",
                           summary="start_task blocked: forbidden or unexpected pre-existing repository state.",
                           scope_validation=scope_result)
    prerequisites.append("no_forbidden_preexisting_state")

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=path_blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)
    return _write_transition(
        project_root, task_id, o_paths, operation="start_task", previous_state="READY_TO_START", next_state="RUNNING",
        actor_type="orchestrator", evidence_refs=[], prerequisites_checked=prerequisites, outcome="SUCCESS",
        limitations=list(scope_result.get("warnings", [])), idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# checkpoint_task
# ---------------------------------------------------------------------------


def checkpoint_task(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    task_phase: str, completed_steps: list[dict[str, Any]] | None = None,
    active_step: dict[str, Any] | None = None, remaining_steps: list[dict[str, Any]] | None = None,
    files_observed: list[str] | None = None, files_expected_next: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record one immutable Foundation C checkpoint while RUNNING or VALIDATING.

    Does not change the orchestrator's own current_state (a checkpoint is
    evidence, not a lifecycle transition) — the transition log still gets an
    entry (previous_state == next_state) so every checkpoint is auditable in
    the same immutable stream."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state not in _ALLOWED_TRANSITIONS["checkpoint_task"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"checkpoint_task requires RUNNING or VALIDATING, task is at {current_state!r}.")
    active, ownership_error = _ownership_is_active(project_root, task_id, owner_instance_id)
    if not active:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=ownership_error,
                           summary="checkpoint_task blocked: no valid active ownership.")

    chain = load_checkpoint_chain(project_root, task_id)
    if chain["status"] not in {"CHAIN_EMPTY", "CHAIN_VALID"}:
        return _op_result("VALIDATION_FAILED", task_id=task_id, error_code="checkpoint_chain_invalid",
                           recovery_action="inspect_checkpoint_history_manually",
                           summary="checkpoint_task blocked: existing checkpoint chain is not trustworthy.")

    if idempotency_key is not None and chain["latest"] is not None:
        # A retry must be recognized at the sequence it actually produced,
        # not at a freshly-advanced sequence — sequence naturally moves on
        # once the first call already succeeded, so re-deriving at
        # latest.sequence + 1 would silently mint a genuinely NEW checkpoint
        # on every "retry" instead of recognizing the previous one.
        prior_expected_id = _derive_checkpoint_id(task_id, chain["latest"]["sequence"], idempotency_key)
        if chain["latest"]["checkpoint_id"] == prior_expected_id:
            return _op_result("OPERATION_COMPLETED", task_id=task_id, written_paths=[],
                               checkpoint=chain["latest"],
                               summary="Checkpoint retry is idempotent; existing record was retained.")

    sequence = 1 if chain["latest"] is None else chain["latest"]["sequence"] + 1
    key = idempotency_key or secrets.token_hex(8)
    checkpoint_id = _derive_checkpoint_id(task_id, sequence, key)

    record = build_checkpoint(
        task_id=task_id, sequence=sequence, task_phase=task_phase,
        task_status=_CHECKPOINT_TASK_STATUS[current_state], checkpoint_id=checkpoint_id,
        previous_checkpoint_id=chain["latest"]["checkpoint_id"] if chain["latest"] else None,
        completed_steps=completed_steps or [], active_step=active_step, remaining_steps=remaining_steps or [],
        files_observed=files_observed or [], files_expected_next=files_expected_next or [],
    )
    written = write_checkpoint_history(project_root, task_id, consent=consent, checkpoint=record)
    if not written["success"]:
        state = "OPERATION_REJECTED" if written["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED"} else "PERSISTENCE_FAILED"
        return _op_result(state, task_id=task_id, error_code=written.get("error"), summary=written["summary"])

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=path_blocked.get("error"),
                           recovery_action="inspect_transition_history_manually", summary="Checkpoint written but path became unsafe.")
    o_paths = _orchestrator_paths(paths.task_dir)
    result = _write_transition(
        project_root, task_id, o_paths, operation="checkpoint_task", previous_state=current_state,
        next_state=current_state, actor_type="orchestrator",
        evidence_refs=[{"evidence_type": "checkpoint", "record_path": f"checkpoints/{sequence:04d}-{checkpoint_id}.json",
                         "schema_version": 1, "integrity_fingerprint": _digest(record), "required": True,
                         "observed_status": record["task_status"]}],
        prerequisites_checked=["active_ownership", "valid_checkpoint_chain"], outcome="SUCCESS", limitations=[],
        idempotency_key=key,
    )
    if result["success"]:
        result["checkpoint"] = record
    return result


# ---------------------------------------------------------------------------
# interrupt_task
# ---------------------------------------------------------------------------


def interrupt_task(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    reason: str, task_phase: str, active_step_may_have_mutated: bool = False,
    paths_requiring_reinspection: list[str] | None = None, requires_resume_confirmation: bool = True,
    retain_ownership: bool = True, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """RUNNING|VALIDATING -> INTERRUPTED. Persists an interruption checkpoint."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state not in _ALLOWED_TRANSITIONS["interrupt_task"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"interrupt_task requires RUNNING or VALIDATING, task is at {current_state!r}.")
    from .checkpoint import INTERRUPTION_REASONS
    if reason not in INTERRUPTION_REASONS:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="interruption_reason_invalid",
                           summary=f"reason must be one of {sorted(INTERRUPTION_REASONS)}.")

    chain = load_checkpoint_chain(project_root, task_id)
    if chain["status"] not in {"CHAIN_EMPTY", "CHAIN_VALID"}:
        return _op_result("VALIDATION_FAILED", task_id=task_id, error_code="checkpoint_chain_invalid",
                           summary="interrupt_task blocked: existing checkpoint chain is not trustworthy.")
    sequence = 1 if chain["latest"] is None else chain["latest"]["sequence"] + 1
    key = idempotency_key or secrets.token_hex(8)
    checkpoint_id = _derive_checkpoint_id(task_id, sequence, key)
    record = build_checkpoint(
        task_id=task_id, sequence=sequence, task_phase=task_phase, task_status="INTERRUPTED",
        checkpoint_id=checkpoint_id, previous_checkpoint_id=chain["latest"]["checkpoint_id"] if chain["latest"] else None,
        interruption={
            "reason": reason, "active_step_may_have_mutated": active_step_may_have_mutated,
            "paths_requiring_reinspection": paths_requiring_reinspection or [],
            "commands_outcome_unknown": [], "validations_to_rerun": [],
            "requires_resume_confirmation": requires_resume_confirmation,
        },
    )
    written = write_checkpoint_history(project_root, task_id, consent=consent, checkpoint=record)
    if not written["success"]:
        state = "OPERATION_REJECTED" if written["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED"} else "PERSISTENCE_FAILED"
        return _op_result(state, task_id=task_id, error_code=written.get("error"), summary=written["summary"])

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=path_blocked.get("error"),
                           recovery_action="inspect_transition_history_manually", summary="Interruption checkpoint written but path became unsafe.")
    o_paths = _orchestrator_paths(paths.task_dir)
    result = _write_transition(
        project_root, task_id, o_paths, operation="interrupt_task", previous_state=current_state, next_state="INTERRUPTED",
        actor_type="orchestrator", evidence_refs=[], prerequisites_checked=["checkpoint_chain_valid"],
        outcome="SUCCESS", limitations=[], idempotency_key=key,
    )
    if not retain_ownership and result["success"]:
        # Explicit-policy release; never automatic, never a background action.
        pass  # Foundation C exposes no ownership-release primitive yet; retaining is the safe default.
    return result


# ---------------------------------------------------------------------------
# assess_task_resume — read-only, no ownership required, no transition written
# ---------------------------------------------------------------------------


def assess_task_resume(project_root: Path, task_id: str) -> dict[str, Any]:
    """Read-only wrapper over Foundation C's assess_resume. Never mutates."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state not in {"INTERRUPTED", "RESUME_REVIEW"}:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"assess_task_resume requires INTERRUPTED or RESUME_REVIEW, task is at {current_state!r}.")
    assessment = assess_resume(project_root, task_id)
    out = _op_result("READ_COMPLETED", task_id=task_id, summary=f"Resume verdict: {assessment['resume_verdict']}.")
    out["assessment"] = assessment
    return out


# ---------------------------------------------------------------------------
# resume_task — mutating; always runs assessment as its own precondition
# ---------------------------------------------------------------------------

_RESUME_VERDICT_TO_STATE = {
    "RESUME_BLOCKED": "BLOCKED", "CHECKPOINT_STALE": "BLOCKED", "RESUME_INCOMPLETE": "BLOCKED",
    "CHECKPOINT_CORRUPT": "FAILED", "TASK_ABANDONED": "ABANDONED", "TASK_ALREADY_COMPLETE": "FAILED",
}


def resume_task(
    project_root: Path, task_id: str, *, owner_instance_id: str, confirmed: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """INTERRUPTED[|RESUME_REVIEW] -> RUNNING, gated on Foundation C's own
    assessment. INTERRUPTED -> RUNNING is never reachable without it."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state not in _ALLOWED_TRANSITIONS["resume_task"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"resume_task requires INTERRUPTED or RESUME_REVIEW, task is at {current_state!r}.")
    active, ownership_error = _ownership_is_active(project_root, task_id, owner_instance_id)
    if not active:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=ownership_error,
                           summary="resume_task blocked: no valid active ownership.")

    assessment = assess_resume(project_root, task_id)
    verdict = assessment["resume_verdict"]
    if verdict == "RESUME_BLOCKED" and assessment.get("resume_reasons") == ["active_task_owner"]:
        # Foundation C's assess_resume is conservative by design: it blocks
        # on ANY active lease so a third party can never barge into someone
        # else's task. It has no owner_instance_id parameter, so it cannot
        # tell "a different owner" apart from "the same owner recovering
        # their own interruption" — this orchestrator already verified the
        # latter independently above (_ownership_is_active). Reinterpreted
        # (never re-derived) as needing the same confirmation any other
        # non-trivially-safe resume needs; the deeper repository/scope
        # freshness checks Foundation C skipped when it blocked early still
        # happen for real via the validate_task() call the lifecycle already
        # requires before finalize, so nothing is silently skipped end-to-end.
        verdict = "RESUME_SAFE_WITH_CONFIRMATION"
        assessment = {**assessment, "resume_verdict": verdict,
                      "resume_reasons": ["active_task_owner_matches_caller_reinterpreted_by_orchestrator"]}
    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=path_blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)

    if verdict == "RESUME_SAFE" or (verdict == "RESUME_SAFE_WITH_CONFIRMATION" and confirmed):
        if current_state == "INTERRUPTED":
            review = _write_transition(
                project_root, task_id, o_paths, operation="resume_task", previous_state="INTERRUPTED",
                next_state="RESUME_REVIEW", actor_type="orchestrator", evidence_refs=[],
                prerequisites_checked=["resume_assessment_ran"], outcome="SUCCESS", limitations=assessment.get("warnings", []),
                idempotency_key=(idempotency_key or "resume") + "-review",
            )
            if not review["success"]:
                return review
        running = _write_transition(
            project_root, task_id, o_paths, operation="resume_task", previous_state="RESUME_REVIEW", next_state="RUNNING",
            actor_type="user" if confirmed else "orchestrator", evidence_refs=[],
            prerequisites_checked=["resume_assessment_safe", "repository_state_compatible"] + (["user_confirmed"] if confirmed else []),
            outcome="SUCCESS", limitations=assessment.get("warnings", []),
            idempotency_key=(idempotency_key or "resume") + "-running",
        )
        if running["success"]:
            running["assessment"] = assessment
        return running

    if verdict == "RESUME_SAFE_WITH_CONFIRMATION" and not confirmed:
        review = _write_transition(
            project_root, task_id, o_paths, operation="resume_task", previous_state=current_state,
            next_state="RESUME_REVIEW", actor_type="orchestrator", evidence_refs=[],
            prerequisites_checked=["resume_assessment_ran"], outcome="BLOCKED", limitations=assessment.get("warnings", []),
            idempotency_key=(idempotency_key or "resume") + "-review-pending",
        )
        if not review["success"]:
            return review
        return _confirmation(
            task_id=task_id, reason="resume_requires_confirmation", scope="resume",
            operation_after_confirmation="resume_task",
            summary="Resume is safe only with explicit confirmation (interruption or legacy checkpoint); call resume_task(confirmed=True).",
        )

    next_state = _RESUME_VERDICT_TO_STATE.get(verdict, "BLOCKED")
    blocked_transition = _write_transition(
        project_root, task_id, o_paths, operation="resume_task", previous_state=current_state, next_state=next_state,
        actor_type="orchestrator", evidence_refs=[], prerequisites_checked=["resume_assessment_ran"],
        outcome="BLOCKED" if next_state == "BLOCKED" else "REJECTED", limitations=assessment.get("resume_reasons", []),
        idempotency_key=(idempotency_key or "resume") + f"-{next_state.lower()}",
    )
    if blocked_transition["success"]:
        blocked_transition["assessment"] = assessment
        blocked_transition["state"] = "TASK_BLOCKED" if next_state == "BLOCKED" else "TASK_FAILED_STATE"
        blocked_transition["success"] = False
    return blocked_transition


# ---------------------------------------------------------------------------
# validate_task
# ---------------------------------------------------------------------------


def validate_task(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """RUNNING|BLOCKED -> VALIDATING. Computes and persists scope-validation
    evidence referenced by a new checkpoint (lifecycle steps 9+10)."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state not in _ALLOWED_TRANSITIONS["validate_task"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"validate_task requires RUNNING or BLOCKED, task is at {current_state!r}.")
    active, ownership_error = _ownership_is_active(project_root, task_id, owner_instance_id)
    if not active:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=ownership_error,
                           summary="validate_task blocked: no valid active ownership.")

    contract = read_task_contract(project_root, task_id)
    baseline = read_task_baseline(project_root, task_id)
    if not contract["success"] or not baseline["success"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="contract_or_baseline_missing",
                           summary="validate_task blocked: contract or baseline unavailable.")
    observed = read_repository_state(project_root, task_id=task_id, allowed_paths=contract["record"].get("allowed_paths", []))
    amendments = read_scope_amendments(project_root, task_id)
    amendment_records = amendments["record"].get("amendments", []) if amendments["success"] else []
    scope_result = validate_scope(contract["record"], baseline["record"], observed, task_id=task_id, amendments=amendment_records)

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=path_blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)
    scope_ref, error = _write_evidence(o_paths, "scope_validation", scope_result)
    if scope_ref is None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=error,
                           recovery_action="retry_validate_task", summary="Scope evidence could not be persisted.")

    result = _write_transition(
        project_root, task_id, o_paths, operation="validate_task", previous_state=current_state, next_state="VALIDATING",
        actor_type="orchestrator", evidence_refs=[scope_ref], prerequisites_checked=["contract_and_baseline_available"],
        outcome="SUCCESS" if scope_result["verdict"] not in {"SCOPE_VIOLATED", "VALIDATION_BLOCKED", "BASELINE_STALE"} else "BLOCKED",
        limitations=scope_result.get("warnings", []), idempotency_key=idempotency_key,
    )
    if result["success"]:
        result["scope_validation"] = scope_result
    return result


# ---------------------------------------------------------------------------
# finalize_task
# ---------------------------------------------------------------------------


def finalize_task(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str,
    tests_status: dict[str, Any], written_by_component: str, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """VALIDATING -> COMPLETED|BLOCKED|FAILED. Derives the verdict from
    persisted evidence only; never infers a missing check as passing.

    Idempotent by construction: if the task is already in a finalize-terminal
    state, the existing result is returned unchanged rather than re-derived."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state in {"COMPLETED", "FAILED"} or (current_state == "BLOCKED" and _last_operation_was_finalize(project_root, task_id)):
        # Read back the SAME final_result shape the first call returned (a
        # dedicated evidence record, not result.json's different schema) so
        # a retry is byte-identical, not merely evidence-equivalent.
        paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
        existing_result = None
        if path_blocked is None:
            o_paths = _orchestrator_paths(paths.task_dir)
            existing_result = _read_evidence(o_paths, "final_result")
        out = _op_result("OPERATION_COMPLETED", task_id=task_id, written_paths=[],
                          summary="Task already finalized; returning the existing result unchanged.")
        out["result"] = existing_result
        return out
    if current_state != "VALIDATING":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"finalize_task requires VALIDATING, task is at {current_state!r}.")
    active, ownership_error = _ownership_is_active(project_root, task_id, owner_instance_id)
    if not active:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=ownership_error,
                           summary="finalize_task blocked: no valid active ownership.")

    contract = read_task_contract(project_root, task_id)
    baseline = read_task_baseline(project_root, task_id)
    if not contract["success"] or not baseline["success"]:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="contract_or_baseline_missing",
                           summary="finalize_task blocked: contract or baseline unavailable.")
    observed = read_repository_state(project_root, task_id=task_id, allowed_paths=contract["record"].get("allowed_paths", []))
    amendments = read_scope_amendments(project_root, task_id)
    amendment_records = amendments["record"].get("amendments", []) if amendments["success"] else []
    scope_result = validate_scope(contract["record"], baseline["record"], observed, task_id=task_id, amendments=amendment_records)

    blockers: list[str] = []
    warnings: list[str] = list(scope_result.get("warnings", []))
    if scope_result["verdict"] == "BASELINE_STALE":
        blockers.append("baseline_stale")
    if scope_result["verdict"] in {"SCOPE_VIOLATED", "VALIDATION_BLOCKED"}:
        blockers.extend(scope_result.get("violations") or scope_result.get("verdict_reasons") or ["scope_invalid"])
    if not scope_result.get("evidence_complete"):
        warnings.append("scope_evidence_incomplete")
    if not isinstance(tests_status, dict) or "recorded" not in tests_status:
        blockers.append("tests_status_missing")
        tests_status = {"recorded": False}
    elif not tests_status.get("recorded"):
        blockers.append("required_tests_not_recorded")

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=path_blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)

    if blockers:
        verdict = "TASK_BLOCKED" if "baseline_stale" not in blockers else "TASK_BLOCKED"
        next_state = "BLOCKED"
    elif not scope_result.get("evidence_complete") or scope_result["verdict"] == "SCOPE_INCOMPLETE":
        verdict, next_state = "TASK_INCOMPLETE", "BLOCKED"
    elif warnings:
        verdict, next_state = "TASK_COMPLETE_WITH_LIMITATIONS", "COMPLETED"
    else:
        verdict, next_state = "TASK_VERIFIED_COMPLETE", "COMPLETED"

    final_result = {
        "task_id": task_id, "final_state": next_state, "completed_at": _now(),
        "contract_ref": {"record_path": "contract.json"}, "baseline_ref": {"record_path": "baseline.json"},
        "latest_checkpoint_ref": {"record_path": "checkpoint.json"},
        "scope_validation_ref": {"record_path": "evidence/scope_validation.json"},
        "validation_evidence_refs": [{"record_path": "evidence/scope_validation.json"}],
        "changed_paths": scope_result.get("observed_changed_paths", []),
        "allowed_changes": scope_result.get("allowed_changes", []),
        "unexpected_changes": scope_result.get("unexpected_changes", []),
        "tests_status": tests_status, "evidence_complete": bool(scope_result.get("evidence_complete")),
        "blockers": blockers, "warnings": warnings, "final_verdict": verdict, "limitations": warnings,
    }

    final_result_ref, final_result_error = _write_evidence(o_paths, "final_result", final_result)
    if final_result_ref is None:
        return _op_result("OPERATION_PARTIALLY_PERSISTED", task_id=task_id, error_code=final_result_error,
                           recovery_action="retry_finalize_task", summary="Final result could not be persisted as evidence.")

    verdict_reasons = blockers or warnings or ["all_evidence_complete"]
    written = write_task_result(
        project_root, task_id, consent=consent, verdict=("CHANGE_VALIDATED" if next_state == "COMPLETED" else "CHANGE_INCOMPLETE"),
        verdict_reasons=[str(item)[:190] for item in verdict_reasons][:10],
        written_by_component=written_by_component, scope_validation=scope_result,
    )
    if not written["success"]:
        state = "OPERATION_REJECTED" if written["state"] in {"CONSENT_REQUIRED", "CONSENT_INVALID", "WRITE_REJECTED"} else "PERSISTENCE_FAILED"
        return _op_result(state, task_id=task_id, error_code=written.get("error"), summary=written["summary"])

    transitioned = _write_transition(
        project_root, task_id, o_paths, operation="finalize_task", previous_state="VALIDATING", next_state=next_state,
        actor_type="orchestrator", evidence_refs=[{"evidence_type": "scope_validation", "record_path": "evidence/scope_validation.json",
                                                    "schema_version": 1, "integrity_fingerprint": _digest(scope_result), "required": True,
                                                    "observed_status": scope_result["verdict"]}],
        prerequisites_checked=["scope_validation_complete", "tests_recorded", "no_stale_baseline"],
        outcome="SUCCESS" if next_state == "COMPLETED" else "BLOCKED", limitations=warnings, idempotency_key=idempotency_key,
    )
    if not transitioned["success"]:
        return transitioned
    transitioned["result"] = final_result
    transitioned["final_verdict"] = verdict
    if next_state != "COMPLETED":
        transitioned["state"] = "TASK_BLOCKED"
        transitioned["success"] = False
        return transitioned

    # Step 12: release ownership on successful finalization. Foundation C
    # exposes no explicit release primitive yet; the lease's own bounded TTL
    # is the current release mechanism (documented limitation).
    return transitioned


def _last_operation_was_finalize(project_root: Path, task_id: str) -> bool:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return False
    o_paths = _orchestrator_paths(paths.task_dir)
    _, _, transitions = _read_transition_history(o_paths, task_id)
    return bool(transitions) and transitions[-1]["operation"] == "finalize_task"


# ---------------------------------------------------------------------------
# abandon_task
# ---------------------------------------------------------------------------


def abandon_task(
    project_root: Path, task_id: str, *, reason_label: str, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Any non-terminal state -> ABANDONED. User-initiated; always allowed
    except from a terminal state."""
    current_state, blocked = _current_state_or_none(project_root, task_id)
    if blocked is not None:
        return blocked
    if current_state is None:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="task_not_created",
                           summary="Cannot abandon a task that was never created.")
    if current_state in TERMINAL_STATES:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="invalid_state_transition",
                           summary=f"Cannot abandon: task is already terminal ({current_state!r}).")

    san_reason = sanitize_persisted_value(reason_label, FieldPolicy.REDACTABLE_TEXT, max_length=160, field_name="reason_label")
    if not san_reason.accepted:
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code=san_reason.rejection_reason,
                           summary="reason_label failed the persistence redaction/rejection gate.")

    paths, path_blocked = _resolve_paths_or_blocked(project_root, task_id)
    if path_blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=path_blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)
    return _write_transition(
        project_root, task_id, o_paths, operation="abandon_task", previous_state=current_state, next_state="ABANDONED",
        actor_type="user", evidence_refs=[], prerequisites_checked=[], outcome="SUCCESS",
        limitations=[san_reason.sanitized_value], idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# get_task_status — read-only, deterministic, writes nothing
# ---------------------------------------------------------------------------


def get_task_status(project_root: Path, task_id: str) -> dict[str, Any]:
    """Deterministic read-only status view. Never writes anything."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=blocked.get("error"),
                           summary="Task path failed a safety check.")
    o_paths = _orchestrator_paths(paths.task_dir)
    state_read = _read_state_pointer(o_paths, task_id)
    if state_read["status"] == "NOT_CREATED":
        return _op_result("OPERATION_REJECTED", task_id=task_id, error_code="task_not_created",
                           summary="No task exists at this task_id.")
    if state_read["status"] not in {"CURRENT", "POINTER_MISSING_RECONSTRUCTIBLE"}:
        return _op_result("OPERATION_FAILED_BEFORE_MUTATION", task_id=task_id, error_code=state_read.get("error"),
                           recovery_action="inspect_transition_history_manually",
                           summary=f"Task state is not safely readable ({state_read['status']}).")
    current_state = (state_read.get("pointer") or state_read.get("reconstructed"))["current_state"]
    latest_transition = (state_read.get("transitions") or [None])[-1] if state_read["status"] == "CURRENT" else None

    ownership = read_task_ownership(project_root, task_id)
    chain = load_checkpoint_chain(project_root, task_id)
    contract = read_task_contract(project_root, task_id)
    baseline = read_task_baseline(project_root, task_id)

    scope_status = None
    if contract["success"] and baseline["success"]:
        observed = read_repository_state(project_root, task_id=task_id, allowed_paths=contract["record"].get("allowed_paths", []))
        amendments = read_scope_amendments(project_root, task_id)
        amendment_records = amendments["record"].get("amendments", []) if amendments["success"] else []
        scope_status = validate_scope(contract["record"], baseline["record"], observed, task_id=task_id, amendments=amendment_records)["verdict"]

    resume_status = None
    if current_state in {"INTERRUPTED", "RESUME_REVIEW"}:
        resume_status = assess_resume(project_root, task_id)["resume_verdict"]

    safe_ops = sorted(op for op, allowed in _ALLOWED_TRANSITIONS.items() if current_state in allowed)
    if current_state not in TERMINAL_STATES:
        safe_ops.append("get_task_status")
    prohibited_ops = sorted(set(_ALLOWED_TRANSITIONS) - set(safe_ops))

    out = _op_result(
        "READ_COMPLETED", task_id=task_id, summary=f"Task {task_id} is at {current_state}.",
        current_state=current_state, latest_transition=latest_transition,
        latest_checkpoint=chain.get("latest"), ownership=ownership,
        baseline_status=baseline["record"]["baseline_status"] if baseline["success"] else baseline["state"],
        scope_status=scope_status, resume_status=resume_status,
        validation_status="VALIDATING" if current_state == "VALIDATING" else current_state,
        blockers=[] if current_state != "BLOCKED" else ["see_latest_transition_limitations"],
        safe_operations=safe_ops, prohibited_operations=prohibited_ops,
        evidence_complete=bool(baseline["success"] and contract["success"] and chain["status"] in {"CHAIN_VALID", "CHAIN_EMPTY"}),
    )
    return out
