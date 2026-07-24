"""Verified Task Execution — public product surface (Milestone E).

Wraps Foundation D's orchestrator into a small set of bounded operations
(`vte_start`, `vte_status`, `vte_checkpoint`, `vte_resume`, `vte_finalize`,
`vte_abandon`) that an MCP-compatible agent can call without knowing that
Foundations A-D exist: no task_id generation, owner_instance_id bookkeeping,
consent objects, evidence digests, or transition sequence numbers are ever
the caller's responsibility.

Purely additive: no changes to persistence.py/baseline.py/checkpoint.py/
resume.py/scope.py/orchestrator.py. This module is the ONLY place a public
MCP tool handler (see ``skilllayer.mcp_server``) is expected to import from
the tasks package.

No LLM is called anywhere in this module. Every returned fact is copied or
derived from Foundation A-D's own persisted, redaction-gated evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .checkpoint import INTERRUPTION_REASONS
from .interventions import record_intervention
from .orchestrator import (
    _orchestrator_paths,
    abandon_task,
    acquire_ownership,
    assess_task_resume,
    capture_baseline,
    checkpoint_task,
    create_task,
    finalize_task,
    get_task_status,
    interrupt_task,
    reclaim_ownership,
    resume_task,
    start_task,
    validate_task,
)
from .persistence import (
    FieldPolicy,
    _check_consent,
    _resolve_paths_or_blocked,
    generate_task_id,
    grant_task_consent,
    read_task_contract,
    sanitize_persisted_value,
)
from .human_report import (
    SUPPORTED_LOCALES,
    HumanReportError,
    build_human_report,
    read_human_report,
    render_human_report_markdown,
    write_human_report,
)
from .receipt import build_receipt, render_receipt_text
from .resume import load_checkpoint_chain

_PROCESS_NONCE = secrets.token_hex(4)
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONFIRMATION_ID_RE = re.compile(r"^cf-[a-f0-9]{16}$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_owner_instance_id() -> str:
    """Stable per-process identity so sequential tool calls within one agent
    session share ownership without the caller tracking anything. A
    different MCP server process (a different agent/session) gets a
    different id automatically, so Foundation C's own conflict rule still
    protects against two sessions racing on the same task."""
    return f"agent-{os.getpid()}-{_PROCESS_NONCE}"[:64]


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _slugify_step(label: str, *, prefix: str, index: int) -> str:
    lowered = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-")[:50]
    slug = f"{prefix}-{index}-{lowered}" if lowered else f"{prefix}-{index}"
    if not slug or not slug[0].isalpha():
        slug = f"s-{slug}"
    return slug[:64]


def _error(*, task_id: str | None, error_code: str, explanation: str, next_action: str) -> dict[str, Any]:
    """Bounded, human-friendly error shape. Never includes a stack trace, an
    absolute filesystem path, or raw internal state."""
    return {
        "status": "ERROR",
        "task_id": task_id,
        "error_code": error_code,
        "explanation": explanation,
        "next_action": next_action,
    }


def _from_lifecycle_result(result: dict[str, Any], *, task_id: str | None, next_action: str) -> dict[str, Any]:
    return _error(
        task_id=task_id,
        error_code=result.get("error_code") or result.get("state", "operation_failed"),
        explanation=result.get("summary") or "The operation could not be completed.",
        next_action=next_action,
    )


# ---------------------------------------------------------------------------
# Confirmation tokens — scoped, bounded, single-purpose, recorded,
# non-transferable between tasks, non-reusable for unrelated operations.
# ---------------------------------------------------------------------------


def _confirmations_dir(task_dir: Path) -> Path:
    return task_dir / "confirmations"


def _issue_confirmation(
    project_root: Path, task_id: str, *, reason: Any, scope: str, operation_after_confirmation: str,
) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"success": False, "error": blocked.get("error", "task_path_invalid")}
    consent = grant_task_consent(project_root, task_id)
    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="result")
    if consent_state is not None:
        return {"success": False, "error": "consent_required_or_invalid"}
    confirmations_dir = _confirmations_dir(paths.task_dir)
    confirmation_id = "cf-" + hashlib.sha256(f"{task_id}:{scope}:{secrets.token_hex(8)}".encode("utf-8")).hexdigest()[:16]
    record = {
        "schema_version": 1, "confirmation_id": confirmation_id, "task_id": task_id, "created_at": _now(),
        "reason": str(reason)[:400], "scope": scope, "operation_after_confirmation": operation_after_confirmation,
        "consumed": False, "consumed_at": None,
    }
    root = project_root.resolve()
    try:
        with memory_lock(root / ".skilllayer"):
            if confirmations_dir.is_symlink():
                return {"success": False, "error": "symlink_not_permitted"}
            confirmations_dir.mkdir(parents=True, exist_ok=True)
            path = confirmations_dir / f"{confirmation_id}.json"
            if path.is_symlink():
                return {"success": False, "error": "symlink_not_permitted"}
            atomic_write_json(path, record)
    except (MemoryLockTimeoutError, OSError) as exc:
        return {"success": False, "error": "io_error", "detail": str(exc)}
    return {"success": True, "confirmation_id": confirmation_id, "record": record}


def _consume_confirmation(project_root: Path, task_id: str, confirmation_id: str | None, *, expected_scope: str) -> bool:
    """Single-use: returns True only the first time a valid, matching,
    unconsumed token is presented. Every later call with the same token
    (even for the same task/scope) returns False."""
    if not confirmation_id or not _CONFIRMATION_ID_RE.fullmatch(confirmation_id):
        return False
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return False
    path = _confirmations_dir(paths.task_dir) / f"{confirmation_id}.json"
    root = project_root.resolve()
    try:
        with memory_lock(root / ".skilllayer"):
            if path.is_symlink() or not path.exists():
                return False
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(record, dict) or record.get("task_id") != task_id or record.get("scope") != expected_scope:
                return False
            if record.get("consumed"):
                return False
            record["consumed"] = True
            record["consumed_at"] = _now()
            atomic_write_json(path, record)
            return True
    except (MemoryLockTimeoutError, OSError):
        return False


def _pending_confirmations(project_root: Path, task_id: str) -> list[dict[str, Any]]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return []
    confirmations_dir = _confirmations_dir(paths.task_dir)
    if confirmations_dir.is_symlink() or not confirmations_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(confirmations_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        if path.is_symlink() or path.suffix != ".json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("task_id") == task_id and not record.get("consumed"):
            out.append({
                "confirmation_id": record.get("confirmation_id"), "reason": record.get("reason"),
                "scope": record.get("scope"), "operation_after_confirmation": record.get("operation_after_confirmation"),
            })
    return out


def _receipt_with_confirmations(project_root: Path, task_id: str) -> dict[str, Any]:
    receipt = build_receipt(project_root, task_id)
    receipt["confirmations_required"] = _pending_confirmations(project_root, task_id)
    return receipt


# ---------------------------------------------------------------------------
# vte_start
# ---------------------------------------------------------------------------


def vte_start(
    project_root: str | Path,
    title: str,
    *,
    description: str = "",
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    scope_mode: str = "EXPLICIT",
    allow_new_files: bool = True,
    allow_deleted_files: bool = True,
    allow_test_files: bool = True,
    allowed_generated_paths: list[str] | None = None,
    max_changed_files: int | None = None,
    expected_checks: list[str] | None = None,
    persist_consent: bool = False,
    owner_instance_id: str | None = None,
) -> dict[str, Any]:
    """Start a verified task: create its contract, capture a repository
    baseline, and acquire ownership — internally, in the correct order.

    ``description`` is used only to help form a readable task_id; only the
    bounded, sanitized ``title`` is persisted as the contract's objective
    label (Foundation A does not have a separate free-text description
    field, and this module cannot add one without modifying persistence.py).

    Nothing is written until ``persist_consent=True`` is passed explicitly.
    """
    owner_instance_id = owner_instance_id or default_owner_instance_id()
    if not persist_consent:
        return _error(
            task_id=None, error_code="persist_consent_required",
            explanation="No task was created: Verified Task Execution requires explicit persistence consent "
                        "before it writes anything to .skilllayer/tasks/.",
            next_action="Call vte_start again with persist_consent=True.",
        )

    project_root = Path(project_root)
    task_id = generate_task_id(project_root, objective_hint=title or description)
    consent = grant_task_consent(project_root, task_id)

    created = create_task(
        project_root, task_id, consent=consent, objective_label=title,
        allowed_paths=allowed_paths, forbidden_paths=forbidden_paths, expected_checks=expected_checks,
        scope_mode=scope_mode, max_changed_files=max_changed_files, allow_new_files=allow_new_files,
        allow_deleted_files=allow_deleted_files, allow_test_files=allow_test_files,
        allowed_generated_paths=allowed_generated_paths,
    )
    if not created["success"]:
        return _from_lifecycle_result(created, task_id=task_id, next_action="Fix the reported field and call vte_start again with a new title.")

    baseline = capture_baseline(project_root, task_id, consent=consent)
    if not baseline["success"]:
        return _from_lifecycle_result(
            baseline, task_id=task_id,
            next_action="Verify the target is a clean git repository with a readable HEAD, then call vte_start again.",
        )

    acquired = acquire_ownership(project_root, task_id, consent=consent, owner_instance_id=owner_instance_id)
    if not acquired["success"]:
        if acquired.get("error_code") == "task_owned_by_another_instance":
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="OWNERSHIP_CONFLICT",
                operation="vte_start", rule="A task may have only one active owner at a time.",
                observed_condition="Another agent instance already holds an active ownership lease.",
                prevented_outcome="Ownership was not acquired; the task remains at BASELINE_READY.",
                user_action_required="Wait for the other instance's lease to expire, or confirm you are that "
                                      "instance and retry.",
            )
        return _from_lifecycle_result(acquired, task_id=task_id, next_action="Call vte_status to check for a conflicting owner, then retry.")

    started = start_task(project_root, task_id, owner_instance_id=owner_instance_id)
    if not started["success"]:
        if started.get("error_code") == "forbidden_preexisting_state":
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="OUT_OF_SCOPE_CHANGE",
                operation="vte_start", rule="start_task requires no forbidden or unexpected pre-existing repository changes.",
                observed_condition="The repository already had forbidden or unexpected changes before the task started.",
                prevented_outcome="The task was not moved to RUNNING.",
                user_action_required="Resolve the pre-existing changes outside of Verified Task Execution "
                                      "(commit, stash, or revert them), then call vte_start again.",
            )
        return _from_lifecycle_result(
            started, task_id=task_id,
            next_action="Resolve the reported pre-existing repository state, then call vte_start again.",
        )

    contract = read_task_contract(project_root, task_id)
    scope = {
        "allowed_paths": contract["record"].get("allowed_paths", []) if contract["success"] else (allowed_paths or []),
        "max_changed_files": max_changed_files,
    }
    status = get_task_status(project_root, task_id)
    return {
        "status": "READY",
        "task_id": task_id,
        "owner_instance_id": owner_instance_id,
        "scope": scope,
        "baseline": status.get("baseline_status") if status.get("success") else baseline.get("summary"),
        "next_action": "Implement only within the approved scope. Call vte_checkpoint as you make progress.",
    }


# ---------------------------------------------------------------------------
# vte_status
# ---------------------------------------------------------------------------


def vte_status(project_root: str | Path, task_id: str) -> dict[str, Any]:
    """Read-only. Never writes anything — including never creating or
    rewriting a human report; it only previews one if vte_finalize already
    persisted it."""
    project_root = Path(project_root)
    status = get_task_status(project_root, task_id)
    if not status.get("success"):
        return _error(
            task_id=task_id, error_code=status.get("error_code", "task_not_found"),
            explanation=status.get("summary", "No task exists at this task_id."),
            next_action="Verify the task_id, or call vte_start to create a new task.",
        )
    existing_report = read_human_report(project_root, task_id)
    report_preview = None
    if existing_report is not None:
        report_preview = {
            "overall_status": existing_report.get("overall_status"),
            "problem_summary": existing_report.get("problem_summary"),
            "next_action": existing_report.get("next_action"),
            "final_verdict": existing_report.get("final_verdict"),
        }
    return {
        "status": "OK",
        "task_id": task_id,
        "task_state": status["current_state"],
        "baseline_status": status.get("baseline_status"),
        "scope_status": status.get("scope_status"),
        "resume_status": status.get("resume_status"),
        "evidence_complete": status.get("evidence_complete"),
        "safe_operations": status.get("safe_operations", []),
        "prohibited_operations": status.get("prohibited_operations", []),
        "confirmations_required": _pending_confirmations(project_root, task_id),
        "report_preview": report_preview,
        "next_action": _status_next_action(status["current_state"]),
    }


_STATUS_NEXT_ACTION = {
    "CREATED": "Call vte_start again; the task did not finish setup.",
    "CONTRACT_READY": "Call vte_start again to capture a baseline and acquire ownership.",
    "BASELINE_READY": "Call vte_start again to acquire ownership and begin.",
    "READY_TO_START": "Call vte_start again to begin the RUNNING phase.",
    "RUNNING": "Implement within the approved scope, then call vte_checkpoint or vte_finalize.",
    "INTERRUPTED": "Call vte_resume to assess whether it is safe to continue.",
    "RESUME_REVIEW": "Call vte_resume with confirmed=True (and the issued confirmation_token) to continue.",
    "VALIDATING": "Call vte_finalize with recorded test evidence.",
    "BLOCKED": "Call vte_finalize again after resolving the reported blockers, or vte_status for detail.",
    "COMPLETED": "Task is complete; no further action needed.",
    "FAILED": "Task failed; start a new task with vte_start if the work should continue.",
    "ABANDONED": "Task was abandoned; start a new task with vte_start if needed.",
}


def _status_next_action(state: str) -> str:
    return _STATUS_NEXT_ACTION.get(state, "Call vte_status again after the next operation.")


# ---------------------------------------------------------------------------
# vte_checkpoint
# ---------------------------------------------------------------------------


def vte_checkpoint(
    project_root: str | Path,
    task_id: str,
    task_phase: str,
    *,
    completed_steps: list[str] | None = None,
    active_step: str | None = None,
    remaining_steps: list[str] | None = None,
    files_observed: list[str] | None = None,
    files_expected_next: list[str] | None = None,
    interruption_reason: str | None = None,
    owner_instance_id: str | None = None,
) -> dict[str, Any]:
    """Record one immutable checkpoint. Pass ``interruption_reason`` (one of
    Foundation C's INTERRUPTION_REASONS) instead of a normal checkpoint when
    work stopped unexpectedly — this moves the task to INTERRUPTED so
    vte_resume is required before continuing.

    The caller never builds a Foundation C step/evidence-ref object by hand:
    plain string labels are turned into valid, evidence-backed step records
    here. A completed step with no corresponding evidence would be rejected
    by Foundation C itself; this wrapper always attaches evidence, so a
    completion claim is only ever rejected for a genuine schema problem
    (e.g. an empty label), never silently dropped.
    """
    project_root = Path(project_root)
    owner_instance_id = owner_instance_id or default_owner_instance_id()
    consent = grant_task_consent(project_root, task_id)

    if interruption_reason is not None:
        if interruption_reason not in INTERRUPTION_REASONS:
            return _error(
                task_id=task_id, error_code="interruption_reason_invalid",
                explanation=f"interruption_reason must be one of {sorted(INTERRUPTION_REASONS)}.",
                next_action="Call vte_checkpoint again with a valid interruption_reason.",
            )
        result = interrupt_task(
            project_root, task_id, consent=consent, owner_instance_id=owner_instance_id,
            reason=interruption_reason, task_phase=task_phase,
            paths_requiring_reinspection=files_observed or [],
        )
        if not result["success"]:
            return _from_lifecycle_result(result, task_id=task_id, next_action="Call vte_status to see the task's current state.")
        return {
            "status": "INTERRUPTED", "task_id": task_id, "task_phase": task_phase,
            "next_action": "Call vte_resume when work continues.",
        }

    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return _error(task_id=task_id, error_code=blocked.get("error", "task_path_invalid"),
                       explanation="The task path failed a safety check.", next_action="Call vte_status to inspect the task.")
    o_paths = _orchestrator_paths(paths.task_dir)
    chain = load_checkpoint_chain(project_root, task_id)
    next_sequence = 1 if chain.get("latest") is None else chain["latest"]["sequence"] + 1

    claim = {
        "schema_version": 1, "task_id": task_id, "recorded_at": _now(),
        "completed_steps": list(completed_steps or []), "active_step": active_step,
        "remaining_steps": list(remaining_steps or []), "files_observed": list(files_observed or []),
    }
    claim_path = o_paths.evidence_dir / f"checkpoint_claim_{next_sequence:04d}.json"
    root = project_root.resolve()
    try:
        with memory_lock(root / ".skilllayer"):
            if o_paths.evidence_dir.is_symlink() or claim_path.is_symlink():
                return _error(task_id=task_id, error_code="symlink_not_permitted",
                               explanation="Refusing to write: an evidence path is a symlink.",
                               next_action="Investigate the .skilllayer directory manually.")
            o_paths.evidence_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(claim_path, claim)
    except (MemoryLockTimeoutError, OSError) as exc:
        return _error(task_id=task_id, error_code="io_error", explanation=str(exc),
                       next_action="Retry vte_checkpoint.")
    claim_ref = {
        "evidence_type": "checkpoint_claim", "record_path": f"evidence/checkpoint_claim_{next_sequence:04d}.json",
        "schema_version": 1, "integrity_fingerprint": _digest(claim), "required": True, "observed_status": "RECORDED",
    }

    completed = [
        {"step_id": _slugify_step(label, prefix="done", index=i), "status": "COMPLETED",
         "action_type": "RECORD_RESULT", "evidence_refs": [claim_ref]}
        for i, label in enumerate(completed_steps or [])
    ]
    active = (
        {"step_id": _slugify_step(active_step, prefix="active", index=0), "status": "IN_PROGRESS",
         "action_type": "RECORD_RESULT", "evidence_refs": []}
        if active_step else None
    )
    remaining = [
        {"step_id": _slugify_step(label, prefix="next", index=i), "status": "PENDING",
         "action_type": "RECORD_RESULT", "evidence_refs": []}
        for i, label in enumerate(remaining_steps or [])
    ]

    result = checkpoint_task(
        project_root, task_id, consent=consent, owner_instance_id=owner_instance_id, task_phase=task_phase,
        completed_steps=completed, active_step=active, remaining_steps=remaining,
        files_observed=files_observed or [], files_expected_next=files_expected_next or [],
    )
    if not result["success"]:
        return _from_lifecycle_result(result, task_id=task_id, next_action="Call vte_status to see the task's current state and allowed operations.")
    return {
        "status": "CHECKPOINTED", "task_id": task_id, "sequence": result.get("checkpoint", {}).get("sequence"),
        "next_action": "Continue implementing within scope. Call vte_finalize once tests have been run and recorded.",
    }


# ---------------------------------------------------------------------------
# vte_resume
# ---------------------------------------------------------------------------

_PROHIBITED_RESUME_ACTIONS = ["RESET_GIT", "STASH", "CHECKOUT", "REBASE", "AUTOMATIC_ROLLBACK"]


def _public_resume_plan(project_root: Path, task_id: str, assessment: dict[str, Any]) -> dict[str, Any]:
    chain = load_checkpoint_chain(project_root, task_id)
    latest = chain.get("latest") or {}
    return {
        "resume_verdict": assessment.get("resume_verdict"),
        "certainly_complete": [s["step_id"] for s in latest.get("completed_steps", [])],
        "may_be_partially_complete": [
            s["step_id"] for s in [*latest.get("remaining_steps", []), *([latest["active_step"]] if latest.get("active_step") else [])]
            if s and s.get("status") == "IN_PROGRESS"
        ],
        "must_be_rechecked": assessment.get("safe_next_steps", []),
        "repository_drift": assessment.get("repository_freshness"),
        "reasons": assessment.get("resume_reasons", []),
        "prohibited_actions": _PROHIBITED_RESUME_ACTIONS,
    }


def vte_resume(
    project_root: str | Path,
    task_id: str,
    *,
    confirmed: bool = False,
    confirmation_token: str | None = None,
    owner_instance_id: str | None = None,
) -> dict[str, Any]:
    """Assess the checkpoint chain, current repository state, scope, and
    ownership, and produce a safe resume plan — resuming into RUNNING only
    when Foundation D's own gating allows it (never inferred here).

    Confirmation gating is driven by ``resume_task`` itself, not by the raw
    ``assess_task_resume`` verdict: Foundation C's ``assess_resume`` has no
    ``owner_instance_id`` parameter, so it cannot tell "a different owner"
    apart from "the same caller recovering their own interruption" and
    blocks on any active lease either way. Only ``resume_task`` (which does
    receive ``owner_instance_id``) applies that reinterpretation, so this
    wrapper always attempts it and reacts to whatever state comes back,
    rather than pre-branching on a verdict that may not reflect the
    reinterpretation at all.
    """
    project_root = Path(project_root)
    owner_instance_id = owner_instance_id or default_owner_instance_id()

    assessed = assess_task_resume(project_root, task_id)
    if not assessed["success"]:
        return _from_lifecycle_result(assessed, task_id=task_id, next_action="Call vte_status to inspect the task before resuming.")
    assessment = assessed["assessment"]
    plan = _public_resume_plan(project_root, task_id, assessment)

    # A supplied token must always be consumed (single-use), even when the
    # caller also passes confirmed=True directly — otherwise an unconsumed
    # token from a redundant-confirmed call would remain valid and could be
    # replayed on a later, unrelated confirmation cycle for the same task.
    token_consumed = (
        _consume_confirmation(project_root, task_id, confirmation_token, expected_scope="resume")
        if confirmation_token is not None else False
    )
    effective_confirmed = confirmed or token_consumed

    consent = grant_task_consent(project_root, task_id)
    result = resume_task(project_root, task_id, owner_instance_id=owner_instance_id, confirmed=effective_confirmed)

    if result.get("state") == "CONFIRMATION_REQUIRED":
        issued = _issue_confirmation(
            project_root, task_id, reason=assessment.get("resume_reasons", []), scope="resume",
            operation_after_confirmation="vte_resume",
        )
        out = {
            "status": "CONFIRMATION_REQUIRED", "task_id": task_id, "plan": plan,
            "confirmation_token": issued.get("confirmation_id"),
        }
        out["next_action"] = (
            f"Review the plan, then call vte_resume again with confirmed=True and "
            f"confirmation_token={issued.get('confirmation_id')!r}."
            if issued.get("success") else
            "Review the plan, then call vte_resume again with confirmed=True."
        )
        return out

    if not result.get("success") and result.get("state") in {"TASK_BLOCKED", "TASK_FAILED_STATE"}:
        blocked_assessment = result.get("assessment", assessment)
        verdict = blocked_assessment.get("resume_verdict")
        reasons = blocked_assessment.get("resume_reasons", [])
        if verdict == "CHECKPOINT_STALE":
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="STALE_RESUME_BLOCKED",
                operation="vte_resume", rule="Resume requires the repository baseline to still be current.",
                observed_condition="The repository HEAD advanced since the baseline was captured.",
                prevented_outcome="The task was not resumed into RUNNING.",
                user_action_required="Investigate what changed since the baseline and start a new task if needed.",
            )
        elif verdict == "RESUME_BLOCKED" and "active_task_owner" in reasons:
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="OWNERSHIP_CONFLICT",
                operation="vte_resume", rule="Resume is blocked while another instance holds an active ownership lease.",
                observed_condition="An active ownership lease is held by a different agent instance.",
                prevented_outcome="The task was not resumed into RUNNING.",
                user_action_required="Wait for the lease to expire, then retry.",
            )
        return {
            "status": "BLOCKED", "task_id": task_id, "plan": plan,
            "next_action": "Review the plan's reasons, resolve them outside of Verified Task Execution, then call vte_resume again.",
        }
    if not result.get("success"):
        return _from_lifecycle_result(result, task_id=task_id, next_action="Call vte_status to inspect the task.")

    return {
        "status": "RUNNING", "task_id": task_id, "plan": plan,
        "next_action": "Continue implementing within scope. Call vte_checkpoint or vte_finalize.",
    }


# ---------------------------------------------------------------------------
# vte_finalize
# ---------------------------------------------------------------------------


def vte_finalize(
    project_root: str | Path,
    task_id: str,
    *,
    tests_recorded: bool,
    tests_passed: bool | None = None,
    tests_summary_label: str | None = None,
    owner_instance_id: str | None = None,
    locale: str = "en",
    persist_report: bool = True,
) -> dict[str, Any]:
    """Validate scope against live repository state, then finalize.

    Never returns a verified-complete verdict when tests were not recorded,
    the outcome is unknown, evidence is incomplete, the baseline is stale, a
    forbidden path changed, or scope was violated — Foundation D's own
    ``finalize_task`` derives the verdict from persisted evidence and this
    wrapper never overrides it.

    Always returns a deterministic human-readable report (``human_report``/
    ``human_report_markdown``) alongside the structured receipt — for a
    blocked or incomplete finalization too, so the caller never needs a
    second tool call just to understand what went wrong. ``persist_report``
    (default True) writes ``report.json``/``report.md`` under the task's own
    consent-gated directory; set it False to get the report back without
    writing it.
    """
    project_root = Path(project_root)
    owner_instance_id = owner_instance_id or default_owner_instance_id()
    consent = grant_task_consent(project_root, task_id)

    if locale not in SUPPORTED_LOCALES:
        return _error(
            task_id=task_id, error_code="unsupported_locale",
            explanation=f"locale must be one of {sorted(SUPPORTED_LOCALES)}.",
            next_action="Call vte_finalize again with locale='en'.",
        )

    status = get_task_status(project_root, task_id)
    if not status.get("success"):
        return _from_lifecycle_result(status, task_id=task_id, next_action="Call vte_status to check the task_id.")

    if status["current_state"] == "RUNNING":
        validated = validate_task(project_root, task_id, consent=consent, owner_instance_id=owner_instance_id)
        if not validated.get("success") and validated.get("state") != "TASK_BLOCKED":
            return _from_lifecycle_result(validated, task_id=task_id, next_action="Call vte_status to inspect the task.")
    elif status["current_state"] not in {"VALIDATING", "BLOCKED", "COMPLETED", "FAILED"}:
        # COMPLETED/FAILED are allowed through: finalize_task has its own
        # idempotent-retry path for those terminal states (it returns the
        # exact prior final_result rather than re-deriving one), so a retry
        # call must reach it rather than being rejected here first.
        return _error(
            task_id=task_id, error_code="invalid_state_transition",
            explanation=f"vte_finalize requires the task to be RUNNING, VALIDATING, or BLOCKED; it is at {status['current_state']!r}.",
            next_action=_status_next_action(status["current_state"]),
        )

    # Foundation D's finalize_task gates ONLY on tests_status["recorded"] — it
    # never inspects "passed" at all. So "recorded" here must mean "the test
    # requirement was conclusively satisfied" (recorded AND passed), never
    # merely "a result of some kind was reported" — otherwise a genuinely
    # failed or inconclusive result would silently let TASK_VERIFIED_COMPLETE
    # through. The caller's raw signal is preserved separately as
    # "reported_recorded"/"passed" so the persisted evidence and the human
    # report can still distinguish "never ran" from "ran and failed" from
    # "ran, outcome unknown".
    tests_satisfied = bool(tests_recorded) and tests_passed is True
    tests_status: dict[str, Any] = {"recorded": tests_satisfied, "reported_recorded": bool(tests_recorded)}
    if tests_recorded:
        tests_status["passed"] = tests_passed
        if tests_summary_label:
            san_label = sanitize_persisted_value(tests_summary_label, FieldPolicy.REDACTABLE_TEXT, max_length=160, field_name="tests_summary_label")
            if san_label.accepted:
                tests_status["summary_label"] = san_label.sanitized_value

    finalized = finalize_task(
        project_root, task_id, consent=consent, owner_instance_id=owner_instance_id,
        tests_status=tests_status, written_by_component="vte_public_api",
    )

    if finalized.get("state") == "TASK_BLOCKED":
        blockers = (finalized.get("result") or {}).get("blockers", [])
        if "tests_status_missing" in blockers or "required_tests_not_recorded" in blockers:
            if not tests_recorded:
                record_intervention(
                    project_root, task_id, consent=consent, intervention_type="FALSE_COMPLETION_PREVENTED",
                    operation="vte_finalize", rule="finalize_task requires recorded test evidence before completion.",
                    observed_condition=f"tests_recorded={tests_recorded}, tests_passed={tests_passed}",
                    prevented_outcome="Finalization was blocked; the task was not marked complete.",
                    user_action_required="Rerun the required tests and call vte_finalize again with tests_recorded=True and a definite tests_passed value.",
                )
            elif tests_passed is None:
                record_intervention(
                    project_root, task_id, consent=consent, intervention_type="UNKNOWN_TEST_RESULT",
                    operation="vte_finalize", rule="finalize_task requires a conclusive (not merely started) test result.",
                    observed_condition=f"tests_recorded={tests_recorded}, tests_passed={tests_passed}",
                    prevented_outcome="Finalization was blocked; the task was not marked complete.",
                    user_action_required="Rerun the required tests and call vte_finalize again with a definite tests_passed value.",
                )
            # tests_passed is False: an honest, evidence-backed failure is not
            # an intervention — nothing unsafe was attempted or prevented,
            # this is simply a correctly-blocked, correctly-reported failure.
        if any(("forbidden_path" in b or "unexpected_path" in b or b == "scope_invalid") for b in blockers):
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="FORBIDDEN_PATH_CHANGE",
                operation="vte_finalize", rule="finalize_task requires no forbidden or unexpected path changes.",
                observed_condition="A forbidden or unexpected path changed since the baseline was captured.",
                prevented_outcome="Finalization was blocked; the task was not marked complete.",
                user_action_required="Revert the out-of-scope change, or request a scope amendment, then retry.",
            )
        if "baseline_stale" in blockers:
            record_intervention(
                project_root, task_id, consent=consent, intervention_type="INCOMPLETE_EVIDENCE",
                operation="vte_finalize", rule="finalize_task requires a current (non-stale) baseline.",
                observed_condition="The repository HEAD advanced since the baseline was captured.",
                prevented_outcome="Finalization was blocked; the task was not marked complete.",
                user_action_required="Investigate the repository change, then start a new task if needed.",
            )

    receipt = _receipt_with_confirmations(project_root, task_id)
    ok = bool(finalized.get("success"))

    try:
        report = build_human_report(receipt, locale=locale)
    except HumanReportError:
        report = None
    markdown = render_human_report_markdown(report) if report is not None else None
    report_paths: list[str] = []
    if report is not None and persist_report:
        written = write_human_report(project_root, task_id, consent=consent, report=report, markdown=markdown)
        if written.get("success"):
            report_paths = written.get("written_paths", [])

    return {
        "status": "COMPLETED" if ok else "BLOCKED",
        "task_id": task_id,
        "receipt": receipt,
        "receipt_text": render_receipt_text(receipt),
        "human_report": report,
        "human_report_markdown": markdown,
        "report_paths": report_paths,
        "next_action": (report or {}).get("next_action", "Task verified complete." if ok else "Review the receipt's limitations and prevented_actions, resolve them, then call vte_finalize again."),
    }


# ---------------------------------------------------------------------------
# vte_abandon (optional sixth tool)
# ---------------------------------------------------------------------------


def vte_abandon(project_root: str | Path, task_id: str, *, reason_label: str) -> dict[str, Any]:
    project_root = Path(project_root)
    result = abandon_task(project_root, task_id, reason_label=reason_label)
    if not result.get("success"):
        return _from_lifecycle_result(result, task_id=task_id, next_action="Call vte_status to inspect the task.")
    return {
        "status": "ABANDONED", "task_id": task_id,
        "next_action": "Start a new task with vte_start if the work should continue.",
    }
