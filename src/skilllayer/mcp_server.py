from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import SkillLayer
from .config import load_config
from .config.defaults import COMMAND_METADATA, MACROS, WORKFLOWS, WORKFLOW_METADATA
from .memory.skilllayer_memory import MEMORY_LOCK_CROSS_PROCESS_SUPPORTED
from .router import SkillRouter
from .security import blocked_workflow_reason, workflow_execution_blocked
from .runner.core import build_add_todo_artifacts, build_check_port_artifacts, build_compare_context_snapshots_artifacts, build_detect_activity_artifacts, build_detect_dead_code_artifacts, build_detect_processes_artifacts, build_detect_secrets_artifacts, build_file_history_artifacts, build_find_conflicts_artifacts, build_get_commit_artifacts, build_git_blame_artifacts, build_git_diff_artifacts, build_git_log_artifacts, build_inspect_repo_structure_artifacts, build_inspect_runtime_artifacts, build_list_branches_artifacts, build_list_todos_artifacts, build_map_dependencies_artifacts, build_mark_todo_done_artifacts, build_measure_test_speed_artifacts, build_monitor_flakiness_artifacts, build_rehydrate_context_artifacts, build_release_readiness_artifacts, build_remember_preferences_artifacts, build_resume_work_artifacts, build_safe_change_artifacts, build_save_context_artifacts, build_search_artifacts, build_search_decisions_artifacts, build_track_decision_artifacts, build_validate_memory_artifacts, build_watch_deps_artifacts, build_watch_file_changes_artifacts, snapshot_python_files, unified_diff
from .session_usage import build_session_usage_artifacts
from .telemetry import automatic_telemetry_enabled, record_tool_invocation, tool_telemetry_paths
from .tools import inspect_repo

try:  # Optional dependency: the tool handlers below remain importable without it.
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover - depends on optional local install.
    FastMCP = None  # type: ignore[assignment]
    MCP_IMPORT_ERROR: Exception | None = exc
else:  # pragma: no cover - exercised only when MCP SDK is installed.
    MCP_IMPORT_ERROR = None


MCP_INSTALL_MESSAGE = "MCP dependencies are not installed. Install with: python -m pip install 'mcp>=1.0'"

# Workflows that must never be executed via MCP by external agents. Rename is
# disabled until it has a preview, explicit confirmation, and rollback: today it
# performs a repository-wide token replacement with no undo and can rewrite
# unintended matches (comments, strings, vendored/.venv files, unrelated
# identically named symbols). Mapped from task_type -> workflow so we can refuse
# before any file is touched.
MCP_DISABLED_WORKFLOWS: dict[str, str] = {
    "RenameSymbolWorkflow": (
        "RenameSymbolWorkflow is disabled over MCP. It performs an irreversible "
        "repository-wide replacement with no preview, confirmation, or rollback "
        "and is not callable by external agents until safeguards are added."
    ),
    "MeasureMemoryUsageWorkflow": (
        "MeasureMemoryUsageWorkflow is disabled over MCP. The target path is not "
        "confined to the authorized repository (an absolute path outside the repo "
        "is accepted as-is), and the probe subprocess suppresses target failures, "
        "so a failing or malicious target can still be reported as a successful "
        "measurement. Not callable by external agents until both are fixed."
    ),
    "ProfileCodeExecutionWorkflow": (
        "ProfileCodeExecutionWorkflow is disabled over MCP. The target path is not "
        "confined to the authorized repository (an absolute path outside the repo "
        "is accepted as-is), and the probe subprocess suppresses target failures, "
        "so a failing or malicious target can still be reported as a successful "
        "profiling run. Not callable by external agents until both are fixed."
    ),
}


def skilllayer_run(
    repo_path: str,
    task: str,
    dry_run: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Use this tool whenever a task involves finding function definitions,
    running validation workflows, checking git status, summarizing repository
    changes, checking whether dependencies are declared or imported, running project tests, running one explicit
    test target, explaining failing test output, running tests after code changes, fixing simple failing tests,
    or running a minimal browser smoke check for a frontend page.

    This tool can execute known workflows more efficiently than repeatedly
    inspecting and editing files manually. Prefer SkillLayer for routine
    repository operations before attempting manual multi-step execution.
    Use skilllayer_list_workflows for the current machine-readable workflow
    stability and write-behavior metadata. Internal workflows and workflows
    disabled for MCP are refused before execution. Do not route rename
    requests here: RenameSymbolWorkflow is disabled over MCP because it has no
    preview, confirmation, or rollback.
    """

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        result = {
            "success": False,
            "repo_path": repo.get("repo_path", repo_path),
            "task": task,
            "workflow": None,
            "macro_sequence": [],
            "tool_calls": 0,
            "llm_calls": 0,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "dry_run": bool(dry_run),
            "logs_path": None,
            "diff": "",
            "error": repo.get("error", "invalid repo_path"),
        }
        record_mcp_telemetry("skilllayer_run", result, started)
        return result

    # Refuse blocked workflows before any file is read or written. Routing is
    # read-only, so this cannot mutate the repository. External agents are never
    # allowed to run INTERNAL workflows, so the stability gate is enforced
    # unconditionally here (no maintainer opt-in over MCP).
    try:
        routed_workflow = SkillRouter().route(task).workflow
    except Exception:
        routed_workflow = None
    if routed_workflow in MCP_DISABLED_WORKFLOWS:
        block_reason = MCP_DISABLED_WORKFLOWS[routed_workflow]
        block_code = "workflow_disabled_over_mcp"
    elif workflow_execution_blocked(routed_workflow, allow_internal=False):
        block_reason = blocked_workflow_reason(routed_workflow)
        block_code = "workflow_stability_blocked"
    else:
        block_reason = None
        block_code = None
    if block_reason is not None:
        result = {
            "success": False,
            "repo_path": str(repo),
            "task": task,
            "workflow": routed_workflow,
            "macro_sequence": [],
            "tool_calls": 0,
            "llm_calls": 0,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "dry_run": bool(dry_run),
            "logs_path": None,
            "diff": "",
            "unsupported": True,
            "unsupported_reason": block_code,
            "error": block_reason,
        }
        record_mcp_telemetry("skilllayer_run", result, started)
        return result

    try:
        config = load_config(config_path)
        before_snapshot = snapshot_python_files(repo)
        raw_result = SkillLayer().run(repo, task, dry_run=bool(dry_run), config=config)
        after_snapshot = snapshot_python_files(repo)
        diff_text = unified_diff(before_snapshot, after_snapshot)
        result = {
            "success": bool(raw_result["success"]),
            "repo_path": str(repo),
            "task": task,
            "workflow": raw_result["workflow"],
            "macro_sequence": list(raw_result["macro_sequence"]),
            "tool_calls": int(raw_result["tool_calls"]),
            "llm_calls": int(raw_result["llm_calls"]),
            "tests_run": bool(raw_result.get("tests_run", False)),
            "tests_passed": raw_result.get("tests_passed"),
            "validation_status": raw_result.get("validation_status", "not_applicable"),
            "dry_run": bool(raw_result["dry_run"]),
            "logs_path": raw_result.get("logs_path"),
            "diff": diff_text,
        }
        result.update(extract_optional_run_fields(result_source=raw_result))
        record_mcp_telemetry("skilllayer_run", result, started)
        return result
    except Exception as exc:
        result = {
            "success": False,
            "repo_path": str(repo),
            "task": task,
            "workflow": None,
            "macro_sequence": [],
            "tool_calls": 0,
            "llm_calls": 0,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "dry_run": bool(dry_run),
            "logs_path": None,
            "diff": "",
            "error": str(exc),
        }
        record_mcp_telemetry("skilllayer_run", result, started)
        return result


def skilllayer_inspect_repo(repo_path: str) -> dict[str, Any]:
    """Use this tool before large repository tasks to quickly obtain repository
    structure, test information, file counts, and project metadata."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_inspect_repo", repo, started)
        return repo
    try:
        result = with_not_applicable_validation(inspect_repo(repo))
        result["success"] = True
        record_mcp_telemetry("skilllayer_inspect_repo", result, started)
        return result
    except Exception as exc:
        result = with_not_applicable_validation({"success": False, "repo_path": str(repo), "error": str(exc)})
        record_mcp_telemetry("skilllayer_inspect_repo", result, started)
        return result


def skilllayer_inspect_repo_structure(repo_path: str) -> dict[str, Any]:
    """Map the full directory structure of a repository: file counts by extension,
    size per directory, and detected entry points. Never reads file contents.
    Zero LLM calls — purely deterministic. Excludes .venv, __pycache__,
    node_modules, .git, and other ignored paths."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_inspect_repo_structure", repo, started)
        return repo
    try:
        artifacts = build_inspect_repo_structure_artifacts(repo)
        result = {
            "success": True,
            "workflow": "InspectRepoStructureWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "workflow"},
        }
        record_mcp_telemetry("skilllayer_inspect_repo_structure", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_inspect_repo_structure", result, started)
        return result


def skilllayer_inspect_runtime(repo_path: str) -> dict[str, Any]:
    """Inspect the active Python runtime environment: version, interpreter path,
    active venv, platform, installed packages (sorted list of {name, version}),
    and CI-relevant environment variables. ANTHROPIC_API_KEY is reported as a
    boolean presence flag only — its value is never returned. port_conflicts is
    always null (reserved for a future check). Never reads source files.
    Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_inspect_runtime", repo, started)
        return repo
    try:
        artifacts = build_inspect_runtime_artifacts(repo)
        result = {
            "success": True,
            "workflow": "InspectRuntimeEnvironmentWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_inspect_runtime", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_inspect_runtime", result, started)
        return result


def skilllayer_check_port(
    repo_path: str, port: int, host: str = "127.0.0.1", allow_non_loopback: bool = False
) -> dict[str, Any]:
    """Check whether a TCP port is available on a given host.

    Loopback only by default (localhost/127.0.0.1/::1). Checking any other
    host requires allow_non_loopback=True — this tool never silently probes
    an arbitrary remote host; without that opt-in a non-loopback host
    returns error_code 'non_loopback_target_not_allowed'.

    Returns:
    - port (int): the port checked
    - host (str): the host checked (default 127.0.0.1)
    - available (bool): true if nothing is bound to this port
    - process (object | null): if port is taken and psutil is available:
        {pid, name, user}; null otherwise
    - checked_at (str): ISO 8601 timestamp
    - note (str, optional): present when psutil is unavailable

    Never reads source files. Zero LLM calls — purely deterministic.
    Port must be in range 1–65535; out-of-range returns error_code 'invalid_port'.
    Socket errors (bad host, resource exhaustion, ...) return error_code
    'port_check_socket_error' rather than raising."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_check_port", repo, started)
        return repo
    try:
        artifacts = build_check_port_artifacts(port, host, allow_non_loopback=allow_non_loopback)
        # Authoritative: artifacts["success"] is the builder's own truth,
        # equivalent to (and derived alongside) error_code is None.
        result = {
            "success": artifacts.get("success", artifacts.get("error_code") is None),
            "workflow": "CheckPortAvailabilityWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_check_port", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_check_port", result, started)
        return result


def skilllayer_detect_processes(repo_path: str, include_system: bool = False) -> dict[str, Any]:
    """Enumerate running processes and classify well-known dev services.

    Returns:
    - processes: list of {pid, name, status, user, cpu_percent, memory_mb,
        ports (list of bound ports), started_at (ISO 8601 or null)}
    - dev_services: {web_server, database, test_runner, dev_server, docker} — bool flags
        for detected categories of processes
    - total_count / inspected_count: number of processes returned
    - skipped_count: processes that could not be read due to permissions
      (counted, not silently dropped); permission_limited is true if any were
    - complete: false if any process was skipped
    - status: "unsupported" if process enumeration itself failed (e.g. a
      sandboxed environment that blocks it entirely) — never a raw traceback
    - checked_at: ISO 8601 timestamp

    Never exposes environment variables or command line arguments.
    System processes (pid < 100) are excluded unless include_system=True.
    Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_processes", repo, started)
        return repo
    try:
        artifacts = build_detect_processes_artifacts(include_system=include_system)
        # Authoritative: artifacts["success"] is the builder's own truth —
        # false only if process enumeration itself failed, never hardcoded.
        result = {
            "success": artifacts.get("success", True),
            "workflow": "DetectRunningProcessesWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_detect_processes", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_detect_processes", result, started)
        return result


def skilllayer_monitor_flakiness(
    repo_path: str, test_identifier: str, runs: int = 5
) -> dict[str, Any]:
    """Run a test target N times using the repo's test runner and report flakiness metrics.

    Returns:
    - test_identifier: the test path or name that was run
    - runs: how many times it was executed (capped at 20)
    - passed / failed: counts
    - pass_rate: float 0.0–1.0
    - flaky: true if 0 < pass_rate < 1
    - deterministic: true if pass_rate is exactly 0.0 or 1.0
    - failure_messages: deduplicated list of failure reasons seen
    - duration_ms: per-run timing list (milliseconds, integers)
    - median_duration_ms: float
    - checked_at: ISO 8601 timestamp

    Uses the repo's existing test runner (pytest or unittest). Zero LLM calls."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_monitor_flakiness", repo, started)
        return repo
    try:
        artifacts = build_monitor_flakiness_artifacts(repo, test_identifier, runs)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "MonitorTestFlakinessWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_monitor_flakiness", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_monitor_flakiness", result, started)
        return result


def skilllayer_measure_test_speed(repo_path: str, persist_baseline: bool = False) -> dict[str, Any]:
    """Run the full test suite once and return structured speed metrics.

    Returns:
    - total_duration_ms: wall-clock time for the full suite (int)
    - test_count: total tests discovered (int)
    - passed / failed / skipped: counts (int)
    - slowest_tests: up to 5 {name, duration_ms} sorted descending (pytest only)
    - speed_rating: "fast" (<10s), "normal" (10–60s), "slow" (60–300s), "very_slow" (>300s)
    - baseline_delta_ms: difference from saved baseline (positive=slower), null if no baseline
    - baseline_saved_at: ISO 8601 timestamp of prior baseline, null if none
    - checked_at: ISO 8601 timestamp of this run

    Baseline persistence is opt-in: pass persist_baseline=True to update
    .skilllayer/test_speed_baseline.json. The workflow never edits .gitignore.
    Uses pytest --durations=5 when pytest is detected; slowest_tests is empty otherwise.
    Zero LLM calls — purely deterministic execution."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_measure_test_speed", repo, started)
        return repo
    try:
        artifacts = build_measure_test_speed_artifacts(repo, persist_baseline=persist_baseline)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "MeasureTestSuiteSpeedWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_measure_test_speed", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_measure_test_speed", result, started)
        return result


def skilllayer_search(
    repo_path: str,
    query: str,
    mode: str = "text",
    case_sensitive: bool = False,
    file_pattern: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Search a repository for text or regex patterns. Zero LLM calls.

    Parameters:
    - query: the text or regex pattern to search for
    - mode: "text" (literal, default) or "regex"
    - case_sensitive: False by default
    - file_pattern: optional glob to filter files (e.g. "*.py", "*.md")
    - limit: max matches returned (default 100)

    Returns:
    - matches: list of {file, line, column, preview, match_start, match_end}
    - total_matches: total count (may exceed limit when truncated: true)
    - files_with_matches: unique files with at least one match
    - scanned_files / skipped_files: coverage counts
    - truncated: true when results exceed limit
    - checked_at: ISO 8601 timestamp

    Skips binary files, files over 1MB, .venv, __pycache__, node_modules, .git,
    and paths matching .gitignore patterns. Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_search", repo, started)
        return repo
    try:
        artifacts = build_search_artifacts(
            repo,
            query,
            mode=mode,
            case_sensitive=case_sensitive,
            file_pattern=file_pattern,
            limit=limit,
        )
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "SearchWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_search", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_search", result, started)
        return result


def skilllayer_watch_deps(repo_path: str) -> dict[str, Any]:
    """Check pinned dependencies against their latest published versions.

    Parses requirements.txt, pyproject.toml, and package.json from the repo root.
    Queries PyPI (https://pypi.org/pypi/{pkg}/json) for Python deps and the npm
    registry (https://registry.npmjs.org/{pkg}/latest) for JS deps (5s timeout each,
    20s total budget across all dependencies combined — a large dependency set
    under a slow/unreachable registry stops early rather than running for minutes).

    Returns:
    - dependencies: [{name, current_version, latest_version, status, error_code,
        error, outdated, major_bump, minor_bump, patch_bump, source}] — status is
        one of up_to_date/outdated/unknown/unsupported; a network failure, timeout,
        rate limit, malformed response, or unresolved package is always
        status="unknown" or "unsupported", never outdated=false/up_to_date
    - checked_count / outdated_count / up_to_date_count / unknown_count / unsupported_count
    - complete: false if the 20s aggregate budget was reached before every
      dependency was checked — remaining ones are reported as explicit unknowns
    - package_manager: "pip" | "npm" | "both" | "none"
    - checked_at: ISO 8601 timestamp

    Only pinned versions checked; unpinned/VCS/workspace deps silently skipped.
    Zero LLM calls — purely deterministic HTTP, no repository writes."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_watch_deps", repo, started)
        return repo
    try:
        artifacts = build_watch_deps_artifacts(repo)
        result = {
            "success": True,
            "workflow": "WatchDependencyUpdatesWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_watch_deps", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_watch_deps", result, started)
        return result


def skilllayer_detect_activity(repo_path: str, persist_snapshot: bool = False) -> dict[str, Any]:
    """Detect repo changes since the last saved snapshot.

    Compares current git state against a snapshot saved to
    .skilllayer/repo_activity_snapshot.json and returns:
    - since: ISO 8601 timestamp of last snapshot (null on first run)
    - commits_since: [{hash, message, author, timestamp}] — author name only, no email
    - files_changed: [{path, status, insertions, deletions}]
    - branches_changed: [{name, status}]
    - summary: {commit_count, files_changed_count, insertions_total, deletions_total, active_since}
    - first_run: true on the initial call
    - checked_at: current ISO 8601 timestamp

    Snapshot persistence is opt-in: pass persist_snapshot=True to update it.
    The workflow never edits .gitignore. Returns an explicit error if called
    outside a git repository. Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_activity", repo, started)
        return repo
    try:
        artifacts = build_detect_activity_artifacts(repo, persist_snapshot=persist_snapshot)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "DetectRepoActivityWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_detect_activity", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_detect_activity", result, started)
        return result


def skilllayer_watch_file_changes(
    repo_path: str,
    scope: str | None = None,
    persist_snapshot: bool = False,
) -> dict[str, Any]:
    """Detect files added/modified/deleted on disk since the last watch.

    Snapshot-and-diff, not real-time watching: hashes (sha256) every in-scope
    file, compares against the previous snapshot at
    .skilllayer/file_watch_snapshot.json, saves a new snapshot for next time.
    Unlike skilllayer_detect_activity (git commit-to-commit diff only), this
    reads the filesystem directly, so it also catches untracked files and
    uncommitted working-tree edits.

    Returns:
    - first_watch: true on the initial call (diff lists empty — no prior baseline yet)
    - scope: the directory scanned (default ".", the whole repo)
    - added: [{path, size}]
    - modified: [{path, size_before, size_after, size_delta, mtime}]
    - deleted: [{path}]
    - summary: {added_count, modified_count, deleted_count, scanned_file_count}
    - checked_at: current ISO 8601 timestamp

    Scope defaults to the whole repo; pass a relative directory to narrow it.
    Files are filtered by should_ignore_search_path plus the repo's own
    .gitignore, and files over 1MB are skipped. Never reads or returns file
    content — only hashes, sizes, and mtimes. Pass persist_snapshot=True to
    update the snapshot; the workflow never edits .gitignore. Zero LLM calls — purely
    deterministic filesystem scan."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_watch_file_changes", repo, started)
        return repo
    try:
        artifacts = build_watch_file_changes_artifacts(
            repo,
            scope=scope,
            persist_snapshot=persist_snapshot,
        )
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "WatchFileChangesWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_watch_file_changes", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_watch_file_changes", result, started)
        return result


def skilllayer_compare_context_snapshots(
    repo_path: str,
    from_index: int | None = None,
    to_index: int | None = None,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> dict[str, Any]:
    """Diff two saved context snapshots to show how understanding evolved between sessions.

    Reads context/latest.md and context/history/*.md (written by
    skilllayer_save_context) — never writes anything.

    Select the pair either by index (0 = latest, 1 = one snapshot back, ...)
    or by exact history filename timestamp. If none are given, defaults to
    latest vs. the single most recent history file.

    Returns:
    - from_snapshot / to_snapshot: {id, created, source}
    - state_diff: {changed, unified_diff, word_count_before, word_count_after}
      — a textual line diff, not a semantic one
    - open_questions_diff: {resolved, new, still_open, resolved_count, new_count,
      still_open_count} — exact string match only; a question that was reworded
      rather than resolved shows up as one resolved + one new, not recognized
      as the same question changed

    Explicit error if fewer than two snapshots exist. Zero LLM calls — purely
    deterministic text comparison."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_compare_context_snapshots", repo, started)
        return repo
    try:
        artifacts = build_compare_context_snapshots_artifacts(
            repo,
            from_index=from_index,
            to_index=to_index,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
        )
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(), never hardcoded/re-derived here.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "CompareContextSnapshotsWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_compare_context_snapshots", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_compare_context_snapshots", result, started)
        return result


def skilllayer_search_decisions(
    repo_path: str,
    query: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Search TrackDecisionLog's decisions/*.md by keyword.

    Case-insensitive substring match (not regex) across title/context/
    decision/reasoning/consequences by default; narrow with an optional
    fields list (any of: title, context, decision, reasoning, consequences).

    Ranking: title match ranks above body-only match, then more matched
    fields ranks higher within that tier, then newest created (decision_id
    breaks ties, since created has only second-level resolution) as the
    final tiebreak.

    Returns:
    - results: [{decision_id, title, status, superseded_by,
      current_decision_id, matched_fields, matched_snippets, created, file}]
      — current_decision_id is resolved by walking the full superseded_by
      chain (not just one hop), so a superseded result is never mistaken for
      the current one
    - total_matches, superseded_count
    - fields_searched: the field list actually used

    Read-only — never writes. Zero LLM calls — purely deterministic string
    comparison."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_search_decisions", repo, started)
        return repo
    try:
        artifacts = build_search_decisions_artifacts(repo, query, fields=fields)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(), never hardcoded/re-derived here.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "SearchDecisionLogWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_search_decisions", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_search_decisions", result, started)
        return result


def skilllayer_add_todo(repo_path: str, text: str) -> dict[str, Any]:
    """Add a discrete, actionable todo to .skilllayer/todos.json.

    Distinct from skilllayer_save_context's open_questions: a todo has a
    stable id (claimed from the same atomic counter TrackDecisionLog uses for
    decision_id) and an independent status, tracked and mutated one item at a
    time rather than rewritten wholesale as part of a narrative snapshot.

    Returns todo_id, text, status ("open"), created (ISO 8601 timestamp).
    Rejects empty text (error_code empty_todo_text) or text over 500
    characters (error_code todo_text_too_long) — keep it a short imperative
    phrase, not a paragraph.

    Regenerates INDEX.md after writing, so the new todo is immediately
    visible there and via skilllayer_rehydrate_context. Zero LLM calls —
    purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_add_todo", repo, started)
        return repo
    try:
        artifacts = build_add_todo_artifacts(repo, text)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(). NOTE: artifacts["status"] here is the
        # ADDED TODO's own state ("open"), not a health-status word — AddTodo
        # is one of the two workflows where those concepts collide; see
        # finalize_memory_result's include_status_field docstring.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "AddTodoWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_add_todo", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_add_todo", result, started)
        return result


def skilllayer_mark_todo_done(repo_path: str, todo_id: str, status: str = "done") -> dict[str, Any]:
    """Set a todo's status by id.

    Defaults to marking it done (stamps done_at); pass status="open" to
    reopen a previously-done todo (clears done_at) rather than needing a
    separate reopen tool.

    Returns error_code memory_record_not_found if the id does not exist, or
    invalid_status if status is not "open"/"done". Regenerates INDEX.md
    after writing. Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_mark_todo_done", repo, started)
        return repo
    try:
        artifacts = build_mark_todo_done_artifacts(repo, todo_id, status=status)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(). NOTE: artifacts["status"] here is the
        # TOUCHED TODO's own state ("open"/"done"), not a health-status word;
        # see finalize_memory_result's include_status_field docstring.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "MarkTodoDoneWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_mark_todo_done", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_mark_todo_done", result, started)
        return result


def skilllayer_list_todos(repo_path: str, status_filter: str = "open") -> dict[str, Any]:
    """List todos from .skilllayer/todos.json.

    status_filter: "open" (default — the far more common query), "done", or
    "all". Returns todos (the filtered list) plus open_count/done_count/
    total_count across the whole store regardless of the filter applied.

    Read-only — never writes. Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_list_todos", repo, started)
        return repo
    try:
        artifacts = build_list_todos_artifacts(repo, status_filter=status_filter)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(), never hardcoded/re-derived here.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "ListTodosWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_list_todos", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_list_todos", result, started)
        return result


def skilllayer_validate_memory(repo_path: str) -> dict[str, Any]:
    """Check the .skilllayer/ memory store for internal consistency.

    Runs 20 deterministic integrity checks across four groups: INDEX.md
    freshness (vs. a freshly regenerated index), decision supersession chain
    integrity (dangling supersedes/superseded_by, status mismatches,
    cycles, duplicate ids), frontmatter/schema validity per file type
    (context/latest.md, decisions/*.md, preferences.md, todos.json), and
    .state.json id-counter drift (next_decision_id/next_todo_id behind the
    highest existing id, which would mint a duplicate on the next write).

    Returns store_exists (false if .skilllayer/ doesn't exist yet — zero
    checks run, never an error), checked_at, summary
    {checks_run, passed, warnings, broken}, and a flat findings[] list with
    check/severity ("broken" or "warning")/file/message/suggested_fix per
    entry.

    Standalone, on-demand diagnostic — NOT run automatically on every memory
    write (unlike NeedsWrap): it fully parses every decision file and walks
    the supersession graph, an O(N) cost not appropriate for every write.
    Read-only — never writes .state.json, INDEX.md, or any memory file.
    Suggested fixes are text only; this workflow never repairs anything.
    Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_validate_memory", repo, started)
        return repo
    try:
        artifacts = build_validate_memory_artifacts(repo)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result() from the store's actual integrity status
        # (broken/warning/healthy) — never hardcoded true regardless of a
        # broken store.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "ValidateMemoryWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "workflow"},
        }
        record_mcp_telemetry("skilllayer_validate_memory", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_validate_memory", result, started)
        return result


def skilllayer_profile_execution(
    repo_path: str,
    target: str,
    include_stdlib: bool = False,
) -> dict[str, Any]:
    """DISABLED over MCP pending an execution-safety fix — see MCP_DISABLED_WORKFLOWS.

    ProfileCodeExecutionWorkflow's target path is not confined to the
    authorized repository (an absolute path outside the repo is accepted
    as-is), and its probe subprocess suppresses target failures, so a failing
    or malicious target could be reported as a successful profiling run. This
    tool is not registered with the MCP server; this function refuses
    immediately, before reading the repo or touching the target file, and
    never calls build_profile_execution_artifacts. Kept in source (unregistered)
    only so the underlying implementation remains available for CLI/internal
    use behind the existing --allow-internal override, and for later
    remediation of the execution-safety gap."""

    started = time.perf_counter()
    result = {
        "success": False,
        "workflow": "ProfileCodeExecutionWorkflow",
        "llm_calls": 0,
        "error_code": "workflow_disabled_over_mcp",
        "error": MCP_DISABLED_WORKFLOWS["ProfileCodeExecutionWorkflow"],
    }
    record_mcp_telemetry("skilllayer_profile_execution", result, started)
    return result


def skilllayer_measure_memory(repo_path: str, target: str) -> dict[str, Any]:
    """DISABLED over MCP pending an execution-safety fix — see MCP_DISABLED_WORKFLOWS.

    MeasureMemoryUsageWorkflow's target path is not confined to the authorized
    repository (an absolute path outside the repo is accepted as-is), and its
    probe subprocess suppresses target failures, so a failing or malicious
    target could be reported as a successful measurement. This tool is not
    registered with the MCP server; this function refuses immediately, before
    reading the repo or touching the target file, and never calls
    build_measure_memory_artifacts. Kept in source (unregistered) only so the
    underlying implementation remains available for CLI/internal use behind
    the existing --allow-internal override, and for later remediation of the
    execution-safety gap."""

    started = time.perf_counter()
    result = {
        "success": False,
        "workflow": "MeasureMemoryUsageWorkflow",
        "llm_calls": 0,
        "error_code": "workflow_disabled_over_mcp",
        "error": MCP_DISABLED_WORKFLOWS["MeasureMemoryUsageWorkflow"],
    }
    record_mcp_telemetry("skilllayer_measure_memory", result, started)
    return result


def skilllayer_detect_secrets(repo_path: str) -> dict[str, Any]:
    """Scan a repository for accidentally committed secrets using regex patterns.
    Advisory, not a security certification.

    Returns:
    - findings: list of {file, line, pattern_name, severity, match_preview, likely_test_fixture}
    - scanned_files / scanned_bytes: files and bytes actually inspected
    - skipped_files / skipped_bytes / skipped_reasons: files never inspected,
      broken down by reason (excluded_dir, gitignored, oversized, binary,
      permission_denied, unreadable)
    - scan_complete: true only if nothing was skipped for a reason that could
      hide a real secret (excluded_dir alone — .git/node_modules/.venv/etc. —
      does not count against this)
    - findings_count: number of findings
    - clean: true ONLY when there are no findings AND scan_complete is true —
      never true for a partial/incomplete scan, even with zero findings so far
    - status: "findings" | "clear_for_supported_patterns" | "incomplete" | "error"
    - checked_at: ISO 8601 timestamp

    Detected patterns (CRITICAL: Anthropic/OpenAI/AWS keys, private key blocks,
    GitHub tokens; HIGH: generic API key/token/password assignments, bearer tokens;
    MEDIUM: non-RFC-1918 IP addresses, database connection strings).

    match_preview is always the first 6 chars of the match + "..." — never the full value.
    Findings in tests/test_* files are flagged with likely_test_fixture: true.
    Zero LLM calls — purely deterministic regex scanning."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_secrets", repo, started)
        return repo
    try:
        artifacts = build_detect_secrets_artifacts(repo)
        # Authoritative: artifacts["success"] is the builder's own truth —
        # false only on a genuine scan execution failure, never hardcoded.
        result = {
            "success": artifacts.get("success", False),
            "workflow": "DetectSecretPatternsWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "success", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_detect_secrets", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_detect_secrets", result, started)
        return result


def skilllayer_safe_change(
    repo_path: str,
    task: str,
    phase: str = "plan",
    test_command: list[str] | None = None,
    save_context: bool = False,
) -> dict[str, Any]:
    """Plan and validate a code change safely — SkillLayer never edits files itself.

    Two-phase professional skill:
    - phase="plan" (default): inspects the repository and git status, searches
      for files/symbols relevant to the task description, and returns a
      bounded change plan plus whether the repository already has
      uncommitted work (verdict: READY_TO_IMPLEMENT, or
      BLOCKED_BY_REPOSITORY_STATE if it isn't a git repository).
    - phase="validate": call again after the host AI coding agent has made the
      actual edit. Inspects the resulting git diff, runs the repository's
      detected test command (or an explicit test_command list), and returns
      changed files, test results, risks, and a bounded verdict
      (CHANGE_VALIDATED / CHANGE_VALIDATED_WITH_WARNINGS / CHANGE_INCOMPLETE /
      VALIDATION_FAILED). Never reports a validated verdict when no changed
      files were found or when validation did not actually run.

    executed_by is always "host_agent": SkillLayer inspects, plans, and
    validates; the calling AI agent performs the actual code edit. Pass
    save_context=True on the validate call to persist a summary snapshot
    under .skilllayer/ (written_paths discloses exactly what was written).
    Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_safe_change", repo, started)
        return repo
    try:
        result = build_safe_change_artifacts(
            repo, task, phase=phase, test_command=test_command, save_context=save_context,
        )
        result.setdefault("workflow", "SafeCodeChangeWorkflow")
        record_mcp_telemetry("skilllayer_safe_change", result, started)
        return result
    except Exception as exc:
        result = {
            "skill": "safe_code_change", "success": False, "repo_path": str(repo),
            "error": str(exc), "verdict": "UNSUPPORTED",
        }
        record_mcp_telemetry("skilllayer_safe_change", result, started)
        return result


def skilllayer_release_readiness(repo_path: str, deep: bool = False) -> dict[str, Any]:
    """Assess whether a repository is ready for careful external release.

    Aggregates repository inspection, git status, an honest secret scan,
    dependency inspection (unrecognized manifests are reported, never assumed
    clean), memory-store integrity, and packaging metadata into one bounded
    verdict. Bounded by default: detects the test command without running it.
    Pass deep=True to actually execute the repository's own test suite.

    Never certifies a repository as secure and never treats an incomplete or
    skipped check as clean — any such gap is listed under checks_incomplete
    and reduces the verdict (to INCOMPLETE_ASSESSMENT or
    READY_WITH_KNOWN_LIMITATIONS) rather than becoming a false READY.
    Read-only except when deep=True, which executes the target repository's
    own tests. Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_release_readiness", repo, started)
        return repo
    try:
        result = build_release_readiness_artifacts(repo, deep=deep)
        result.setdefault("workflow", "ReleaseReadinessWorkflow")
        record_mcp_telemetry("skilllayer_release_readiness", result, started)
        return result
    except Exception as exc:
        result = {
            "skill": "release_readiness", "success": False, "repo_path": str(repo),
            "error": str(exc), "verdict": "INCOMPLETE_ASSESSMENT",
        }
        record_mcp_telemetry("skilllayer_release_readiness", result, started)
        return result


def skilllayer_resume_work(
    repo_path: str,
    confirm_update: bool = False,
    new_state: str | None = None,
    new_open_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Reconstruct project state for a brand-new session from saved memory alone.

    Rehydrates the saved .skilllayer/ context, validates memory integrity,
    inspects current git status, and compares against the last saved activity
    snapshot to report completed work, constraints, the remembered next
    action, and any drift detected since that snapshot. Reports uncertainty
    honestly (e.g. no baseline snapshot yet, or unsaved-work status unknown)
    rather than guessing.

    Always read-only unless confirm_update=True is passed together with
    new_state — memory is never overwritten implicitly. Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_resume_work", repo, started)
        return repo
    try:
        result = build_resume_work_artifacts(
            repo, confirm_update=confirm_update, new_state=new_state,
            new_open_questions=new_open_questions,
        )
        result.setdefault("workflow", "ResumeProjectWorkWorkflow")
        record_mcp_telemetry("skilllayer_resume_work", result, started)
        return result
    except Exception as exc:
        result = {
            "skill": "resume_project_work", "success": False, "repo_path": str(repo),
            "error": str(exc), "verdict": "UNSUPPORTED",
        }
        record_mcp_telemetry("skilllayer_resume_work", result, started)
        return result


def skilllayer_list_branches(repo_path: str) -> dict[str, Any]:
    """List all local and remote git branches with metadata.

    Returns:
    - branches: list of {name, current, remote, last_commit_hash, last_commit_message,
      last_commit_date, ahead (int or null), behind (int or null)}
    - current_branch: name of the currently checked-out branch
    - total_count: total number of branches
    - checked_at: ISO 8601 timestamp

    ahead/behind are relative to the default branch (main or master) for local
    branches. Author email is never exposed. Explicit error if not a git repository.
    Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_list_branches", repo, started)
        return repo
    try:
        artifacts = build_list_branches_artifacts(repo)
        result = {"success": True, "workflow": "ListBranchesWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_list_branches", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_list_branches", result, started)
        return result


def skilllayer_git_log(
    repo_path: str,
    limit: int = 20,
    author: str | None = None,
    since: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Read git commit log with per-commit stats.

    Parameters:
    - limit: max commits to return (default 20, max 100)
    - author: filter by author name (optional)
    - since: ISO 8601 date filter — only commits after this date (optional)
    - path: only show commits touching this file/directory (optional)

    Returns:
    - commits: list of {hash, full_hash, message, author_name, date,
      files_changed, insertions, deletions}
    - total_shown: number of commits returned
    - filters_applied: active filters
    - checked_at: ISO 8601 timestamp

    Author email is never exposed. Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_git_log", repo, started)
        return repo
    try:
        artifacts = build_git_log_artifacts(repo, limit=limit, author=author, since=since, path=path)
        result = {"success": True, "workflow": "GitLogWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_git_log", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_git_log", result, started)
        return result


def skilllayer_get_commit(repo_path: str, commit_hash: str) -> dict[str, Any]:
    """Get full details for a specific git commit.

    Parameters:
    - commit_hash: full or short (≥7 char) commit hash (required)

    Returns:
    - hash: full 40-char hash
    - short_hash: 7-char hash
    - message: full commit message (all lines)
    - author_name: author name (never email)
    - date: ISO 8601
    - files_changed: list of {path, status, insertions, deletions}
    - total_insertions, total_deletions: aggregate stats
    - parent_hashes: list of short parent hashes
    - checked_at: ISO 8601 timestamp

    Explicit error if commit hash not found. Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_get_commit", repo, started)
        return repo
    try:
        artifacts = build_get_commit_artifacts(repo, commit_hash)
        result = {"success": artifacts.get("error_code") is None, "workflow": "GetCommitDetailsWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_get_commit", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_get_commit", result, started)
        return result


def skilllayer_git_diff(
    repo_path: str,
    base: str = "HEAD",
    target: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Show diff between two git refs or between a ref and the working tree.

    Parameters:
    - base: base ref (default "HEAD")
    - target: target ref (optional — omit to diff base against working tree)
    - path: optional file/directory filter

    Returns:
    - files: list of {path, status, insertions, deletions, diff_preview}
      diff_preview is capped at 500 chars per file
    - total_files, total_insertions, total_deletions: aggregate stats
    - truncated: true if more than 20 files (previews only for first 20)
    - checked_at: ISO 8601 timestamp

    Explicit errors for unresolvable refs. Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_git_diff", repo, started)
        return repo
    try:
        artifacts = build_git_diff_artifacts(repo, base=base, target=target, path=path)
        result = {"success": artifacts.get("error_code") is None, "workflow": "GitDiffWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_git_diff", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_git_diff", result, started)
        return result


def skilllayer_file_history(
    repo_path: str,
    file_path: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Get commit history for a specific file (git log -- file).

    Parameters:
    - file_path: path to the file relative to repo root (required)
    - limit: max commits to return (default 10, max 50)

    Returns:
    - file: the queried path
    - commits: list of {hash, message, author_name, date, insertions, deletions}
    - total_shown: number of commits returned
    - first_commit_date: date of the earliest commit touching this file
    - last_modified_date: date of the most recent commit
    - checked_at: ISO 8601 timestamp

    Author email is never exposed. Explicit error if file not found in git history.
    Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_file_history", repo, started)
        return repo
    try:
        artifacts = build_file_history_artifacts(repo, file_path=file_path, limit=limit)
        result = {"success": artifacts.get("error_code") is None, "workflow": "GetFileHistoryWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_file_history", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_file_history", result, started)
        return result


def skilllayer_git_blame(
    repo_path: str,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Run git blame on a file to see who wrote each line.

    Parameters:
    - file_path: path to the file relative to repo root (required)
    - start_line: first line number (optional, 1-based)
    - end_line: last line number (optional, requires start_line)

    Returns:
    - file: the queried path
    - lines: list of {line_number, content, commit_hash, author_name, date, summary}
    - total_lines: number of lines returned (capped at 200)
    - unique_authors: sorted list of distinct author names
    - date_range: {earliest, latest} ISO 8601 dates
    - truncated: true if output was capped at 200 lines
    - checked_at: ISO 8601 timestamp

    Author email is NEVER included in any field. Explicit error if file not found.
    Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_git_blame", repo, started)
        return repo
    try:
        artifacts = build_git_blame_artifacts(repo, file_path=file_path, start_line=start_line, end_line=end_line)
        result = {"success": artifacts.get("error_code") is None, "workflow": "GitBlameWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_git_blame", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_git_blame", result, started)
        return result


def skilllayer_find_conflicts(repo_path: str) -> dict[str, Any]:
    """Scan the repository for merge conflict markers (<<<<<<< HEAD).

    Returns:
    - conflicts: list of {file, conflict_count, sections[{start_line, separator_line, end_line}]}
    - total_files_with_conflicts: number of files with conflicts
    - total_conflict_sections: total count of conflict sections
    - clean: true if no conflicts found
    - checked_at: ISO 8601 timestamp

    Pure file scan — no git commands needed. Uses standard search-ignore rules to
    skip binary files, build artifacts, and node_modules. Zero LLM calls — purely
    deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_find_conflicts", repo, started)
        return repo
    try:
        artifacts = build_find_conflicts_artifacts(repo)
        result = {"success": True, "workflow": "FindMergeConflictsWorkflow", "llm_calls": 0, **{k: v for k, v in artifacts.items() if k != "workflow"}}
        record_mcp_telemetry("skilllayer_find_conflicts", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_find_conflicts", result, started)
        return result


def skilllayer_report_session_usage(
    scope: str = "current",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    projects_dir: str | None = None,
    cwd: str | None = None,
    redact_paths: bool = False,
) -> dict[str, Any]:
    """Report real, measured token usage from Claude Code's own local session logs.

    Reads only ~/.claude/projects/<slug>/*.jsonl (read-only, never modified).

    Parameters:
    - scope: "current" (default, the project matching cwd) or "all" (every project)
    - project: a specific project path-or-slug (overrides scope)
    - since / until: inclusive YYYY-MM-DD date filters
    - projects_dir: override the Claude Code projects directory (default ~/.claude/projects)
    - cwd: working directory used to derive the current-project slug (default: process cwd)
    - redact_paths: reduce project identifiers to basenames for shareable reports

    Returns totals, by_model, by_session, by_tool (skilllayer_tools vs other_tools),
    a dedicated skilllayer_tool_usage block, and mandatory methodology / privacy /
    parse_health blocks. Usage is measured per assistant MESSAGE, not per tool call;
    prompt/response text is never read. Estimated cost uses a SkillLayer-maintained
    rate table; unknown models yield null cost, never a guess. Never crashes on a
    missing directory or malformed data. Zero LLM calls, zero network — deterministic."""

    started = time.perf_counter()
    try:
        artifacts = build_session_usage_artifacts(
            cwd=cwd,
            scope=scope,
            project=project,
            since=since,
            until=until,
            projects_dir=projects_dir,
            redact_paths=redact_paths,
        )
        result = {
            "success": True,
            "workflow": "ReportRealSessionUsageWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "workflow"},
        }
        record_mcp_telemetry("skilllayer_report_session_usage", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        record_mcp_telemetry("skilllayer_report_session_usage", result, started)
        return result


def skilllayer_detect_dead_code(repo_path: str) -> dict[str, Any]:
    """Scan Python files in a repository for functions and classes that are defined
    but never called or imported anywhere in the codebase. Returns findings with
    file path, name, line number, kind (function/class), and confidence level:

    - certain: private names (_foo) with zero references anywhere — reliably unused.
    - possible: public names with no detected references — may be external API.

    Test files are excluded from the unused-symbol check (test helpers are not dead
    code). References inside test files DO count as references, preventing false
    positives for symbols that are only exercised by tests.

    Never modifies any file. Zero LLM calls — purely deterministic static analysis."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_dead_code", repo, started)
        return repo
    try:
        artifacts = build_detect_dead_code_artifacts(repo)
        result = {
            "success": True,
            "workflow": "DetectDeadCodeWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_detect_dead_code", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_detect_dead_code", result, started)
        return result


def skilllayer_map_dependencies(repo_path: str) -> dict[str, Any]:
    """Parse all dependency files in a repository (requirements.txt, pyproject.toml,
    package.json, Pipfile) and return every declared dependency with its name,
    version spec, type (direct/dev/optional/peer), and whether it is pinned (==)
    or unpinned (no version constraint). Never modifies any file. Zero LLM calls."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_map_dependencies", repo, started)
        return repo
    try:
        artifacts = build_map_dependencies_artifacts(repo)
        result = {
            "success": True,
            "workflow": "MapDependenciesWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_map_dependencies", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_map_dependencies", result, started)
        return result


def skilllayer_save_context(
    repo_path: str,
    state: str,
    open_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Save a context snapshot to .skilllayer/context/latest.md.

    Records the current repo state and open questions. Previous snapshot is
    rotated to history (last 5 kept). Regenerates INDEX.md digest.
    Never reads source files. Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_save_context", repo, started)
        return repo
    try:
        artifacts = build_save_context_artifacts(repo, state, list(open_questions or []))
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result() (e.g. false on a memory_lock_timeout),
        # never hardcoded true. Filtered out of the spread so this explicit
        # key is unambiguously the one in effect, not an accident of dict
        # key-ordering.
        result = {
            "success": artifacts.get("success", False),
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "success"},
        }
        record_mcp_telemetry("skilllayer_save_context", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_save_context", result, started)
        return result


def skilllayer_track_decision(
    repo_path: str,
    title: str,
    context: str,
    decision: str,
    reasoning: str,
    consequences: str,
    status: str = "proposed",
    related_files: list[str] | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Append an immutable ADR (architectural decision record) to .skilllayer/decisions/.

    Decisions are sequence-numbered and never edited after accepted. To change
    a decision, record a new one with supersedes=<old_id> — the old file's
    status is flipped to superseded automatically. Regenerates INDEX.md.
    Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_track_decision", repo, started)
        return repo
    try:
        artifacts = build_track_decision_artifacts(
            repo, title=title, context=context, decision=decision,
            reasoning=reasoning, consequences=consequences, status=status,
            related_files=related_files, supersedes=supersedes,
        )
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result() — false for memory_state_corrupt or
        # memory_lock_timeout, never hardcoded true.
        result = {
            "success": artifacts.get("success", False),
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "success"},
        }
        record_mcp_telemetry("skilllayer_track_decision", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_track_decision", result, started)
        return result


def skilllayer_remember_preferences(
    repo_path: str,
    preferences: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Upsert per-repo coding preferences into .skilllayer/preferences.md.

    Preferences are grouped by domain (e.g. "testing", "style", "commits").
    Last-writer-wins per key within each domain. Regenerates INDEX.md.
    Zero LLM calls.

    Example preferences:
      {"testing": {"runner": "pytest"}, "style": {"type_hints": "required"}}"""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_remember_preferences", repo, started)
        return repo
    try:
        artifacts = build_remember_preferences_artifacts(repo, preferences)
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result() (e.g. false on a memory_lock_timeout),
        # never hardcoded true. Filtered out of the spread so this explicit
        # key is unambiguously the one in effect, not an accident of dict
        # key-ordering.
        result = {
            "success": artifacts.get("success", False),
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "success"},
        }
        record_mcp_telemetry("skilllayer_remember_preferences", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_remember_preferences", result, started)
        return result


def skilllayer_rehydrate_context(
    repo_path: str,
    full_context: bool = False,
    decision_id: str | None = None,
    full_preferences: bool = False,
) -> dict[str, Any]:
    """Read the .skilllayer/ memory store. Returns INDEX.md digest by default.

    The digest is token-minimal: context state + open questions, last 3 decision
    titles, and first preference per domain. Use flags for detail:
    - full_context: include complete context/latest.md
    - decision_id: include full ADR text for a specific decision (e.g. "0001")
    - full_preferences: include complete preferences.md
    Never modifies files. Zero LLM calls."""
    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_rehydrate_context", repo, started)
        return repo
    try:
        artifacts = build_rehydrate_context_artifacts(
            repo, full_context=full_context, decision_id=decision_id,
            full_preferences=full_preferences,
        )
        # Authoritative: artifacts["success"] is computed by
        # finalize_memory_result(). A repo with no .skilllayer/ store yet is
        # status="warning"/success=true (a valid empty result), never a
        # false-negative "error".
        result = {
            "success": artifacts.get("success", False),
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k != "success"},
        }
        record_mcp_telemetry("skilllayer_rehydrate_context", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_rehydrate_context", result, started)
        return result


def skilllayer_list_workflows() -> dict[str, Any]:
    started = time.perf_counter()
    result = {
        "success": True,
        "workflows": [
            {
                "name": name,
                **WORKFLOW_METADATA.get(name, {}),
                "macro_sequence": list(macros),
                "mcp_enabled": name not in MCP_DISABLED_WORKFLOWS,
                "mcp_disabled_reason": MCP_DISABLED_WORKFLOWS.get(name),
            }
            for name, macros in WORKFLOWS.items()
        ],
        "commands": [{"name": name, **metadata} for name, metadata in COMMAND_METADATA.items()],
    }
    record_mcp_telemetry("skilllayer_list_workflows", result, started)
    return result


def skilllayer_list_skills() -> dict[str, Any]:
    started = time.perf_counter()
    primitive_tools = sorted({tool for tools in MACROS.values() for tool in tools})
    result = {
        "success": True,
        "macros": [{"name": name, "tools": list(tools)} for name, tools in MACROS.items()],
        "primitive_tools": primitive_tools,
    }
    record_mcp_telemetry("skilllayer_list_skills", result, started)
    return result


def skilllayer_doctor(repo_path: str | None = None, config_path: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    pytest_available = is_pytest_available()
    config_ok = True
    config_error = None
    try:
        config = load_config(config_path)
    except Exception as exc:
        config = {}
        config_ok = False
        config_error = str(exc)

    backend = config.get("router", {}).get("backend")
    configured_log_dir = config.get("logging", {}).get("log_dir")

    # Read-only memory integrity peek: never creates .skilllayer/, never
    # acquires the lock (validate_memory_store is documented never to write or
    # raise), and never counted toward overall doctor success/ok — memory
    # health is explicitly optional here, not silently folded into readiness.
    if repo_path is None:
        memory_store_check: dict[str, Any] = {
            "value": "not_applicable",
            "ok": True,
            "required": False,
            "warning": None,
        }
    else:
        memory_artifacts = build_validate_memory_artifacts(Path(repo_path).expanduser())
        memory_status = memory_artifacts.get("status")
        memory_store_check = {
            "value": memory_status,
            "ok": memory_status != "broken",
            "required": False,
            "store_exists": memory_artifacts.get("store_exists"),
            "warning": None
            if memory_status in ("healthy", None)
            else (
                f"Memory store status={memory_status}: "
                + ("; ".join(memory_artifacts.get("warnings") or []) or "see skilllayer_validate_memory for findings.")
            ),
        }
    required_checks: dict[str, Any] = {
        "python_version": {
            "value": ".".join(str(part) for part in sys.version_info[:3]),
            "ok": sys.version_info >= (3, 10),
            "required": True,
        },
        "repo_path": {
            "value": repo_path,
            "ok": True if repo_path is None else Path(repo_path).expanduser().exists(),
            "required": True,
        },
        "config_valid": {
            "value": config_path or "default",
            "ok": config_ok,
            "error": config_error,
            "required": True,
        },
        "ollama_available": {
            "value": shutil.which("ollama") is not None,
            "ok": True if backend != "ollama" else shutil.which("ollama") is not None,
            "required": backend == "ollama",
        },
    }
    if configured_log_dir:
        log_dir = Path(configured_log_dir)
        required_checks["log_dir_writable"] = {
            "value": str(log_dir),
            "ok": is_writable_dir(log_dir),
            "required": True,
            "method": "read_only_permission_check",
        }
    optional_checks: dict[str, Any] = {
        "pytest_available": {
            "value": pytest_available,
            "ok": pytest_available,
            "required": False,
            "warning": None
            if pytest_available
            else "pytest is optional unless running pytest-based smoke tests or pytest-only projects.",
        },
        "mcp_sdk_available": {
            "value": FastMCP is not None,
            "ok": FastMCP is not None,
            "required": False,
            "install_hint": None if FastMCP is not None else MCP_INSTALL_MESSAGE,
            "warning": None if FastMCP is not None else "MCP SDK is optional for CLI use. Install with python -m pip install \".[mcp]\" --no-build-isolation for MCP clients.",
        },
        "memory_lock_cross_process_supported": {
            "value": MEMORY_LOCK_CROSS_PROCESS_SUPPORTED,
            "ok": MEMORY_LOCK_CROSS_PROCESS_SUPPORTED,
            "required": False,
            "warning": None if MEMORY_LOCK_CROSS_PROCESS_SUPPORTED else (
                "This platform has no fcntl, so .skilllayer memory writes are NOT "
                "protected against races between separate processes (e.g. two "
                "Claude Code sessions writing at once). Same-process/same-thread "
                "writes are still serialized. Cross-process protection requires a "
                "POSIX platform (Linux/macOS)."
            ),
        },
        "memory_store_status": memory_store_check,
    }
    checks = {**required_checks, **optional_checks}
    warnings = [check["warning"] for check in optional_checks.values() if check.get("warning")]
    result = with_not_applicable_validation(
        {
            "success": all(bool(check["ok"]) for check in required_checks.values()),
            "ok": all(bool(check["ok"]) for check in required_checks.values()),
            "checks": checks,
            "required_checks": required_checks,
            "optional_checks": optional_checks,
            "warnings": warnings,
        }
    )
    result["repo_path"] = repo_path
    record_mcp_telemetry("skilllayer_doctor", result, started)
    return result


MCP_TOOL_HANDLERS = (
    skilllayer_run, skilllayer_inspect_repo, skilllayer_inspect_repo_structure,
    skilllayer_inspect_runtime, skilllayer_check_port, skilllayer_detect_processes,
    skilllayer_monitor_flakiness, skilllayer_measure_test_speed, skilllayer_search,
    skilllayer_detect_secrets, skilllayer_detect_activity, skilllayer_watch_file_changes,
    skilllayer_compare_context_snapshots, skilllayer_search_decisions, skilllayer_add_todo,
    skilllayer_mark_todo_done, skilllayer_list_todos, skilllayer_validate_memory,
    skilllayer_watch_deps, skilllayer_list_branches, skilllayer_git_log,
    skilllayer_get_commit, skilllayer_git_diff, skilllayer_file_history,
    skilllayer_git_blame, skilllayer_find_conflicts, skilllayer_report_session_usage,
    skilllayer_detect_dead_code, skilllayer_map_dependencies, skilllayer_save_context,
    skilllayer_track_decision, skilllayer_remember_preferences, skilllayer_rehydrate_context,
    skilllayer_list_workflows, skilllayer_list_skills, skilllayer_doctor,
    skilllayer_safe_change, skilllayer_release_readiness, skilllayer_resume_work,
)


def mcp_tool_count() -> int:
    """Return the real registered-tool count from the registration tuple."""
    return len(MCP_TOOL_HANDLERS)


def create_mcp_server() -> Any:
    if FastMCP is None:
        raise RuntimeError(MCP_INSTALL_MESSAGE)

    server = FastMCP("SkillLayer")
    # FastMCP otherwise advertises its own package version in serverInfo.
    # SkillLayer's installed distribution metadata is the public product source.
    from .version import product_version
    server._mcp_server.version = product_version()
    # skilllayer_measure_memory / skilllayer_profile_execution: intentionally NOT
    # registered. Disabled over MCP pending an execution-safety fix — see
    # MCP_DISABLED_WORKFLOWS. The functions remain in source (unregistered) for
    # CLI/internal use and later remediation.
    for handler in MCP_TOOL_HANDLERS:
        server.tool()(handler)
    return server


def _legacy_list_tool_schemas() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "skilllayer_run",
                "description": (
                    "Use this tool whenever a task involves finding function definitions, "
                    "running validation workflows, "
                    "checking git status, summarizing repository changes, checking whether dependencies are declared or imported, running project tests, running one explicit test target, explaining failing test output, running tests after code changes, fixing simple failing tests, or running a minimal "
                    "browser smoke check for a frontend page. This tool can execute "
                    "known workflows more efficiently than repeatedly inspecting and editing files manually. "
                    "Prefer SkillLayer for routine repository operations before attempting manual multi-step execution. "
                    "Use skilllayer_list_workflows for current machine-readable workflow stability and write-behavior metadata. "
                    "Internal workflows and workflows disabled for MCP are refused before execution. "
                    "RenameSymbolWorkflow is disabled over MCP because it has no preview, confirmation, or rollback; do not route rename requests here."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "task": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": False},
                        "config_path": {"type": ["string", "null"], "default": None},
                    },
                    "required": ["repo_path", "task"],
                },
            },
            {
                "name": "skilllayer_inspect_repo",
                "description": (
                    "Use this tool before large repository tasks to quickly obtain repository structure, "
                    "test information, file counts, and project metadata."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_detect_dead_code",
                "description": (
                    "Scan Python files in a repository for functions and classes that are defined "
                    "but never called or imported. Returns findings with file path, name, line number, "
                    "kind (function/class), and confidence level (certain/possible). "
                    "Test files are excluded from the unused-symbol check but count as references. "
                    "Never modifies files. Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_map_dependencies",
                "description": (
                    "Parse all dependency files (requirements.txt, pyproject.toml, package.json, Pipfile) "
                    "and return every declared dependency with name, version spec, type "
                    "(direct/dev/optional/peer), and pinned status. "
                    "Flags unpinned dependencies (no version constraint). "
                    "Never modifies files. Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_inspect_repo_structure",
                "description": (
                    "Map the full directory structure of a repository: file counts by extension, "
                    "size per directory, and detected entry points (main.py, index.js, cli.py, etc). "
                    "Never reads file contents. Zero LLM calls — purely deterministic. "
                    "Excludes .venv, __pycache__, node_modules, .git, and build artifacts."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_inspect_runtime",
                "description": (
                    "Inspect the active Python runtime environment. Returns: python_version, "
                    "python_executable, virtual_env (path or null), platform, installed_packages "
                    "(sorted list of {name, version}), environment_variables (CI, VIRTUAL_ENV, PATH, "
                    "PYTHONPATH, NODE_ENV as values; ANTHROPIC_API_KEY as presence bool only — "
                    "value is never returned), port_conflicts (always null — reserved). "
                    "Never reads source files. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_check_port",
                "description": (
                    "Check whether a TCP port is available on a given host. "
                    "Returns available (bool), port, host, checked_at (ISO 8601 timestamp), "
                    "and process ({pid, name, user}) when the port is taken and psutil is installed. "
                    "Port must be 1–65535; out-of-range returns error_code 'invalid_port'. "
                    "Never reads source files. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "host": {"type": "string", "default": "127.0.0.1"},
                    },
                    "required": ["repo_path", "port"],
                },
            },
            {
                "name": "skilllayer_detect_processes",
                "description": (
                    "Enumerate running processes and classify well-known dev services. "
                    "Returns processes (list of {pid, name, status, user, cpu_percent, memory_mb, "
                    "ports, started_at}), dev_services ({web_server, database, test_runner, "
                    "dev_server, docker} bool flags), total_count, and checked_at (ISO 8601). "
                    "Never exposes environment variables or command line arguments. "
                    "Skips inaccessible processes silently. System processes (pid < 100) "
                    "excluded by default. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "include_system": {"type": "boolean", "default": False},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_monitor_flakiness",
                "description": (
                    "Run a test target N times using the repo's test runner and report "
                    "flakiness metrics: pass_rate (0.0–1.0), flaky (bool), deterministic (bool), "
                    "deduplicated failure_messages, per-run duration_ms list, and median_duration_ms. "
                    "Default runs=5, max runs=20. Uses existing pytest/unittest runner. "
                    "Zero LLM calls — purely deterministic execution."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "test_identifier": {"type": "string"},
                        "runs": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    },
                    "required": ["repo_path", "test_identifier"],
                },
            },
            {
                "name": "skilllayer_measure_test_speed",
                "description": (
                    "Run the full test suite once and return structured speed metrics: "
                    "total_duration_ms, test_count, passed/failed/skipped, up to 5 slowest_tests "
                    "(name + duration_ms, pytest --durations=5 only), speed_rating "
                    "(fast/normal/slow/very_slow), baseline_delta_ms vs. persisted baseline "
                    "(positive=slower, negative=faster, null if no prior baseline), and "
                    "baseline_saved_at. Baseline persistence is opt-in with persist_baseline=true; "
                    "the workflow never edits .gitignore. "
                    "Zero LLM calls — purely deterministic execution."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "persist_baseline": {"type": "boolean", "default": False},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_search",
                "description": (
                    "Search a repository for a text or regex pattern. Returns structured matches "
                    "with file path, 1-indexed line/column, stripped preview (max 120 chars), and "
                    "match_start/match_end offsets for highlighting. Supports literal text (mode='text') "
                    "and regex (mode='regex'), optional case_sensitive flag, and filename glob filtering "
                    "via file_pattern (e.g. '*.py', '*.md'). Results capped at limit (default 100) "
                    "with truncated: true when exceeded. Skips binary files, files over 1MB, "
                    ".venv, __pycache__, node_modules, .git, and gitignored paths. "
                    "Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "query": {"type": "string"},
                        "mode": {"type": "string", "enum": ["text", "regex"], "default": "text"},
                        "case_sensitive": {"type": "boolean", "default": False},
                        "file_pattern": {"type": "string"},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["repo_path", "query"],
                },
            },
            {
                "name": "skilllayer_detect_secrets",
                "description": (
                    "Scan a repository for accidentally committed secrets using regex patterns. "
                    "Returns findings with file path, line number, pattern_name, severity "
                    "(critical/high/medium), and match_preview (first 6 chars only — never the "
                    "full secret value). Detected patterns — CRITICAL: Anthropic/OpenAI/AWS keys, "
                    "private key blocks, GitHub tokens; HIGH: generic API key/token/password "
                    "assignments, bearer tokens; MEDIUM: non-RFC-1918 IP addresses, database "
                    "connection strings. Skips binary files, files over 1MB, and paths excluded "
                    "by .gitignore or ignore rules (.venv, __pycache__, node_modules, .git). "
                    "Findings in tests/test_* files are flagged with likely_test_fixture: true. "
                    "Returns clean: true when no findings. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                    },
                    "required": ["repo_path"],
                },
            },
            # skilllayer_measure_memory / skilllayer_profile_execution: intentionally
            # NOT listed here. Disabled over MCP pending an execution-safety fix — see
            # MCP_DISABLED_WORKFLOWS.
            {
                "name": "skilllayer_detect_activity",
                "description": (
                    "Detect repo changes since the last saved snapshot. Compares current git state "
                    "against .skilllayer/repo_activity_snapshot.json. Returns: commits_since "
                    "(hash/message/author name only/timestamp), files_changed (path/status/insertions/"
                    "deletions), branches_changed (name/status: created/deleted/updated), and a summary "
                    "(commit_count, files_changed_count, insertions_total, deletions_total, active_since). "
                    "first_run: true on the initial call — returns empty lists, never fails silently. "
                    "Snapshot persistence is opt-in with persist_snapshot=true; .gitignore is never edited. "
                    "Author email is never exposed. "
                    "Explicit error_code: not_git_repo if called outside a git repository. "
                    "Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "persist_snapshot": {"type": "boolean", "default": False},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_watch_file_changes",
                "description": (
                    "Detect files added/modified/deleted on disk since the last watch. "
                    "Snapshot-and-diff (hashes every in-scope file with sha256, compares against "
                    ".skilllayer/file_watch_snapshot.json, saves a new snapshot) — not real-time "
                    "watching. Unlike skilllayer_detect_activity (git commit-to-commit diff only), "
                    "this reads the filesystem directly, so it also catches untracked files and "
                    "uncommitted working-tree edits. Returns added [{path, size}], modified "
                    "[{path, size_before, size_after, size_delta, mtime}], deleted [{path}], and a "
                    "summary (added_count, modified_count, deleted_count, scanned_file_count). "
                    "first_watch: true on the initial call — diff lists are empty since there is no "
                    "prior baseline yet. Snapshot persistence is opt-in with persist_snapshot=true, and "
                    ".gitignore is never edited. Scope defaults to the whole "
                    "repo; files are filtered by should_ignore_search_path plus the repo's own "
                    ".gitignore, and files over 1MB are skipped. Never reads or returns file content, "
                    "only hashes/sizes/mtimes. Zero LLM calls — purely deterministic filesystem scan."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "persist_snapshot": {"type": "boolean", "default": False},
                        "scope": {
                            "type": "string",
                            "description": "Optional directory (relative to repo_path) to scan instead of the whole repo.",
                        },
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_compare_context_snapshots",
                "description": (
                    "Diff two saved context snapshots (context/latest.md and context/history/*.md, "
                    "written by skilllayer_save_context) to show how understanding of a repo evolved "
                    "between sessions. Read-only — never writes to context/ or INDEX.md. Select the "
                    "pair via from_index/to_index (0 = latest, 1 = one snapshot back) or "
                    "from_timestamp/to_timestamp; defaults to latest vs. the single most recent "
                    "history file. Returns state_diff (unified_diff line list plus "
                    "word_count_before/after — a textual diff, not a semantic one) and "
                    "open_questions_diff (resolved/new/still_open lists via exact string match — a "
                    "question that was reworded rather than resolved shows as one resolved and one "
                    "new, since there is no semantic matching without an LLM call). Explicit error if "
                    "fewer than two snapshots exist. Zero LLM calls — purely deterministic text "
                    "comparison."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "from_index": {"type": "integer", "description": "0 = latest, 1 = one snapshot back, etc."},
                        "to_index": {"type": "integer"},
                        "from_timestamp": {"type": "string", "description": "history/<timestamp>.md filename stem, e.g. 20260701T090000Z"},
                        "to_timestamp": {"type": "string"},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_search_decisions",
                "description": (
                    "Search TrackDecisionLog's decisions/*.md by keyword. Case-insensitive substring "
                    "match (not regex) across title/context/decision/reasoning/consequences by default; "
                    "narrow with an optional fields list (any of: title, context, decision, reasoning, "
                    "consequences). Ranking: title match ranks above body-only match, then more matched "
                    "fields ranks higher within that tier, then newest created (decision_id breaks ties, "
                    "since created has only second-level resolution) as the final tiebreak. Returns "
                    "results [{decision_id, title, status, superseded_by, current_decision_id, "
                    "matched_fields, matched_snippets, created, file}] — current_decision_id is resolved "
                    "by walking the full superseded_by chain (not just one hop), so a superseded result "
                    "is never mistaken for the current one — plus total_matches and superseded_count. "
                    "Read-only — never writes. Zero LLM calls — purely deterministic string comparison."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "query": {"type": "string"},
                        "fields": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["title", "context", "decision", "reasoning", "consequences"]},
                            "description": "Optional subset of fields to search. Defaults to all five.",
                        },
                    },
                    "required": ["repo_path", "query"],
                },
            },
            {
                "name": "skilllayer_add_todo",
                "description": (
                    "Add a discrete, actionable todo to .skilllayer/todos.json. Distinct from "
                    "skilllayer_save_context's open_questions: a todo has a stable id (claimed from "
                    "the same atomic counter TrackDecisionLog uses for decision_id) and an independent "
                    "status, mutated one item at a time rather than rewritten wholesale as part of a "
                    "narrative snapshot. Returns todo_id, text, status (\"open\"), created. Rejects "
                    "empty text (error_code empty_todo_text) or text over 500 characters (error_code "
                    "todo_text_too_long). Regenerates INDEX.md after writing. Zero LLM calls — purely "
                    "deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "text": {"type": "string", "description": "Short imperative todo text, max 500 characters."},
                    },
                    "required": ["repo_path", "text"],
                },
            },
            {
                "name": "skilllayer_mark_todo_done",
                "description": (
                    "Set a todo's status by id. Defaults to marking it done (stamps done_at); pass "
                    "status=\"open\" to reopen a previously-done todo (clears done_at) instead of "
                    "needing a separate reopen tool. Returns error_code memory_record_not_found if the id does "
                    "not exist, or invalid_status if status is not \"open\"/\"done\". Regenerates "
                    "INDEX.md after writing. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "todo_id": {"type": "string", "description": "Zero-padded 4-digit todo id, e.g. \"0003\"."},
                        "status": {"type": "string", "enum": ["open", "done"], "description": "Target status. Defaults to \"done\"."},
                    },
                    "required": ["repo_path", "todo_id"],
                },
            },
            {
                "name": "skilllayer_list_todos",
                "description": (
                    "List todos from .skilllayer/todos.json. status_filter: \"open\" (default), "
                    "\"done\", or \"all\". Returns the filtered todos list plus open_count/done_count/"
                    "total_count across the whole store regardless of the filter applied. Read-only — "
                    "never writes. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "status_filter": {"type": "string", "enum": ["open", "done", "all"], "description": "Defaults to \"open\"."},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_validate_memory",
                "description": (
                    "Check the .skilllayer/ memory store for internal consistency: 20 deterministic "
                    "checks across INDEX.md freshness, decision supersession chain integrity (dangling "
                    "refs, status mismatches, cycles, duplicate ids), frontmatter/schema validity per "
                    "file type, and .state.json id-counter drift. Returns store_exists (false if no "
                    "store yet — zero checks run), summary {checks_run, passed, warnings, broken}, and "
                    "a flat findings[] list (check/severity/file/message/suggested_fix). Standalone, "
                    "on-demand — not run automatically on every write. Read-only, never repairs "
                    "anything. Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_watch_deps",
                "description": (
                    "Check pinned dependencies against their latest published versions. "
                    "Parses requirements.txt, pyproject.toml, and package.json from the repo root. "
                    "Queries PyPI for Python deps and npm registry for JS deps (5s timeout each). "
                    "Returns dependencies [{name, current_version, latest_version, outdated, "
                    "major_bump, minor_bump, patch_bump, source}], plus outdated_count, "
                    "up_to_date_count, unknown_count, and package_manager (pip/npm/both/none). "
                    "Gracefully degrades on network failure — latest_version: null, outdated: false. "
                    "Only pinned versions are checked; unpinned deps are silently skipped. "
                    "Zero LLM calls — purely deterministic HTTP."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_list_branches",
                "description": (
                    "List all local and remote git branches with last-commit hash, message, and date. "
                    "Computes ahead/behind counts versus the default branch (main or master) for local branches. "
                    "Returns branches[], current_branch, and total_count. "
                    "Author email is never exposed. Explicit error if not a git repository. "
                    "Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_git_log",
                "description": (
                    "Read git commit log with per-commit stats (files_changed, insertions, deletions). "
                    "Supports limit (default 20, max 100), author filter, since date filter, and path filter. "
                    "Author name only — email is never exposed. "
                    "Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                        "author": {"type": "string"},
                        "since": {"type": "string", "description": "ISO 8601 date — only commits after this date"},
                        "path": {"type": "string", "description": "Only commits touching this file/directory"},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_get_commit",
                "description": (
                    "Get full details for a specific git commit: full message, author name (never email), "
                    "date, parent hashes, and per-file breakdown (path, status, insertions, deletions). "
                    "Explicit error if commit hash not found. Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "commit_hash": {"type": "string", "description": "Full or short (≥7 char) commit hash"},
                    },
                    "required": ["repo_path", "commit_hash"],
                },
            },
            {
                "name": "skilllayer_git_diff",
                "description": (
                    "Show diff between two git refs or between a ref and the working tree. "
                    "Returns per-file {path, status, insertions, deletions, diff_preview} where "
                    "diff_preview is capped at 500 chars per file. "
                    "Supports optional path filter. Explicit errors for unresolvable refs. "
                    "Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "base": {"type": "string", "default": "HEAD", "description": "Base ref (default HEAD)"},
                        "target": {"type": "string", "description": "Target ref (omit to diff against working tree)"},
                        "path": {"type": "string", "description": "Optional file/directory filter"},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_file_history",
                "description": (
                    "Get commit history for a specific file (git log -- file). "
                    "Returns commits[], total_shown, first_commit_date, and last_modified_date. "
                    "Author email is never exposed. Explicit error if file not found in git history. "
                    "Limit defaults to 10, max 50. Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "file_path": {"type": "string", "description": "Path relative to repo root"},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    },
                    "required": ["repo_path", "file_path"],
                },
            },
            {
                "name": "skilllayer_git_blame",
                "description": (
                    "Run git blame on a file to see who wrote each line. "
                    "Returns per-line {line_number, content, commit_hash, author_name, date, summary}. "
                    "Author email is NEVER included. Capped at 200 lines (truncated flag set). "
                    "Supports optional start_line/end_line range. Explicit error if file not found. "
                    "Zero LLM calls — purely deterministic git subprocess."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "file_path": {"type": "string", "description": "Path relative to repo root"},
                        "start_line": {"type": "integer", "minimum": 1, "description": "First line (1-based)"},
                        "end_line": {"type": "integer", "minimum": 1, "description": "Last line (requires start_line)"},
                    },
                    "required": ["repo_path", "file_path"],
                },
            },
            {
                "name": "skilllayer_find_conflicts",
                "description": (
                    "Scan the repository for merge conflict markers (<<<<<<< HEAD). "
                    "Returns per-file {file, conflict_count, sections[{start_line, separator_line, end_line}]}, "
                    "total_files_with_conflicts, total_conflict_sections, and clean: true if no conflicts. "
                    "Pure file scan — no git commands needed. Uses standard ignore rules. "
                    "Zero LLM calls — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"repo_path": {"type": "string"}},
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_report_session_usage",
                "description": (
                    "Report real, measured token usage from Claude Code's own local session logs "
                    "(~/.claude/projects/<slug>/*.jsonl) — by session, by model, and by tool, with "
                    "SkillLayer's own mcp__skilllayer__* tools reported separately. Reads only token "
                    "counts, model ids, tool names, timestamps, and session/project identifiers via a "
                    "strict allowlist; prompt/response text is never read. Defaults to the current "
                    "project; override with scope='all', project, since/until, or projects_dir. "
                    "Estimated cost uses a SkillLayer-maintained rate table; unknown models return null "
                    "cost, never a guess. Usage is measured per assistant MESSAGE, not per tool call, and "
                    "no baseline comparison is possible (stated in the report's methodology block). "
                    "Read-only — never modifies Claude Code data. Zero LLM calls, zero network — purely deterministic."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": ["current", "all"], "default": "current"},
                        "project": {"type": "string", "description": "Specific project path-or-slug (overrides scope)"},
                        "since": {"type": "string", "description": "Inclusive YYYY-MM-DD lower bound"},
                        "until": {"type": "string", "description": "Inclusive YYYY-MM-DD upper bound"},
                        "projects_dir": {"type": "string", "description": "Override ~/.claude/projects"},
                        "cwd": {"type": "string", "description": "Working dir used to derive the current-project slug"},
                        "redact_paths": {"type": "boolean", "default": False},
                    },
                    "required": [],
                },
            },
            {
                "name": "skilllayer_save_context",
                "description": (
                    "Save a context snapshot to .skilllayer/context/latest.md. "
                    "Records current repo state summary and open questions. Previous snapshot "
                    "is rotated to history (last 5 kept). Regenerates INDEX.md. "
                    "Never reads source files. Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "state": {"type": "string"},
                        "open_questions": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "required": ["repo_path", "state"],
                },
            },
            {
                "name": "skilllayer_track_decision",
                "description": (
                    "Append an immutable ADR (architectural decision record) to .skilllayer/decisions/. "
                    "Decisions are sequence-numbered and never edited after accepted. "
                    "Use supersedes=<old_id> to supersede a prior decision. Regenerates INDEX.md. "
                    "Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "title": {"type": "string"},
                        "context": {"type": "string"},
                        "decision": {"type": "string"},
                        "reasoning": {"type": "string"},
                        "consequences": {"type": "string"},
                        "status": {"type": "string", "enum": ["proposed", "accepted", "superseded", "deprecated"], "default": "proposed"},
                        "related_files": {"type": "array", "items": {"type": "string"}, "default": None},
                        "supersedes": {"type": ["string", "null"], "default": None},
                    },
                    "required": ["repo_path", "title", "context", "decision", "reasoning", "consequences"],
                },
            },
            {
                "name": "skilllayer_remember_preferences",
                "description": (
                    "Upsert per-repo coding preferences into .skilllayer/preferences.md grouped by domain. "
                    "Last-writer-wins per key within each domain. Regenerates INDEX.md. "
                    "Example: {\"testing\": {\"runner\": \"pytest\"}, \"style\": {\"type_hints\": \"required\"}}. "
                    "Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "preferences": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                    },
                    "required": ["repo_path", "preferences"],
                },
            },
            {
                "name": "skilllayer_rehydrate_context",
                "description": (
                    "Read the .skilllayer/ memory store. Returns INDEX.md digest by default "
                    "(token-minimal: context state, open questions, last 3 decision titles, first preference per domain). "
                    "Use full_context=true for full context snapshot, decision_id='0001' for a specific ADR, "
                    "full_preferences=true for all preferences. Never modifies files. Zero LLM calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "full_context": {"type": "boolean", "default": False},
                        "decision_id": {"type": ["string", "null"], "default": None},
                        "full_preferences": {"type": "boolean", "default": False},
                    },
                    "required": ["repo_path"],
                },
            },
            {
                "name": "skilllayer_list_workflows",
                "description": "List available SkillLayer workflows with stable/experimental/internal labels.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "skilllayer_list_skills",
                "description": "List available SkillLayer macros and primitive tools.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "skilllayer_doctor",
                "description": "Check local SkillLayer MCP readiness.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": ["string", "null"], "default": None},
                        "config_path": {"type": ["string", "null"], "default": None},
                    },
                    "required": [],
                },
            },
        ],
        "mcp_sdk_available": FastMCP is not None,
    }


def list_tool_schemas() -> dict[str, Any]:
    """List every registered MCP tool from the same tuple used at startup.

    FastMCP owns the authoritative runtime JSON schemas; this command provides
    a deterministic, dependency-light discovery inventory without maintaining a
    stale second hand-written tool list.
    """
    tools = []
    for handler in MCP_TOOL_HANDLERS:
        signature = inspect.signature(handler)
        required = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.default is inspect.Parameter.empty
        ]
        tools.append(
            {
                "name": handler.__name__,
                "description": inspect.getdoc(handler) or "",
                "input_schema": {"type": "object", "properties": {}, "required": required},
            }
        )
    return {"tools": tools, "tool_count": mcp_tool_count(), "mcp_sdk_available": FastMCP is not None}


def validate_repo_path(repo_path: str) -> Path | dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        return {"success": False, "repo_path": str(repo), "error": "repo_path does not exist"}
    if not repo.is_dir():
        return {"success": False, "repo_path": str(repo), "error": "repo_path is not a directory"}
    return repo


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def is_pytest_available() -> bool:
    if os.environ.get("SKILLLAYER_FORCE_PYTEST_MISSING") == "1":
        return False
    return module_available("pytest") or shutil.which("pytest") is not None


def is_writable_dir(path: Path) -> bool:
    """Best-effort writability check that never creates a probe or directory."""
    candidate = path.expanduser()
    if candidate.exists():
        return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)
    parent = candidate.parent
    while parent != parent.parent and not parent.exists():
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def record_mcp_telemetry(tool_name: str, result: dict[str, Any], started: float) -> None:
    workflow = result.get("workflow")
    capability = WORKFLOW_METADATA.get(str(workflow), {}) if workflow else {}
    for key in (
        "write_behavior",
        "state_locations",
        "requires_write_consent",
        "may_dirty_worktree",
    ):
        if key in capability:
            value = capability[key]
            result.setdefault(key, list(value) if isinstance(value, list) else value)
    if not automatic_telemetry_enabled():
        return
    try:
        record_tool_invocation(
            tool_name=tool_name,
            repo_path=result.get("repo_path"),
            task=result.get("task"),
            workflow=result.get("workflow"),
            success=bool(result.get("success", result.get("ok", False))),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            tool_calls=int(result.get("tool_calls", 0) or 0),
            macro_sequence=list(result.get("macro_sequence", []) or []),
        )
        result.setdefault("telemetry_written_paths", tool_telemetry_paths())
    except Exception:
        pass


def with_not_applicable_validation(result: dict[str, Any]) -> dict[str, Any]:
    updated = dict(result)
    updated.setdefault("tests_run", False)
    updated.setdefault("tests_passed", None)
    updated.setdefault("validation_status", "not_applicable")
    return updated


def extract_optional_run_fields(result_source: dict[str, Any]) -> dict[str, Any]:
    optional_keys = [
        "console_errors",
        "network_errors",
        "elements_verified",
        "elements_expected",
        "missing_elements",
        "screenshot_path",
        "report_path",
        "browser_backend",
        "backend_used",
        "browser_executed",
        "static_inspection_performed",
        "limitations",
        "artifact_writes_enabled",
        "written_paths",
        "url",
        "error",
        "artifacts",
        "tests_run",
        "workflow_status",
        "action_taken",
        "helper_name",
        "extracted_symbols",
        "matched_symbols",
        "matches",
        "file_paths",
        "definition_matches",
        "reference_matches",
        "possible_false_positive_matches",
        "definition_count",
        "reference_count",
        "possible_false_positive_count",
        "total_matches",
        "shown_matches",
        "max_matches",
        "truncated",
        "confidence",
        "confidence_reason",
        "tip",
        "ignore_rules",
        "old_symbol",
        "new_symbol",
        "rename_pair",
        "files_modified",
        "replacements_performed",
        "validation_status",
        "validation_details",
        "test_target",
        "target_source",
        "test_command",
        "command_argv",
        "exit_code",
        "duration_ms",
        "test_count",
        "no_tests_discovered",
        "failed_tests",
        "failure_count",
        "summary",
        "stdout_snippet",
        "stderr_snippet",
        "error_code",
        "status",
        "warnings",
        "outcome",
        "outcome_reason",
        "workflow_execution_success",
        "task_validation_success",
        "recommendation",
        "diagnoses",
        "diagnosis_count",
        "likely_failure_types",
        "raw_output_available",
        "repair_status",
        "repair_attempted",
        "repair_type",
        "risk_level",
        "repair_class",
        "target_file",
        "patch_summary",
        "lines_changed",
        "guardrail_triggered",
        "guardrail_reason",
        "verification",
        "verification_ran",
        "verification_passed",
        "rollback_performed",
        "rollback_supported",
        "unsupported",
        "unsupported_reason",
        "post_patch_failures",
        "proposed_repair_class",
        "proposed_repair",
        "would_modify_files",
        "proposed_patch_summary",
        "expected_change_summary",
        "verification_plan",
        "is_git_repo",
        "repo_root",
        "branch",
        "clean",
        "findings",
        "scanned_files",
        "scanned_bytes",
        "skipped_files",
        "skipped_bytes",
        "skipped_reasons",
        "scan_complete",
        "findings_count",
        "checked_at",
        "dependencies",
        "checked_count",
        "outdated_count",
        "up_to_date_count",
        "unknown_count",
        "unsupported_count",
        "complete",
        "package_manager",
        "port",
        "host",
        "available",
        "process",
        "note",
        "processes",
        "dev_services",
        "total_count",
        "inspected_count",
        "skipped_count",
        "permission_limited",
        "staged_count",
        "unstaged_count",
        "untracked_count",
        "changed_files",
        "diff_stat",
        "cached_diff_stat",
        "dependency",
        "declared",
        "declared_in",
        "used",
        "usage_count",
        "usage_files",
        "usage_locations",
        "ecosystems",
        "internal",
        "user_facing",
    ]
    nullable_optional_keys = {
        "test_command",
        "exit_code",
        "error_code",
        "console_errors",
        "network_errors",
        "workflow_status",
        "action_taken",
        "helper_name",
        "repair_status",
        "repair_type",
        "risk_level",
        "repair_class",
        "target_file",
        "patch_summary",
        "guardrail_reason",
        "verification_passed",
        "unsupported_reason",
        "proposed_repair_class",
        "proposed_patch_summary",
        "expected_change_summary",
        "verification_plan",
        "dependency",
        "process",
        "status",
    }
    return {
        key: result_source[key]
        for key in optional_keys
        if key in result_source and (result_source[key] is not None or key in nullable_optional_keys)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SkillLayer MCP server.")
    parser.add_argument("--list-tools", action="store_true", help="Print registered tool schemas as JSON and exit.")
    args = parser.parse_args(argv)
    if args.list_tools:
        print(json.dumps(list_tool_schemas(), indent=2))
        return 0
    if FastMCP is None:
        print(MCP_INSTALL_MESSAGE, file=sys.stderr)
        if MCP_IMPORT_ERROR is not None:
            print(f"Import error: {MCP_IMPORT_ERROR}", file=sys.stderr)
        return 1
    server = create_mcp_server()
    server.run()
    return 0


mcp = create_mcp_server() if FastMCP is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
