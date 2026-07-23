"""Bounded, append-only VTE checkpoint records.

This is an internal Foundation C primitive.  It never executes a resume plan,
edits source files, or exposes a workflow.  A checkpoint is factual structured
state plus references to separately persisted evidence, not a prose summary.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .persistence import (
    FieldPolicy,
    TaskConsent,
    _check_consent,
    _contains_rejected_secret,
    _rel,
    _resolve_paths_or_blocked,
    _result,
    sanitize_persisted_value,
)

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINTS = 100
MAX_STEPS = 64
MAX_COMMANDS = 32
MAX_VALIDATIONS = 32
MAX_PATHS = 128
MAX_EVIDENCE_REFS = 64
MAX_QUESTIONS = 32
_CHECKPOINT_ID_RE = re.compile(r"^cp-[a-f0-9]{12,32}$")
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

TASK_STATUSES = frozenset({
    "NOT_STARTED", "IN_PROGRESS", "BLOCKED", "INTERRUPTED", "VALIDATING",
    "COMPLETED", "FAILED", "ABANDONED",
})
STEP_STATUSES = frozenset({"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED", "SKIPPED"})
ACTION_TYPES = frozenset({
    "READ_FILE", "INSPECT_DIFF", "RUN_TEST", "RUN_COMMAND", "VALIDATE_SCOPE",
    "REQUEST_CONFIRMATION", "CREATE_CHECKPOINT", "RECORD_RESULT",
})
INTERRUPTION_REASONS = frozenset({
    "PROCESS_TERMINATED", "HOST_SHUTDOWN", "USER_STOPPED", "TIME_LIMIT",
    "DEPENDENCY_UNAVAILABLE", "TOOL_FAILURE", "VALIDATION_FAILURE", "UNKNOWN_INTERRUPTION",
})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_digest(value: dict[str, Any]) -> str:
    material = {key: item for key, item in value.items() if key != "integrity_fingerprint"}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def new_checkpoint_id() -> str:
    return "cp-" + secrets.token_hex(8)


def _bounded_list(value: Any, maximum: int) -> bool:
    return isinstance(value, list) and len(value) <= maximum


def _safe_paths(value: Any) -> bool:
    if not _bounded_list(value, MAX_PATHS):
        return False
    for item in value:
        if not isinstance(item, str) or not item or item.startswith("/") or "\\" in item or ".." in item.split("/"):
            return False
    return True


def _valid_step(step: Any) -> bool:
    return (
        isinstance(step, dict)
        and set(step) == {"step_id", "status", "action_type", "evidence_refs"}
        and isinstance(step["step_id"], str) and _STEP_ID_RE.fullmatch(step["step_id"])
        and step["status"] in STEP_STATUSES and step["action_type"] in ACTION_TYPES
        and isinstance(step["evidence_refs"], list) and len(step["evidence_refs"]) <= MAX_EVIDENCE_REFS
    )


def _valid_evidence_ref(ref: Any) -> bool:
    return (
        isinstance(ref, dict)
        and set(ref) == {"evidence_type", "record_path", "schema_version", "integrity_fingerprint", "required", "observed_status"}
        and isinstance(ref["evidence_type"], str) and bool(ref["evidence_type"])
        and isinstance(ref["record_path"], str) and ref["record_path"] and not ref["record_path"].startswith("/")
        and ".." not in ref["record_path"].split("/") and "\\" not in ref["record_path"]
        and isinstance(ref["schema_version"], int) and ref["schema_version"] >= 1
        and isinstance(ref["integrity_fingerprint"], str) and _SHA256_RE.fullmatch(ref["integrity_fingerprint"])
        and isinstance(ref["required"], bool)
        and isinstance(ref["observed_status"], str) and bool(ref["observed_status"])
    )


def _valid_command(command: Any) -> bool:
    required = {"command_id", "command_kind", "normalized_executable", "argument_metadata", "working_directory", "started_at", "finished_at", "exit_code", "timed_out", "output_recorded", "output_digest", "result_status"}
    return (
        isinstance(command, dict) and set(command) == required
        and all(isinstance(command[key], str) and len(command[key]) <= 160 for key in ("command_id", "command_kind", "normalized_executable", "working_directory", "result_status"))
        and _ISO_RE.fullmatch(command["started_at"])
        and (command["finished_at"] is None or (isinstance(command["finished_at"], str) and _ISO_RE.fullmatch(command["finished_at"])))
        and (command["exit_code"] is None or isinstance(command["exit_code"], int))
        and isinstance(command["timed_out"], bool) and isinstance(command["output_recorded"], bool)
        and (command["output_digest"] is None or (isinstance(command["output_digest"], str) and _SHA256_RE.fullmatch(command["output_digest"])))
        and isinstance(command["argument_metadata"], list) and len(command["argument_metadata"]) <= 16
    )


def _valid_validation(validation: Any) -> bool:
    required = {"validation_id", "validation_kind", "target", "started_at", "finished_at", "started_successfully", "exit_code", "passed", "tests_collected", "tests_passed", "tests_failed", "tests_skipped", "evidence_complete", "limitations"}
    return (
        isinstance(validation, dict) and set(validation) == required
        and all(isinstance(validation[key], str) and len(validation[key]) <= 160 for key in ("validation_id", "validation_kind", "target"))
        and _ISO_RE.fullmatch(validation["started_at"])
        and (validation["finished_at"] is None or (isinstance(validation["finished_at"], str) and _ISO_RE.fullmatch(validation["finished_at"])))
        and isinstance(validation["started_successfully"], bool) and isinstance(validation["passed"], bool) and isinstance(validation["evidence_complete"], bool)
        and (validation["exit_code"] is None or isinstance(validation["exit_code"], int))
        and all(isinstance(validation[key], int) and validation[key] >= 0 for key in ("tests_collected", "tests_passed", "tests_failed", "tests_skipped"))
        and isinstance(validation["limitations"], list) and len(validation["limitations"]) <= 16
    )


def validate_checkpoint(value: Any, *, task_id: str | None = None) -> dict[str, Any]:
    """Validate a Foundation C record without modifying it or following paths."""
    required = {
        "schema_version", "checkpoint_id", "task_id", "created_at", "sequence", "previous_checkpoint_id",
        "task_phase", "task_status", "completed_steps", "active_step", "remaining_steps", "blocked_steps",
        "evidence_refs", "repository_state_ref", "contract_ref", "baseline_ref", "scope_validation_ref",
        "commands_attempted", "validations_attempted", "files_observed", "files_expected_next",
        "unresolved_questions", "known_failures", "limitations", "resume_requirements", "integrity_fingerprint",
        "interruption",
    }
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != required:
        return {"valid": False, "errors": ["checkpoint_schema_invalid"]}
    if value["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        errors.append("checkpoint_schema_unsupported")
    if task_id is not None and value["task_id"] != task_id:
        errors.append("checkpoint_task_id_mismatch")
    if not isinstance(value["checkpoint_id"], str) or not _CHECKPOINT_ID_RE.fullmatch(value["checkpoint_id"]):
        errors.append("checkpoint_id_invalid")
    if not isinstance(value["task_id"], str) or not value["task_id"]:
        errors.append("task_id_invalid")
    if not isinstance(value["sequence"], int) or value["sequence"] < 1:
        errors.append("sequence_invalid")
    if value["sequence"] == 1 and value["previous_checkpoint_id"] is not None:
        errors.append("first_checkpoint_has_previous")
    if value["sequence"] > 1 and (not isinstance(value["previous_checkpoint_id"], str) or not _CHECKPOINT_ID_RE.fullmatch(value["previous_checkpoint_id"])):
        errors.append("previous_checkpoint_id_invalid")
    if not isinstance(value["created_at"], str) or not _ISO_RE.fullmatch(value["created_at"]):
        errors.append("created_at_invalid")
    if not isinstance(value["task_phase"], str) or len(value["task_phase"]) > 80:
        errors.append("task_phase_invalid")
    if value["task_status"] not in TASK_STATUSES:
        errors.append("task_status_invalid")
    interruption = value["interruption"]
    if interruption is not None:
        interruption_keys = {"reason", "active_step_may_have_mutated", "paths_requiring_reinspection", "commands_outcome_unknown", "validations_to_rerun", "requires_resume_confirmation"}
        if (
            not isinstance(interruption, dict) or set(interruption) != interruption_keys
            or interruption["reason"] not in INTERRUPTION_REASONS
            or not isinstance(interruption["active_step_may_have_mutated"], bool)
            or not isinstance(interruption["requires_resume_confirmation"], bool)
            or not _safe_paths(interruption["paths_requiring_reinspection"])
            or not _bounded_list(interruption["commands_outcome_unknown"], MAX_COMMANDS)
            or not _bounded_list(interruption["validations_to_rerun"], MAX_VALIDATIONS)
        ):
            errors.append("interruption_invalid")
    if value["task_status"] == "INTERRUPTED" and interruption is None:
        errors.append("interruption_required_for_interrupted_task")
    for key, maximum in (("completed_steps", MAX_STEPS), ("remaining_steps", MAX_STEPS), ("blocked_steps", MAX_STEPS)):
        if not _bounded_list(value[key], maximum) or not all(_valid_step(item) for item in value[key]):
            errors.append(f"{key}_invalid")
    if value["active_step"] is not None and not _valid_step(value["active_step"]):
        errors.append("active_step_invalid")
    if not _bounded_list(value["evidence_refs"], MAX_EVIDENCE_REFS) or not all(_valid_evidence_ref(item) for item in value["evidence_refs"]):
        errors.append("evidence_refs_invalid")
    for key in ("repository_state_ref", "contract_ref", "baseline_ref", "scope_validation_ref"):
        if value[key] is not None and not _valid_evidence_ref(value[key]):
            errors.append(f"{key}_invalid")
    if not _bounded_list(value["commands_attempted"], MAX_COMMANDS) or not all(_valid_command(item) for item in value["commands_attempted"]):
        errors.append("commands_attempted_invalid")
    if not _bounded_list(value["validations_attempted"], MAX_VALIDATIONS) or not all(_valid_validation(item) for item in value["validations_attempted"]):
        errors.append("validations_attempted_invalid")
    for key, maximum in (("files_observed", MAX_PATHS), ("files_expected_next", MAX_PATHS), ("unresolved_questions", MAX_QUESTIONS), ("known_failures", MAX_QUESTIONS), ("limitations", MAX_QUESTIONS), ("resume_requirements", MAX_QUESTIONS)):
        valid = _safe_paths(value[key]) if key in {"files_observed", "files_expected_next"} else _bounded_list(value[key], maximum)
        if not valid:
            errors.append(f"{key}_invalid")
        elif key not in {"files_observed", "files_expected_next"} and not all(isinstance(item, str) and 0 < len(item) <= 160 for item in value[key]):
            errors.append(f"{key}_invalid")
    if value["integrity_fingerprint"] != _canonical_digest(value):
        errors.append("checkpoint_integrity_mismatch")
    if _contains_rejected_secret(value):
        errors.append("checkpoint_sensitive_value_rejected")
    completed_ids = {item["step_id"] for item in value["completed_steps"]}
    if any(not item["evidence_refs"] for item in value["completed_steps"]):
        errors.append("completed_step_missing_evidence")
    if len(completed_ids) != len(value["completed_steps"]):
        errors.append("completed_step_duplicate")
    return {"valid": not errors, "errors": errors}


def build_checkpoint(*, task_id: str, sequence: int, task_phase: str, task_status: str, previous_checkpoint_id: str | None = None, checkpoint_id: str | None = None, created_at: str | None = None, **fields: Any) -> dict[str, Any]:
    """Build a bounded record for callers that already have factual evidence."""
    record = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION, "checkpoint_id": checkpoint_id or new_checkpoint_id(), "task_id": task_id,
        "created_at": created_at or _now(), "sequence": sequence, "previous_checkpoint_id": previous_checkpoint_id,
        "task_phase": task_phase, "task_status": task_status, "completed_steps": fields.get("completed_steps", []),
        "active_step": fields.get("active_step"), "remaining_steps": fields.get("remaining_steps", []), "blocked_steps": fields.get("blocked_steps", []),
        "evidence_refs": fields.get("evidence_refs", []), "repository_state_ref": fields.get("repository_state_ref"),
        "contract_ref": fields.get("contract_ref"), "baseline_ref": fields.get("baseline_ref"), "scope_validation_ref": fields.get("scope_validation_ref"),
        "commands_attempted": fields.get("commands_attempted", []), "validations_attempted": fields.get("validations_attempted", []),
        "files_observed": fields.get("files_observed", []), "files_expected_next": fields.get("files_expected_next", []),
        "unresolved_questions": fields.get("unresolved_questions", []), "known_failures": fields.get("known_failures", []),
        "limitations": fields.get("limitations", []), "resume_requirements": fields.get("resume_requirements", []),
        "interruption": fields.get("interruption"), "integrity_fingerprint": "",
    }
    record["integrity_fingerprint"] = _canonical_digest(record)
    return record


def _history_path(paths: Any, checkpoint: dict[str, Any]) -> Path:
    return paths.checkpoints_dir / f"{checkpoint['sequence']:04d}-{checkpoint['checkpoint_id']}.json"


def write_checkpoint_history(project_root: Path, task_id: str, *, consent: TaskConsent | None, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Append one immutable checkpoint and atomically refresh latest pointer."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    root = project_root.resolve()
    checked = validate_checkpoint(checkpoint, task_id=task_id)
    if not checked["valid"]:
        return _result("WRITE_REJECTED", task_id=task_id, error=checked["errors"][0], summary="No checkpoint written: checkpoint evidence is invalid.")
    history_path = _history_path(paths, checkpoint)
    planned = [_rel(history_path, root), _rel(paths.checkpoint_path, root)]
    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="checkpoint")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned, summary="No checkpoint written: explicit checkpoint consent is required.")
    from .resume import load_checkpoint_chain
    try:
        with memory_lock(root / ".skilllayer"):
            if paths.checkpoints_dir.is_symlink() or history_path.is_symlink() or paths.checkpoint_path.is_symlink():
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="symlink_not_permitted", summary="Checkpoint path is a symlink.")
            chain = load_checkpoint_chain(project_root, task_id)
            if chain["status"] not in {"CHAIN_EMPTY", "CHAIN_VALID"}:
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="checkpoint_chain_invalid", summary="No checkpoint written: existing history is not trustworthy.")
            latest = chain.get("latest")
            if latest is None:
                if checkpoint["sequence"] != 1 or checkpoint["previous_checkpoint_id"] is not None:
                    return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="checkpoint_sequence_conflict", summary="First checkpoint must use sequence 1 with no predecessor.")
            elif checkpoint["checkpoint_id"] == latest["checkpoint_id"] and checkpoint["integrity_fingerprint"] == latest["integrity_fingerprint"]:
                return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=[], summary="Checkpoint retry is idempotent; existing record was retained.")
            elif checkpoint["sequence"] != latest["sequence"] + 1 or checkpoint["previous_checkpoint_id"] != latest["checkpoint_id"]:
                return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="checkpoint_sequence_conflict", summary="No checkpoint written: sequence or predecessor conflicts with immutable history.")
            paths.checkpoints_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(history_path, checkpoint)
            atomic_write_json(paths.checkpoint_path, checkpoint)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="io_error", summary=str(exc))
    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=planned, summary="Immutable checkpoint and latest pointer written.")


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def read_task_ownership(project_root: Path, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Read bounded ownership metadata; assessment remains possible without it."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"ownership_status": "CONFLICTED", "error": blocked.get("error")}
    if paths.ownership_path.is_symlink():
        return {"ownership_status": "CONFLICTED", "error": "ownership_symlink"}
    if not paths.ownership_path.exists():
        return {"ownership_status": "UNOWNED", "record": None}
    try:
        record = json.loads(paths.ownership_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ownership_status": "CONFLICTED", "error": "ownership_malformed"}
    required = {"schema_version", "task_id", "owner_instance_id", "acquired_at", "heartbeat_at", "lease_expires_at"}
    if not isinstance(record, dict) or set(record) != required or record.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or record.get("task_id") != task_id:
        return {"ownership_status": "CONFLICTED", "error": "ownership_schema_invalid"}
    observed_now = now or datetime.now(timezone.utc)
    heartbeat = _parse_time(record["heartbeat_at"])
    expires = _parse_time(record["lease_expires_at"])
    if heartbeat is None or expires is None:
        return {"ownership_status": "CONFLICTED", "error": "ownership_time_invalid"}
    if heartbeat > observed_now:
        return {"ownership_status": "CONFLICTED", "error": "ownership_clock_skew"}
    return {"ownership_status": "ACTIVE" if expires > observed_now else "EXPIRED", "record": record}


def acquire_task_ownership(project_root: Path, task_id: str, *, consent: TaskConsent | None, owner_instance_id: str, lease_seconds: int = 120, now: datetime | None = None) -> dict[str, Any]:
    """Explicit-operation lease acquisition. No background heartbeat is created."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    root = project_root.resolve()
    planned = [_rel(paths.ownership_path, root)]
    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="ownership")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned, summary="No ownership record written: explicit ownership consent is required.")
    if not _STEP_ID_RE.fullmatch(owner_instance_id) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="ownership_arguments_invalid", summary="No ownership record written.")
    moment = now or datetime.now(timezone.utc)
    try:
        with memory_lock(root / ".skilllayer"):
            current = read_task_ownership(project_root, task_id, now=moment)
            if current["ownership_status"] == "ACTIVE" and current["record"]["owner_instance_id"] != owner_instance_id:
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="task_owned_by_another_instance", summary="Task has an active owner; no second writer was started.")
            record = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION, "task_id": task_id, "owner_instance_id": owner_instance_id,
                "acquired_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "heartbeat_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lease_expires_at": datetime.fromtimestamp(moment.timestamp() + lease_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            atomic_write_json(paths.ownership_path, record)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="io_error", summary=str(exc))
    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=planned, summary="Task ownership lease recorded.")
