from __future__ import annotations

import argparse
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
from .router import SkillRouter
from .security import blocked_workflow_reason, workflow_execution_blocked
from .runner.core import build_check_port_artifacts, build_detect_activity_artifacts, build_detect_dead_code_artifacts, build_detect_processes_artifacts, build_detect_secrets_artifacts, build_file_history_artifacts, build_find_conflicts_artifacts, build_get_commit_artifacts, build_git_blame_artifacts, build_git_diff_artifacts, build_git_log_artifacts, build_inspect_repo_structure_artifacts, build_inspect_runtime_artifacts, build_list_branches_artifacts, build_map_dependencies_artifacts, build_measure_memory_artifacts, build_measure_test_speed_artifacts, build_monitor_flakiness_artifacts, build_profile_execution_artifacts, build_rehydrate_context_artifacts, build_remember_preferences_artifacts, build_save_context_artifacts, build_search_artifacts, build_track_decision_artifacts, build_watch_deps_artifacts, snapshot_python_files, unified_diff
from .telemetry import record_tool_invocation
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
    Stable workflows currently include FindFunctionWorkflow and
    BrowserSmokeWorkflow. RunTestsWorkflow,
    SingleTestWorkflow, ExplainFailureWorkflow, GitStatusWorkflow, DependencyCheckWorkflow,
    and FixFailingTestWorkflow are experimental and may return unsupported when
    repository-specific assumptions are not met. AddHelperWorkflow is internal.
    RenameSymbolWorkflow is disabled over MCP and is not callable here: it has no
    preview, confirmation, or rollback and can perform an irreversible
    repository-wide replacement. Do not route rename requests to this tool.
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


def skilllayer_check_port(repo_path: str, port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    """Check whether a TCP port is available on a given host.

    Returns:
    - port (int): the port checked
    - host (str): the host checked (default 127.0.0.1)
    - available (bool): true if nothing is bound to this port
    - process (object | null): if port is taken and psutil is available:
        {pid, name, user}; null otherwise
    - checked_at (str): ISO 8601 timestamp
    - note (str, optional): present when psutil is unavailable

    Never reads source files. Zero LLM calls — purely deterministic.
    Port must be in range 1–65535; out-of-range returns error_code 'invalid_port'."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_check_port", repo, started)
        return repo
    try:
        artifacts = build_check_port_artifacts(port, host)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "CheckPortAvailabilityWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
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
    - total_count: number of processes returned
    - checked_at: ISO 8601 timestamp

    Never exposes environment variables or command line arguments.
    Processes that cannot be read due to permissions are silently skipped.
    System processes (pid < 100) are excluded unless include_system=True.
    Zero LLM calls — purely deterministic."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_processes", repo, started)
        return repo
    try:
        artifacts = build_detect_processes_artifacts(include_system=include_system)
        result = {
            "success": True,
            "workflow": "DetectRunningProcessesWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
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


def skilllayer_measure_test_speed(repo_path: str) -> dict[str, Any]:
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

    Baseline is persisted to .skilllayer/test_speed_baseline.json and updated after each run.
    Uses pytest --durations=5 when pytest is detected; slowest_tests is empty otherwise.
    Zero LLM calls — purely deterministic execution."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_measure_test_speed", repo, started)
        return repo
    try:
        artifacts = build_measure_test_speed_artifacts(repo)
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
    registry (https://registry.npmjs.org/{pkg}/latest) for JS deps (5s timeout each).

    Returns:
    - dependencies: [{name, current_version, latest_version, outdated,
        major_bump, minor_bump, patch_bump, source}]
    - outdated_count / up_to_date_count / unknown_count
    - package_manager: "pip" | "npm" | "both" | "none"
    - checked_at: ISO 8601 timestamp

    Gracefully degrades on network errors — sets latest_version: null, outdated: false.
    Only pinned versions checked; unpinned deps silently skipped.
    Zero LLM calls — purely deterministic HTTP."""

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


def skilllayer_detect_activity(repo_path: str) -> dict[str, Any]:
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

    Snapshot is updated on every run. Returns an explicit error if called
    outside a git repository. Zero LLM calls — purely deterministic git subprocess."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_detect_activity", repo, started)
        return repo
    try:
        artifacts = build_detect_activity_artifacts(repo)
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


def skilllayer_profile_execution(
    repo_path: str,
    target: str,
    include_stdlib: bool = False,
) -> dict[str, Any]:
    """Profile execution time of a Python file using cProfile in an isolated subprocess.

    Parameters:
    - target: relative path (from repo_path) or absolute path to a .py file
    - include_stdlib: if True, include Python standard library frames in hotspots
      (default False — stdlib frames are filtered out)

    Returns:
    - total_duration_ms: wall clock time for the entire run
    - cpu_time_ms: CPU time only
    - function_calls: total number of function calls recorded
    - hotspots: up to 10 {function, file, line, calls, total_time_ms, per_call_ms}
      sorted by total_time_ms descending; file paths are relative to repo root
    - rating: "fast" (<100ms), "normal" (100-1000ms), "slow" (1-5s), "very_slow" (>5s)
    - baseline_delta_ms: difference from saved baseline (null on first run)
    - baseline_saved_at: ISO 8601 timestamp of prior baseline

    Only .py files are supported. Explicit errors for missing or non-Python targets.
    Baseline saved to .skilllayer/profile_baseline.json after each run.
    Zero LLM calls — purely deterministic subprocess execution."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_profile_execution", repo, started)
        return repo
    try:
        artifacts = build_profile_execution_artifacts(repo, target, include_stdlib=include_stdlib)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "ProfileCodeExecutionWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_profile_execution", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_profile_execution", result, started)
        return result


def skilllayer_measure_memory(repo_path: str, target: str) -> dict[str, Any]:
    """Measure peak RSS memory of a Python file by running it in an isolated subprocess.

    Parameters:
    - target: relative path (from repo root) or absolute path to a .py file

    Returns:
    - peak_memory_mb: peak subprocess RSS during execution
    - baseline_memory_mb: subprocess RSS before running the target
    - delta_memory_mb: peak minus baseline (growth caused by the target)
    - duration_ms: execution time
    - tracemalloc_top: up to 10 largest allocations {file, line, size_kb, count}
    - rating: "efficient" (<50MB), "moderate" (50-200MB), "heavy" (200-500MB), "very_heavy" (>500MB)
    - baseline_delta_mb: delta vs. saved baseline (null on first run)
    - baseline_saved_at: ISO 8601 timestamp of prior baseline

    Only .py files are supported. Explicit errors for missing or non-Python targets.
    Baseline is saved to .skilllayer/memory_baseline.json after each run.
    Zero LLM calls — purely deterministic subprocess execution."""

    started = time.perf_counter()
    repo = validate_repo_path(repo_path)
    if isinstance(repo, dict):
        record_mcp_telemetry("skilllayer_measure_memory", repo, started)
        return repo
    try:
        artifacts = build_measure_memory_artifacts(repo, target)
        result = {
            "success": artifacts.get("error_code") is None,
            "workflow": "MeasureMemoryUsageWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_measure_memory", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_measure_memory", result, started)
        return result


def skilllayer_detect_secrets(repo_path: str) -> dict[str, Any]:
    """Scan a repository for accidentally committed secrets using regex patterns.

    Returns:
    - findings: list of {file, line, pattern_name, severity, match_preview, likely_test_fixture}
    - scanned_files: total files examined
    - skipped_files: files skipped (binary, >1MB, gitignored, or search-ignored)
    - findings_count: number of findings
    - clean: true if no findings
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
        result = {
            "success": True,
            "workflow": "DetectSecretPatternsWorkflow",
            "llm_calls": 0,
            **{k: v for k, v in artifacts.items() if k not in ("workflow", "tests_run", "tests_passed", "validation_status")},
        }
        record_mcp_telemetry("skilllayer_detect_secrets", result, started)
        return result
    except Exception as exc:
        result = {"success": False, "repo_path": str(repo), "error": str(exc)}
        record_mcp_telemetry("skilllayer_detect_secrets", result, started)
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
        result = {"success": True, "llm_calls": 0, **artifacts}
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
        result = {"success": True, "llm_calls": 0, **artifacts}
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
        result = {"success": True, "llm_calls": 0, **artifacts}
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
        result = {"success": artifacts.get("error_code") is None, "llm_calls": 0, **artifacts}
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
                "stability": WORKFLOW_METADATA.get(name, {}).get("stability", "experimental"),
                "summary": WORKFLOW_METADATA.get(name, {}).get("summary", ""),
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
    log_dir = Path(config.get("logging", {}).get("log_dir", "runs/skilllayer_logs"))
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
        "log_dir_writable": {
            "value": str(log_dir),
            "ok": is_writable_dir(log_dir),
            "required": True,
        },
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
            "warning": None if FastMCP is not None else "MCP SDK is optional for CLI use. Install with python -m pip install -e \".[mcp]\" --no-build-isolation for MCP clients.",
        },
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
    record_mcp_telemetry("skilllayer_doctor", {**result, "repo_path": repo_path}, started)
    return result


def create_mcp_server() -> Any:
    if FastMCP is None:
        raise RuntimeError(MCP_INSTALL_MESSAGE)

    server = FastMCP("SkillLayer")
    server.tool()(skilllayer_run)
    server.tool()(skilllayer_inspect_repo)
    server.tool()(skilllayer_inspect_repo_structure)
    server.tool()(skilllayer_inspect_runtime)
    server.tool()(skilllayer_check_port)
    server.tool()(skilllayer_detect_processes)
    server.tool()(skilllayer_monitor_flakiness)
    server.tool()(skilllayer_measure_test_speed)
    server.tool()(skilllayer_search)
    server.tool()(skilllayer_detect_secrets)
    server.tool()(skilllayer_measure_memory)
    server.tool()(skilllayer_profile_execution)
    server.tool()(skilllayer_detect_activity)
    server.tool()(skilllayer_watch_deps)
    server.tool()(skilllayer_list_branches)
    server.tool()(skilllayer_git_log)
    server.tool()(skilllayer_get_commit)
    server.tool()(skilllayer_git_diff)
    server.tool()(skilllayer_file_history)
    server.tool()(skilllayer_git_blame)
    server.tool()(skilllayer_find_conflicts)
    server.tool()(skilllayer_detect_dead_code)
    server.tool()(skilllayer_map_dependencies)
    server.tool()(skilllayer_save_context)
    server.tool()(skilllayer_track_decision)
    server.tool()(skilllayer_remember_preferences)
    server.tool()(skilllayer_rehydrate_context)
    server.tool()(skilllayer_list_workflows)
    server.tool()(skilllayer_list_skills)
    server.tool()(skilllayer_doctor)
    return server


def list_tool_schemas() -> dict[str, Any]:
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
                    "Stable workflows include FindFunctionWorkflow and BrowserSmokeWorkflow. "
                    "RunTestsWorkflow, SingleTestWorkflow, ExplainFailureWorkflow, GitStatusWorkflow, DependencyCheckWorkflow, and FixFailingTestWorkflow are experimental. "
                    "AddHelperWorkflow is internal. RenameSymbolWorkflow is disabled over MCP "
                    "(no preview, confirmation, or rollback) and will be refused; do not route rename requests here."
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
                    "baseline_saved_at. Saves a new baseline after each run to "
                    ".skilllayer/test_speed_baseline.json. Never modifies source files. "
                    "Zero LLM calls — purely deterministic execution."
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
            {
                "name": "skilllayer_measure_memory",
                "description": (
                    "Measure peak RSS memory of a Python (.py) file by running it in an isolated subprocess. "
                    "Returns peak_memory_mb (peak RSS during execution), baseline_memory_mb (subprocess RSS "
                    "before running the target), delta_memory_mb (growth caused by the target code), "
                    "duration_ms, and tracemalloc_top (up to 10 largest allocations with file/line/size_kb/count, "
                    "sorted descending by size). Also returns a rating: efficient (<50MB delta), moderate "
                    "(50-200MB), heavy (200-500MB), very_heavy (>500MB). Persists a baseline to "
                    ".skilllayer/memory_baseline.json and computes baseline_delta_mb on subsequent runs. "
                    "Only .py files supported — explicit error_code for missing or non-Python targets. "
                    "Zero LLM calls — purely deterministic subprocess execution."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "target": {
                            "type": "string",
                            "description": "Path to the .py file to measure (relative to repo_path or absolute)",
                        },
                    },
                    "required": ["repo_path", "target"],
                },
            },
            {
                "name": "skilllayer_profile_execution",
                "description": (
                    "Profile execution time of a Python (.py) file using cProfile in an isolated subprocess. "
                    "Returns total_duration_ms (wall clock), cpu_time_ms, function_calls, and hotspots "
                    "(up to 10 entries sorted by cumulative time: function, file, line, calls, "
                    "total_time_ms, per_call_ms — file paths relative to repo root). "
                    "Python stdlib hotspots are filtered by default (pass include_stdlib=true to include them). "
                    "Rating: fast (<100ms), normal (100-1000ms), slow (1-5s), very_slow (>5s). "
                    "Persists baseline to .skilllayer/profile_baseline.json and computes baseline_delta_ms "
                    "on subsequent runs for the same target. "
                    "Only .py files supported — explicit error_code for missing or non-Python targets. "
                    "Zero LLM calls — purely deterministic subprocess execution."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"},
                        "target": {
                            "type": "string",
                            "description": "Path to the .py file to profile (relative to repo_path or absolute)",
                        },
                        "include_stdlib": {
                            "type": "boolean",
                            "description": "Include Python stdlib frames in hotspots (default false)",
                        },
                    },
                    "required": ["repo_path", "target"],
                },
            },
            {
                "name": "skilllayer_detect_activity",
                "description": (
                    "Detect repo changes since the last saved snapshot. Compares current git state "
                    "against .skilllayer/repo_activity_snapshot.json. Returns: commits_since "
                    "(hash/message/author name only/timestamp), files_changed (path/status/insertions/"
                    "deletions), branches_changed (name/status: created/deleted/updated), and a summary "
                    "(commit_count, files_changed_count, insertions_total, deletions_total, active_since). "
                    "first_run: true on the initial call — returns empty lists, never fails silently. "
                    "Snapshot updated on every run. Author email is never exposed. "
                    "Explicit error_code: not_git_repo if called outside a git repository. "
                    "Zero LLM calls — purely deterministic git subprocess."
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
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".skilllayer_mcp_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def record_mcp_telemetry(tool_name: str, result: dict[str, Any], started: float) -> None:
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
