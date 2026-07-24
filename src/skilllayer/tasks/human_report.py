"""Verified Task Execution — deterministic human-readable task report.

Turns an already-built, structured VTE receipt (``skilllayer.tasks.receipt``)
into a human-readable Markdown/text report. Every sentence here is either a
fixed template string or a direct copy/count of a field already present in
the receipt — no LLM is ever called, and no fact is invented. This module
never inspects source code, raw command output, or anything outside the
receipt/consent objects it is given.

Persistence (``report.json``/``report.md``) reuses Foundation A's consent,
atomic-write, lock, and path-confinement primitives exactly like every other
VTE record; it introduces no second persistence mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock
from .persistence import TaskConsent, _check_consent, _rel, _resolve_paths_or_blocked

REPORT_SCHEMA_VERSION = 1
SUPPORTED_RECEIPT_VERSIONS = frozenset({1})
SUPPORTED_LOCALES = frozenset({"en"})

OVERALL_STATUSES = frozenset({
    "SUCCESS", "SUCCESS_WITH_LIMITATIONS", "PARTIAL", "BLOCKED", "FAILED", "ABANDONED", "UNKNOWN",
})

_MAX_ITEMS = 20
_MAX_ITEM_LENGTH = 200
_MAX_CHANGED_PATHS = 50
# Backstop on the rendered document itself, independent of the per-field
# bounds above — field-level bounding keeps the document small in practice,
# but this is the hard, deterministic ceiling the MCP handler can rely on.
MAX_MARKDOWN_CHARS = 8_000


class HumanReportError(ValueError):
    """Raised for a caller error the milestone requires to be explicit
    (unsupported receipt version, unsupported locale) — never silently
    downgraded or guessed at."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded(items: list[str], *, max_items: int = _MAX_ITEMS, max_length: int = _MAX_ITEM_LENGTH) -> list[str]:
    out = [str(item)[:max_length] for item in items[:max_items]]
    omitted = len(items) - len(out)
    if omitted > 0:
        out.append(f"{omitted} additional item(s) omitted from this view; see the structured receipt.")
    return out


def _truncate_paths(paths: list[str]) -> list[str]:
    if len(paths) <= _MAX_CHANGED_PATHS:
        return list(paths)
    shown = list(paths[:_MAX_CHANGED_PATHS])
    omitted = len(paths) - _MAX_CHANGED_PATHS
    shown.append(f"{omitted} additional changed paths omitted from this view; see the structured receipt.")
    return shown


# ---------------------------------------------------------------------------
# Deterministic mapping: receipt evidence -> report sections
# ---------------------------------------------------------------------------

_PROBLEM_TEMPLATES: dict[str, str] = {
    "UNKNOWN_TEST_RESULT": "Required tests were started, but no completed result was recorded.",
    "FALSE_COMPLETION_PREVENTED": "Finalization was attempted without any recorded test evidence.",
    "TESTS_FAILED": "Required tests completed with a failing result.",
    "FORBIDDEN_PATH_CHANGE": "A changed path is outside the approved task scope.",
    "OUT_OF_SCOPE_CHANGE": "A changed path is outside the approved task scope.",
    "STALE_RESUME_BLOCKED": "The repository changed after the task baseline was captured.",
    "BASELINE_STALE": "The repository changed after the task baseline was captured.",
    "OWNERSHIP_CONFLICT": "Another active task owner currently holds the task lease.",
    "INCOMPLETE_EVIDENCE": "The available evidence is insufficient to verify task completion.",
    "SCOPE_AMENDMENT_REQUIRED": "The change requires an approved scope amendment.",
}

_NEXT_ACTION_TEMPLATES: dict[str, str] = {
    "UNKNOWN_TEST_RESULT": "Rerun the required tests and record a definite pass/fail result.",
    "FALSE_COMPLETION_PREVENTED": "Run the required tests and record a definite pass/fail result.",
    "TESTS_FAILED": "Fix the failing test(s), then rerun them before finalizing again.",
    "FORBIDDEN_PATH_CHANGE": "Review and revert the forbidden change manually, or request a bounded scope amendment.",
    "OUT_OF_SCOPE_CHANGE": "Review and revert the forbidden change manually, or request a bounded scope amendment.",
    "STALE_RESUME_BLOCKED": "Confirm resumption after reviewing what changed in the repository.",
    "BASELINE_STALE": "Confirm resumption after reviewing what changed in the repository.",
    "OWNERSHIP_CONFLICT": "Reclaim ownership once the existing lease has expired.",
    "INCOMPLETE_EVIDENCE": "Inspect the incomplete task evidence, then retry.",
    "SCOPE_AMENDMENT_REQUIRED": "Request a bounded scope amendment for the additional paths.",
    "MAX_CHANGED_FILES_EXCEEDED": "Reduce the change to the approved maximum number of files, or request a scope amendment.",
}

_BLOCKER_LABELS: dict[str, str] = {
    "baseline_stale": "The repository changed after the baseline was captured (stale baseline).",
    "max_changed_files_exceeded": "More files changed than the approved maximum.",
}
_BLOCKER_PREFIX_LABELS: dict[str, str] = {
    "forbidden_path:": "Forbidden path changed: {}",
    "unexpected_path:": "Unexpected (out-of-scope) path changed: {}",
    "new_file_not_allowed:": "New file not permitted by scope: {}",
    "deleted_file_not_allowed:": "Deleted file not permitted by scope: {}",
    "test_file_not_allowed:": "Test file change not permitted by scope: {}",
    "rename_crosses_scope:": "Renamed path crosses approved scope: {}",
}
_TEST_RELATED_BLOCKER_CODES = frozenset({"tests_status_missing", "required_tests_not_recorded"})


def _blocker_label(code: str) -> str:
    if code in _BLOCKER_LABELS:
        return _BLOCKER_LABELS[code]
    for prefix, template in _BLOCKER_PREFIX_LABELS.items():
        if code.startswith(prefix):
            return template.format(code[len(prefix):])
    return f"Blocked: {code}"


def _tests_view(receipt: dict[str, Any]) -> dict[str, Any]:
    tests = receipt.get("tests_summary") or {}
    if not isinstance(tests, dict):
        return {"reported_recorded": False, "passed": None, "summary_label": None}
    return {
        "reported_recorded": bool(tests.get("reported_recorded", tests.get("recorded", False))),
        "passed": tests.get("passed"),
        "summary_label": tests.get("summary_label"),
    }


def _succeeded_items(receipt: dict[str, Any]) -> list[str]:
    items: list[str] = []
    if receipt.get("baseline_status") == "BASELINE_CAPTURED":
        items.append("Repository baseline was captured.")
    if receipt.get("scope_status") == "SCOPE_CLEAN":
        items.append("Scope validation passed; all changes were within the approved scope.")
    allowed = receipt.get("allowed_changes") or []
    unexpected = receipt.get("unexpected_changes") or []
    if allowed and not unexpected:
        items.append(f"{len(allowed)} approved file(s) were changed.")
    if receipt.get("changed_paths") and not unexpected:
        items.append("No forbidden or unexpected paths were changed.")
    tests = _tests_view(receipt)
    if tests["reported_recorded"] and tests["passed"] is True:
        label = tests["summary_label"]
        items.append(f"Required tests completed successfully ({label})." if label else "Required tests completed successfully.")
    checkpoints = receipt.get("checkpoints_created", 0) or 0
    if checkpoints:
        items.append(f"{checkpoints} checkpoint(s) were created during the task.")
    recovered = receipt.get("interruptions_recovered", 0) or 0
    if recovered:
        items.append(f"{recovered} interrupted task run(s) were safely resumed.")
    if receipt.get("evidence_complete"):
        items.append("Final evidence was complete.")
    return items


def _failed_items(receipt: dict[str, Any]) -> list[str]:
    items: list[str] = []
    tests = _tests_view(receipt)
    if tests["reported_recorded"] and tests["passed"] is False:
        label = tests["summary_label"]
        items.append(f"Required tests completed with a failing result ({label})." if label else "Required tests completed with a failing result.")
    return items


def _blocked_items(receipt: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for code in receipt.get("blockers") or []:
        if code in _TEST_RELATED_BLOCKER_CODES:
            continue  # represented in failed_items/unknown_items and problem_*, not here
        items.append(_blocker_label(code))
    return items


def _unknown_items(receipt: dict[str, Any]) -> list[str]:
    items: list[str] = []
    tests = _tests_view(receipt)
    if tests["reported_recorded"] and tests["passed"] is None:
        items.append("Required tests were started, but no completed result was recorded.")
    if receipt.get("final_verdict") == "TASK_INCOMPLETE" and not receipt.get("evidence_complete"):
        items.append("Available evidence is insufficient to verify task completion.")
    if receipt.get("resume_status") == "CHECKPOINT_CORRUPT":
        items.append("Checkpoint history could not be verified.")
    if receipt.get("final_verdict") is None and receipt.get("task_state") not in {
        None, "COMPLETED", "FAILED", "ABANDONED",
    }:
        items.append("The task has not been finalized yet, so its outcome is not yet known.")
    return items


def _derive_problem(receipt: dict[str, Any]) -> tuple[str | None, list[str]]:
    tests = _tests_view(receipt)
    if tests["reported_recorded"] and tests["passed"] is False:
        summary = _PROBLEM_TEMPLATES["TESTS_FAILED"]
        return summary, [summary]

    prevented = receipt.get("prevented_actions") or []
    if prevented:
        details: list[str] = []
        seen: set[str] = set()
        for item in prevented:
            kind = item.get("intervention_type")
            label = _PROBLEM_TEMPLATES.get(kind, f"Blocked: {kind}.")
            if label not in seen:
                details.append(label)
                seen.add(label)
        summary = details[0]
        return summary, details

    blockers = receipt.get("blockers") or []
    real_blockers = [b for b in blockers if b not in _TEST_RELATED_BLOCKER_CODES]
    if real_blockers:
        details = [_blocker_label(code) for code in real_blockers]
        return details[0], details

    if receipt.get("final_verdict") == "TASK_INCOMPLETE":
        summary = _PROBLEM_TEMPLATES["INCOMPLETE_EVIDENCE"]
        return summary, [summary]

    return None, []


def _derive_next_action(receipt: dict[str, Any], overall_status: str, problem_summary: str | None) -> str:
    if overall_status == "SUCCESS":
        return "No further verified action is required."
    if overall_status == "SUCCESS_WITH_LIMITATIONS":
        return "Review the reported limitations; no further verified action is strictly required."

    prevented = receipt.get("prevented_actions") or []
    if prevented:
        kind = prevented[-1].get("intervention_type")
        if kind in _NEXT_ACTION_TEMPLATES:
            return _NEXT_ACTION_TEMPLATES[kind]

    tests = _tests_view(receipt)
    if tests["reported_recorded"] and tests["passed"] is False:
        return _NEXT_ACTION_TEMPLATES["TESTS_FAILED"]
    if tests["reported_recorded"] and tests["passed"] is None:
        return _NEXT_ACTION_TEMPLATES["UNKNOWN_TEST_RESULT"]

    for code in receipt.get("blockers") or []:
        if code == "max_changed_files_exceeded":
            return _NEXT_ACTION_TEMPLATES["MAX_CHANGED_FILES_EXCEEDED"]
        if code.startswith("forbidden_path:") or code.startswith("unexpected_path:"):
            return _NEXT_ACTION_TEMPLATES["FORBIDDEN_PATH_CHANGE"]
        if code == "baseline_stale":
            return _NEXT_ACTION_TEMPLATES["BASELINE_STALE"]

    if overall_status == "ABANDONED":
        return "Start a new task with vte_start if the work should continue."
    if overall_status == "UNKNOWN":
        return "Call vte_status to inspect the task's current state before taking further action."
    return "Review the reported problem, resolve it, then call vte_finalize again."


def _overall_status(receipt: dict[str, Any]) -> str:
    verdict = receipt.get("final_verdict")
    task_state = receipt.get("task_state")
    tests = _tests_view(receipt)

    if verdict == "TASK_VERIFIED_COMPLETE":
        return "SUCCESS"
    if verdict == "TASK_COMPLETE_WITH_LIMITATIONS":
        return "SUCCESS_WITH_LIMITATIONS"
    if verdict == "TASK_INCOMPLETE":
        return "PARTIAL"
    if verdict == "TASK_BLOCKED":
        # A genuinely recorded, definite test failure is a more specific
        # outcome than a generic block (scope/staleness/ownership) — Foundation
        # D's finalize_task collapses both into TASK_BLOCKED (it only inspects
        # tests_status["recorded"], never "passed"), so this report layer
        # distinguishes them using the receipt's own tests_summary evidence.
        if tests["reported_recorded"] and tests["passed"] is False:
            return "FAILED"
        return "BLOCKED"
    if verdict == "TASK_FAILED":
        return "FAILED"
    if verdict == "TASK_ABANDONED" or task_state == "ABANDONED":
        return "ABANDONED"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# build_human_report
# ---------------------------------------------------------------------------


def build_human_report(receipt: dict[str, Any], *, title: str | None = None, locale: str = "en") -> dict[str, Any]:
    """Build the structured, versioned report model from a VTE receipt.

    Raises ``HumanReportError`` for an unsupported ``receipt_version`` or an
    unsupported ``locale`` — never silently guesses or falls back."""
    if locale not in SUPPORTED_LOCALES:
        raise HumanReportError(f"unsupported_locale:{locale}")
    receipt_version = receipt.get("receipt_version")
    if receipt_version not in SUPPORTED_RECEIPT_VERSIONS:
        raise HumanReportError(f"unsupported_receipt_version:{receipt_version!r}")

    overall_status = _overall_status(receipt)
    problem_summary, problem_details = _derive_problem(receipt)
    next_action = _derive_next_action(receipt, overall_status, problem_summary)

    succeeded = _succeeded_items(receipt) if overall_status not in {"UNKNOWN"} else _succeeded_items(receipt)
    failed = [] if overall_status == "SUCCESS" else _failed_items(receipt)
    blocked = [] if overall_status == "SUCCESS" else _blocked_items(receipt)
    unknown = [] if overall_status == "SUCCESS" else _unknown_items(receipt)

    changed_paths = _truncate_paths(list(receipt.get("changed_paths") or []))

    report = {
        "report_version": REPORT_SCHEMA_VERSION,
        "receipt_version": receipt_version,
        "task_id": receipt.get("task_id"),
        "title": title or receipt.get("objective_label") or receipt.get("task_id"),
        "final_verdict": receipt.get("final_verdict"),
        "overall_status": overall_status,
        "succeeded_items": _bounded(succeeded),
        "failed_items": _bounded(failed),
        "blocked_items": _bounded(blocked),
        "unknown_items": _bounded(unknown),
        "problem_summary": (problem_summary[:_MAX_ITEM_LENGTH] if problem_summary else None),
        "problem_details": _bounded(problem_details),
        "prevented_actions": list(receipt.get("prevented_actions") or []),
        "changed_paths": changed_paths,
        "tests": _tests_view(receipt),
        "scope": {
            "status": receipt.get("scope_status"),
            "allowed_changes": list(receipt.get("allowed_changes") or []),
            "unexpected_changes": list(receipt.get("unexpected_changes") or []),
        },
        "resume": {
            "status": receipt.get("resume_status"),
            "interruptions_recovered": receipt.get("interruptions_recovered", 0),
        },
        "evidence_status": "COMPLETE" if receipt.get("evidence_complete") else "INCOMPLETE",
        "limitations": _bounded(list(receipt.get("limitations") or [])),
        "next_action": next_action[:_MAX_ITEM_LENGTH],
        "created_at": _now(),
        "locale": locale,
    }
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_OVERALL_STATUS_LABEL = {
    "SUCCESS": "SUCCESS",
    "SUCCESS_WITH_LIMITATIONS": "SUCCESS WITH LIMITATIONS",
    "PARTIAL": "PARTIAL",
    "BLOCKED": "BLOCKED",
    "FAILED": "FAILED",
    "ABANDONED": "ABANDONED",
    "UNKNOWN": "UNKNOWN",
}


def render_human_report_markdown(report: dict[str, Any]) -> str:
    """Deterministic Markdown rendering. Sections are omitted when empty,
    except Result/Next action/Verdict, which are always present."""
    lines: list[str] = ["# Verified Task Report", ""]

    lines.append("## Result")
    lines.append("")
    lines.append(f"**{_OVERALL_STATUS_LABEL.get(report['overall_status'], report['overall_status'])}** — {report['title']}")
    lines.append("")

    if report["succeeded_items"]:
        lines.append("## What succeeded")
        lines.append("")
        lines.extend(f"- {item}" for item in report["succeeded_items"])
        lines.append("")

    not_succeeded = report["failed_items"] + report["blocked_items"] + report["unknown_items"]
    if not_succeeded:
        lines.append("## What did not succeed")
        lines.append("")
        for item in report["failed_items"]:
            lines.append(f"- [FAILED] {item}")
        for item in report["blocked_items"]:
            lines.append(f"- [BLOCKED] {item}")
        for item in report["unknown_items"]:
            lines.append(f"- [UNKNOWN] {item}")
        lines.append("")

    if report["problem_summary"]:
        lines.append("## Problem")
        lines.append("")
        lines.append(report["problem_summary"])
        if len(report["problem_details"]) > 1 or (report["problem_details"] and report["problem_details"][0] != report["problem_summary"]):
            lines.append("")
            lines.extend(f"- {item}" for item in report["problem_details"])
        lines.append("")

    if report["prevented_actions"]:
        lines.append("## Prevented by SkillLayer")
        lines.append("")
        for item in report["prevented_actions"]:
            lines.append(f"- {item.get('prevented_outcome', item.get('intervention_type', 'unknown'))}")
        lines.append("")

    if report["changed_paths"]:
        lines.append("## Changed files")
        lines.append("")
        lines.extend(f"- {path}" for path in report["changed_paths"])
        lines.append("")

    tests = report["tests"]
    if tests.get("reported_recorded"):
        lines.append("## Tests and validation")
        lines.append("")
        passed = tests.get("passed")
        label = tests.get("summary_label")
        if passed is True:
            lines.append(f"Tests passed{f' ({label})' if label else ''}.")
        elif passed is False:
            lines.append(f"Tests failed{f' ({label})' if label else ''}.")
        else:
            lines.append("Tests were started, but no completed result was recorded.")
        lines.append("")

    if report["limitations"]:
        lines.append("## Limitations")
        lines.append("")
        lines.extend(f"- {item}" for item in report["limitations"])
        lines.append("")

    tail_lines = [
        "## Next action", "", report["next_action"], "",
        "## Verdict", "", str(report.get("final_verdict") or "NOT_FINALIZED"), "",
    ]

    body = "\n".join(lines).rstrip() + "\n"
    tail = "\n".join(tail_lines).rstrip() + "\n"
    full = body + "\n" + tail
    if len(full) <= MAX_MARKDOWN_CHARS:
        return full

    # Next action/Verdict are always fully preserved (required sections);
    # only the body above them (What succeeded/failed/Problem/etc.) is cut.
    notice = "\n[Report truncated: additional content omitted from this view; see the structured receipt.]\n\n"
    budget = max(MAX_MARKDOWN_CHARS - len(tail) - len(notice), 0)
    return body[:budget] + notice + tail


def render_human_report_text(report: dict[str, Any]) -> str:
    """Plain-text rendering — same content as the Markdown form, without
    Markdown syntax."""
    markdown = render_human_report_markdown(report)
    text = markdown.replace("# ", "").replace("## ", "").replace("**", "")
    lines = [line[2:] if line.startswith("- ") else line for line in text.splitlines()]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Persistence — reuses Foundation A's consent/atomic-write/lock/path-
# confinement primitives directly; no second mechanism.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportPaths:
    report_json_path: Path
    report_md_path: Path


def _report_paths(task_dir: Path) -> ReportPaths:
    return ReportPaths(report_json_path=task_dir / "report.json", report_md_path=task_dir / "report.md")


def write_human_report(
    project_root: Path, task_id: str, *, consent: TaskConsent | None, report: dict[str, Any], markdown: str,
) -> dict[str, Any]:
    """Persist report.json + report.md. Write-once per exact content:
    a retry with byte-identical content is idempotent (no error, no
    rewrite); a retry with DIFFERENT content for an existing report is
    rejected explicitly rather than silently overwritten."""
    if report.get("task_id") != task_id:
        return {"success": False, "error": "cross_task_report_reference", "written_paths": []}

    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return {"success": False, "error": blocked.get("error", "task_path_invalid"), "written_paths": []}

    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="result")
    if consent_state is not None:
        return {"success": False, "error": "consent_required_or_invalid", "written_paths": []}

    report_paths = _report_paths(paths.task_dir)
    root = project_root.resolve()

    import json as _json
    encoded_report = _json.dumps(report, sort_keys=True, ensure_ascii=False)

    try:
        with memory_lock(root / ".skilllayer"):
            if report_paths.report_json_path.is_symlink() or report_paths.report_md_path.is_symlink():
                return {"success": False, "error": "symlink_not_permitted", "written_paths": []}
            existing_json = None
            if report_paths.report_json_path.exists():
                try:
                    existing_json = report_paths.report_json_path.read_text(encoding="utf-8")
                except OSError:
                    return {"success": False, "error": "existing_report_unreadable", "written_paths": []}
            existing_md = report_paths.report_md_path.read_text(encoding="utf-8") if report_paths.report_md_path.exists() else None

            if existing_json is not None or existing_md is not None:
                same_json = existing_json is not None and _json.loads(existing_json) == report
                same_md = existing_md == markdown
                if same_json and same_md:
                    return {"success": True, "error": None, "written_paths": [], "idempotent": True}
                return {"success": False, "error": "conflicting_report_rewrite", "written_paths": []}

            paths.task_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(report_paths.report_json_path, report)
            _atomic_write_text(report_paths.report_md_path, markdown)
    except MemoryLockTimeoutError as exc:
        return {"success": False, "error": "memory_lock_timeout", "written_paths": [], "detail": str(exc)}
    except OSError as exc:
        return {"success": False, "error": "io_error", "written_paths": [], "detail": str(exc)}

    return {
        "success": True, "error": None, "idempotent": False,
        "written_paths": [_rel(report_paths.report_json_path, root), _rel(report_paths.report_md_path, root)],
    }


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{id(content)}")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_human_report(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """Read-only. Returns the persisted report.json, or None if absent/invalid."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return None
    report_paths = _report_paths(paths.task_dir)
    if report_paths.report_json_path.is_symlink() or not report_paths.report_json_path.exists():
        return None
    import json as _json
    try:
        data = _json.loads(report_paths.report_json_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("task_id") == task_id else None


# ---------------------------------------------------------------------------
# Consistency validation — fail closed
# ---------------------------------------------------------------------------


def validate_report_consistency(report: dict[str, Any], receipt: dict[str, Any], markdown: str) -> dict[str, Any]:
    """Prove the report cannot contradict its receipt. Returns
    {"valid": bool, "errors": [...]}. Never raises."""
    errors: list[str] = []

    if report.get("task_id") != receipt.get("task_id"):
        errors.append("task_id_mismatch")
    if report.get("final_verdict") != receipt.get("final_verdict"):
        errors.append("final_verdict_mismatch")

    report_paths_set = {p for p in report.get("changed_paths", []) if not p.startswith(("additional", "0 additional"))}
    receipt_paths_set = set(receipt.get("changed_paths", []))
    if not report_paths_set.issubset(receipt_paths_set) and len(report.get("changed_paths", [])) <= _MAX_CHANGED_PATHS:
        errors.append("changed_paths_not_subset_of_receipt")

    report_tests = report.get("tests", {})
    receipt_tests = receipt.get("tests_summary") or {}
    if bool(report_tests.get("reported_recorded")) != bool(receipt_tests.get("reported_recorded", receipt_tests.get("recorded", False))):
        errors.append("tests_recorded_mismatch")
    if report_tests.get("passed") != receipt_tests.get("passed"):
        errors.append("tests_passed_mismatch")

    report_prevented_ids = {item.get("intervention_id") for item in report.get("prevented_actions", [])}
    receipt_prevented_ids = {item.get("intervention_id") for item in receipt.get("prevented_actions", [])}
    if report_prevented_ids != receipt_prevented_ids:
        errors.append("prevented_actions_mismatch")

    if receipt.get("limitations") and not report.get("limitations"):
        errors.append("limitations_omitted")

    overall = report.get("overall_status")
    if overall == "SUCCESS" and (report.get("unknown_items") or report.get("blocked_items") or report.get("failed_items")):
        errors.append("success_coexists_with_non_success_items")
    if overall == "BLOCKED" and (not report.get("problem_summary") or not report.get("next_action")):
        errors.append("blocked_missing_problem_or_next_action")
    if len([n for n in [report.get("next_action")] if n]) != 1:
        errors.append("next_action_not_exactly_one")

    verdict_text = str(report.get("final_verdict") or "NOT_FINALIZED")
    if verdict_text not in markdown:
        errors.append("markdown_verdict_mismatch")

    return {"valid": not errors, "errors": errors}
