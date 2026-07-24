"""Verified Task Execution — prevented-action (intervention) records.

An intervention is written only when the orchestrator (Foundation D) actually
refused, blocked, or gated an operation for a safety reason — never
speculatively, never for a merely-possible risk. This module is purely
additive, like ``orchestrator.py``: no changes to persistence.py/baseline.py/
checkpoint.py/resume.py/scope.py/orchestrator.py, and it reuses Foundation A's
consent/atomic-write/lock/symlink-safety/redaction primitives directly.

Storage: ``.skilllayer/tasks/<task-id>/interventions/<seq>-<id>.json``, one
immutable file per intervention (same append-only pattern as Foundation D's
transition history). Records hold only bounded structured facts and short
labels — never raw logs, source contents, prompts, or chain-of-thought.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .persistence import (
    FieldPolicy,
    TaskConsent,
    _check_consent,
    _rel,
    _resolve_paths_or_blocked,
    sanitize_persisted_value,
)

INTERVENTION_SCHEMA_VERSION = 1

INTERVENTION_TYPES = frozenset({
    "OUT_OF_SCOPE_CHANGE",
    "FORBIDDEN_PATH_CHANGE",
    "FALSE_COMPLETION_PREVENTED",
    "STALE_RESUME_BLOCKED",
    "UNKNOWN_TEST_RESULT",
    "OWNERSHIP_CONFLICT",
    "INCOMPLETE_EVIDENCE",
    "SCOPE_AMENDMENT_REQUIRED",
})

_MAX_LABEL = 160
_MAX_EVIDENCE_REFS = 32


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _interventions_dir(task_dir: Path) -> Path:
    return task_dir / "interventions"


def _derive_intervention_id(task_id: str, operation: str, rule: str, nonce: str) -> str:
    digest = hashlib.sha256(f"{task_id}:{operation}:{rule}:{nonce}".encode("utf-8")).hexdigest()[:16]
    return f"iv-{digest}"


def record_intervention(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    intervention_type: str,
    operation: str,
    rule: str,
    observed_condition: str,
    prevented_outcome: str,
    user_action_required: str,
    evidence_refs: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist one immutable intervention record. Never overwrites a prior one.

    Returns a bounded result dict with ``success``/``error``/``written_paths``
    — the same shape convention as Foundation A/D writers."""
    if intervention_type not in INTERVENTION_TYPES:
        return {"success": False, "error": "intervention_type_invalid", "written_paths": []}

    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"success": False, "error": blocked.get("error", "task_path_invalid"), "written_paths": []}

    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="result")
    if consent_state is not None:
        return {"success": False, "error": "consent_required_or_invalid", "written_paths": []}

    san_rule = sanitize_persisted_value(rule, FieldPolicy.REDACTABLE_TEXT, max_length=_MAX_LABEL, field_name="rule")
    san_observed = sanitize_persisted_value(observed_condition, FieldPolicy.REDACTABLE_TEXT, max_length=_MAX_LABEL, field_name="observed_condition")
    san_outcome = sanitize_persisted_value(prevented_outcome, FieldPolicy.REDACTABLE_TEXT, max_length=_MAX_LABEL, field_name="prevented_outcome")
    san_action = sanitize_persisted_value(user_action_required, FieldPolicy.REDACTABLE_TEXT, max_length=_MAX_LABEL, field_name="user_action_required")
    san_operation = sanitize_persisted_value(operation, FieldPolicy.SAFE_STRUCTURED, max_length=80, field_name="operation")
    rejections = [r for r in (san_rule, san_observed, san_outcome, san_action, san_operation) if not r.accepted]
    if rejections:
        return {"success": False, "error": rejections[0].rejection_reason, "written_paths": []}

    refs = list(evidence_refs or [])
    if len(refs) > _MAX_EVIDENCE_REFS:
        return {"success": False, "error": "evidence_refs_too_many", "written_paths": []}

    key = idempotency_key or secrets.token_hex(8)
    intervention_id = _derive_intervention_id(task_id, operation, rule, key)
    root = project_root.resolve()
    interventions_dir = _interventions_dir(paths.task_dir)

    record = {
        "schema_version": INTERVENTION_SCHEMA_VERSION,
        "intervention_id": intervention_id,
        "task_id": task_id,
        "timestamp": _now(),
        "intervention_type": intervention_type,
        "operation": san_operation.sanitized_value,
        "rule": san_rule.sanitized_value,
        "observed_condition": san_observed.sanitized_value,
        "prevented_outcome": san_outcome.sanitized_value,
        "user_action_required": san_action.sanitized_value,
        "evidence_refs": refs,
    }

    record_path = interventions_dir / f"{intervention_id}.json"
    try:
        with memory_lock(root / ".skilllayer"):
            if interventions_dir.is_symlink() or record_path.is_symlink():
                return {"success": False, "error": "symlink_not_permitted", "written_paths": []}
            if record_path.exists():
                # Idempotent retry: an intervention with this exact derived id
                # already exists — never overwritten, treated as already-recorded.
                return {"success": True, "error": None, "written_paths": [], "intervention_id": intervention_id,
                         "already_recorded": True}
            interventions_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(record_path, record)
    except MemoryLockTimeoutError as exc:
        return {"success": False, "error": "memory_lock_timeout", "written_paths": [], "detail": str(exc)}
    except OSError as exc:
        return {"success": False, "error": "io_error", "written_paths": [], "detail": str(exc)}

    return {"success": True, "error": None, "written_paths": [_rel(record_path, root)],
            "intervention_id": intervention_id, "already_recorded": False}


def list_interventions(project_root: Path, task_id: str) -> dict[str, Any]:
    """Read-only. Returns bounded, sorted intervention records for one task.

    Never trusts filename ordering alone — sorts by the record's own
    ``timestamp`` field after validating each record's shape; a malformed
    entry is skipped (recorded in ``skipped``), never silently misreported
    as clean."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"success": False, "error": blocked.get("error", "task_path_invalid"), "interventions": [], "skipped": []}

    interventions_dir = _interventions_dir(paths.task_dir)
    if interventions_dir.is_symlink():
        return {"success": False, "error": "symlink_not_permitted", "interventions": [], "skipped": []}
    if not interventions_dir.exists():
        return {"success": True, "error": None, "interventions": [], "skipped": []}

    required = {
        "schema_version", "intervention_id", "task_id", "timestamp", "intervention_type",
        "operation", "rule", "observed_condition", "prevented_outcome", "user_action_required", "evidence_refs",
    }
    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        entries = sorted(interventions_dir.iterdir())
    except OSError:
        return {"success": False, "error": "interventions_dir_unreadable", "interventions": [], "skipped": []}
    for path in entries:
        if path.is_symlink() or path.suffix != ".json":
            skipped.append(path.name)
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped.append(path.name)
            continue
        if not isinstance(value, dict) or set(value) != required:
            skipped.append(path.name)
            continue
        if value.get("schema_version") != INTERVENTION_SCHEMA_VERSION or value.get("task_id") != task_id:
            skipped.append(path.name)
            continue
        if value.get("intervention_type") not in INTERVENTION_TYPES:
            skipped.append(path.name)
            continue
        records.append(value)
    records.sort(key=lambda item: item["timestamp"])
    return {"success": True, "error": None, "interventions": records, "skipped": skipped}
