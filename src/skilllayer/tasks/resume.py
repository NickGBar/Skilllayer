"""Read-only Foundation C checkpoint-chain and safe-resume assessment."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .baseline import read_repository_state
from .checkpoint import ACTION_TYPES, TASK_STATUSES, read_task_ownership, validate_checkpoint
from .persistence import _resolve_paths_or_blocked, read_scope_amendments, read_task_baseline, read_task_contract
from .scope import apply_scope_amendments, classify_path, validate_scope


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_checkpoint_chain(project_root: Path, task_id: str) -> dict[str, Any]:
    """Load immutable history; never trusts filename ordering alone."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"status": "CHAIN_CORRUPT", "errors": [blocked.get("error", "task_path_invalid")], "checkpoints": [], "latest": None}
    history = paths.checkpoints_dir
    if history.is_symlink():
        return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_history_symlink"], "checkpoints": [], "latest": None}
    if not history.exists():
        # A Foundation A pointer is retained as a legacy record, never
        # silently upgraded into a factual Foundation C checkpoint.
        if paths.checkpoint_path.exists():
            return {"status": "CHAIN_LEGACY", "errors": ["legacy_checkpoint_requires_confirmation"], "checkpoints": [], "latest": None}
        return {"status": "CHAIN_EMPTY", "errors": [], "checkpoints": [], "latest": None}
    try:
        files = sorted(history.iterdir())
    except OSError:
        return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_history_unreadable"], "checkpoints": [], "latest": None}
    if len(files) > 100:
        return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_history_limit_exceeded"], "checkpoints": [], "latest": None}
    records: list[dict[str, Any]] = []
    for path in files:
        if path.is_symlink() or path.suffix != ".json":
            return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_history_entry_invalid"], "checkpoints": [], "latest": None}
        record = _load_json(path)
        checked = validate_checkpoint(record, task_id=task_id)
        if not checked["valid"]:
            return {"status": "CHAIN_CORRUPT", "errors": checked["errors"], "checkpoints": [], "latest": None}
        records.append(record)
    records.sort(key=lambda item: item["sequence"])
    for index, record in enumerate(records, start=1):
        if record["sequence"] != index:
            return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_sequence_gap_or_duplicate"], "checkpoints": records, "latest": None}
        if index == 1 and record["previous_checkpoint_id"] is not None:
            return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_first_predecessor_invalid"], "checkpoints": records, "latest": None}
        if index > 1 and record["previous_checkpoint_id"] != records[index - 2]["checkpoint_id"]:
            return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_previous_id_mismatch"], "checkpoints": records, "latest": None}
    latest = records[-1] if records else None
    if latest is not None and paths.checkpoint_path.exists():
        pointer = _load_json(paths.checkpoint_path)
        if pointer is None or pointer.get("integrity_fingerprint") != latest["integrity_fingerprint"]:
            return {"status": "CHAIN_CORRUPT", "errors": ["checkpoint_pointer_stale_or_missing"], "checkpoints": records, "latest": None}
    return {"status": "CHAIN_VALID" if records else "CHAIN_EMPTY", "errors": [], "checkpoints": records, "latest": latest}


def _reference_path_is_safe(task_dir: Path, record_path: str) -> Path | None:
    candidate = task_dir / record_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(task_dir.resolve())
    except (OSError, ValueError):
        return None
    return None if candidate.is_symlink() else resolved


def validate_evidence_references(project_root: Path, task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"status": "EVIDENCE_INVALID", "errors": [blocked.get("error", "task_path_invalid")]}
    refs = list(checkpoint.get("evidence_refs", []))
    for key in ("repository_state_ref", "contract_ref", "baseline_ref", "scope_validation_ref"):
        if checkpoint.get(key) is not None:
            refs.append(checkpoint[key])
    errors: list[str] = []
    for ref in refs:
        path = _reference_path_is_safe(paths.task_dir, ref["record_path"])
        if path is None:
            errors.append("evidence_reference_path_unsafe")
            continue
        value = _load_json(path)
        if value is None:
            errors.append("evidence_reference_missing_or_invalid")
            continue
        if _digest(value) != ref["integrity_fingerprint"]:
            errors.append("evidence_reference_integrity_mismatch")
    return {"status": "EVIDENCE_VALID" if not errors else "EVIDENCE_INVALID", "errors": errors}


def build_resume_plan(checkpoint: dict[str, Any], *, scope_result: dict[str, Any], requires_confirmation: bool) -> dict[str, Any]:
    remaining = [item for item in checkpoint.get("remaining_steps", []) if item.get("status") in {"PENDING", "IN_PROGRESS", "BLOCKED"}]
    next_steps = [{"step_id": item["step_id"], "action_type": item["action_type"]} for item in remaining]
    if checkpoint.get("task_status") == "INTERRUPTED" and checkpoint.get("validations_attempted"):
        next_steps = [{"step_id": "revalidate-interrupted-work", "action_type": "RUN_TEST"}, *next_steps]
    return {
        "plan_id": "resume-" + checkpoint["checkpoint_id"], "task_id": checkpoint["task_id"], "based_on_checkpoint": checkpoint["checkpoint_id"],
        "generated_at": _now(), "current_phase": checkpoint["task_phase"], "safe_next_steps": next_steps,
        "prerequisites": ["scope_validation_current"], "required_confirmations": ["resume_confirmation"] if requires_confirmation else [],
        "prohibited_actions": ["RESET_GIT", "STASH", "CHECKOUT", "REBASE", "AUTOMATIC_ROLLBACK"],
        "validation_needed": any(item["action_type"] == "RUN_TEST" for item in next_steps),
        "expected_paths": checkpoint.get("files_expected_next", []), "stop_conditions": ["scope_violation", "baseline_stale", "missing_required_evidence"],
        "limitations": list(checkpoint.get("limitations", [])),
    }


def assess_resume(project_root: Path, task_id: str) -> dict[str, Any]:
    """Pure, conservative resume assessment. It never changes task or Git state."""
    result: dict[str, Any] = {
        "task_id": task_id, "assessed_at": _now(), "latest_checkpoint_id": None, "chain_status": None,
        "contract_status": None, "baseline_status": None, "repository_freshness": "FRESHNESS_UNKNOWN", "scope_status": None,
        "evidence_status": None, "task_status": None, "resume_verdict": "RESUME_INCOMPLETE", "resume_reasons": [],
        "safe_next_steps": [], "blocked_next_steps": [], "required_user_actions": [], "warnings": [], "evidence_complete": False,
    }
    chain = load_checkpoint_chain(project_root, task_id)
    result["chain_status"] = chain["status"]
    if chain["status"] == "CHAIN_EMPTY":
        result.update(resume_verdict="RESUME_INCOMPLETE", resume_reasons=["checkpoint_not_present"])
        return result
    if chain["status"] == "CHAIN_LEGACY":
        result.update(resume_verdict="RESUME_SAFE_WITH_CONFIRMATION", resume_reasons=chain["errors"], required_user_actions=["create_foundation_c_checkpoint"])
        return result
    if chain["status"] != "CHAIN_VALID":
        result.update(resume_verdict="CHECKPOINT_CORRUPT", resume_reasons=chain["errors"])
        return result
    checkpoint = chain["latest"]
    assert checkpoint is not None
    result["latest_checkpoint_id"] = checkpoint["checkpoint_id"]
    result["task_status"] = checkpoint["task_status"]
    if checkpoint["task_status"] == "COMPLETED":
        result.update(resume_verdict="TASK_ALREADY_COMPLETE", resume_reasons=["checkpoint_marks_task_completed"], evidence_complete=True)
        return result
    if checkpoint["task_status"] == "ABANDONED":
        result.update(resume_verdict="TASK_ABANDONED", resume_reasons=["checkpoint_marks_task_abandoned"])
        return result
    if checkpoint["task_status"] in {"FAILED", "BLOCKED"}:
        result.update(resume_verdict="RESUME_BLOCKED", resume_reasons=["checkpoint_marks_task_non_resumable"])
        return result
    contract = read_task_contract(project_root, task_id)
    baseline = read_task_baseline(project_root, task_id)
    result["contract_status"] = contract["state"]
    result["baseline_status"] = baseline["state"]
    if not contract["success"] or not baseline["success"]:
        result.update(resume_verdict="RESUME_BLOCKED", resume_reasons=["contract_or_baseline_missing_or_invalid"])
        return result
    ownership = read_task_ownership(project_root, task_id)
    if ownership["ownership_status"] in {"ACTIVE", "CONFLICTED"}:
        result.update(
            resume_verdict="RESUME_BLOCKED",
            resume_reasons=["active_task_owner" if ownership["ownership_status"] == "ACTIVE" else ownership.get("error", "ownership_conflicted")],
        )
        return result
    amendments = read_scope_amendments(project_root, task_id)
    amendment_records = amendments.get("record", {}).get("amendments", []) if amendments["success"] else []
    observed = read_repository_state(project_root, task_id=task_id, allowed_paths=contract["record"].get("allowed_paths", []))
    scope_result = validate_scope(contract["record"], baseline["record"], observed, task_id=task_id, amendments=amendment_records)
    result["scope_status"] = scope_result["verdict"]
    result["repository_freshness"] = scope_result["freshness_class"]
    if scope_result["verdict"] == "BASELINE_STALE":
        result.update(resume_verdict="CHECKPOINT_STALE", resume_reasons=scope_result["verdict_reasons"])
        return result
    if scope_result["verdict"] in {"SCOPE_VIOLATED", "VALIDATION_BLOCKED"}:
        result.update(resume_verdict="RESUME_BLOCKED", resume_reasons=scope_result["verdict_reasons"], blocked_next_steps=checkpoint.get("remaining_steps", []))
        return result
    evidence = validate_evidence_references(project_root, task_id, checkpoint)
    result["evidence_status"] = evidence["status"]
    if evidence["status"] != "EVIDENCE_VALID":
        result.update(resume_verdict="RESUME_BLOCKED", resume_reasons=evidence["errors"])
        return result
    if scope_result["verdict"] == "SCOPE_INCOMPLETE" or not scope_result["evidence_complete"]:
        result.update(resume_verdict="RESUME_INCOMPLETE", resume_reasons=scope_result["verdict_reasons"], warnings=scope_result["warnings"])
        return result
    effective = apply_scope_amendments(contract["record"], amendment_records)
    invalid_next = [path for path in checkpoint.get("files_expected_next", []) if not effective["valid"] or classify_path(path, effective["contract"], task_id=task_id) not in {"allowed", "allowed_generated"}]
    if invalid_next:
        result.update(resume_verdict="RESUME_BLOCKED", resume_reasons=["next_step_exceeds_scope"], blocked_next_steps=invalid_next)
        return result
    interruption = checkpoint.get("interruption") or {}
    requires_confirmation = checkpoint["task_status"] == "INTERRUPTED" or bool(interruption.get("requires_resume_confirmation")) or bool(checkpoint.get("resume_requirements")) or bool(checkpoint.get("unresolved_questions"))
    plan = build_resume_plan(checkpoint, scope_result=scope_result, requires_confirmation=requires_confirmation)
    result.update(
        resume_verdict="RESUME_SAFE_WITH_CONFIRMATION" if requires_confirmation else "RESUME_SAFE",
        resume_reasons=["checkpoint_and_repository_evidence_current"], safe_next_steps=plan["safe_next_steps"],
        required_user_actions=plan["required_confirmations"], evidence_complete=True,
    )
    return result
