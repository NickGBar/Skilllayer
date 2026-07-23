"""Bounded, read-only repository evidence capture for VTE Foundation B."""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scope import normalize_repository_path


BASELINE_SCHEMA_VERSION = 1
MAX_FINGERPRINT_FILES = 64
MAX_FINGERPRINT_FILE_BYTES = 512 * 1024
MAX_FINGERPRINT_TOTAL_BYTES = 2 * 1024 * 1024
MAX_TRAVERSAL_DEPTH = 8
_TEST_CONFIGS = (
    "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "setup.py",
    "requirements.txt", "requirements-test.txt", "requirements-dev.txt",
    "package.json", "vitest.config.js", "jest.config.js",
)
_IGNORED_PREFIXES = (".git/", ".venv/", "venv/", "env/", "node_modules/", "__pycache__/", ".pytest_cache/", ".mypy_cache/")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root_fingerprint(root: Path) -> str:
    return "sha256:" + hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _parse_porcelain_z(raw: str) -> tuple[list[str], list[str], list[str], list[str], list[str], list[dict[str, str]], list[dict[str, str]]]:
    changed: set[str] = set(); staged: set[str] = set(); untracked: set[str] = set(); added: set[str] = set(); deleted: set[str] = set()
    renamed: list[dict[str, str]] = []; copied: list[dict[str, str]] = []
    records = raw.split("\0")
    index = 0
    while index < len(records):
        item = records[index]; index += 1
        if not item or len(item) < 4:
            continue
        xy, path = item[:2], item[3:]
        try:
            path = normalize_repository_path(path, directory=False)
        except ValueError:
            continue
        source: str | None = None
        if xy[0] in {"R", "C"} and index < len(records) and records[index]:
            try:
                source = normalize_repository_path(records[index], directory=False)
            except ValueError:
                source = None
            index += 1
        changed.add(path)
        if source:
            changed.add(source)
        if xy[0] not in {" ", "?", "!"}:
            staged.add(path)
            if source: staged.add(source)
        if xy == "??":
            untracked.add(path)
            added.add(path)
        if xy[0] == "A":
            added.add(path)
        if "D" in xy:
            deleted.add(path)
        if source and xy[0] == "R": renamed.append({"from": source, "to": path})
        if source and xy[0] == "C": copied.append({"from": source, "to": path})
    return sorted(changed), sorted(staged), sorted(untracked), sorted(added), sorted(deleted), renamed, copied


def _safe_relative_file(root: Path, rel: str) -> Path | None:
    try:
        candidate = root / normalize_repository_path(rel, directory=False)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def fingerprint_relevant_files(project_root: Path, paths: list[str]) -> dict[str, Any]:
    """Hash only bounded, existing regular files; never read their contents into results."""
    root = project_root.resolve()
    fingerprints: list[dict[str, Any]] = []
    limitations: list[str] = []
    total = 0
    for rel in sorted(set(paths)):
        if len(fingerprints) >= MAX_FINGERPRINT_FILES:
            limitations.append("fingerprint_file_limit_exceeded")
            break
        if rel.startswith(_IGNORED_PREFIXES) or rel == ".env" or rel.startswith(".env/"):
            continue
        candidate = _safe_relative_file(root, rel)
        if candidate is None:
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            limitations.append(f"fingerprint_stat_unavailable:{rel}")
            continue
        if size > MAX_FINGERPRINT_FILE_BYTES:
            limitations.append(f"fingerprint_file_too_large:{rel}")
            continue
        if total + size > MAX_FINGERPRINT_TOTAL_BYTES:
            limitations.append("fingerprint_total_read_limit_exceeded")
            break
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            limitations.append(f"fingerprint_read_unavailable:{rel}")
            continue
        total += size
        fingerprints.append({"path": rel, "size_bytes": size, "sha256": digest})
    return {"fingerprints": fingerprints, "limitations": limitations, "bytes_read": total}


def read_repository_state(project_root: Path, *, task_id: str | None = None, allowed_paths: list[str] | None = None) -> dict[str, Any]:
    """Collect factual Git state.  Results never attribute a change to an actor."""
    root = project_root.resolve()
    git_root = _run_git(root, ["rev-parse", "--show-toplevel"])
    identity = {"path_fingerprint": _root_fingerprint(root), "kind": "directory"}
    if git_root is None:
        return {"repository_identity": identity, "repository_kind": "directory", "git_available": False, "collection_status": "STATE_UNAVAILABLE", "limitations": ["git_unavailable"]}
    if git_root.returncode != 0:
        return {"repository_identity": identity, "repository_kind": "directory", "git_available": True, "collection_status": "NOT_A_GIT_REPOSITORY", "limitations": ["not_a_git_repository"]}
    try:
        git_root_path = Path(git_root.stdout.strip()).resolve()
        if git_root_path != root:
            return {"repository_identity": identity, "repository_kind": "git", "git_available": True, "collection_status": "STATE_UNAVAILABLE", "limitations": ["project_root_is_not_git_root"]}
    except OSError:
        return {"repository_identity": identity, "repository_kind": "git", "git_available": True, "collection_status": "STATE_UNAVAILABLE", "limitations": ["git_root_unreadable"]}
    identity["kind"] = "git"
    status = _run_git(root, ["status", "--porcelain=v1", "-z", "--renames"])
    head = _run_git(root, ["rev-parse", "HEAD"])
    branch = _run_git(root, ["branch", "--show-current"])
    if status is None or head is None or branch is None or status.returncode != 0 or head.returncode != 0:
        return {"repository_identity": identity, "repository_kind": "git", "git_available": True, "collection_status": "STATE_UNAVAILABLE", "limitations": ["git_state_collection_failed"]}
    changed, staged, untracked, added, deleted, renamed, copied = _parse_porcelain_z(status.stdout)
    current_task_prefix = f".skilllayer/tasks/{task_id}/" if task_id else None
    if current_task_prefix:
        changed = [p for p in changed if not p.startswith(current_task_prefix)]
        staged = [p for p in staged if not p.startswith(current_task_prefix)]
        untracked = [p for p in untracked if not p.startswith(current_task_prefix)]
        added = [p for p in added if not p.startswith(current_task_prefix)]
        deleted = [p for p in deleted if not p.startswith(current_task_prefix)]
    fingerprint_paths = set(changed)
    fingerprint_paths.update(p for p in (allowed_paths or []) if not p.endswith("/"))
    fingerprint_paths.update(name for name in _TEST_CONFIGS if (root / name).is_file())
    fp = fingerprint_relevant_files(root, sorted(fingerprint_paths))
    return {
        "repository_identity": identity, "repository_kind": "git", "git_available": True,
        "observed_git_head": head.stdout.strip(), "observed_branch": branch.stdout.strip() or None,
        "git_detached": not bool(branch.stdout.strip()), "worktree_clean": not changed,
        "changed_paths": changed, "staged_paths": staged, "untracked_paths": untracked,
        "added_paths": added, "deleted_paths": deleted, "renamed_paths": renamed, "copied_paths": copied,
        "relevant_file_fingerprints": fp["fingerprints"], "collection_status": "STATE_COLLECTED",
        "limitations": fp["limitations"],
    }


def capture_repository_baseline(project_root: Path, task_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Return a versioned in-memory baseline; this function performs no write."""
    state = read_repository_state(project_root, task_id=task_id, allowed_paths=list(contract.get("allowed_paths", []) or []))
    status = "BASELINE_CAPTURED"
    if state.get("collection_status") == "NOT_A_GIT_REPOSITORY": status = "NOT_A_GIT_REPOSITORY"
    elif state.get("collection_status") != "STATE_COLLECTED": status = "BASELINE_UNAVAILABLE"
    elif state.get("limitations"): status = "BASELINE_INCOMPLETE"
    return {
        "schema_version": BASELINE_SCHEMA_VERSION, "task_id": task_id, "captured_at": _now(),
        "repository_identity": state.get("repository_identity"), "repository_kind": state.get("repository_kind"),
        "git_available": state.get("git_available", False), "git_head": state.get("observed_git_head"),
        "git_branch": state.get("observed_branch"), "git_detached": state.get("git_detached"),
        "worktree_clean": state.get("worktree_clean"), "changed_paths": state.get("changed_paths", []),
        "staged_paths": state.get("staged_paths", []), "untracked_paths": state.get("untracked_paths", []),
        "relevant_file_fingerprints": state.get("relevant_file_fingerprints", []),
        "test_config_fingerprints": [item for item in state.get("relevant_file_fingerprints", []) if item["path"] in _TEST_CONFIGS],
        "baseline_status": status, "limitations": state.get("limitations", []),
    }


def baseline_is_persistable(value: Any) -> bool:
    required = {
        "schema_version", "task_id", "captured_at", "repository_identity", "repository_kind", "git_available",
        "git_head", "git_branch", "git_detached", "worktree_clean", "changed_paths", "staged_paths",
        "untracked_paths", "relevant_file_fingerprints", "test_config_fingerprints", "baseline_status", "limitations",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != BASELINE_SCHEMA_VERSION:
        return False
    if value.get("baseline_status") not in {"BASELINE_CAPTURED", "BASELINE_INCOMPLETE", "BASELINE_UNAVAILABLE", "NOT_A_GIT_REPOSITORY"}:
        return False
    if not isinstance(value.get("task_id"), str) or not isinstance(value.get("repository_identity"), dict):
        return False
    for field in ("changed_paths", "staged_paths", "untracked_paths", "limitations"):
        if not isinstance(value.get(field), list) or not all(isinstance(item, str) for item in value[field]):
            return False
    for field in ("relevant_file_fingerprints", "test_config_fingerprints"):
        if not isinstance(value.get(field), list) or any(
            not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}
            or not isinstance(item["path"], str) or not isinstance(item["size_bytes"], int)
            or not isinstance(item["sha256"], str)
            for item in value[field]
        ):
            return False
    return True
