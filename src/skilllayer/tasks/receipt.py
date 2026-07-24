"""Verified Task Execution — verification receipt.

The receipt is a stable, structured summary derived ONLY from already-persisted
Foundation A-D evidence (task status, latest final result, checkpoint chain,
transition history, intervention records). It never asks an LLM to state a
fact: every field here is copied or counted from records that already passed
through Foundation A's redaction/rejection gate. This module is read-only —
it writes nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checkpoint import read_task_ownership  # noqa: F401  (kept for callers that want ownership too)
from .orchestrator import _orchestrator_paths, _read_transition_history, get_task_status
from .persistence import _resolve_paths_or_blocked
from .resume import load_checkpoint_chain

RECEIPT_SCHEMA_VERSION = 1
# Must match config.defaults.VERIFIED_TASK_EXECUTION_SKILL["name"] exactly —
# this is the identifier a receipt's consumer uses to look up the skill that
# produced it in the professional skill catalog.
SKILL_NAME = "verified_task_execution"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_final_result(project_root: Path, task_id: str) -> dict[str, Any] | None:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return None
    o_paths = _orchestrator_paths(paths.task_dir)
    path = o_paths.evidence_dir / "final_result.json"
    if path.is_symlink() or not path.exists():
        return None
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _count_recovered_interruptions(project_root: Path, task_id: str) -> int:
    """Count transitions where resume_task successfully reached RUNNING."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return 0
    o_paths = _orchestrator_paths(paths.task_dir)
    ok, _, transitions = _read_transition_history(o_paths, task_id)
    if not ok:
        return 0
    return sum(
        1 for record in transitions
        if record.get("operation") == "resume_task"
        and record.get("outcome") == "SUCCESS"
        and record.get("next_state") == "RUNNING"
    )


def build_receipt(project_root: Path, task_id: str) -> dict[str, Any]:
    """Assemble the stable public receipt shape from persisted evidence only."""
    from .interventions import list_interventions

    status = get_task_status(project_root, task_id)
    if not status.get("success"):
        return {
            "receipt_version": RECEIPT_SCHEMA_VERSION, "task_id": task_id, "skill_name": SKILL_NAME,
            "final_verdict": "TASK_NOT_FOUND", "task_state": None, "baseline_status": None,
            "scope_status": None, "resume_status": None, "changed_paths": [], "allowed_changes": [],
            "unexpected_changes": [], "tests_summary": None, "checkpoints_created": 0,
            "interruptions_recovered": 0, "prevented_actions": [], "confirmations_required": [],
            "evidence_complete": False, "limitations": ["task_status_unavailable"], "blockers": [],
            "objective_label": None, "created_at": _now(),
        }

    final_result = _read_final_result(project_root, task_id)
    chain = load_checkpoint_chain(project_root, task_id)
    interventions = list_interventions(project_root, task_id)

    if final_result is not None:
        final_verdict = final_result.get("final_verdict")
        changed_paths = final_result.get("changed_paths", [])
        allowed_changes = final_result.get("allowed_changes", [])
        unexpected_changes = final_result.get("unexpected_changes", [])
        tests_summary = final_result.get("tests_status")
        evidence_complete = bool(final_result.get("evidence_complete"))
        limitations = list(final_result.get("limitations", []))
        blockers = list(final_result.get("blockers", []))
    else:
        final_verdict = None
        changed_paths, allowed_changes, unexpected_changes = [], [], []
        tests_summary = None
        evidence_complete = bool(status.get("evidence_complete"))
        limitations = []
        blockers = []

    from .persistence import read_task_contract
    contract = read_task_contract(project_root, task_id)
    objective_label = contract["record"].get("objective_label") if contract.get("success") else None

    return {
        "receipt_version": RECEIPT_SCHEMA_VERSION,
        "task_id": task_id,
        "skill_name": SKILL_NAME,
        "final_verdict": final_verdict,
        "task_state": status.get("current_state"),
        "baseline_status": status.get("baseline_status"),
        "scope_status": status.get("scope_status"),
        "resume_status": status.get("resume_status"),
        "changed_paths": changed_paths,
        "allowed_changes": allowed_changes,
        "unexpected_changes": unexpected_changes,
        "tests_summary": tests_summary,
        "checkpoints_created": len(chain.get("checkpoints", [])),
        "interruptions_recovered": _count_recovered_interruptions(project_root, task_id),
        "prevented_actions": interventions.get("interventions", []) if interventions.get("success") else [],
        "confirmations_required": [],  # populated by public_api.py callers that track live confirmation state
        "evidence_complete": evidence_complete,
        "limitations": limitations,
        "blockers": blockers,
        "objective_label": objective_label,
        "created_at": _now(),
    }


_VERDICT_LINE = {
    "TASK_VERIFIED_COMPLETE": "Verdict: VERIFIED COMPLETE",
    "TASK_COMPLETE_WITH_LIMITATIONS": "Verdict: COMPLETE WITH LIMITATIONS",
    "TASK_INCOMPLETE": "Verdict: INCOMPLETE",
    "TASK_BLOCKED": "Verdict: BLOCKED",
    "TASK_FAILED": "Verdict: FAILED",
    "TASK_ABANDONED": "Verdict: ABANDONED",
}


def render_receipt_text(receipt: dict[str, Any]) -> str:
    """Human-readable checklist rendering. Every line states a fact already
    present in ``receipt`` — no line here is generated freely."""
    lines = ["Verified Task Execution", ""]

    if receipt.get("baseline_status") == "BASELINE_CAPTURED":
        lines.append("✓ Baseline captured")
    else:
        lines.append(f"✗ Baseline status: {receipt.get('baseline_status')}")

    changed_n = len(receipt.get("changed_paths") or [])
    allowed_n = len(receipt.get("allowed_changes") or [])
    unexpected = receipt.get("unexpected_changes") or []
    if changed_n:
        if not unexpected:
            lines.append(f"✓ {changed_n} changed file(s) matched approved scope")
        else:
            lines.append(f"✗ {len(unexpected)} changed file(s) outside approved scope")
    if unexpected:
        lines.append(f"✗ Forbidden/unexpected paths changed: {', '.join(unexpected[:5])}")
    else:
        lines.append("✓ No forbidden paths changed")

    tests = receipt.get("tests_summary")
    if isinstance(tests, dict) and tests.get("recorded"):
        passed = tests.get("passed")
        if passed is True:
            lines.append("✓ Tests passed")
        elif passed is False:
            lines.append("✗ Tests failed")
        else:
            lines.append("? Test outcome recorded but not conclusively pass/fail")
    else:
        lines.append("✗ No test evidence recorded")

    recovered = receipt.get("interruptions_recovered", 0)
    if recovered:
        lines.append(f"✓ {recovered} interrupted validation(s) safely rerun")

    prevented = receipt.get("prevented_actions") or []
    if prevented:
        lines.append(f"⚠ {len(prevented)} unsafe action(s) were prevented (see prevented_actions)")

    if receipt.get("confirmations_required"):
        lines.append(f"⚠ {len(receipt['confirmations_required'])} confirmation(s) still required")

    lines.append("")
    lines.append(_VERDICT_LINE.get(receipt.get("final_verdict"), f"Verdict: {receipt.get('final_verdict')}"))
    return "\n".join(lines)
