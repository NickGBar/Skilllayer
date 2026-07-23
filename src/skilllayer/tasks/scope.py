"""Declarative scope rules for internal Verified Task Execution records.

This module deliberately does not execute commands or write files.  It turns
the small, persisted task-contract shape into deterministic path decisions and
compares an observed repository state with a captured baseline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


SCOPE_SCHEMA_VERSION = 1
SCOPE_MODES = frozenset({"EXPLICIT", "PREFIX", "CANDIDATE"})
_CHECK_NAMES = frozenset({"tests", "secrets"})
_PROTECTED_FILE_NAMES = frozenset({".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials"})
_PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_repository_path(value: str, *, directory: bool | None = None) -> str:
    """Return a safe POSIX repository-relative path or raise ``ValueError``.

    Path matching never resolves symlinks: resolution is a filesystem concern
    and callers must reject an escaping symlink before fingerprinting it.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path_invalid")
    value = value.replace("\\", "/")
    if value.startswith("/") or value.startswith("//") or (len(value) > 1 and value[1] == ":"):
        raise ValueError("path_must_be_repository_relative")
    if value.startswith("./"):
        value = value[2:]
    if value.endswith("/"):
        value = value[:-1]
        if not value:
            raise ValueError("path_contains_traversal_or_empty_segment")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("path_contains_traversal_or_empty_segment")
    normalized = str(PurePosixPath(*parts))
    if normalized == "." or normalized.startswith("../"):
        raise ValueError("path_contains_traversal_or_empty_segment")
    if directory is True and not normalized.endswith("/"):
        normalized += "/"
    if directory is False:
        normalized = normalized.rstrip("/")
    return normalized


def is_protected_path(path: str, *, task_id: str | None = None) -> bool:
    """Paths no Foundation B contract may authorize."""
    normalized = normalize_repository_path(path, directory=False)
    lower_name = normalized.rsplit("/", 1)[-1].lower()
    if normalized == ".git" or normalized.startswith(".git/"):
        return True
    if lower_name in _PROTECTED_FILE_NAMES or lower_name.endswith(_PROTECTED_SUFFIXES):
        return True
    if normalized == ".skilllayer" or normalized.startswith(".skilllayer/"):
        current = f".skilllayer/tasks/{task_id}/" if task_id else None
        # Task state is never user source scope.  It may only be ignored by
        # the validator when it is exactly the current task directory.
        return not (current is not None and normalized.startswith(current))
    return False


def _normalise_path_list(value: Any, *, directory_allowed: bool, task_id: str | None, allow_protected: bool = False) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["paths_must_be_a_list"]
    result: list[str] = []
    errors: list[str] = []
    for item in value:
        try:
            wants_directory = isinstance(item, str) and item.endswith("/")
            if wants_directory and not directory_allowed:
                raise ValueError("directory_entries_not_allowed")
            path = normalize_repository_path(item, directory=wants_directory)
            if not allow_protected and is_protected_path(path, task_id=task_id):
                raise ValueError("protected_path_not_permitted")
            result.append(path)
        except ValueError as exc:
            errors.append(f"invalid_path:{exc}")
    return sorted(set(result)), errors


def validate_scope_contract(contract: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    """Validate the bounded Foundation B contract extension without mutation."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(contract, dict):
        return {"valid": False, "error_code": "contract_not_object", "errors": ["contract_not_object"], "normalized": None}
    actual_task_id = task_id or contract.get("task_id")
    mode = contract.get("scope_mode", "EXPLICIT")
    if mode not in SCOPE_MODES:
        errors.append("scope_mode_invalid")
    allowed, path_errors = _normalise_path_list(contract.get("allowed_paths", []), directory_allowed=mode == "PREFIX", task_id=actual_task_id)
    forbidden, forbidden_errors = _normalise_path_list(contract.get("forbidden_paths", []), directory_allowed=True, task_id=actual_task_id, allow_protected=True)
    errors.extend(path_errors)
    errors.extend(forbidden_errors)
    if mode == "EXPLICIT" and any(path.endswith("/") for path in allowed):
        errors.append("explicit_scope_does_not_allow_directory_entries")
    if mode == "CANDIDATE":
        # Representation is retained for future UI agreement, but never
        # automatically inferred or evaluated as a Foundation B permission.
        errors.append("candidate_scope_not_supported_for_validation")
    max_changed_files = contract.get("max_changed_files")
    if max_changed_files is not None and (not isinstance(max_changed_files, int) or isinstance(max_changed_files, bool) or max_changed_files < 1 or max_changed_files > 10000):
        errors.append("max_changed_files_invalid")
    bool_fields = ("allow_new_files", "allow_deleted_files", "allow_test_files")
    normalized: dict[str, Any] = {
        "scope_mode": mode,
        "allowed_paths": allowed,
        "forbidden_paths": forbidden,
        "max_changed_files": max_changed_files,
        "allow_new_files": contract.get("allow_new_files", True),
        "allow_deleted_files": contract.get("allow_deleted_files", True),
        "allow_test_files": contract.get("allow_test_files", True),
        "allowed_generated_paths": [],
        "scope_amendments": contract.get("scope_amendments", []),
    }
    for field in bool_fields:
        if not isinstance(normalized[field], bool):
            errors.append(f"{field}_must_be_boolean")
    generated, generated_errors = _normalise_path_list(contract.get("allowed_generated_paths", []), directory_allowed=True, task_id=actual_task_id)
    normalized["allowed_generated_paths"] = generated
    errors.extend(generated_errors)
    checks = contract.get("required_checks", [])
    if checks and (not isinstance(checks, list) or any(item not in _CHECK_NAMES for item in checks)):
        errors.append("required_checks_invalid")
    if not allowed:
        warnings.append("no_allowed_paths_declared")
    return {
        "valid": not errors,
        "error_code": errors[0] if errors else None,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized if not errors else None,
    }


def apply_scope_amendments(contract: dict[str, Any], amendments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Apply validated append-only additions in chronological order."""
    checked = validate_scope_contract({**contract, "scope_amendments": []})
    if not checked["valid"]:
        return {"valid": False, "errors": checked["errors"], "contract": None}
    effective = dict(checked["normalized"])
    records = amendments if amendments is not None else contract.get("scope_amendments", [])
    if not isinstance(records, list):
        return {"valid": False, "errors": ["scope_amendments_must_be_a_list"], "contract": None}
    seen: set[str] = set()
    for amendment in records:
        if not isinstance(amendment, dict):
            return {"valid": False, "errors": ["amendment_not_object"], "contract": None}
        amendment_id = amendment.get("amendment_id")
        if not isinstance(amendment_id, str) or not amendment_id or amendment_id in seen:
            return {"valid": False, "errors": ["amendment_id_invalid_or_duplicate"], "contract": None}
        seen.add(amendment_id)
        if not amendment.get("consent_reference") or not amendment.get("approved_at"):
            return {"valid": False, "errors": ["amendment_consent_missing"], "contract": None}
        added, errors = _normalise_path_list(amendment.get("added_allowed_paths", []), directory_allowed=effective["scope_mode"] == "PREFIX", task_id=contract.get("task_id"))
        generated, generated_errors = _normalise_path_list(amendment.get("added_generated_paths", []), directory_allowed=True, task_id=contract.get("task_id"))
        if errors or generated_errors:
            return {"valid": False, "errors": errors + generated_errors, "contract": None}
        effective["allowed_paths"] = sorted(set(effective["allowed_paths"]) | set(added))
        effective["allowed_generated_paths"] = sorted(set(effective["allowed_generated_paths"]) | set(generated))
    return {"valid": True, "errors": [], "contract": effective}


def _matches(path: str, rule: str, mode: str) -> bool:
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule if mode == "EXPLICIT" else (path == rule or path.startswith(f"{rule}/"))


def classify_path(path: str, effective_contract: dict[str, Any], *, task_id: str) -> str:
    """Classify one observed path with forbidden rules taking precedence."""
    normalized = normalize_repository_path(path, directory=False)
    current_task_prefix = f".skilllayer/tasks/{task_id}/"
    if normalized.startswith(current_task_prefix):
        return "ignored_internal"
    if is_protected_path(normalized, task_id=task_id):
        return "forbidden"
    mode = effective_contract["scope_mode"]
    if any(_matches(normalized, rule, "PREFIX") for rule in effective_contract["forbidden_paths"]):
        return "forbidden"
    if any(_matches(normalized, rule, mode) for rule in effective_contract["allowed_paths"]):
        return "allowed"
    if any(_matches(normalized, rule, "PREFIX") for rule in effective_contract["allowed_generated_paths"]):
        return "allowed_generated"
    return "unexpected"


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or path.startswith("test/") or path.rsplit("/", 1)[-1].startswith("test_")


def validate_scope(contract: dict[str, Any], baseline: dict[str, Any], observed_state: dict[str, Any], *, task_id: str | None = None, amendments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pure factual scope comparison.  It intentionally assigns no author."""
    task = task_id or contract.get("task_id") if isinstance(contract, dict) else task_id
    base = {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "task_id": task,
        "evaluated_at": _now(),
        "baseline_status": baseline.get("baseline_status") if isinstance(baseline, dict) else None,
        "freshness_class": "FRESHNESS_UNKNOWN",
        "observed_changed_paths": [], "allowed_changes": [], "forbidden_changes": [],
        "unexpected_changes": [], "ignored_internal_changes": [], "new_files": [],
        "deleted_files": [], "renamed_files": [], "changed_file_count": 0,
        "max_changed_files": None, "violations": [], "warnings": [],
        "verdict": "VALIDATION_BLOCKED", "verdict_reasons": [], "evidence_complete": False,
    }
    if not task:
        base.update(verdict_reasons=["task_id_missing"], violations=["task_id_missing"])
        return base
    checked = validate_scope_contract(contract, task_id=task)
    if not checked["valid"]:
        base.update(verdict_reasons=checked["errors"], violations=checked["errors"])
        return base
    amended = apply_scope_amendments(contract, amendments)
    if not amended["valid"]:
        base.update(verdict_reasons=amended["errors"], violations=amended["errors"])
        return base
    rules = amended["contract"]
    base["max_changed_files"] = rules["max_changed_files"]
    if not isinstance(baseline, dict) or baseline.get("task_id") != task:
        base.update(verdict="VALIDATION_BLOCKED", verdict_reasons=["baseline_task_id_mismatch"], violations=["baseline_task_id_mismatch"])
        return base
    if baseline.get("baseline_status") != "BASELINE_CAPTURED":
        base.update(verdict="SCOPE_INCOMPLETE", freshness_class="FRESHNESS_UNKNOWN", verdict_reasons=["baseline_not_complete"])
        return base
    if not isinstance(observed_state, dict) or observed_state.get("collection_status") != "STATE_COLLECTED":
        base.update(verdict="SCOPE_INCOMPLETE", freshness_class="FRESHNESS_UNKNOWN", verdict_reasons=["observed_state_incomplete"])
        return base
    if baseline.get("repository_identity") != observed_state.get("repository_identity"):
        base.update(verdict="VALIDATION_BLOCKED", verdict_reasons=["repository_identity_mismatch"], violations=["repository_identity_mismatch"])
        return base
    if baseline.get("git_head") != observed_state.get("observed_git_head"):
        base.update(verdict="BASELINE_STALE", freshness_class="BASELINE_STALE", verdict_reasons=["git_head_changed_since_baseline"])
        return base
    if baseline.get("limitations") or observed_state.get("limitations"):
        base["warnings"].append("collection_limitations_present")
    paths = sorted(set(observed_state.get("changed_paths", [])))
    try:
        paths = [normalize_repository_path(path, directory=False) for path in paths]
    except ValueError:
        base.update(verdict="VALIDATION_BLOCKED", verdict_reasons=["unsafe_observed_path"], violations=["unsafe_observed_path"])
        return base
    base["observed_changed_paths"] = paths
    base["changed_file_count"] = len(paths)
    baseline_fingerprints = {
        item.get("path"): item.get("sha256")
        for item in baseline.get("relevant_file_fingerprints", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    observed_fingerprints = {
        item.get("path"): item.get("sha256")
        for item in observed_state.get("relevant_file_fingerprints", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    changed_without_git_evidence = sorted(
        path for path, digest in baseline_fingerprints.items()
        if path in observed_fingerprints and observed_fingerprints[path] != digest and path not in paths
    )
    if changed_without_git_evidence:
        base.update(
            verdict="BASELINE_STALE", freshness_class="BASELINE_STALE",
            verdict_reasons=[f"fingerprint_changed_without_git_evidence:{path}" for path in changed_without_git_evidence],
        )
        return base
    new_files = set(observed_state.get("untracked_paths", [])) | set(observed_state.get("added_paths", []))
    deleted_files = set(observed_state.get("deleted_paths", []))
    base["new_files"] = sorted(new_files)
    base["deleted_files"] = sorted(deleted_files)
    base["renamed_files"] = list(observed_state.get("renamed_paths", []))
    for path in paths:
        category = classify_path(path, rules, task_id=task)
        if category == "allowed" or category == "allowed_generated":
            base["allowed_changes"].append(path)
        elif category == "forbidden":
            base["forbidden_changes"].append(path)
        elif category == "ignored_internal":
            base["ignored_internal_changes"].append(path)
        else:
            base["unexpected_changes"].append(path)
        if path in new_files and not rules["allow_new_files"]:
            base["violations"].append(f"new_file_not_allowed:{path}")
        if path in deleted_files and not rules["allow_deleted_files"]:
            base["violations"].append(f"deleted_file_not_allowed:{path}")
        if _is_test_path(path) and not rules["allow_test_files"]:
            base["violations"].append(f"test_file_not_allowed:{path}")
    for rename in base["renamed_files"]:
        if not isinstance(rename, dict):
            base["violations"].append("rename_record_invalid")
            continue
        for side in (rename.get("from"), rename.get("to")):
            if not isinstance(side, str):
                base["violations"].append("rename_path_invalid")
            elif classify_path(side, rules, task_id=task) not in {"allowed", "allowed_generated", "ignored_internal"}:
                base["violations"].append(f"rename_crosses_scope:{side}")
    if base["forbidden_changes"]:
        base["violations"].extend(f"forbidden_path:{path}" for path in base["forbidden_changes"])
    if base["unexpected_changes"]:
        base["violations"].extend(f"unexpected_path:{path}" for path in base["unexpected_changes"])
    if rules["max_changed_files"] is not None and base["changed_file_count"] > rules["max_changed_files"]:
        base["violations"].append("max_changed_files_exceeded")
    evidence_complete = not baseline.get("limitations") and not observed_state.get("limitations")
    base["evidence_complete"] = evidence_complete
    preexisting_changes = set(baseline.get("changed_paths", [])) | set(baseline.get("staged_paths", [])) | set(baseline.get("untracked_paths", []))
    if base["violations"]:
        base["verdict"] = "SCOPE_VIOLATED"
        base["freshness_class"] = "EXTERNAL_DRIFT_POSSIBLE" if base["unexpected_changes"] else "EXPECTED_TASK_CHANGES"
        base["verdict_reasons"] = list(base["violations"])
    elif preexisting_changes:
        base["verdict"] = "SCOPE_INCOMPLETE"
        base["freshness_class"] = "EXTERNAL_DRIFT_POSSIBLE"
        base["warnings"].append("preexisting_repository_changes_make_attribution_uncertain")
        base["verdict_reasons"] = ["preexisting_repository_changes"]
    elif not evidence_complete:
        base["verdict"] = "SCOPE_INCOMPLETE"
        base["freshness_class"] = "FRESHNESS_UNKNOWN"
        base["verdict_reasons"] = ["evidence_incomplete"]
    elif not base["allowed_changes"]:
        base["verdict"] = "SCOPE_EMPTY"
        base["freshness_class"] = "CURRENT_BASELINE"
        base["verdict_reasons"] = ["no_relevant_source_changes"]
    else:
        base["verdict"] = "SCOPE_CLEAN"
        base["freshness_class"] = "EXPECTED_TASK_CHANGES"
        base["verdict_reasons"] = ["all_observed_changes_within_scope"]
    return base


def scope_validation_is_persistable(value: Any) -> bool:
    """Small guard used by persistence before serialising scope evidence."""
    required = {
        "schema_version", "task_id", "evaluated_at", "baseline_status", "freshness_class", "observed_changed_paths",
        "allowed_changes", "forbidden_changes", "unexpected_changes", "ignored_internal_changes", "new_files",
        "deleted_files", "renamed_files", "changed_file_count", "max_changed_files", "violations", "warnings",
        "verdict", "verdict_reasons", "evidence_complete",
    }
    return (
        isinstance(value, dict)
        and set(value) == required
        and value.get("schema_version") == SCOPE_SCHEMA_VERSION
        and value.get("verdict") in {"SCOPE_CLEAN", "SCOPE_EMPTY", "SCOPE_VIOLATED", "SCOPE_INCOMPLETE", "BASELINE_STALE", "VALIDATION_BLOCKED"}
        and isinstance(value.get("task_id"), str)
        and all(isinstance(value.get(field), list) for field in ("observed_changed_paths", "allowed_changes", "forbidden_changes", "unexpected_changes", "ignored_internal_changes", "new_files", "deleted_files", "renamed_files", "violations", "warnings", "verdict_reasons"))
    )
