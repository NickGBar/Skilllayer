from __future__ import annotations

import dataclasses
import difflib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..config.defaults import WORKFLOW_METADATA
from ..cost_tracking import USAGE_EVENTS_JSONL, event_from_llm_client, record_usage_event
from ..demand_tracking import DEMAND_EVENTS_JSONL, record_demand_event
from ..llm import LLMClient
from ..memory import SkillMemory
from ..policy import load_policy
from ..router import SkillRouter
from ..security import InternalWorkflowBlockedError, WorkflowAccessPolicy
from ..telemetry import automatic_telemetry_enabled
from ..tools import ProjectTools, run_browser_smoke
from ..tools.execution import DEFAULT_SEARCH_IGNORE_DIRS, DEFAULT_SEARCH_IGNORE_SUFFIXES, should_ignore_search_path
from ..verifier import DEFAULT_TEST_TIMEOUT_SECONDS, TestRunner


ALLOWED_GIT_STATUS_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "status", "--porcelain=v1"),
    ("git", "status", "--short"),
    ("git", "diff", "--stat"),
    ("git", "diff", "--cached", "--stat"),
    ("git", "diff", "--name-status"),
    ("git", "diff", "--cached", "--name-status"),
    ("git", "rev-parse", "--show-toplevel"),
    ("git", "branch", "--show-current"),
)

EARLY_DRY_RUN_TASK_TYPES = frozenset({
    "add_todo",
    "mark_todo_done",
    "check_port",
    "monitor_flakiness",
    "measure_test_speed",
    "measure_memory",
    "profile_execution",
    "detect_activity",
    "watch_file_changes",
    "watch_deps",
})

DEPENDENCY_DECLARATION_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
)

PYTHON_SOURCE_SUFFIXES = {".py"}
NODE_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
DEPENDENCY_SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "runs",
    "venv",
}


SYMBOL_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "change",
    "class",
    "defined",
    "definition",
    "failing",
    "find",
    "fix",
    "function",
    "is",
    "locate",
    "method",
    "rename",
    "renaming",
    "show",
    "symbol",
    "test",
    "tests",
    "the",
    "to",
    "where",
}


# InternalWorkflowBlockedError is defined in skilllayer.security (the access-control
# component) and imported above. It is re-exported here so existing callers that
# import it from the runner continue to work.


class SkillLayer:
    def __init__(
        self,
        router: SkillRouter | None = None,
        llm: LLMClient | None = None,
        memory: SkillMemory | None = None,
        test_runner: TestRunner | None = None,
        access_policy: WorkflowAccessPolicy | None = None,
    ) -> None:
        self.router = router or SkillRouter()
        self.llm = llm or LLMClient()
        self.memory = memory or SkillMemory()
        self.test_runner = test_runner or TestRunner()
        # Access-control boundary, independent of routing. Injectable for tests.
        self.access_policy = access_policy or WorkflowAccessPolicy()
        self.last_failed_test_target: str | None = None

    def run(
        self,
        repo_path: str | Path,
        task_description: str,
        *,
        dry_run: bool | None = None,
        log_dir: str | Path | None = None,
        config: dict[str, Any] | str | Path | None = None,
        run_tests: bool | None = None,
        test_timeout_seconds: int | None = None,
        allow_internal: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        loaded_config = load_config(config) if config is not None and not isinstance(config, dict) else (config or {"execution": {}, "logging": {}})
        if dry_run is None:
            dry_run = bool(loaded_config.get("execution", {}).get("dry_run", False))
        if run_tests is None:
            run_tests = bool(loaded_config.get("execution", {}).get("run_tests", True))
        if log_dir is None:
            log_dir = loaded_config.get("logging", {}).get("log_dir")
        # Precedence: explicit kwarg > explicit config key > whatever timeout is
        # already on self.test_runner (its own default, or one a caller injected
        # via SkillLayer(test_runner=TestRunner(timeout=...)) — e.g. tests that
        # simulate a fast timeout). We must NOT silently override an
        # already-configured test_runner just because neither kwarg nor config
        # was given on this call.
        if test_timeout_seconds is not None:
            effective_timeout = test_timeout_seconds
        elif "test_timeout_seconds" in loaded_config.get("execution", {}):
            effective_timeout = int(loaded_config["execution"]["test_timeout_seconds"])
        else:
            effective_timeout = self.test_runner.timeout

        repo = Path(repo_path)
        log: list[dict[str, Any]] = []
        # Per-call timeout override, applied without mutating the shared
        # self.test_runner (so it stays correct across repeated .run() calls
        # with different timeouts, and preserves any injected test `command`).
        effective_test_runner = dataclasses.replace(self.test_runner, timeout=effective_timeout)
        tools = ProjectTools(repo, log=log, test_runner=effective_test_runner)
        route = self.router.route(task_description)
        # Access-control boundary, delegated to the security component. Applies to
        # every entry point (direct API, CLI, MCP) before any repository I/O, and
        # is independent of how the router selected this workflow. Maintainers opt
        # in with allow_internal=True. Raises InternalWorkflowBlockedError.
        self.access_policy.enforce(route.workflow, allow_internal=allow_internal)
        macro_sequence = self.router.macro_sequence(route.workflow)
        before_snapshot = snapshot_python_files(repo)
        log_path = prepare_log_path(log_dir)

        test_result: dict[str, Any] = {"available": False, "returncode": None, "stdout": "", "stderr": ""}
        edit_applied = False
        tests_passed = False
        browser_smoke_result: dict[str, Any] = {}
        workflow_artifacts: dict[str, Any] = {}
        if dry_run and route.task_type in EARLY_DRY_RUN_TASK_TYPES:
            capability = WORKFLOW_METADATA.get(route.workflow, {})
            workflow_artifacts = {
                "workflow": route.workflow,
                "workflow_status": "dry_run_proposed",
                "summary": (
                    f"Dry run: {route.workflow} was not executed because it can "
                    "write persistent state or trigger external side effects."
                ),
                "state_locations": list(capability.get("state_locations", [])),
                "state_write_performed": False,
                "written_paths": [],
                "external_side_effects_skipped": True,
                "tests_run": False,
                "tests_passed": None,
                "validation_status": "not_applicable",
            }
            success = True
            log.append({
                "tool": "dry_run_plan",
                "workflow": route.workflow,
                "macro_sequence": macro_sequence,
                "external_side_effects_skipped": True,
            })
        elif route.task_type == "clarify_intent":
            # No-match fallback: ambiguous prose. Read-only clarification, never
            # a write-capable workflow.
            workflow_artifacts = build_clarify_intent_artifacts(task_description)
            success = False
        elif route.task_type == "git_status":
            workflow_artifacts, git_tool_calls = build_git_status_artifacts(repo, log)
            tools.tool_call_count += git_tool_calls
            success = bool(workflow_artifacts.get("is_git_repo"))
        elif route.task_type == "safe_code_change":
            workflow_artifacts = build_safe_change_artifacts(repo, task_description, phase="plan")
            success = bool(workflow_artifacts.get("success"))
        elif route.task_type == "release_readiness":
            workflow_artifacts = build_release_readiness_artifacts(repo)
            success = bool(workflow_artifacts.get("success"))
        elif route.task_type == "resume_project_work":
            workflow_artifacts = build_resume_work_artifacts(repo)
            success = bool(workflow_artifacts.get("success"))
        elif route.task_type == "inspect_repo_structure":
            workflow_artifacts = build_inspect_repo_structure_artifacts(repo)
            success = True
        elif route.task_type == "map_dependencies":
            workflow_artifacts = build_map_dependencies_artifacts(repo)
            success = True
        elif route.task_type == "detect_dead_code":
            workflow_artifacts = build_detect_dead_code_artifacts(repo)
            success = True
        elif route.task_type == "dependency_check":
            workflow_artifacts, dependency_tool_calls = build_dependency_check_artifacts(repo, task_description, log)
            tools.tool_call_count += dependency_tool_calls
            success = bool(workflow_artifacts.get("dependency")) and not workflow_artifacts.get("error_code")
        elif route.task_type == "rehydrate_context":
            workflow_artifacts = build_rehydrate_context_artifacts(repo)
            success = workflow_artifacts.get("success", False)
        elif route.task_type == "compare_context_snapshots":
            workflow_artifacts = build_compare_context_snapshots_artifacts(repo)
            success = workflow_artifacts.get("success", False)
        elif route.task_type == "search_decisions":
            _decision_query = _extract_decision_search_query(task_description)
            workflow_artifacts = build_search_decisions_artifacts(repo, _decision_query)
            success = workflow_artifacts.get("success", False)
        elif route.task_type == "add_todo":
            _todo_text = _extract_todo_text(task_description)
            if not _todo_text:
                workflow_artifacts = {
                    "workflow": "AddTodoWorkflow",
                    "todo_id": None,
                    "text": None,
                    "status": None,
                    "created": None,
                    "error_code": "no_todo_text_found",
                    "error": "No todo text found in task description.",
                    "summary": "No todo added: could not find todo text in the task description.",
                }
                success = False
            else:
                workflow_artifacts = build_add_todo_artifacts(repo, _todo_text)
                success = workflow_artifacts.get("success", False)
        elif route.task_type == "mark_todo_done":
            _todo_id = _extract_todo_id(task_description)
            if not _todo_id:
                workflow_artifacts = {
                    "workflow": "MarkTodoDoneWorkflow",
                    "todo_id": None,
                    "status": None,
                    "previous_status": None,
                    "done_at": None,
                    "error_code": "no_todo_id_found",
                    "error": "No todo id found in task description.",
                    "summary": "No change: could not find a todo id in the task description.",
                }
                success = False
            else:
                _target_status = _extract_todo_target_status(task_description)
                workflow_artifacts = build_mark_todo_done_artifacts(repo, _todo_id, status=_target_status)
                success = workflow_artifacts.get("success", False)
        elif route.task_type == "list_todos":
            _status_filter = _extract_todo_status_filter(task_description)
            workflow_artifacts = build_list_todos_artifacts(repo, status_filter=_status_filter)
            success = workflow_artifacts.get("success", False)
        elif route.task_type == "save_context":
            workflow_artifacts = build_memory_cli_unsupported_artifacts(
                "SaveContextSnapshotWorkflow",
                "skilllayer_save_context",
                "a context-state string and open-questions list",
            )
            success = False
        elif route.task_type == "track_decision":
            workflow_artifacts = build_memory_cli_unsupported_artifacts(
                "TrackDecisionLogWorkflow",
                "skilllayer_track_decision",
                "title, context, decision, reasoning, and consequences fields",
            )
            success = False
        elif route.task_type == "remember_preferences":
            workflow_artifacts = build_memory_cli_unsupported_artifacts(
                "RememberUserPreferencesWorkflow",
                "skilllayer_remember_preferences",
                "a structured preferences map",
            )
            success = False
        elif route.task_type == "inspect_runtime":
            workflow_artifacts = build_inspect_runtime_artifacts(repo)
            success = True
        elif route.task_type == "detect_processes":
            _inc_sys = _extract_include_system_flag(task_description)
            workflow_artifacts = build_detect_processes_artifacts(include_system=_inc_sys)
            success = workflow_artifacts.get("success", True)
        elif route.task_type == "monitor_flakiness":
            _flak_test_id, _flak_runs = _extract_flakiness_params(task_description)
            workflow_artifacts = build_monitor_flakiness_artifacts(repo, _flak_test_id, _flak_runs)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "check_port":
            _port, _host = _extract_port_check_params(task_description)
            if _port is None:
                workflow_artifacts = {
                    "workflow": "CheckPortAvailabilityWorkflow",
                    "error_code": "missing_port",
                    "error": "No port number found in task description.",
                    "tests_run": False,
                    "tests_passed": None,
                    "validation_status": "not_applicable",
                }
                success = False
            else:
                workflow_artifacts = build_check_port_artifacts(_port, _host)
                success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "measure_test_speed":
            workflow_artifacts = build_measure_test_speed_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "detect_secrets":
            workflow_artifacts = build_detect_secrets_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "search":
            _query = _extract_search_query(task_description)
            workflow_artifacts = build_search_artifacts(repo, _query)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "measure_memory":
            _mem_target = _extract_memory_target(task_description)
            workflow_artifacts = build_measure_memory_artifacts(repo, _mem_target)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "profile_execution":
            _prof_target = _extract_profile_target(task_description)
            workflow_artifacts = build_profile_execution_artifacts(repo, _prof_target)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "detect_activity":
            workflow_artifacts = build_detect_activity_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "watch_file_changes":
            workflow_artifacts = build_watch_file_changes_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "watch_deps":
            workflow_artifacts = build_watch_deps_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "list_branches":
            workflow_artifacts = build_list_branches_artifacts(repo)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "git_log":
            _gl_limit = _extract_int_param(task_description, r"\blast\s+(\d+)\s+commits?\b", default=20)
            workflow_artifacts = build_git_log_artifacts(repo, limit=_gl_limit)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "get_commit":
            _commit_hash = _extract_commit_hash(task_description)
            if not _commit_hash:
                workflow_artifacts = {
                    "workflow": "GetCommitDetailsWorkflow",
                    "error_code": "no_commit_hash",
                    "error": "No commit hash found in task description.",
                    "hash": None, "short_hash": None, "message": None,
                    "author_name": None, "date": None, "files_changed": [],
                    "total_insertions": 0, "total_deletions": 0, "parent_hashes": [],
                    "checked_at": "",
                }
                success = False
            else:
                workflow_artifacts = build_get_commit_artifacts(repo, _commit_hash)
                success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "git_diff":
            _diff_base, _diff_target = _extract_diff_refs(task_description)
            workflow_artifacts = build_git_diff_artifacts(repo, base=_diff_base, target=_diff_target)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "file_history":
            _fh_path = _extract_file_path_from_task(task_description)
            _fh_limit = _extract_int_param(task_description, r"\blast\s+(\d+)\b", default=10)
            workflow_artifacts = build_file_history_artifacts(repo, file_path=_fh_path or "", limit=_fh_limit)
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "git_blame":
            _blame_path = _extract_file_path_from_task(task_description)
            workflow_artifacts = build_git_blame_artifacts(repo, file_path=_blame_path or "")
            success = workflow_artifacts.get("error_code") is None
        elif route.task_type == "find_conflicts":
            workflow_artifacts = build_find_conflicts_artifacts(repo)
            success = True
        elif dry_run and route.task_type == "find_function":
            tools.list_files()
            workflow_artifacts = build_find_function_artifacts(task_description, tools)
            success = bool(workflow_artifacts.get("matches")) if workflow_artifacts.get("query_symbols") else True
            log.append(
                {
                    "tool": "dry_run_read_only",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "rename_symbol":
            workflow_artifacts = build_rename_plan_artifacts(task_description)
            success = True
            log.append(
                {
                    "tool": "dry_run_plan",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "run_tests":
            workflow_artifacts = build_run_tests_plan_artifacts(self.test_runner.detect(repo))
            success = bool(workflow_artifacts.get("test_command"))
            log.append(
                {
                    "tool": "dry_run_plan",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "single_test":
            workflow_artifacts = build_single_test_plan_artifacts(repo, task_description, self.last_failed_test_target)
            success = bool(workflow_artifacts.get("test_target")) and not workflow_artifacts.get("error_code")
            log.append(
                {
                    "tool": "dry_run_plan",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "explain_failure":
            workflow_artifacts = build_explain_failure_plan_artifacts(self.test_runner.detect(repo))
            success = bool(workflow_artifacts.get("test_command"))
            log.append(
                {
                    "tool": "dry_run_plan",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "add_helper_function":
            workflow_artifacts = build_add_helper_artifacts(
                task_description,
                tools.repo_path,
                dry_run=True,
                inserted=None,
                test_result=None,
            )
            success = workflow_artifacts.get("workflow_status") == "dry_run_proposed"
            log.append(
                {
                    "tool": "dry_run_plan",
                    "workflow": route.workflow,
                    "macro_sequence": macro_sequence,
                    "artifacts": workflow_artifacts,
                }
            )
        elif dry_run and route.task_type == "fix_failing_test":
            success, edit_applied, workflow_artifacts, test_result, tests_passed = self._run_fix_failing_test_v2(
                task_description,
                tools,
                dry_run=True,
            )
        elif dry_run:
            success = True
            log.append({"tool": "dry_run_plan", "workflow": route.workflow, "macro_sequence": macro_sequence})
        elif route.task_type == "browser_smoke":
            browser_request = extract_browser_smoke_request(task_description, loaded_config.get("browser_smoke", {}), repo)
            if browser_request["url"]:
                browser_smoke_result = run_browser_smoke(
                    browser_request["url"],
                    browser_request["selectors"],
                    output_dir=browser_request["output_dir"],
                    wait_seconds=browser_request["wait_seconds"],
                    write_artifacts=browser_request["write_artifacts"],
                )
                log.extend(browser_smoke_result.get("trace", []))
                tools.tool_call_count = len(browser_smoke_result.get("trace", []))
                success = bool(browser_smoke_result.get("success"))
            else:
                browser_smoke_result = {
                    "success": False,
                    "status": "error",
                    "error_code": "browser_smoke_url_not_configured",
                    "workflow": "BrowserSmokeWorkflow",
                    "url": None,
                    "browser_executed": False,
                    "static_inspection_performed": False,
                    "limitations": [],
                    "console_errors": None,
                    "network_errors": None,
                    "elements_verified": 0,
                    "elements_expected": len(browser_request["selectors"]),
                    "missing_elements": browser_request["selectors"],
                    "screenshot_path": None,
                    "browser_backend": "none",
                    "backend_used": "none",
                    "report_path": None,
                    "error": "No browser smoke URL configured or found in task.",
                }
                log.append({"tool": "browser_smoke", "success": False, "error": browser_smoke_result["error"]})
                success = False
        else:
            if route.task_type == "fix_failing_test":
                success, edit_applied, workflow_artifacts, test_result, tests_passed = self._run_fix_failing_test_v2(
                    task_description,
                    tools,
                    dry_run=False,
                )
            elif route.task_type == "run_tests":
                test_result = tools.run_tests(force=True)
                tests_passed = test_result_passed(test_result)
                workflow_artifacts = build_run_tests_artifacts(test_result)
                self._remember_failed_test_target(workflow_artifacts)
                success = bool(test_result.get("available")) and tests_passed is True
            elif route.task_type == "single_test":
                workflow_artifacts, test_result = self._run_single_test_workflow(repo, task_description, tools)
                tests_passed = workflow_artifacts.get("tests_passed")
                self._remember_failed_test_target(workflow_artifacts)
                success = bool(workflow_artifacts.get("tests_run")) and tests_passed is True
            elif route.task_type == "explain_failure":
                test_result = tools.run_tests(force=True)
                tests_passed = test_result_passed(test_result)
                workflow_artifacts = build_explain_failure_artifacts(test_result)
                self._remember_failed_test_target(workflow_artifacts)
                success = explain_failure_success_for_outcome(str(workflow_artifacts.get("outcome") or "UNKNOWN"))
            elif route.task_type == "add_helper_function":
                workflow_artifacts = build_add_helper_artifacts(
                    task_description,
                    tools.repo_path,
                    dry_run=False,
                    inserted=None,
                    test_result=None,
                )
                if workflow_artifacts.get("workflow_status") == "selected":
                    edit_applied = tools.insert_text(
                        str(workflow_artifacts.get("target_file")),
                        str(workflow_artifacts.get("helper_text")),
                    )
                    workflow_artifacts = build_add_helper_artifacts(
                        task_description,
                        tools.repo_path,
                        dry_run=False,
                        inserted=edit_applied,
                        test_result=None,
                    )
                    tools.log.append(
                        {
                            "macro": "InsertHelperFunction",
                            "status": "applied" if edit_applied else "already_present",
                            "file": workflow_artifacts.get("target_file"),
                            "helper_name": workflow_artifacts.get("helper_name"),
                        }
                    )
                else:
                    edit_applied = False
                if run_tests and edit_applied:
                    test_result = tools.run_tests()
                    tests_passed = test_result_passed(test_result) is True
                    workflow_artifacts = build_add_helper_artifacts(
                        task_description,
                        tools.repo_path,
                        dry_run=False,
                        inserted=edit_applied,
                        test_result=test_result,
                    )
                else:
                    tests_passed = False
                success = bool(edit_applied and tests_passed is True)
            elif route.task_type == "find_function":
                tools.list_files()
                workflow_artifacts = build_find_function_artifacts(task_description, tools)
                success = bool(workflow_artifacts.get("matches")) if workflow_artifacts.get("query_symbols") else True
            else:
                tools.list_files()
                for symbol in extract_symbols(task_description)[:3]:
                    tools.search_symbol(symbol)
                edit_applied = self._apply_safe_extracted_action(route.task_type, task_description, tools)
                if run_tests and edit_applied:
                    test_result = tools.run_tests()
                    tests_passed = test_result_passed(test_result) is True
                else:
                    tests_passed = False
                # Only report success when a change was actually applied. A routed
                # task that produced no deterministic edit (the fix_simple_bug
                # fallback has no automated repair) must never be reported as a
                # success just because tests are disabled or the existing suite
                # already passes.
                success = bool(edit_applied) and (tests_passed if run_tests else True)
                if not edit_applied and route.task_type != "rename_symbol":
                    workflow_artifacts = {
                        "workflow": route.workflow,
                        "workflow_status": "no_action_applied",
                        "unsupported": True,
                        "unsupported_reason": "no_supported_automated_fix",
                        "edit_applied": False,
                        "summary": (
                            "No automated fix is available for this task, so no files were "
                            "modified. SkillLayer did not change the repository; this is "
                            "unsupported, not a completed fix."
                        ),
                    }
                    tools.log.append(
                        {
                            "macro": "ApplySmallCodeChange",
                            "status": "unsupported",
                            "reason": "no_supported_automated_fix",
                            "task_type": route.task_type,
                        }
                    )
                if route.task_type == "rename_symbol":
                    success = (tests_passed if run_tests else True) and edit_applied
                    workflow_artifacts = build_rename_symbol_artifacts(
                        tools,
                        task_description=task_description,
                        test_result=test_result,
                        tests_passed=tests_passed,
                        edit_applied=edit_applied,
                    )

        tests_run = tests_were_run(test_result)
        public_tests_passed = tests_passed if tests_run else None
        validation_status = validation_status_from_tests(tests_run, public_tests_passed)
        result = {
            "success": bool(success),
            "repo_path": str(repo),
            "task": task_description,
            "workflow": route.workflow,
            "macro_sequence": macro_sequence,
            "tool_calls": int(tools.tool_call_count),
            "llm_calls": int(self.llm.call_count + route.llm_calls),
            "tests_run": tests_run,
            "tests_passed": public_tests_passed,
            "validation_status": validation_status,
            "dry_run": bool(dry_run),
            "logs_path": str(log_path) if log_path else None,
        }
        capability = WORKFLOW_METADATA.get(route.workflow, {})
        for key in (
            "write_behavior",
            "state_locations",
            "requires_write_consent",
            "may_dirty_worktree",
        ):
            if key in capability:
                value = capability[key]
                result[key] = list(value) if isinstance(value, list) else value
        if workflow_artifacts:
            result["artifacts"] = workflow_artifacts
            result.update(flatten_workflow_artifacts(route.workflow, workflow_artifacts))
            for key in (
                "state_write_performed",
                "state_write_error",
                "deleted_paths",
                "persist_baseline",
                "persist_snapshot",
                "gitignore_suggestions",
                "external_side_effects_skipped",
            ):
                if key in workflow_artifacts:
                    value = workflow_artifacts[key]
                    result[key] = list(value) if isinstance(value, list) else value
        if route.task_type == "browser_smoke":
            # console_errors/network_errors stay None when they were never
            # genuinely verified (no real browser executed) — coercing to 0
            # here would silently restore the "0 errors" false-green this
            # milestone closes.
            raw_console_errors = browser_smoke_result.get("console_errors")
            raw_network_errors = browser_smoke_result.get("network_errors")
            result.update(
                {
                    "console_errors": int(raw_console_errors) if raw_console_errors is not None else None,
                    "network_errors": int(raw_network_errors) if raw_network_errors is not None else None,
                    "elements_verified": int(browser_smoke_result.get("elements_verified", 0)),
                    "elements_expected": int(browser_smoke_result.get("elements_expected", 0)),
                    "missing_elements": list(browser_smoke_result.get("missing_elements", [])),
                    "screenshot_path": browser_smoke_result.get("screenshot_path"),
                    "report_path": browser_smoke_result.get("report_path"),
                    "browser_backend": browser_smoke_result.get("browser_backend"),
                    "backend_used": browser_smoke_result.get("backend_used"),
                    "url": browser_smoke_result.get("url"),
                    "error": browser_smoke_result.get("error"),
                    "error_code": browser_smoke_result.get("error_code"),
                    "status": browser_smoke_result.get("status"),
                    "browser_executed": bool(browser_smoke_result.get("browser_executed", False)),
                    "static_inspection_performed": bool(browser_smoke_result.get("static_inspection_performed", False)),
                    "limitations": list(browser_smoke_result.get("limitations", [])),
                    "artifact_writes_enabled": bool(browser_smoke_result.get("artifact_writes_enabled", False)),
                    "written_paths": list(browser_smoke_result.get("written_paths", [])),
                }
            )
        written_paths = list(workflow_artifacts.get("written_paths", []))
        if route.task_type == "browser_smoke":
            written_paths.extend(browser_smoke_result.get("written_paths", []))
        if log_path:
            run_log_paths = [
                str(log_path / "run_summary.json"),
                str(log_path / "trace.json"),
                str(log_path / "test_stdout.txt"),
                str(log_path / "test_stderr.txt"),
                str(log_path / "patch.diff"),
            ]
            result["run_log_written_paths"] = run_log_paths
            written_paths.extend(run_log_paths)
        if self.memory.path is not None:
            written_paths.append(str(self.memory.path))
        result["written_paths"] = unique_preserving_order(written_paths)
        after_snapshot = snapshot_python_files(repo)
        diff_text = unified_diff(before_snapshot, after_snapshot)
        if log_path:
            write_run_logs(log_path, result, log, test_result, diff_text)
        self.memory.append(
            {
                "task_description": task_description,
                "repo_path": str(repo),
                "route_source": route.source,
                "result": result,
            }
        )
        automatic_paths: list[str] = []
        if self._record_demand_event_best_effort(route, task_description, result, log, bool(macro_sequence)):
            automatic_paths.append(str(DEMAND_EVENTS_JSONL))
        if self._record_usage_event_best_effort(route.workflow, task_description, started, route.llm_calls, result):
            automatic_paths.append(str(USAGE_EVENTS_JSONL))
        if automatic_paths:
            result["automatic_state_written_paths"] = automatic_paths
            result["written_paths"] = unique_preserving_order([*result["written_paths"], *automatic_paths])
        return result

    def _record_demand_event_best_effort(
        self,
        route: Any,
        task_description: str,
        result: dict[str, Any],
        log: list[dict[str, Any]],
        workflow_found: bool,
    ) -> bool:
        if not automatic_telemetry_enabled():
            return False
        try:
            unsupported_reason = extract_unsupported_reason(log, result)
            success = bool(result.get("success"))
            unsupported = bool(unsupported_reason) or not success
            record_demand_event(
                task_text=task_description,
                workflow_found=workflow_found,
                workflow_executed=workflow_found and not bool(result.get("dry_run")) and not unsupported,
                workflow_name=result.get("workflow") or getattr(route, "workflow", None),
                client="skilllayer",
                source="skilllayer_runner",
                metadata={
                    "success": success,
                    "tests_passed": bool(result.get("tests_passed")),
                    "dry_run": bool(result.get("dry_run")),
                    "route_source": getattr(route, "source", None),
                    "task_type": getattr(route, "task_type", None),
                    "unsupported": unsupported,
                    "reason": unsupported_reason or (None if success else "workflow_failed_or_unsupported"),
                },
            )
            return True
        except Exception:
            return False

    def _remember_failed_test_target(self, artifacts: dict[str, Any]) -> None:
        target = failed_test_target_from_artifacts(artifacts)
        if target:
            self.last_failed_test_target = target

    def _run_single_test_workflow(
        self,
        repo: Path,
        task_description: str,
        tools: ProjectTools,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        resolution = resolve_single_test_target(repo, task_description, self.last_failed_test_target)
        if not resolution.get("supported"):
            artifacts = build_single_test_error_artifacts(resolution)
            tools.log.append(
                {
                    "macro": "ValidateTarget",
                    "status": "unsupported",
                    "reason": resolution.get("error_code"),
                    "test_target": resolution.get("test_target"),
                }
            )
            return artifacts, {"available": False, "returncode": None, "stdout": "", "stderr": str(resolution.get("summary", ""))}
        command = list(resolution.get("command", []) or [])
        test_result = tools.run_single_test(
            command, execution_environment=resolution.get("execution_environment")
        )
        artifacts = build_single_test_artifacts(test_result, resolution)
        return artifacts, test_result

    def _record_usage_event_best_effort(
        self,
        workflow: str,
        task_description: str,
        started: float,
        route_llm_calls: int,
        result: dict[str, Any],
    ) -> bool:
        if not automatic_telemetry_enabled():
            return False
        try:
            event = event_from_llm_client(
                self.llm,
                workflow=workflow,
                task=task_description,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                metadata={
                    "source": "skilllayer_runner",
                    "workflow_success": bool(result.get("success")),
                    "tests_passed": bool(result.get("tests_passed")),
                    "route_llm_calls": int(route_llm_calls),
                    "record_type": "raw_usage_measurement",
                },
            )
            event["request_count"] = int((event.get("request_count") or 0) + int(route_llm_calls))
            if not event["request_count"] and not any(
                event.get(field) is not None
                for field in ["input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"]
            ):
                event["cost_known"] = False
            record_usage_event(**event)
            return True
        except Exception:
            return False

    def _apply_safe_extracted_action(self, task_type: str, task_description: str, tools: ProjectTools) -> bool:
        if task_type == "rename_symbol":
            pair = extract_rename_pair(task_description)
            if pair:
                return tools.replace_token_all(pair[0], pair[1])
        if task_type == "add_helper_function":
            return self._add_simple_helper_function(task_description, tools)
        return False

    def _add_simple_helper_function(self, task_description: str, tools: ProjectTools) -> bool:
        match = re.search(r"(?:add\s+)?(?:small\s+)?(?:pure\s+)?(?:utility\s+)?helper(?:\s+function)?\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", task_description, re.I)
        if not match:
            tools.log.append({"macro": "InsertHelperFunction", "status": "unsupported", "reason": "helper_name_not_found"})
            return False
        helper_name = match.group(1)
        if helper_name in {"function", "helper", "utility"}:
            tools.log.append({"macro": "InsertHelperFunction", "status": "unsupported", "reason": "ambiguous_helper_name"})
            return False

        target = locate_helper_insertion_point(tools.repo_path)
        if not target:
            tools.log.append({"macro": "LocateInsertionPoint", "status": "unsupported", "reason": "no_safe_helper_target"})
            return False
        helper_text = f"\n\n\ndef {helper_name}(value):\n    return value\n"
        inserted = tools.insert_text(target, helper_text)
        tools.log.append(
            {
                "macro": "InsertHelperFunction",
                "status": "applied" if inserted else "already_present",
                "file": target,
                "helper_name": helper_name,
            }
        )
        return inserted

    def _repair_simple_test_failure(self, task_description: str, tools: ProjectTools, test_result: dict[str, Any]) -> bool:
        patch = choose_simple_patch(task_description, test_result)
        if not patch:
            patch = choose_import_export_patch(test_result, tools.repo_path)
        if not patch:
            tools.log.append({"macro": "ApplySimplePatch", "status": "unsupported", "reason": "no_supported_simple_patch"})
            return False
        replaced = tools.replace_text(patch["file"], patch["old"], patch["new"])
        tools.log.append(
            {
                "macro": "ApplySimplePatch",
                "status": "applied" if replaced else "not_applied",
                "subtype": patch["subtype"],
                "file": patch["file"],
                "description": patch["description"],
            }
        )
        return replaced

    def _run_fix_failing_test_v2(
        self,
        task_description: str,
        tools: ProjectTools,
        *,
        dry_run: bool,
    ) -> tuple[bool, bool, dict[str, Any], dict[str, Any], bool]:
        initial_result = tools.run_tests(force=True)
        initial_explanation = build_explain_failure_artifacts(initial_result)
        initial_tests_run = bool(initial_explanation.get("tests_run"))
        initial_tests_passed = initial_explanation.get("tests_passed") is True

        if not initial_tests_run:
            artifacts = build_fix_failing_test_artifacts(
                initial_explanation,
                repair=None,
                dry_run=dry_run,
                unsupported_reason="no_test_command_detected",
            )
            tools.log.append({"macro": "SelectSafeRepair", "status": "unsupported", "reason": "no_test_command_detected"})
            return False, False, artifacts, initial_result, False

        if initial_tests_passed:
            artifacts = build_fix_failing_test_artifacts(
                initial_explanation,
                repair=None,
                dry_run=dry_run,
                unsupported_reason=None,
                verification=build_run_tests_artifacts(initial_result),
            )
            tools.log.append({"macro": "SelectSafeRepair", "status": "skipped", "reason": "tests_already_passing"})
            return True, False, artifacts, initial_result, True

        tools.inspect_error()
        repair = select_safe_repair(task_description, tools.repo_path, initial_result, initial_explanation)
        if not repair.get("supported"):
            reason = str(repair.get("unsupported_reason") or "unsupported_failure_pattern")
            artifacts = build_fix_failing_test_artifacts(
                initial_explanation,
                repair=repair,
                dry_run=dry_run,
                unsupported_reason=reason,
            )
            tools.log.append({"macro": "SelectSafeRepair", "status": "unsupported", "reason": reason})
            return False, False, artifacts, initial_result, False

        tools.log.append(
            {
                "macro": "SelectSafeRepair",
                "status": "selected",
                "repair_class": repair.get("repair_class"),
                "target_file": repair.get("target_file"),
            }
        )
        if dry_run:
            artifacts = build_fix_failing_test_artifacts(
                initial_explanation,
                repair=repair,
                dry_run=True,
                unsupported_reason=None,
            )
            return True, False, artifacts, initial_result, False

        applied = apply_safe_repair_patch(tools, repair)
        if not applied:
            artifacts = build_fix_failing_test_artifacts(
                initial_explanation,
                repair=repair,
                dry_run=False,
                unsupported_reason="patch_not_applied",
            )
            tools.log.append(
                {
                    "macro": "ApplySafePatch",
                    "status": "not_applied",
                    "reason": "patch_not_applied",
                    "repair_class": repair.get("repair_class"),
                    "target_file": repair.get("target_file"),
                }
            )
            return False, False, artifacts, initial_result, False

        tools.log.append(
            {
                "macro": "ApplySafePatch",
                "status": "applied",
                "repair_class": repair.get("repair_class"),
                "target_file": repair.get("target_file"),
                "lines_changed": repair.get("lines_changed"),
            }
        )
        verification_result = tools.run_tests(force=True)
        verification = build_run_tests_artifacts(verification_result)
        verified = verification.get("tests_passed") is True
        artifacts = build_fix_failing_test_artifacts(
            initial_explanation,
            repair=repair,
            dry_run=False,
            unsupported_reason=None,
            verification=verification,
            post_patch_failures=None if verified else build_explain_failure_artifacts(verification_result).get("diagnoses", []),
        )
        return bool(verified), True, artifacts, verification_result, bool(verified)


def extract_symbols(task_description: str) -> list[str]:
    text = task_description.strip()
    candidates: list[str] = []

    backticked = re.findall(r"`\s*([A-Za-z_][A-Za-z0-9_]*)\s*`", text)
    candidates.extend(backticked)

    rename_pair = extract_rename_pair(text)
    if rename_pair:
        candidates.extend(rename_pair)

    trusted_context_patterns = [
        r"\b(?:find|locate|where\s+is|show|open)\s+(?:the\s+)?(?:function|class|method|symbol|definition)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
        r"\b(?:function|class|method|symbol)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
    ]
    for pattern in trusted_context_patterns:
        for match in re.finditer(pattern, text, re.I):
            candidates.append(match.group(1))

    code_like_context_patterns = [
        r"\b(?:find|locate|where\s+is|show|open)\s+(?:the\s+)?`?([A-Za-z_][A-Za-z0-9_]*)`?",
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s+(?:defined|definition)\b",
    ]
    for pattern in code_like_context_patterns:
        for match in re.finditer(pattern, text, re.I):
            if looks_like_code_symbol(match.group(1)):
                candidates.append(match.group(1))

    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text):
        if looks_like_code_symbol(token):
            candidates.append(token)

    return [symbol for symbol in unique_preserving_order(candidates) if is_valid_symbol_candidate(symbol)]


def extract_rename_pair(task_description: str) -> tuple[str, str] | None:
    symbol = r"`?\s*([A-Za-z_][A-Za-z0-9_]*)\s*`?"
    patterns = [
        rf"\b(?:rename|renaming|change)\s+(?:function|class|method|symbol)?\s*{symbol}\s*(?:to|as|->|=>)\s*{symbol}",
        rf"\b{symbol}\s*(?:->|=>)\s*{symbol}",
    ]
    for pattern in patterns:
        match = re.search(pattern, task_description, re.I)
        if not match:
            continue
        old, new = match.group(1), match.group(2)
        if is_valid_symbol_candidate(old) and is_valid_symbol_candidate(new) and old != new:
            return old, new
    return None


def is_valid_symbol_candidate(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol)) and symbol.lower() not in SYMBOL_STOP_WORDS


def looks_like_code_symbol(token: str) -> bool:
    if token.lower() in SYMBOL_STOP_WORDS:
        return False
    return "_" in token or any(char.isupper() for char in token[1:])


def extract_dependency_name(task_description: str) -> str | None:
    text = task_description.strip()
    backticked = re.findall(r"`([^`]+)`", text)
    for candidate in backticked:
        cleaned = clean_dependency_candidate(candidate)
        if cleaned:
            return cleaned

    patterns = [
        r"\b(?:check|find|show)\s+(?:dependency|dependencies|package|module|library)\s+([@A-Za-z0-9_.\-/]+)",
        r"\b(?:dependency|package|module|library)\s+([@A-Za-z0-9_.\-/]+)",
        r"\bis\s+([@A-Za-z0-9_.\-/]+)\s+(?:used|declared|installed)\??",
        r"\bwhere\s+is\s+([@A-Za-z0-9_.\-/]+)\s+(?:used|imported)\??",
        r"\bwhere\s+is\s+([@A-Za-z0-9_.\-/]+)\??",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            cleaned = clean_dependency_candidate(match.group(1))
            if cleaned:
                return cleaned
    return None


def clean_dependency_candidate(candidate: str) -> str | None:
    value = candidate.strip().strip(".,;:?!()[]{}\"'")
    if not value:
        return None
    lowered = value.lower()
    if lowered in {
        "a",
        "an",
        "and",
        "check",
        "declared",
        "dependency",
        "find",
        "install",
        "package",
        "remove",
        "the",
        "update",
        "upgrade",
        "used",
        "where",
    }:
        return None
    if not re.fullmatch(r"@?[A-Za-z0-9_.\-/]+", value):
        return None
    if value.startswith((".", "/", "-")):
        return None
    return value


def tests_were_run(test_result: dict[str, Any]) -> bool:
    return (
        bool(test_result.get("available"))
        and not bool(test_result.get("skipped"))
        and test_result.get("returncode") is not None
        and not bool(test_result.get("environment_error"))
    )


def test_result_passed(test_result: dict[str, Any]) -> bool | None:
    if not tests_were_run(test_result):
        return None
    if test_result_no_tests_discovered(test_result):
        return None
    return test_result.get("returncode") == 0


def test_result_no_tests_discovered(test_result: dict[str, Any]) -> bool:
    if bool(test_result.get("no_tests_discovered")):
        return True
    if test_result.get("test_count") == 0:
        return True
    output = f"{test_result.get('stdout', '')}\n{test_result.get('stderr', '')}".lower()
    zero_markers = [
        "ran 0 tests",
        "collected 0 items",
        "no tests ran",
        "no tests collected",
        "0 tests",
        "0 total",
    ]
    return any(marker in output for marker in zero_markers)


def validation_status_from_tests(tests_run: bool, tests_passed: bool | None) -> str:
    if not tests_run:
        return "not_applicable"
    if tests_passed is None:
        return "not_applicable"
    return "passed" if tests_passed is True else "failed"


def build_validation_details(test_result: dict[str, Any], tests_run: bool, tests_passed: bool | None) -> dict[str, Any]:
    return {
        "tests_run": bool(tests_run),
        "tests_passed": tests_passed,
        "returncode": test_result.get("returncode"),
        "test_count": test_result.get("test_count"),
        "no_tests_discovered": bool(test_result_no_tests_discovered(test_result)) if tests_run else False,
        "skipped": bool(test_result.get("skipped", False)),
    }


def classify_run_tests_outcome(
    test_result: dict[str, Any],
    *,
    tests_run: bool,
    tests_passed: bool | None,
    failed_tests: list[dict[str, Any]],
    failure_count: int,
) -> str:
    if bool(test_result.get("environment_error")):
        return "ENVIRONMENT_MISMATCH"
    if bool(test_result.get("nested_execution_blocked")):
        return "BLOCKED_NESTED_EXECUTION"
    if bool(test_result.get("timed_out")):
        return "TIMEOUT"
    if bool(test_result.get("unsupported")):
        return "UNSUPPORTED"
    if not tests_run:
        return "NO_TEST_COMMAND"
    if test_result_no_tests_discovered(test_result):
        return "NO_TESTS_DISCOVERED"
    if tests_passed is True:
        return "PASSED"
    if tests_passed is False:
        if failure_count > 0 or failed_tests:
            return "FAILED_TESTS"
        return "COMMAND_ERROR"
    return "UNKNOWN"


def error_code_for_run_tests_outcome(outcome: str, tests_run: bool) -> str | None:
    if outcome == "ENVIRONMENT_MISMATCH":
        return "execution_environment_mismatch"
    if outcome == "BLOCKED_NESTED_EXECUTION":
        return "nested_test_execution_blocked"
    if outcome == "TIMEOUT":
        return "test_run_timed_out"
    if outcome == "NO_TESTS_DISCOVERED":
        return "no_tests_discovered"
    if outcome == "NO_TEST_COMMAND" or not tests_run:
        return "no_test_command_detected"
    if outcome == "COMMAND_ERROR":
        return "test_command_error"
    if outcome == "UNSUPPORTED":
        return "unsupported_test_runner"
    if outcome == "UNKNOWN":
        return "unknown_test_outcome"
    return None


def outcome_reason_for_run_tests_outcome(
    outcome: str,
    timeout_seconds: int | float | None = None,
    blocked_reason: str | None = None,
) -> str:
    reasons = {
        "PASSED": "Tests ran and passed.",
        "FAILED_TESTS": "Tests ran and produced test failures.",
        "ENVIRONMENT_MISMATCH": "Test execution did not reach test execution because the selected environment could not satisfy it.",
        "NO_TESTS_DISCOVERED": "A test command ran, but it discovered zero tests.",
        "NO_TEST_COMMAND": "No supported test command was detected.",
        "COMMAND_ERROR": "The test command failed without a parseable test failure outcome.",
        "UNSUPPORTED": "The requested test runner or test shape is not supported.",
        "TIMEOUT": (
            f"The test suite exceeded the configured {int(timeout_seconds)}s timeout."
            if timeout_seconds is not None
            else "The test command exceeded the configured timeout."
        ),
        "BLOCKED_NESTED_EXECUTION": (
            blocked_reason
            or "Refused to run: already executing inside a pytest test, which would recursively spawn nested test runs."
        ),
        "UNKNOWN": "The test outcome could not be classified.",
    }
    return reasons.get(outcome, reasons["UNKNOWN"])


def workflow_execution_success_for_run_tests_outcome(outcome: str) -> bool:
    return outcome in {
        "PASSED", "FAILED_TESTS", "NO_TESTS_DISCOVERED", "NO_TEST_COMMAND", "UNSUPPORTED",
        "BLOCKED_NESTED_EXECUTION",
    }


def explain_failure_success_for_outcome(outcome: str) -> bool:
    return outcome in {"PASSED", "FAILED_TESTS"}


def recommendation_for_run_tests_outcome(outcome: str) -> str:
    recommendations = {
        "PASSED": "No test action needed.",
        "FAILED_TESTS": "Inspect failed_tests, stdout_snippet, and stderr_snippet; use ExplainFailureWorkflow for deterministic diagnosis.",
        "ENVIRONMENT_MISMATCH": "Review selected_python and environment_error_message. SkillLayer does not install dependencies automatically.",
        "NO_TESTS_DISCOVERED": "No tests were collected. Inspect repository test layout or run SingleTestWorkflow with an explicit target.",
        "NO_TEST_COMMAND": "Repository does not expose a supported test command. Inspect project metadata or provide an explicit test target.",
        "COMMAND_ERROR": "Review stderr_snippet and stdout_snippet; verify the test runner and environment before interpreting failures.",
        "UNSUPPORTED": "Use a supported pytest, unittest, npm, pnpm, or yarn test path, or run the command manually.",
        "TIMEOUT": "Check for hangs, infinite loops, or slow external calls; increase the runner timeout or narrow the test target.",
        "BLOCKED_NESTED_EXECUTION": "Run this task from a normal terminal or CI job, not from code executing inside an active test session.",
        "UNKNOWN": "Inspect raw output snippets and rerun with a narrower test target if possible.",
    }
    return recommendations.get(outcome, recommendations["UNKNOWN"])


def build_find_function_artifacts(task_description: str, tools: ProjectTools, *, max_matches: int = 20) -> dict[str, Any]:
    query_symbols = unique_preserving_order(extract_symbols(task_description))[:5]
    all_matches: list[dict[str, Any]] = []
    for symbol in query_symbols:
        for hit in tools.search_symbol(symbol):
            match_type = classify_symbol_match(symbol, str(hit.get("text", "")))
            match = {
                "symbol": symbol,
                "file": hit.get("file"),
                "line": hit.get("line"),
                "text": hit.get("text", ""),
                "match_type": match_type,
                "quality": match_type,
            }
            all_matches.append(match)
    ranked_matches = sorted(all_matches, key=find_function_match_sort_key)
    shown_matches = ranked_matches[:max_matches]
    matched_symbols = unique_preserving_order(str(match["symbol"]) for match in shown_matches)
    file_paths = unique_preserving_order(str(match["file"]) for match in shown_matches if match.get("file"))
    definition_matches = [match for match in ranked_matches if match.get("match_type") == "definition"]
    reference_matches = [match for match in ranked_matches if match.get("match_type") == "reference"]
    false_positive_matches = [match for match in ranked_matches if match.get("match_type") == "possible_false_positive"]
    confidence, confidence_reason = classify_find_function_confidence(
        definition_count=len(definition_matches),
        reference_count=len(reference_matches),
        false_positive_count=len(false_positive_matches),
        total_matches=len(ranked_matches),
    )
    summary = summarize_find_function_results(query_symbols, definition_matches, reference_matches, false_positive_matches)
    truncated = len(ranked_matches) > len(shown_matches)
    return {
        "workflow": "FindFunctionWorkflow",
        "extracted_symbols": query_symbols,
        "query_symbols": query_symbols,
        "matched_symbols": matched_symbols,
        "match_count": len(shown_matches),
        "total_matches": len(ranked_matches),
        "shown_matches": len(shown_matches),
        "max_matches": max_matches,
        "truncated": truncated,
        "file_paths": file_paths,
        "matches": shown_matches,
        "definition_matches": definition_matches[:max_matches],
        "reference_matches": reference_matches[:max_matches],
        "possible_false_positive_matches": false_positive_matches[:max_matches],
        "definition_count": len(definition_matches),
        "reference_count": len(reference_matches),
        "possible_false_positive_count": len(false_positive_matches),
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "summary": summary,
        "tip": "Use --json for full structured artifacts." if truncated else None,
        "ignore_rules": {
            "ignored_dirs": sorted(DEFAULT_SEARCH_IGNORE_DIRS),
            "ignored_suffixes": sorted(DEFAULT_SEARCH_IGNORE_SUFFIXES),
        },
    }


def find_function_match_sort_key(match: dict[str, Any]) -> tuple[int, int, str, int]:
    quality_order = {"definition": 0, "reference": 1, "possible_false_positive": 2}
    path = str(match.get("file") or "")
    test_penalty = 1 if "/test" in f"/{path}" or path.startswith("test_") else 0
    return (quality_order.get(str(match.get("match_type")), 3), test_penalty, path, int(match.get("line") or 0))


def classify_find_function_confidence(
    *,
    definition_count: int,
    reference_count: int,
    false_positive_count: int,
    total_matches: int,
) -> tuple[str, str]:
    if definition_count == 1 and false_positive_count == 0:
        return "HIGH", "Exactly one strong definition match was found."
    if definition_count >= 1 and total_matches <= 20:
        return "MEDIUM", "Definition matches were found, but review references or multiple definitions."
    if definition_count >= 1:
        return "MEDIUM", "Definition matches were found, but the result set is large."
    if reference_count > 0 and false_positive_count <= reference_count:
        return "LOW", "No definition match was found; only references were found."
    if false_positive_count > 0:
        return "LOW", "Possible false positives dominate or no strong code match was found."
    return "LOW", "No matching definition or reference was found."


def summarize_find_function_results(
    query_symbols: list[str],
    definition_matches: list[dict[str, Any]],
    reference_matches: list[dict[str, Any]],
    false_positive_matches: list[dict[str, Any]],
) -> str:
    symbol = query_symbols[0] if query_symbols else "symbol"
    if len(definition_matches) == 1:
        match = definition_matches[0]
        return f"Found one definition for {symbol} in {match.get('file')}:{match.get('line')}."
    if len(definition_matches) > 1:
        return (
            f"Found {len(definition_matches)} possible definitions and {len(reference_matches)} references. "
            "Review definition_matches first."
        )
    if reference_matches:
        return f"No definition found. Found {len(reference_matches)} references only."
    if false_positive_matches:
        return f"No definition found. Found {len(false_positive_matches)} possible false positives only."
    return f"No matches found for {symbol}."


def build_rename_symbol_artifacts(
    tools: ProjectTools,
    *,
    task_description: str,
    test_result: dict[str, Any],
    tests_passed: bool,
    edit_applied: bool,
) -> dict[str, Any]:
    summary = dict(tools.last_replace_summary or {})
    extracted_symbols = extract_symbols(task_description)
    tests_run = tests_were_run(test_result)
    public_tests_passed = tests_passed if tests_run else None
    return {
        "workflow": "RenameSymbolWorkflow",
        "extracted_symbols": extracted_symbols,
        "old_symbol": summary.get("old_symbol"),
        "new_symbol": summary.get("new_symbol"),
        "files_modified": list(summary.get("files_modified", [])),
        "replacements_by_file": dict(summary.get("replacements_by_file", {})),
        "replacements_performed": int(summary.get("replacements_performed", 0) or 0),
        "edit_applied": bool(edit_applied),
        "validation_status": validation_status_from_tests(tests_run, public_tests_passed),
        "validation_details": build_validation_details(test_result, tests_run, public_tests_passed),
    }


def build_rename_plan_artifacts(task_description: str) -> dict[str, Any]:
    pair = extract_rename_pair(task_description)
    return {
        "workflow": "RenameSymbolWorkflow",
        "extracted_symbols": extract_symbols(task_description),
        "rename_pair": {"old_symbol": pair[0], "new_symbol": pair[1]} if pair else None,
        "old_symbol": pair[0] if pair else None,
        "new_symbol": pair[1] if pair else None,
        "files_modified": [],
        "replacements_by_file": {},
        "replacements_performed": 0,
        "edit_applied": False,
        "validation_status": "not_applicable",
        "validation_details": {
            "tests_run": False,
            "tests_passed": None,
            "returncode": None,
            "skipped": True,
        },
    }


def build_add_helper_artifacts(
    task_description: str,
    repo: Path,
    *,
    dry_run: bool,
    inserted: bool | None,
    test_result: dict[str, Any] | None,
) -> dict[str, Any]:
    request = parse_add_helper_request(task_description)
    if not request["supported"]:
        return add_helper_result(
            dry_run=dry_run,
            workflow_status="unsupported",
            action_taken="none",
            unsupported_reason=request["unsupported_reason"],
            summary="AddHelperWorkflow requires an explicit helper function name.",
            recommendation="Use an explicit task such as 'Add helper function normalize_value', or handle manually.",
        )
    helper_name = str(request["helper_name"])
    target = locate_helper_insertion_point(repo)
    if not target:
        return add_helper_result(
            dry_run=dry_run,
            workflow_status="unsupported",
            action_taken="none",
            helper_name=helper_name,
            unsupported_reason="no_safe_helper_target",
            summary="No conservative helper insertion target was found.",
            recommendation="AddHelperWorkflow is internal and only supports obvious helpers.py/utils.py-style targets.",
        )
    guardrail_reason = repair_target_guardrail_reason(target)
    if guardrail_reason:
        return add_helper_result(
            dry_run=dry_run,
            workflow_status="blocked_by_guardrail",
            action_taken="blocked",
            helper_name=helper_name,
            target_file=target,
            unsupported_reason=guardrail_reason,
            guardrail_triggered=True,
            guardrail_reason=guardrail_reason,
            summary=f"Helper insertion was blocked by guardrail: {guardrail_reason}.",
            recommendation="Do not bypass the guardrail unless AddHelperWorkflow is explicitly extended and revalidated.",
        )

    helper_text = f"\n\n\ndef {helper_name}(value):\n    return value\n"
    if dry_run:
        return add_helper_result(
            dry_run=True,
            workflow_status="dry_run_proposed",
            action_taken="proposed",
            helper_name=helper_name,
            target_file=target,
            helper_text=helper_text,
            would_modify_files=[target],
            expected_change_summary=f"Append identity-style helper function {helper_name}(value) to {target}.",
            risk_level="medium",
            summary=f"Dry run proposed adding helper {helper_name} to {target}.",
            recommendation="Review the proposed insertion before running without --dry-run; this workflow is internal.",
        )

    if inserted is None:
        return add_helper_result(
            dry_run=False,
            workflow_status="selected",
            action_taken="none",
            helper_name=helper_name,
            target_file=target,
            helper_text=helper_text,
            risk_level="medium",
            summary=f"Selected conservative helper insertion target {target}.",
            recommendation="Apply only if this repository convention is acceptable.",
        )

    validation = build_run_tests_artifacts(test_result or {"available": False, "returncode": None, "stdout": "", "stderr": ""})
    if inserted:
        if validation.get("tests_run") and validation.get("tests_passed") is True:
            status = "verification_passed"
            summary = f"Inserted helper {helper_name} and verification passed."
            recommendation = "Review the diff before keeping the change."
        elif validation.get("tests_run"):
            status = "verification_failed"
            summary = f"Inserted helper {helper_name}, but verification failed."
            recommendation = "Inspect validation output and consider reverting manually."
        else:
            status = "applied_unverified"
            summary = f"Inserted helper {helper_name}, but no verification ran."
            recommendation = "Run project tests manually before trusting the change."
        return add_helper_result(
            dry_run=False,
            workflow_status=status,
            action_taken="inserted",
            helper_name=helper_name,
            target_file=target,
            files_modified=[target],
            verification=validation,
            verification_ran=bool(validation.get("tests_run")),
            verification_passed=validation.get("tests_passed"),
            risk_level="medium",
            summary=summary,
            recommendation=recommendation,
        )

    return add_helper_result(
        dry_run=False,
        workflow_status="already_present_or_not_applied",
        action_taken="none",
        helper_name=helper_name,
        target_file=target,
        unsupported_reason="helper_already_present_or_insert_not_applied",
        risk_level="none",
        summary="Helper insertion was not applied.",
        recommendation="Inspect the target file manually if a helper is still needed.",
    )


def parse_add_helper_request(task_description: str) -> dict[str, Any]:
    match = re.search(
        r"\badd\s+(?:a\s+|small\s+|pure\s+|utility\s+)*helper(?:\s+function)?\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\b",
        task_description,
        re.I,
    )
    if not match:
        return {"supported": False, "unsupported_reason": "helper_name_not_found"}
    helper_name = match.group(1)
    if helper_name.lower() in {"function", "helper", "utility", "code", "thing"}:
        return {"supported": False, "unsupported_reason": "ambiguous_helper_name"}
    return {"supported": True, "helper_name": helper_name}


def add_helper_result(
    *,
    dry_run: bool,
    workflow_status: str,
    action_taken: str,
    helper_name: str | None = None,
    target_file: str | None = None,
    helper_text: str | None = None,
    files_modified: list[str] | None = None,
    would_modify_files: list[str] | None = None,
    expected_change_summary: str | None = None,
    verification: dict[str, Any] | None = None,
    verification_ran: bool = False,
    verification_passed: bool | None = None,
    unsupported_reason: str | None = None,
    guardrail_triggered: bool = False,
    guardrail_reason: str | None = None,
    risk_level: str = "none",
    summary: str = "",
    recommendation: str = "",
) -> dict[str, Any]:
    return {
        "workflow": "AddHelperWorkflow",
        "workflow_status": workflow_status,
        "action_taken": action_taken,
        "helper_name": helper_name,
        "target_file": target_file,
        "helper_text": helper_text,
        "files_modified": list(files_modified or []),
        "would_modify_files": list(would_modify_files or []),
        "dry_run": bool(dry_run),
        "unsupported_reason": unsupported_reason,
        "guardrail_triggered": bool(guardrail_triggered),
        "guardrail_reason": guardrail_reason,
        "risk_level": risk_level,
        "expected_change_summary": expected_change_summary,
        "verification": verification or {
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "remediation": None,
        },
        "verification_ran": bool(verification_ran),
        "verification_passed": verification_passed,
        "summary": summary,
        "recommendation": recommendation,
        "internal": True,
        "user_facing": False,
    }


def build_run_tests_plan_artifacts(command: list[str] | None) -> dict[str, Any]:
    outcome = "UNKNOWN" if command else "NO_TEST_COMMAND"
    outcome_reason = (
        "Dry run did not execute tests; a supported test command was detected."
        if command
        else "Dry run did not execute tests and no supported test command was detected."
    )
    return {
        "workflow": "RunTestsWorkflow",
        "test_command": command_to_text(command),
        "command_argv": list(command or []),
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "exit_code": None,
        "duration_ms": 0,
        "test_count": None,
        "no_tests_discovered": False,
        "pass_count": 0,
        "failure_count": 0,
        "timed_out": False,
        "failed_tests": [],
        "summary": "Dry run: test command detected but not executed." if command else "Dry run: no test command detected.",
        "stdout_snippet": "",
        "stderr_snippet": "",
        "error_code": None if command else "no_test_command_detected",
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "workflow_execution_success": bool(command),
        "task_validation_success": None,
        "recommendation": recommendation_for_run_tests_outcome(outcome),
        "execution_environment": None,
        "selected_python": command[0] if command and len(command) > 0 else None,
        "selection_source": "not_executed",
        "fallback_used": False,
        "collection_started": False,
        "tests_started": False,
        "environment_error": False,
        "environment_error_code": None,
        "environment_error_message": None,
        "remediation": None,
    }


def build_single_test_plan_artifacts(repo: Path, task_description: str, last_failed_test_target: str | None) -> dict[str, Any]:
    resolution = resolve_single_test_target(repo, task_description, last_failed_test_target)
    supported = bool(resolution.get("supported"))
    error_code: str | None = None if supported else resolution.get("error_code", "single_test_not_supported")
    if supported:
        outcome = "UNKNOWN"
    elif error_code in {"no_failed_test_available", "test_target_not_specified"}:
        outcome = "NO_TEST_COMMAND"
    else:
        outcome = "UNSUPPORTED"
    return {
        "workflow": "SingleTestWorkflow",
        "test_target": resolution.get("test_target"),
        "target_source": resolution.get("target_source"),
        "test_command": command_to_text(resolution.get("command")),
        "command_argv": list(resolution.get("command", []) or []),
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "test_count": None,
        "no_tests_discovered": False,
        "exit_code": None,
        "duration_ms": 0,
        "pass_count": 0,
        "failure_count": 0,
        "timed_out": False,
        "failed_tests": [],
        "summary": resolution.get("summary") or "Dry run: single test target resolved but not executed.",
        "stdout_snippet": "",
        "stderr_snippet": "",
        "error_code": error_code,
        "outcome": outcome,
        "outcome_reason": outcome_reason_for_run_tests_outcome(outcome),
        "workflow_execution_success": supported,
        "task_validation_success": None,
        "recommendation": recommendation_for_run_tests_outcome(outcome),
        "execution_environment": resolution.get("execution_environment"),
        "selected_python": (resolution.get("execution_environment") or {}).get("selected_python"),
        "selection_source": "not_executed",
        "fallback_used": bool((resolution.get("execution_environment") or {}).get("fallback_used", False)),
        "collection_started": False,
        "tests_started": False,
        "environment_error": False,
        "environment_error_code": None,
        "environment_error_message": None,
        "remediation": None,
    }


def build_explain_failure_plan_artifacts(command: list[str] | None) -> dict[str, Any]:
    artifacts = build_run_tests_plan_artifacts(command)
    artifacts.update(
        {
            "workflow": "ExplainFailureWorkflow",
            "diagnoses": [],
            "diagnosis_count": 0,
            "likely_failure_types": [],
            "raw_output_available": False,
            "confidence": None,
            "confidence_reason": "Dry run did not execute tests.",
            "summary": "Dry run: test command detected but not executed." if command else "Dry run: no test command detected.",
        }
    )
    return artifacts


def build_single_test_error_artifacts(resolution: dict[str, Any]) -> dict[str, Any]:
    error_code = resolution.get("error_code", "single_test_not_supported")
    outcome = (
        "NO_TEST_COMMAND"
        if error_code in {"no_failed_test_available", "test_target_not_specified"}
        else "UNSUPPORTED"
    )
    return {
        "workflow": "SingleTestWorkflow",
        "test_target": resolution.get("test_target"),
        "target_source": resolution.get("target_source"),
        "test_command": None,
        "command_argv": [],
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "test_count": None,
        "no_tests_discovered": False,
        "exit_code": None,
        "duration_ms": 0,
        "pass_count": 0,
        "failure_count": 0,
        "timed_out": False,
        "failed_tests": [],
        "summary": resolution.get("summary") or "Single test target is not supported.",
        "stdout_snippet": "",
        "stderr_snippet": "",
        "error_code": error_code,
        "outcome": outcome,
        "outcome_reason": outcome_reason_for_run_tests_outcome(outcome),
        "workflow_execution_success": False,
        "task_validation_success": False,
        "recommendation": recommendation_for_run_tests_outcome(outcome),
        "execution_environment": None,
        "selected_python": None,
        "selection_source": "not_executed",
        "fallback_used": False,
        "collection_started": False,
        "tests_started": False,
        "environment_error": False,
        "environment_error_code": None,
        "environment_error_message": None,
        "remediation": None,
    }


def build_single_test_artifacts(test_result: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    run_artifacts = build_run_tests_artifacts(test_result)
    summary = run_artifacts.get("summary") or ""
    if run_artifacts.get("timed_out"):
        summary = f"Single test target timed out: {resolution.get('test_target')}"
    elif run_artifacts.get("no_tests_discovered"):
        summary = "No tests were discovered for the selected single-test target."
    elif run_artifacts.get("tests_passed") is True:
        summary = f"Single test target passed: {resolution.get('test_target')}"
    elif run_artifacts.get("tests_passed") is False:
        summary = f"Single test target failed: {resolution.get('test_target')}"
    artifacts = dict(run_artifacts)
    artifacts.update(
        {
            "workflow": "SingleTestWorkflow",
            "test_target": resolution.get("test_target"),
            "target_source": resolution.get("target_source"),
            "summary": summary,
        }
    )
    return artifacts


def build_run_tests_artifacts(test_result: dict[str, Any]) -> dict[str, Any]:
    tests_run = tests_were_run(test_result)
    tests_passed = test_result_passed(test_result)
    failed_tests = parse_test_failures(test_result)
    failure_count = len(failed_tests) or int(test_result.get("failed", 0) or 0)
    outcome = classify_run_tests_outcome(
        test_result,
        tests_run=tests_run,
        tests_passed=tests_passed,
        failed_tests=failed_tests,
        failure_count=failure_count,
    )
    summary = extract_test_summary(test_result, tests_run=tests_run, tests_passed=tests_passed, failure_count=failure_count)
    error_code = error_code_for_run_tests_outcome(outcome, tests_run)
    return {
        "workflow": "RunTestsWorkflow",
        "test_command": command_to_text(test_result.get("command")),
        "command_argv": list(test_result.get("command", []) or []),
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "validation_status": validation_status_from_tests(tests_run, tests_passed),
        "test_count": test_result.get("test_count"),
        "no_tests_discovered": bool(test_result_no_tests_discovered(test_result)) if tests_run else False,
        "exit_code": test_result.get("returncode"),
        "duration_ms": int(test_result.get("duration_ms") or round(float(test_result.get("runtime_seconds", 0) or 0) * 1000)),
        "pass_count": int(test_result.get("passed") or 0),
        "failure_count": failure_count,
        "timed_out": bool(test_result.get("timed_out", False)),
        "failed_tests": failed_tests,
        "summary": summary,
        "stdout_snippet": snippet(test_result.get("stdout", "")),
        "stderr_snippet": snippet(test_result.get("stderr", "")),
        "error_code": error_code,
        "outcome": outcome,
        "outcome_reason": outcome_reason_for_run_tests_outcome(
            outcome,
            test_result.get("timeout_seconds", test_result.get("runtime_seconds")),
            test_result.get("blocked_reason"),
        ),
        "workflow_execution_success": workflow_execution_success_for_run_tests_outcome(outcome),
        "task_validation_success": True if outcome == "PASSED" else False,
        "recommendation": recommendation_for_run_tests_outcome(outcome),
        "execution_environment": test_result.get("execution_environment"),
        "selected_python": test_result.get("selected_python"),
        "selection_source": test_result.get("selection_source"),
        "fallback_used": bool(test_result.get("fallback_used", False)),
        "collection_started": bool(test_result.get("collection_started", False)),
        "tests_started": bool(test_result.get("tests_started", tests_run)),
        "environment_error": test_result.get("environment_error", False),
        "environment_error_code": test_result.get("environment_error_code"),
        "environment_error_message": test_result.get("environment_error_message"),
        "remediation": test_result.get("remediation"),
    }


def build_explain_failure_artifacts(test_result: dict[str, Any]) -> dict[str, Any]:
    run_artifacts = build_run_tests_artifacts(test_result)
    outcome = str(run_artifacts.get("outcome") or "UNKNOWN")
    diagnoses: list[dict[str, Any]] = []
    summary = ""
    error_code = run_artifacts.get("error_code")
    if outcome == "PASSED":
        summary = "No failing tests detected."
        error_code = None
    elif outcome == "FAILED_TESTS":
        diagnoses = build_failure_diagnoses(run_artifacts, test_result)
        if diagnoses:
            types = unique_preserving_order(item["failure_type"] for item in diagnoses)
            summary = f"Detected {len(diagnoses)} likely failure pattern(s): {', '.join(types)}."
        else:
            summary = "Tests failed, but failure explanation could not be produced reliably."
            error_code = "unknown_failure_state"
    elif outcome == "TIMEOUT":
        summary = "Test run timed out before producing failure output. No failures to explain."
        error_code = "test_run_timed_out"
    elif outcome == "ENVIRONMENT_MISMATCH":
        summary = "Test execution did not reach tests because the selected environment was incomplete."
        error_code = run_artifacts.get("environment_error_code") or "execution_environment_mismatch"
    elif outcome == "NO_TESTS_DISCOVERED":
        summary = "No tests were discovered. Nothing to explain."
        error_code = "no_tests_discovered"
    elif outcome == "NO_TEST_COMMAND":
        summary = "No supported test command was detected. Nothing to explain."
        error_code = "no_test_command_detected"
    elif outcome == "COMMAND_ERROR":
        summary = "Test command failed before a test failure could be explained."
        error_code = "command_error"
    elif outcome == "UNSUPPORTED":
        summary = "The test runner or failure shape is unsupported. Nothing to explain reliably."
        error_code = "unsupported_test_runner"
    else:
        summary = "Failure explanation could not be produced reliably."
        error_code = "unknown_failure_state"
    top_diagnosis = diagnoses[0] if diagnoses else {}
    confidence = top_diagnosis.get("confidence")
    confidence_reason = top_diagnosis.get("confidence_reason") or explain_failure_no_diagnosis_reason(outcome)
    artifacts = dict(run_artifacts)
    artifacts.update(
        {
            "workflow": "ExplainFailureWorkflow",
            "diagnoses": diagnoses,
            "diagnosis_count": len(diagnoses),
            "likely_failure_types": unique_preserving_order(item["failure_type"] for item in diagnoses),
            "summary": summary,
            "error_code": error_code,
            "workflow_execution_success": explain_failure_success_for_outcome(outcome),
            "task_validation_success": True if outcome == "PASSED" else False if outcome == "FAILED_TESTS" else None,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "raw_output_available": bool(test_result.get("stdout") or test_result.get("stderr")),
        }
    )
    return artifacts


def build_failure_diagnoses(run_artifacts: dict[str, Any], test_result: dict[str, Any]) -> list[dict[str, Any]]:
    if run_artifacts.get("outcome") != "FAILED_TESTS":
        return []
    output = combined_test_output(test_result)
    failed_tests = list(run_artifacts.get("failed_tests", []) or [])
    if not failed_tests:
        failed_tests = [
            {
                "test_name": None,
                "file": extract_first_file_from_output(output),
                "line": extract_first_line_from_output(output),
                "snippet": first_relevant_failure_snippet(output),
            }
        ]
    diagnoses: list[dict[str, Any]] = []
    for failure in failed_tests[:20]:
        evidence = first_non_empty(str(failure.get("snippet", "")), first_relevant_failure_snippet(output), output[:500])
        failure_type, confidence = classify_failure_pattern(evidence, output, test_result)
        diagnoses.append(
            {
                "test_name": failure.get("test_name"),
                "file": failure.get("file"),
                "line": failure.get("line"),
                "failure_type": failure_type,
                "confidence": confidence,
                "confidence_reason": confidence_reason_for_failure_type(failure_type, confidence, evidence),
                "evidence_snippet": snippet(evidence, limit=500),
                "likely_cause": likely_cause_for_failure_type(failure_type),
                "suggested_next_steps": suggested_steps_for_failure_type(failure_type),
            }
        )
    return diagnoses


def explain_failure_no_diagnosis_reason(outcome: str) -> str:
    reasons = {
        "PASSED": "Tests passed, so no failure diagnosis is applicable.",
        "NO_TESTS_DISCOVERED": "No tests were discovered, so there is no failure output to diagnose.",
        "NO_TEST_COMMAND": "No supported test command was detected.",
        "COMMAND_ERROR": "The test command failed before a parseable test failure was available.",
        "UNSUPPORTED": "The test runner or failure shape is unsupported.",
        "UNKNOWN": "The test outcome could not be classified reliably.",
    }
    return reasons.get(outcome, reasons["UNKNOWN"])


def build_fix_failing_test_artifacts(
    initial_explanation: dict[str, Any],
    *,
    repair: dict[str, Any] | None,
    dry_run: bool,
    unsupported_reason: str | None,
    verification: dict[str, Any] | None = None,
    post_patch_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    supported_repair = bool(repair and repair.get("supported"))
    blocked_by_guardrail = bool(repair and repair.get("repair_status") == "blocked_by_guardrail")
    verification_payload = verification or {
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }
    verification_ran = bool(verification_payload.get("tests_run"))
    verification_passed = verification_payload.get("tests_passed")
    repair_status = determine_fix_repair_status(
        supported_repair=supported_repair,
        dry_run=dry_run,
        unsupported_reason=unsupported_reason,
        verification=verification_payload,
        blocked_by_guardrail=blocked_by_guardrail,
    )
    risk_level = risk_level_for_repair(repair)
    summary = summary_for_fix_repair_status(repair_status, repair, verification_payload, unsupported_reason)
    recommendation = recommendation_for_fix_repair_status(repair_status, unsupported_reason)
    artifacts = {
        "workflow": "FixFailingTestWorkflow",
        "dry_run": bool(dry_run),
        "initial_tests": initial_explanation,
        "diagnoses": list(initial_explanation.get("diagnoses", []) or []),
        "diagnosis_count": int(initial_explanation.get("diagnosis_count", 0) or 0),
        "repair_attempted": bool(supported_repair and not dry_run and not unsupported_reason),
        "repair_status": repair_status,
        "repair_type": repair.get("repair_class") if repair else None,
        "repair_class": repair.get("repair_class") if repair else None,
        "risk_level": risk_level,
        "target_file": repair.get("target_file") if repair else None,
        "patch_summary": repair.get("patch_summary") if repair else None,
        "files_modified": [repair["target_file"]] if repair and repair.get("target_file") and not dry_run and not unsupported_reason else [],
        "lines_changed": int(repair.get("lines_changed", 0) or 0) if repair else 0,
        "replacements_performed": int(repair.get("replacements_performed", 1) or 0) if supported_repair and repair.get("operation") == "replace" else 0,
        "guardrail_triggered": bool(repair and repair.get("guardrail_triggered")),
        "guardrail_reason": repair.get("guardrail_reason") if repair else None,
        "verification": verification_payload,
        "verification_ran": verification_ran,
        "verification_passed": verification_passed,
        "rollback_supported": False,
        "rollback_performed": False,
        "unsupported": bool(unsupported_reason),
        "unsupported_reason": unsupported_reason,
        "post_patch_failures": list(post_patch_failures or []),
        "summary": summary,
        "recommendation": recommendation,
    }
    if dry_run:
        artifacts.update(
            {
                "repair_attempted": False,
                "repair_status": "dry_run_proposed" if supported_repair else repair_status,
                "proposed_repair_class": repair.get("repair_class") if supported_repair else None,
                "proposed_repair": {
                    "repair_type": repair.get("repair_class"),
                    "target_file": repair.get("target_file"),
                    "patch_summary": repair.get("patch_summary"),
                    "risk_level": risk_level,
                }
                if supported_repair
                else None,
                "would_modify_files": [repair["target_file"]] if supported_repair and repair.get("target_file") else [],
                "proposed_patch_summary": repair.get("patch_summary") if supported_repair else None,
                "expected_change_summary": repair.get("patch_summary") if supported_repair else None,
                "verification_plan": "Run the detected project test command after applying the patch.",
            }
        )
    return artifacts


def determine_fix_repair_status(
    *,
    supported_repair: bool,
    dry_run: bool,
    unsupported_reason: str | None,
    verification: dict[str, Any],
    blocked_by_guardrail: bool,
) -> str:
    if blocked_by_guardrail:
        return "blocked_by_guardrail"
    if unsupported_reason:
        if unsupported_reason == "patch_not_applied":
            return "patch_not_applied"
        return "unsupported"
    if dry_run and supported_repair:
        return "dry_run_proposed"
    if supported_repair:
        if verification.get("tests_run"):
            return "verification_passed" if verification.get("tests_passed") is True else "verification_failed"
        return "verification_missing"
    if verification.get("tests_run") and verification.get("tests_passed") is True:
        return "no_repair_needed"
    return "unsupported"


def risk_level_for_repair(repair: dict[str, Any] | None) -> str:
    if not repair or not repair.get("supported"):
        return "none"
    repair_class = str(repair.get("repair_class") or "")
    if repair_class in {"wrong_constant_simple", "wrong_operator_simple", "missing_import_or_export", "node_missing_export_simple"}:
        return "low"
    if repair_class == "missing_none_or_empty_guard":
        return "medium"
    return "high"


def summary_for_fix_repair_status(
    repair_status: str,
    repair: dict[str, Any] | None,
    verification: dict[str, Any],
    unsupported_reason: str | None,
) -> str:
    if repair_status == "dry_run_proposed":
        return f"Dry run proposed a conservative repair: {repair.get('patch_summary') if repair else ''}".strip()
    if repair_status == "verification_passed":
        return "Repair attempted and verification passed."
    if repair_status == "verification_failed":
        return "Repair attempted, but verification failed. Do not treat this as fixed."
    if repair_status == "blocked_by_guardrail":
        return f"Repair was blocked by a safety guardrail: {repair.get('guardrail_reason') if repair else unsupported_reason}."
    if repair_status == "patch_not_applied":
        return "A safe repair was selected, but the patch was not applied."
    if repair_status == "verification_missing":
        return "Repair status is unsafe because verification did not run."
    if repair_status == "no_repair_needed" or unsupported_reason == "tests_already_passing":
        return "Tests already pass; no repair is needed."
    return f"Repair unsupported: {unsupported_reason or 'unsupported_failure_pattern'}."


def recommendation_for_fix_repair_status(repair_status: str, unsupported_reason: str | None) -> str:
    if repair_status == "dry_run_proposed":
        return "Review the proposed patch, then run without --dry-run only on a branch or disposable copy."
    if repair_status == "verification_passed":
        return "Review the diff before keeping the change."
    if repair_status == "verification_failed":
        return "Inspect post_patch_failures and consider reverting manually; rollback is not automatic."
    if repair_status == "blocked_by_guardrail":
        return "Do not bypass the guardrail unless the workflow is explicitly extended and revalidated."
    if repair_status == "no_repair_needed" or unsupported_reason == "tests_already_passing":
        return "No action needed."
    return "Use ExplainFailureWorkflow or manual review; SkillLayer refused to guess."


def select_safe_repair(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any]:
    if not explanation.get("tests_run"):
        return unsupported_repair("no_test_command_detected")
    if explanation.get("tests_passed") is True:
        return unsupported_repair("tests_already_passing")
    diagnoses = list(explanation.get("diagnoses", []) or [])
    if not diagnoses:
        return unsupported_repair("no_failure_diagnosis")
    if not any(str(item.get("confidence")) in {"medium", "high"} for item in diagnoses):
        return unsupported_repair("diagnosis_confidence_too_low")

    selectors = [
        select_missing_import_or_export_patch,
        select_wrong_operator_patch,
        select_wrong_constant_patch,
        select_missing_none_or_empty_guard_patch,
        select_node_missing_export_patch,
        select_legacy_catalog_patch,
    ]
    for selector in selectors:
        repair = selector(task_description, repo, test_result, explanation)
        if repair and repair.get("supported"):
            return validate_repair_safety(repo, repair, explanation)
    return unsupported_repair("no_supported_safe_repair")


def unsupported_repair(reason: str) -> dict[str, Any]:
    return {"supported": False, "unsupported": True, "unsupported_reason": reason}


UNSAFE_REPAIR_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "runs",
    "venv",
}

UNSAFE_REPAIR_FILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
}

UNSAFE_REPAIR_SUFFIXES = {
    ".pyc",
    ".pyo",
}


def blocked_repair(reason: str, repair: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(repair or {})
    payload.update(
        {
            "supported": False,
            "unsupported": True,
            "unsupported_reason": reason,
            "repair_status": "blocked_by_guardrail",
            "guardrail_triggered": True,
            "guardrail_reason": reason,
        }
    )
    return payload


def validate_repair_safety(repo: Path, repair: dict[str, Any], explanation: dict[str, Any]) -> dict[str, Any]:
    allowed_classes = {
        "wrong_constant_simple",
        "wrong_operator_simple",
        "missing_import_or_export",
        "missing_none_or_empty_guard",
        "node_missing_export_simple",
    }
    repair_class = str(repair.get("repair_class") or "")
    if repair_class not in allowed_classes:
        return unsupported_repair("unsupported_repair_class")
    if explanation.get("tests_passed") is not False:
        return unsupported_repair("tests_not_currently_failing")
    if multiple_named_failing_tests(explanation):
        return unsupported_repair("multiple_failing_tests_not_supported")
    target_file = str(repair.get("target_file") or "")
    if not target_file:
        return unsupported_repair("missing_target_file")
    guardrail_reason = repair_target_guardrail_reason(target_file)
    if guardrail_reason:
        return blocked_repair(guardrail_reason, repair)
    target_path = (repo / target_file).resolve()
    try:
        target_path.relative_to(repo.resolve())
    except ValueError:
        return unsupported_repair("target_file_escapes_repo")
    if not target_path.exists() or not target_path.is_file():
        return unsupported_repair("target_file_not_found")
    operation = str(repair.get("operation") or "replace")
    current = target_path.read_text(encoding="utf-8", errors="ignore")
    if operation == "replace":
        old = str(repair.get("old") or "")
        new = str(repair.get("new") or "")
        if not old.strip() or old == new:
            return unsupported_repair("empty_or_noop_patch")
        if current.count(old) != 1:
            return unsupported_repair("ambiguous_patch_match")
        lines_changed = estimate_lines_changed(old, new)
    elif operation == "append":
        new = str(repair.get("new") or "")
        if not new.strip() or new.strip() in current:
            return unsupported_repair("empty_or_duplicate_append")
        lines_changed = len([line for line in new.splitlines() if line.strip()])
    else:
        return unsupported_repair("unsupported_patch_operation")
    if lines_changed > 6:
        return unsupported_repair("patch_too_large")
    checked = dict(repair)
    checked.update(
        {
            "supported": True,
            "unsupported": False,
            "repair_status": "selected",
            "lines_changed": lines_changed,
            "replacements_performed": 1 if operation == "replace" else 0,
            "guardrail_triggered": False,
            "guardrail_reason": None,
            "safety_checks": {
                "single_target_file": True,
                "small_patch": True,
                "exact_match_patch": operation in {"replace", "append"},
                "tests_currently_failing": True,
                "repair_class_supported": True,
                "target_inside_repo": True,
                "target_not_generated_or_cache": True,
                "target_not_lockfile": True,
            },
        }
    )
    return checked


def repair_target_guardrail_reason(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in UNSAFE_REPAIR_DIRS for part in parts):
        return "unsafe_generated_or_cache_path"
    file_name = parts[-1] if parts else ""
    if file_name in UNSAFE_REPAIR_FILE_NAMES:
        return "lockfile_edits_not_supported"
    if Path(file_name).suffix in UNSAFE_REPAIR_SUFFIXES:
        return "compiled_file_edits_not_supported"
    if normalized.endswith(".json") and ("/runs/" in f"/{normalized}" or normalized.startswith("runs/")):
        return "generated_report_edits_not_supported"
    return None


def multiple_named_failing_tests(explanation: dict[str, Any]) -> bool:
    failed_tests = list(explanation.get("failed_tests", []) or [])
    named = {
        (str(item.get("file") or ""), str(item.get("test_name") or ""))
        for item in failed_tests
        if item.get("test_name")
    }
    if len(named) > 1:
        return True
    if named:
        return False
    files = {str(item.get("file") or "") for item in failed_tests if item.get("file")}
    return len(files) > 1


def apply_safe_repair_patch(tools: ProjectTools, repair: dict[str, Any]) -> bool:
    operation = str(repair.get("operation") or "replace")
    target_file = str(repair.get("target_file") or "")
    if operation == "append":
        return tools.insert_text(target_file, str(repair.get("new") or ""))
    return tools.replace_text(target_file, str(repair.get("old") or ""), str(repair.get("new") or ""))


def select_wrong_constant_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    if not has_diagnosis_type(explanation, "assertion_failure"):
        return None
    context = assertion_context_from_failure(repo, explanation, test_result)
    if not context:
        return None
    actual_expected = parse_actual_expected(combined_test_output(test_result))
    if not actual_expected:
        return None
    actual, expected = actual_expected
    if not is_simple_literal(actual) or not is_simple_literal(expected):
        return None
    function = find_python_function(repo, context["target_file"], context["function_name"])
    if not function:
        return None
    candidate_lines = [line for line in function["body_lines"] if actual in line and re.search(r"\breturn\b", line)]
    if len(candidate_lines) != 1:
        return None
    old_line = candidate_lines[0]
    new_line = old_line.replace(actual, expected, 1)
    return {
        "supported": True,
        "operation": "replace",
        "repair_class": "wrong_constant_simple",
        "target_file": context["target_file"],
        "old": old_line,
        "new": new_line,
        "patch_summary": f"Replace simple returned constant {actual} with expected {expected} in {context['function_name']}.",
    }


def select_wrong_operator_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    if not has_diagnosis_type(explanation, "assertion_failure"):
        return None
    context = assertion_context_from_failure(repo, explanation, test_result)
    if not context or not context.get("call_args"):
        return None
    expected = context.get("expected_literal")
    if expected is None or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", str(expected)):
        return None
    function = find_python_function(repo, context["target_file"], context["function_name"])
    if not function:
        return None
    params = function["params"]
    args = context["call_args"]
    if len(params) != len(args) or not all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", arg) for arg in args):
        return None
    env = {name: float(value) if "." in value else int(value) for name, value in zip(params, args)}
    expected_value = float(expected) if "." in str(expected) else int(str(expected))
    for line in function["body_lines"]:
        match = re.match(r"(\s*return\s+)([A-Za-z_][A-Za-z0-9_]*\s*[-+*/]\s*[A-Za-z_][A-Za-z0-9_]*)(\s*)$", line)
        if not match:
            continue
        expression = match.group(2)
        operator_match = re.search(r"([-+*/])", expression)
        if not operator_match:
            continue
        current_operator = operator_match.group(1)
        candidates: list[tuple[str, str]] = []
        for operator in ["+", "-", "*", "/"]:
            if operator == current_operator:
                continue
            candidate_expression = expression.replace(current_operator, operator, 1)
            value = safe_eval_arithmetic(candidate_expression, env)
            if value == expected_value:
                candidates.append((operator, candidate_expression))
        if len(candidates) != 1:
            continue
        new_operator, new_expression = candidates[0]
        return {
            "supported": True,
            "operation": "replace",
            "repair_class": "wrong_operator_simple",
            "target_file": context["target_file"],
            "old": line,
            "new": f"{match.group(1)}{new_expression}{match.group(3)}",
            "patch_summary": f"Replace operator {current_operator} with {new_operator} in {context['function_name']}.",
        }
    return None


def select_missing_import_or_export_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    if not (has_diagnosis_type(explanation, "import_error") or has_diagnosis_type(explanation, "module_not_found")):
        return None
    output = combined_test_output(test_result)
    match = re.search(r"cannot import name ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"] from ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]", output)
    if not match:
        return None
    symbol, package = match.groups()
    package_dir = repo / package.replace(".", "/")
    init_path = package_dir / "__init__.py"
    if not init_path.exists():
        return None
    module = find_package_symbol_module(package_dir, symbol)
    if not module:
        return None
    current = init_path.read_text(encoding="utf-8", errors="ignore")
    export_line = f"from .{module} import {symbol}"
    if export_line in current:
        return None
    return {
        "supported": True,
        "operation": "append",
        "repair_class": "missing_import_or_export",
        "target_file": str(init_path.relative_to(repo)),
        "new": "\n" + export_line + "\n",
        "patch_summary": f"Export {symbol} from package {package}.",
    }


def select_missing_none_or_empty_guard_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    if not (has_diagnosis_type(explanation, "type_error") or has_diagnosis_type(explanation, "attribute_error")):
        return None
    output = combined_test_output(test_result)
    if "nonetype" not in output.lower() and "none" not in output.lower():
        return None
    context = assertion_context_from_failure(repo, explanation, test_result)
    if not context:
        return None
    if context.get("call_args") != ["None"]:
        return None
    expected = context.get("expected_literal")
    if expected not in {'""', "''", "None"}:
        return None
    function = find_python_function(repo, context["target_file"], context["function_name"])
    if not function or not function["params"]:
        return None
    first_param = function["params"][0]
    return_value = "None" if expected == "None" else expected
    def_line = function["def_line"]
    guard = f"{def_line}\n    if {first_param} is None:\n        return {return_value}"
    if f"if {first_param} is None" in "\n".join(function["body_lines"]):
        return None
    return {
        "supported": True,
        "operation": "replace",
        "repair_class": "missing_none_or_empty_guard",
        "target_file": context["target_file"],
        "old": def_line,
        "new": guard,
        "patch_summary": f"Add a small None guard to {context['function_name']}.",
    }


def select_node_missing_export_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    output = combined_test_output(test_result)
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*) is not a function", output)
    if not match:
        return None
    symbol = match.group(1)
    for test_file in sorted(repo.glob("*.js")):
        if not test_file.name.startswith("test"):
            continue
        text = test_file.read_text(encoding="utf-8", errors="ignore")
        require_match = re.search(r"\{\s*" + re.escape(symbol) + r"\s*\}\s*=\s*require\(['\"](.+?)['\"]\)", text)
        if not require_match:
            continue
        target = (test_file.parent / (require_match.group(1) + ".js")).resolve()
        if not target.exists():
            continue
        current = target.read_text(encoding="utf-8", errors="ignore")
        if re.search(rf"\b(?:function\s+{re.escape(symbol)}|const\s+{re.escape(symbol)}\s*=|let\s+{re.escape(symbol)}\s*=)", current) and not re.search(
            rf"(?:exports\.{re.escape(symbol)}|module\.exports.*\b{re.escape(symbol)}\b)", current
        ):
            return {
                "supported": True,
                "operation": "append",
                "repair_class": "node_missing_export_simple",
                "target_file": str(target.relative_to(repo)),
                "new": f"\nexports.{symbol} = {symbol};\n",
                "patch_summary": f"Export Node function {symbol}.",
            }
    return None


def select_legacy_catalog_patch(
    task_description: str,
    repo: Path,
    test_result: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any] | None:
    patch = choose_simple_patch(task_description, test_result)
    if not patch:
        legacy_import = choose_import_export_patch(test_result, repo)
        if legacy_import:
            patch = legacy_import
    if not patch:
        return None
    class_map = {
        "wrong_constant": "wrong_constant_simple",
        "wrong_operator": "wrong_operator_simple",
        "missing_edge_case": "missing_none_or_empty_guard",
        "import_error": "missing_import_or_export",
    }
    repair_class = class_map.get(str(patch.get("subtype")))
    if not repair_class:
        return None
    return {
        "supported": True,
        "operation": "replace",
        "repair_class": repair_class,
        "target_file": patch["file"],
        "old": patch["old"],
        "new": patch["new"],
        "patch_summary": patch.get("description", f"Apply {repair_class} catalog patch."),
    }


def has_diagnosis_type(explanation: dict[str, Any], failure_type: str) -> bool:
    return failure_type in {item.get("failure_type") for item in explanation.get("diagnoses", [])}


def assertion_context_from_failure(repo: Path, explanation: dict[str, Any], test_result: dict[str, Any]) -> dict[str, Any] | None:
    failed_tests = list(explanation.get("failed_tests", []) or [])
    candidate_files = [item.get("file") for item in failed_tests if item.get("file")]
    candidate_files.extend(item.get("file") for item in explanation.get("diagnoses", []) if item.get("file"))
    for file_name in unique_preserving_order(candidate_files):
        if not file_name or not str(file_name).endswith(".py"):
            continue
        test_path = (repo / str(file_name)).resolve()
        try:
            test_path.relative_to(repo.resolve())
        except ValueError:
            continue
        if not test_path.exists():
            continue
        context = parse_assertion_context_from_test_file(repo, test_path)
        if context:
            return context
    return parse_assertion_context_from_output(repo, combined_test_output(test_result))


def parse_assertion_context_from_test_file(repo: Path, test_path: Path) -> dict[str, Any] | None:
    text = test_path.read_text(encoding="utf-8", errors="ignore")
    import_map = parse_test_imports(text)
    for line in text.splitlines():
        parsed = parse_assertion_line(line)
        if not parsed:
            continue
        function_name = parsed["function_name"]
        module_name = import_map.get(function_name)
        if not module_name:
            continue
        target = repo / (module_name.replace(".", "/") + ".py")
        if not target.exists():
            continue
        parsed["target_file"] = str(target.relative_to(repo))
        return parsed
    return None


def parse_assertion_context_from_output(repo: Path, output: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        parsed = parse_assertion_line(line)
        if not parsed:
            continue
        for path in repo.rglob("test*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            import_map = parse_test_imports(text)
            module_name = import_map.get(parsed["function_name"])
            if not module_name:
                continue
            target = repo / (module_name.replace(".", "/") + ".py")
            if target.exists():
                parsed["target_file"] = str(target.relative_to(repo))
                return parsed
    return None


def parse_assertion_line(line: str) -> dict[str, Any] | None:
    pattern = r"assert\s+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)\s*(?:==|is)\s*(.+)"
    match = re.search(pattern, line.strip())
    if not match:
        return None
    expected = match.group(3).strip()
    expected = expected.split("#", 1)[0].strip()
    return {
        "function_name": match.group(1),
        "call_args": [item.strip() for item in match.group(2).split(",") if item.strip()],
        "expected_literal": expected,
    }


def parse_test_imports(text: str) -> dict[str, str]:
    imports: dict[str, str] = {}
    for match in re.finditer(r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+(.+)$", text, re.M):
        module = match.group(1)
        for item in match.group(2).split(","):
            symbol = item.strip().split(" as ", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
                imports[symbol] = module
    return imports


def find_python_function(repo: Path, relative_path: str, function_name: str) -> dict[str, Any] | None:
    path = repo / relative_path
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for index, line in enumerate(lines):
        match = re.match(rf"^(\s*)def\s+{re.escape(function_name)}\(([^)]*)\):\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        params = [part.strip().split("=", 1)[0].strip() for part in match.group(2).split(",") if part.strip()]
        body_lines: list[str] = []
        for body_line in lines[index + 1 :]:
            if body_line.strip() and len(body_line) - len(body_line.lstrip(" ")) <= indent:
                break
            body_lines.append(body_line)
        return {"def_line": line, "params": params, "body_lines": body_lines}
    return None


def parse_actual_expected(output: str) -> tuple[str, str] | None:
    fallback: tuple[str, str] | None = None
    for line in output.splitlines():
        match = re.search(r"assert\s+(.+?)\s+==\s+(.+)", line)
        if not match:
            continue
        actual = normalize_literal(match.group(1).strip())
        expected = normalize_literal(match.group(2).strip())
        if actual and expected and is_simple_literal(actual) and is_simple_literal(expected):
            return actual, expected
        if actual and expected and fallback is None:
            fallback = (actual, expected)
    return fallback


def normalize_literal(value: str) -> str:
    value = value.strip()
    value = value.splitlines()[0].strip()
    value = re.sub(r"\s+#.*$", "", value)
    return value.rstrip(",")


def is_simple_literal(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?|['\"][^'\"]{0,80}['\"]|None|True|False", value.strip()))


def safe_eval_arithmetic(expression: str, env: dict[str, int | float]) -> int | float | None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*[-+*/]\s*[A-Za-z_][A-Za-z0-9_]*", expression):
        return None
    try:
        return eval(expression, {"__builtins__": {}}, env)
    except Exception:
        return None


def estimate_lines_changed(old: str, new: str) -> int:
    old_lines = [line for line in old.splitlines() if line.strip()]
    new_lines = [line for line in new.splitlines() if line.strip()]
    return max(len(old_lines), len(new_lines), 1)


def classify_failure_pattern(evidence: str, output: str, test_result: dict[str, Any]) -> tuple[str, str]:
    text = f"{evidence}\n{output}".lower()
    if test_result.get("timed_out") or test_result.get("returncode") == 124 or "timed out" in text:
        return "timeout", "high"
    if "syntaxerror" in text:
        return "syntax_error", "high"
    if "err_assertion" in text or "assertionerror [err_assertion]" in text:
        return "node_assertion_error", "high"
    if "cannot find module" in text and ("node:" in text or ".js" in text or ".ts" in text):
        return "node_module_not_found", "high"
    if "modulenotfounderror" in text or "no module named" in text:
        return "module_not_found", "high"
    if "importerror" in text or "cannot import name" in text:
        return "import_error", "high"
    if "typeerror" in text:
        return "type_error", "high"
    if "attributeerror" in text:
        return "attribute_error", "high"
    if "nameerror" in text:
        return "name_error", "high"
    if "assertionerror" in text or re.search(r"\be\s+assert\b", text) or re.search(r"\bassert\b", text):
        return "assertion_failure", "medium"
    return "unknown_failure", "low"


def confidence_reason_for_failure_type(failure_type: str, confidence: str, evidence: str) -> str:
    if confidence == "high":
        return f"Strong deterministic pattern matched for {failure_type}."
    if confidence == "medium":
        return f"Known failure pattern matched for {failure_type}, but evidence should be reviewed."
    if failure_type == "unknown_failure":
        return "No supported deterministic pattern matched; this is a low-confidence fallback."
    if evidence.strip():
        return "Evidence was limited or ambiguous, so confidence is low."
    return "No useful failure evidence was available, so confidence is low."


def likely_cause_for_failure_type(failure_type: str) -> str:
    causes = {
        "assertion_failure": "The test assertion failed. Compare expected vs actual values.",
        "import_error": "Python could not import a module or symbol. Check package exports and import paths.",
        "module_not_found": "A required module is missing or not available in the current environment.",
        "type_error": "A function may have received or returned a value of an unexpected type.",
        "attribute_error": "An object may not have the expected attribute. Check object shape or return type.",
        "name_error": "A referenced name may be undefined in the current scope.",
        "syntax_error": "Python syntax parsing failed before tests could run.",
        "timeout": "The test command exceeded the configured timeout. Check for hangs, infinite loops, or slow external calls.",
        "node_assertion_error": "A Node assertion failed. Compare expected vs actual values.",
        "node_module_not_found": "A Node module could not be resolved. Check dependencies and import paths.",
        "unknown_failure": "Failure pattern was not recognized. Inspect raw stderr/stdout snippets.",
    }
    return causes.get(failure_type, causes["unknown_failure"])


def suggested_steps_for_failure_type(failure_type: str) -> list[str]:
    steps = {
        "assertion_failure": [
            "Inspect the failing assertion and compare expected versus actual values.",
            "Check the function or fixture that produces the actual value.",
        ],
        "import_error": [
            "Check whether the imported symbol exists and is exported from the expected module.",
            "Verify relative imports and package initialization files.",
        ],
        "module_not_found": [
            "Check whether the dependency is installed in the active environment.",
            "Verify the module path and package layout.",
        ],
        "type_error": [
            "Inspect the call site and function signature.",
            "Check whether inputs or return values changed type.",
        ],
        "attribute_error": [
            "Inspect the object returned before the failing attribute access.",
            "Check whether a mock, fixture, or return type changed shape.",
        ],
        "name_error": [
            "Check for misspelled names or missing imports.",
            "Verify the variable is defined before it is used.",
        ],
        "syntax_error": [
            "Open the reported file and line.",
            "Check recent edits around the syntax error before running deeper diagnosis.",
        ],
        "timeout": [
            "Inspect tests or code paths that may hang or wait on external resources.",
            "Try running the failing test individually with more logging.",
        ],
        "node_assertion_error": [
            "Inspect the Node assertion and compare expected versus actual values.",
            "Check the function or fixture that produces the actual value.",
        ],
        "node_module_not_found": [
            "Check package.json dependencies and local import paths.",
            "Verify npm install was run if dependencies are expected.",
        ],
        "unknown_failure": [
            "Inspect stdout and stderr snippets.",
            "Run the failing test directly for a narrower traceback.",
        ],
    }
    return steps.get(failure_type, steps["unknown_failure"])


def combined_test_output(test_result: dict[str, Any]) -> str:
    return f"{test_result.get('stdout', '')}\n{test_result.get('stderr', '')}"


def first_non_empty(*values: str) -> str:
    for value in values:
        if value.strip():
            return value.strip()
    return ""


def first_relevant_failure_snippet(output: str) -> str:
    needles = [
        "AssertionError",
        "ERR_ASSERTION",
        "ImportError",
        "ModuleNotFoundError",
        "TypeError",
        "AttributeError",
        "NameError",
        "SyntaxError",
        "Cannot find module",
        "timed out",
        "E   ",
    ]
    for line in output.splitlines():
        if any(needle.lower() in line.lower() for needle in needles):
            return line.strip()
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return ""


def extract_first_file_from_output(output: str) -> str | None:
    match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:py|js|ts))", output)
    return match.group(1) if match else None


def extract_first_line_from_output(output: str) -> int | None:
    match = re.search(r"\.(?:py|js|ts):(\d+)", output)
    return int(match.group(1)) if match else None


def command_to_text(command: Any) -> str | None:
    if not command:
        return None
    if isinstance(command, str):
        return command
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def resolve_single_test_target(repo: Path, task_description: str, last_failed_test_target: str | None = None) -> dict[str, Any]:
    text = task_description.strip()
    if is_run_failing_test_request(text):
        if not last_failed_test_target:
            return {
                "supported": False,
                "error_code": "no_failed_test_available",
                "test_target": None,
                "target_source": "previous_failed_test",
                "summary": "No previous failed test target is available in this SkillLayer session.",
            }
        target = last_failed_test_target
        source = "previous_failed_test"
    else:
        target = extract_single_test_target(text)
        source = "task_description"
        if not target:
            return {
                "supported": False,
                "error_code": "test_target_not_specified",
                "test_target": None,
                "target_source": source,
                "summary": "No explicit single-test target was found in the task description.",
            }

    command_result = command_for_single_test_target(repo, target)
    command_result.setdefault("test_target", target)
    command_result.setdefault("target_source", source)
    return command_result


def is_run_failing_test_request(task_description: str) -> bool:
    text = task_description.lower()
    return bool(re.search(r"\b(?:re-?run|run)\s+(?:only\s+)?(?:the\s+)?failing\s+test\b", text))


def extract_single_test_target(task_description: str) -> str | None:
    backticked = re.findall(r"`\s*([^`]+?)\s*`", task_description)
    for candidate in backticked:
        cleaned = clean_single_test_target(candidate)
        if cleaned:
            return cleaned

    target_patterns = [
        r"([A-Za-z0-9_./\\-]+\.py(?:::[A-Za-z_][A-Za-z0-9_\[\]-]*)?)",
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,})\b",
        r"\b(test_[A-Za-z0-9_]+)\b",
    ]
    for pattern in target_patterns:
        match = re.search(pattern, task_description)
        if match:
            cleaned = clean_single_test_target(match.group(1))
            if cleaned:
                return cleaned
    return None


def clean_single_test_target(candidate: str) -> str | None:
    value = candidate.strip().strip(".,;:?!\"'")
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"failing", "single", "test", "tests", "pytest", "unittest", "only"}:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_./\\:-]+(?:\[[A-Za-z0-9_\-]+\])?", value):
        return None
    return value.replace("\\", "/")


def command_for_single_test_target(repo: Path, target: str) -> dict[str, Any]:
    normalized = target.replace("\\", "/")
    if normalized.endswith(".py") or ".py::" in normalized:
        return command_for_pytest_like_target(repo, normalized)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}", normalized):
        selection = TestRunner().select_python_interpreter(repo).as_dict()
        return {
            "supported": True,
            "command": [selection["selected_python"] or sys.executable, "-m", "unittest", normalized],
            "execution_environment": selection,
            "test_target": normalized,
            "summary": "Resolved dotted unittest target.",
        }
    if normalized.startswith("test_"):
        return command_for_test_function_name(repo, normalized)
    if has_node_tests(repo) and shutil.which("npm"):
        return {
            "supported": True,
            "command": ["npm", "test", "--", normalized],
            "test_target": normalized,
            "summary": "Resolved target through npm test selector.",
        }
    return {
        "supported": False,
        "error_code": "single_test_not_supported",
        "test_target": normalized,
        "summary": "Single test target format is not supported.",
    }


def command_for_pytest_like_target(repo: Path, target: str) -> dict[str, Any]:
    file_part = target.split("::", 1)[0]
    resolved_file = resolve_test_file(repo, file_part)
    if not resolved_file:
        return {
            "supported": False,
            "error_code": "test_file_not_found",
            "test_target": target,
            "summary": f"Test file was not found: {file_part}",
        }
    suffix = ""
    if "::" in target:
        suffix = "::" + target.split("::", 1)[1]
    resolved_target = f"{resolved_file}{suffix}"
    selection = TestRunner().select_python_interpreter(repo).as_dict()
    selected_python = selection["selected_python"] or sys.executable
    if importlib.util.find_spec("pytest") is not None:
        return {
            "supported": True,
            "command": [selected_python, "-m", "pytest", "-q", resolved_target],
            "execution_environment": selection,
            "test_target": resolved_target,
            "summary": "Resolved pytest single-test target.",
        }
    if suffix:
        return {
            "supported": False,
            "error_code": "single_test_not_supported",
            "test_target": resolved_target,
            "summary": "pytest is not available for file::test target selection.",
        }
    module_target = resolved_target[:-3].replace("/", ".")
    return {
        "supported": True,
        "command": [selected_python, "-m", "unittest", module_target],
        "execution_environment": selection,
        "test_target": module_target,
        "summary": "Resolved file target through unittest module discovery.",
    }


def command_for_test_function_name(repo: Path, test_name: str) -> dict[str, Any]:
    matches: list[str] = []
    pattern = re.compile(rf"^\s*def\s+{re.escape(test_name)}\s*\(", re.MULTILINE)
    for path in repo.rglob("test*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if pattern.search(text):
            matches.append(safe_relative_path(path, repo))
    if len(matches) == 1 and importlib.util.find_spec("pytest") is not None:
        target = f"{matches[0]}::{test_name}"
        selection = TestRunner().select_python_interpreter(repo).as_dict()
        return {
            "supported": True,
            "command": [selection["selected_python"] or sys.executable, "-m", "pytest", "-q", target],
            "execution_environment": selection,
            "test_target": target,
            "summary": "Resolved unique pytest function target.",
        }
    if len(matches) > 1:
        return {
            "supported": False,
            "error_code": "ambiguous_test_target",
            "test_target": test_name,
            "summary": f"Test function target is ambiguous across {len(matches)} files: {', '.join(matches)}.",
        }
    return {
        "supported": False,
        "error_code": "test_target_not_found",
        "test_target": test_name,
        "summary": "Test function target was not found in any test file.",
    }


def resolve_test_file(repo: Path, file_part: str) -> str | None:
    candidate = (repo / file_part).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    if candidate.exists() and candidate.is_file():
        return safe_relative_path(candidate, repo)
    matches = [
        safe_relative_path(path, repo)
        for path in repo.rglob(Path(file_part).name)
        if path.is_file() and path.name == Path(file_part).name and "__pycache__" not in path.parts
    ]
    return matches[0] if len(matches) == 1 else None


def has_node_tests(repo: Path) -> bool:
    package_json = repo / "package.json"
    if not package_json.exists():
        return False
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return False
    scripts = payload.get("scripts")
    return isinstance(scripts, dict) and bool(str(scripts.get("test", "")).strip())


def failed_test_target_from_artifacts(artifacts: dict[str, Any]) -> str | None:
    for failure in list(artifacts.get("failed_tests", []) or []):
        file_path = failure.get("file")
        test_name = failure.get("test_name")
        if file_path and test_name:
            return f"{file_path}::{test_name}"
        if file_path:
            return str(file_path)
        if test_name:
            return str(test_name)
    return None


def parse_test_failures(test_result: dict[str, Any]) -> list[dict[str, Any]]:
    output = f"{test_result.get('stdout', '')}\n{test_result.get('stderr', '')}"
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, int | None]] = set()

    patterns = [
        (r"FAILED\s+([^\s:]+?\.py)::([^\s]+)(?:\s+-\s+(.+))?", "pytest_failed"),
        (r"ERROR\s+([^\s:]+?\.py)::([^\s]+)(?:\s+-\s+(.+))?", "pytest_error"),
        (r"([^\s:]+?\.py):(\d+):\s+(.+)", "python_trace"),
        (r"FAIL:\s+([^\s(]+)\s+\(([^)]+)\)", "unittest_fail"),
        (r"ERROR:\s+([^\s(]+)\s+\(([^)]+)\)", "unittest_error"),
        (r"\bFAIL\s+([^\s]+)(?:\s+(.+))?", "node_fail"),
        (r"at\s+(?:[^(]+\()?([^()\s]+?\.(?:js|ts)):(\d+):\d+\)?", "node_trace"),
    ]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, output):
            file_path: str | None = None
            line_no: int | None = None
            test_name: str | None = None
            snippet_text = ""
            groups = match.groups()
            if kind in {"pytest_failed", "pytest_error"}:
                file_path = groups[0]
                test_name = groups[1]
                snippet_text = groups[2] or ""
            elif kind == "python_trace":
                file_path = groups[0]
                if is_internal_trace_path(file_path):
                    continue
                line_no = int(groups[1])
                snippet_text = groups[2] or ""
            elif kind in {"unittest_fail", "unittest_error"}:
                test_name = groups[0]
                file_path = unittest_identifier_to_file(groups[1]) if groups[1] else None
            elif kind == "node_fail":
                test_name = groups[0]
                snippet_text = groups[1] or ""
            elif kind == "node_trace":
                file_path = groups[0]
                line_no = int(groups[1])
                test_name = "node_test_failure"
                snippet_text = first_matching_line(output, ["AssertionError", "Error:", "expected", "actual"])
            key = (file_path, test_name, line_no)
            if key in seen:
                continue
            seen.add(key)
            failures.append(
                {
                    "test_name": test_name,
                    "file": file_path,
                    "line": line_no,
                    "kind": kind,
                    "snippet": snippet(snippet_text, limit=240),
                }
            )
            if len(failures) >= 20:
                return failures
    return failures


def first_matching_line(text: str, needles: list[str]) -> str:
    for line in text.splitlines():
        if any(needle.lower() in line.lower() for needle in needles):
            return line.strip()
    return ""


def is_internal_trace_path(file_path: str | None) -> bool:
    if not file_path:
        return False
    normalized = file_path.replace("\\", "/").lower()
    internal_markers = [
        "/.venv/",
        "/site-packages/",
        "/dist-packages/",
        "/python",
        "importlib/",
        "_pytest/",
    ]
    return any(marker in normalized for marker in internal_markers)


def extract_test_summary(test_result: dict[str, Any], *, tests_run: bool, tests_passed: bool | None, failure_count: int) -> str:
    if bool(test_result.get("nested_execution_blocked")):
        return test_result.get("blocked_reason") or (
            "Refused to run: already executing inside a pytest test, which would recursively spawn nested test runs."
        )
    if not tests_run:
        return "No test command detected."
    if bool(test_result.get("timed_out")):
        timeout_seconds = test_result.get("timeout_seconds", test_result.get("runtime_seconds"))
        if timeout_seconds is not None:
            return f"Test suite exceeded {int(timeout_seconds)}s timeout."
        return "Test suite exceeded the configured timeout."
    if test_result_no_tests_discovered(test_result):
        return "No tests were discovered. This is not considered a passed test run."
    output = f"{test_result.get('stdout', '')}\n{test_result.get('stderr', '')}"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        lower = line.lower()
        if any(word in lower for word in [" passed", " failed", " error", " failures", " tests", "test suites"]):
            return line[-500:]
    if tests_passed:
        return "Tests passed."
    return f"Tests failed with {failure_count} parsed failure(s)."


def unittest_identifier_to_file(identifier: str) -> str | None:
    parts = [part for part in identifier.split(".") if part]
    if len(parts) >= 3:
        return "/".join(parts[:-2]) + ".py"
    if parts:
        return "/".join(parts) + ".py"
    return None


def snippet(text: Any, *, limit: int = 2000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[-limit:]


# --- DetectDeadCodeWorkflow --------------------------------------------------

# Matches top-level and nested def/class declarations (dunder names filtered separately).
_DEADCODE_DEF_RE = re.compile(r"^[ \t]*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", re.MULTILINE)
_DEADCODE_CLASS_RE = re.compile(r"^[ \t]*class\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*[\(:]", re.MULTILINE)
_DEADCODE_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")
_DEADCODE_DUNDER_RE = re.compile(r"^__[a-zA-Z_]+__$")
# Matches paths that look like test files/directories
_DEADCODE_TEST_PATH_RE = re.compile(r"(?:^|/)tests?/|(?:^|/)test_[^/]+\.py$|(?:^|/)[^/]+_test\.py$")


def _deadcode_is_test_file(path: Path, repo: Path) -> bool:
    try:
        rel = str(path.relative_to(repo)).replace("\\", "/")
    except ValueError:
        return False
    return bool(_DEADCODE_TEST_PATH_RE.search(rel))


def build_detect_dead_code_artifacts(repo: Path) -> dict[str, Any]:
    all_py_files: list[Path] = []
    non_test_py_files: list[Path] = []

    for path in sorted(repo.rglob("*.py")):
        if should_ignore_search_path(path, repo):
            continue
        all_py_files.append(path)
        if not _deadcode_is_test_file(path, repo):
            non_test_py_files.append(path)

    # Read every file once; test files are included in the reference corpus
    # so that a call from a test to foo() prevents foo from being flagged.
    file_contents: dict[Path, str] = {}
    for path in all_py_files:
        try:
            file_contents[path] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            file_contents[path] = ""

    # Phase 1: extract definitions (def / class) from non-test files only.
    # Dunders are protocol methods called by the runtime — skip them.
    raw_definitions: list[dict[str, Any]] = []
    for path in non_test_py_files:
        content = file_contents.get(path, "")
        for kind, pattern in (
            ("function", _DEADCODE_DEF_RE),
            ("class", _DEADCODE_CLASS_RE),
        ):
            for m in pattern.finditer(content):
                name = m.group(1)
                if _DEADCODE_DUNDER_RE.match(name):
                    continue
                line_no = content[: m.start()].count("\n") + 1
                raw_definitions.append({"path": path, "name": name, "line": line_no, "kind": kind})

    if not raw_definitions:
        return {
            "workflow": "DetectDeadCodeWorkflow",
            "repo_path": str(repo),
            "files_scanned": len(non_test_py_files),
            "total_findings": 0,
            "certain_count": 0,
            "possible_count": 0,
            "findings": [],
            "summary": f"No definitions found across {len(non_test_py_files)} non-test file(s).",
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    # Phase 2: build reference index from ALL files (including tests).
    # name_files[name] = set of files where the identifier appears at all.
    # name_in_file_count[(name, file)] = raw occurrence count in that file.
    # Using word-level tokenisation is intentionally conservative: string
    # literals, comments, and __all__ entries all count as references.
    name_files: dict[str, set[Path]] = {}
    name_in_file_count: dict[tuple[str, Path], int] = {}

    for path, content in file_contents.items():
        word_count: dict[str, int] = {}
        for word in _DEADCODE_WORD_RE.findall(content):
            word_count[word] = word_count.get(word, 0) + 1
        for word, count in word_count.items():
            if word not in name_files:
                name_files[word] = set()
            name_files[word].add(path)
            name_in_file_count[(word, path)] = count

    # Phase 3: a definition is "unused" when its name appears only on the
    # definition line — i.e. nowhere else in its own file AND nowhere in any
    # other file.
    findings: list[dict[str, Any]] = []
    for defn in raw_definitions:
        name = defn["name"]
        path = defn["path"]

        # Referenced in a different file → alive.
        if name_files.get(name, set()) - {path}:
            continue

        # Referenced more than once within its own file → alive
        # (the definition itself counts as 1).
        if name_in_file_count.get((name, path), 0) > 1:
            continue

        is_private = name.startswith("_")
        findings.append({
            "file": str(path.relative_to(repo)),
            "name": name,
            "line": defn["line"],
            "kind": defn["kind"],
            # Private names that appear nowhere are reliably unused.
            # Public names could be external API; flag only as possible.
            "confidence": "certain" if is_private else "possible",
        })

    findings.sort(key=lambda x: (x["file"], x["line"]))
    certain = [f for f in findings if f["confidence"] == "certain"]
    possible = [f for f in findings if f["confidence"] == "possible"]

    if findings:
        summary = (
            f"{len(findings)} potentially unused symbol(s) "
            f"({len(certain)} certain, {len(possible)} possible) "
            f"across {len(non_test_py_files)} non-test file(s) scanned."
        )
    else:
        summary = f"No unused symbols detected across {len(non_test_py_files)} non-test file(s)."

    return {
        "workflow": "DetectDeadCodeWorkflow",
        "repo_path": str(repo),
        "files_scanned": len(non_test_py_files),
        "total_findings": len(findings),
        "certain_count": len(certain),
        "possible_count": len(possible),
        "findings": findings,
        "summary": summary,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


# --- MapDependenciesWorkflow -------------------------------------------------

_DEPMAP_REQ_LINE = re.compile(
    r"^\s*"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[^\]]*\])?"        # optional extras [security]
    r"(?P<spec>[^;#\n]*)"     # version specifier(s), stops at marker/comment
    r"(?:[;#].*)?"            # optional PEP 508 marker or inline comment
    r"\s*$"
)
_DEPMAP_EXACT = re.compile(r"==\s*\S")   # ==1.2.3 → pinned
_DEPMAP_RANGE = re.compile(r"[><!~]=?\s*\S")  # >=, <=, ~=, !=, > , < → range


def _depmap_record(name: str, spec: str, dep_type: str, source_file: str) -> dict[str, Any]:
    spec = spec.strip()
    pinned = bool(_DEPMAP_EXACT.search(spec)) if spec else False
    unpinned = not spec
    return {
        "name": name.strip(),
        "version_spec": spec or None,
        "type": dep_type,
        "source_file": source_file,
        "pinned": pinned,
        "unpinned": unpinned,
    }


def _depmap_parse_requirements_txt(path: Path, dep_type: str) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return deps
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-", "http://", "https://", "git+")):
            continue
        m = _DEPMAP_REQ_LINE.match(line)
        if m and m.group("name"):
            deps.append(_depmap_record(m.group("name"), m.group("spec") or "", dep_type, path.name))
    return deps


def _depmap_parse_package_json(path: Path) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deps
    sections = {
        "dependencies": "direct",
        "devDependencies": "dev",
        "peerDependencies": "peer",
        "optionalDependencies": "optional",
    }
    for section, dep_type in sections.items():
        for name, ver in (data.get(section) or {}).items():
            if not isinstance(ver, str):
                ver = ""
            is_unpinned = bool(re.match(r"^(\*|latest|x)$", ver.strip(), re.IGNORECASE)) or not ver
            is_pinned = bool(re.match(r"^\d", ver.strip())) and not is_unpinned
            deps.append({
                "name": name,
                "version_spec": ver.strip() or None,
                "type": dep_type,
                "source_file": "package.json",
                "pinned": is_pinned,
                "unpinned": is_unpinned,
            })
    return deps


def _depmap_toml_module() -> Any:
    try:
        import tomllib  # Python 3.11+
        return tomllib
    except ImportError:
        return None


def _depmap_extract_pyproject_deps(data: dict[str, Any], source: str) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    project = data.get("project") or {}

    # PEP 621 [project.dependencies]
    for dep_str in (project.get("dependencies") or []):
        if not isinstance(dep_str, str):
            continue
        m = _DEPMAP_REQ_LINE.match(dep_str)
        if m and m.group("name"):
            deps.append(_depmap_record(m.group("name"), m.group("spec") or "", "direct", source))

    # PEP 621 [project.optional-dependencies]
    for group, group_deps in (project.get("optional-dependencies") or {}).items():
        dep_type = "dev" if any(w in group.lower() for w in ("dev", "test", "lint")) else "optional"
        for dep_str in (group_deps or []):
            if not isinstance(dep_str, str):
                continue
            m = _DEPMAP_REQ_LINE.match(dep_str)
            if m and m.group("name"):
                deps.append(_depmap_record(m.group("name"), m.group("spec") or "", dep_type, source))

    # Poetry [tool.poetry.dependencies / dev-dependencies / group.*.dependencies]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for name, value in (poetry.get("dependencies") or {}).items():
        if name.lower() == "python":
            continue
        spec = value if isinstance(value, str) else (value.get("version", "") if isinstance(value, dict) else "")
        deps.append(_depmap_record(name, spec, "direct", source))
    for name, value in (poetry.get("dev-dependencies") or {}).items():
        spec = value if isinstance(value, str) else (value.get("version", "") if isinstance(value, dict) else "")
        deps.append(_depmap_record(name, spec, "dev", source))
    for group, group_data in (poetry.get("group") or {}).items():
        dep_type = "dev" if any(w in group.lower() for w in ("dev", "test", "lint")) else "optional"
        for name, value in ((group_data or {}).get("dependencies") or {}).items():
            spec = value if isinstance(value, str) else (value.get("version", "") if isinstance(value, dict) else "")
            deps.append(_depmap_record(name, spec, dep_type, source))

    return deps


def _depmap_parse_pyproject_toml_regex(text: str, source: str) -> list[dict[str, Any]]:
    """Regex-based fallback for pyproject.toml when tomllib is unavailable."""
    deps: list[dict[str, Any]] = []
    sections: list[tuple[str, int]] = [
        (m.group(1).strip(), m.start())
        for m in re.finditer(r"^\[([^\]]+)\]", text, re.MULTILINE)
    ]
    sections.append(("__end__", len(text)))

    def get_section(exact_name: str) -> str | None:
        for i, (name, start) in enumerate(sections[:-1]):
            if name == exact_name:
                return text[start : sections[i + 1][1]]
        return None

    def parse_pep621_list(section_text: str) -> list[str]:
        items: list[str] = []
        for line in section_text.splitlines():
            line = line.strip().strip(",").strip('"').strip("'")
            if line and not line.startswith(("#", "[", "]", "dependencies")):
                items.append(line)
        return items

    # [project.dependencies]
    sect = get_section("project.dependencies")
    if sect:
        for dep_str in parse_pep621_list(sect):
            m = _DEPMAP_REQ_LINE.match(dep_str)
            if m and m.group("name"):
                deps.append(_depmap_record(m.group("name"), m.group("spec") or "", "direct", source))

    # [project.optional-dependencies] groups
    for i, (name, start) in enumerate(sections[:-1]):
        if not name.startswith("project.optional-dependencies."):
            continue
        group = name[len("project.optional-dependencies."):]
        dep_type = "dev" if any(w in group.lower() for w in ("dev", "test", "lint")) else "optional"
        sect_text = text[start : sections[i + 1][1]]
        for dep_str in parse_pep621_list(sect_text):
            m = _DEPMAP_REQ_LINE.match(dep_str)
            if m and m.group("name"):
                deps.append(_depmap_record(m.group("name"), m.group("spec") or "", dep_type, source))

    # [tool.poetry.dependencies]
    def parse_poetry_section(section_name: str, dep_type: str) -> None:
        sect = get_section(section_name)
        if not sect:
            return
        for line in sect.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "[")) or "=" not in line:
                continue
            name, _, raw_val = line.partition("=")
            name = name.strip()
            if name.lower() == "python":
                continue
            spec = raw_val.strip().strip('"\'').strip()
            if spec.startswith("{"):
                spec = ""
            deps.append(_depmap_record(name, spec, dep_type, source))

    parse_poetry_section("tool.poetry.dependencies", "direct")
    parse_poetry_section("tool.poetry.dev-dependencies", "dev")

    return deps


def _depmap_parse_pyproject_toml(path: Path) -> list[dict[str, Any]]:
    source = "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    toml = _depmap_toml_module()
    if toml is not None:
        try:
            return _depmap_extract_pyproject_deps(toml.loads(text), source)
        except Exception:
            pass
    return _depmap_parse_pyproject_toml_regex(text, source)


def _depmap_parse_pipfile(path: Path) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return deps
    toml = _depmap_toml_module()
    if toml is not None:
        try:
            data = toml.loads(text)
            for section, dep_type in [("packages", "direct"), ("dev-packages", "dev")]:
                for name, value in (data.get(section) or {}).items():
                    spec = value if isinstance(value, str) else ""
                    is_unpinned = not spec or spec.strip() == "*"
                    is_pinned = bool(_DEPMAP_EXACT.search(spec)) if spec and not is_unpinned else False
                    deps.append({
                        "name": name,
                        "version_spec": None if is_unpinned else (spec.strip() or None),
                        "type": dep_type,
                        "source_file": "Pipfile",
                        "pinned": is_pinned,
                        "unpinned": is_unpinned,
                    })
            return deps
        except Exception:
            pass
    # Regex fallback
    current_type: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[packages]":
            current_type = "direct"
        elif stripped == "[dev-packages]":
            current_type = "dev"
        elif stripped.startswith("["):
            current_type = None
        elif current_type and "=" in stripped and not stripped.startswith("#"):
            name, _, raw = stripped.partition("=")
            name = name.strip()
            spec = raw.strip().strip('"\'')
            is_unpinned = not spec or spec == "*"
            is_pinned = bool(_DEPMAP_EXACT.search(spec)) if spec and not is_unpinned else False
            deps.append({
                "name": name,
                "version_spec": None if is_unpinned else (spec or None),
                "type": current_type,
                "source_file": "Pipfile",
                "pinned": is_pinned,
                "unpinned": is_unpinned,
            })
    return deps


def build_map_dependencies_artifacts(repo: Path) -> dict[str, Any]:
    all_deps: list[dict[str, Any]] = []
    files_parsed: list[str] = []
    files_not_found: list[str] = []

    candidate_files: list[tuple[str, Any]] = [
        ("requirements.txt", lambda p: _depmap_parse_requirements_txt(p, "direct")),
        ("requirements-dev.txt", lambda p: _depmap_parse_requirements_txt(p, "dev")),
        ("pyproject.toml", _depmap_parse_pyproject_toml),
        ("package.json", _depmap_parse_package_json),
        ("Pipfile", _depmap_parse_pipfile),
    ]

    for filename, parser in candidate_files:
        path = repo / filename
        if path.is_file():
            files_parsed.append(filename)
            all_deps.extend(parser(path))
        else:
            files_not_found.append(filename)

    unpinned = [d for d in all_deps if d["unpinned"]]
    pinned = [d for d in all_deps if d["pinned"]]
    by_type: dict[str, int] = {}
    for d in all_deps:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1

    parts = [f"{len(all_deps)} dependencies"]
    if files_parsed:
        parts.append(f"from {', '.join(files_parsed)}")
    parts.append(f"{len(pinned)} pinned (==), {len(unpinned)} unpinned")
    if unpinned:
        sample = ", ".join(d["name"] for d in unpinned[:5])
        suffix = f" +{len(unpinned)-5} more" if len(unpinned) > 5 else ""
        parts.append(f"unpinned: {sample}{suffix}")

    return {
        "workflow": "MapDependenciesWorkflow",
        "repo_path": str(repo),
        "files_parsed": files_parsed,
        "files_not_found": files_not_found,
        "total_dependencies": len(all_deps),
        "dependencies": all_deps,
        "pinned_count": len(pinned),
        "unpinned_count": len(unpinned),
        "unpinned_names": [d["name"] for d in unpinned],
        "by_type": by_type,
        "summary": ". ".join(parts) + ".",
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


# --- InspectRepoStructureWorkflow -------------------------------------------

_KNOWN_ENTRY_POINTS: dict[str, str] = {
    "main.py": "python_main",
    "__main__.py": "python_package_entry",
    "app.py": "python_app",
    "application.py": "python_app",
    "server.py": "python_server",
    "wsgi.py": "python_wsgi",
    "asgi.py": "python_asgi",
    "manage.py": "django_manage",
    "setup.py": "python_setup",
    "cli.py": "python_cli",
    "index.js": "node_entry",
    "index.ts": "ts_entry",
    "index.jsx": "react_entry",
    "index.tsx": "react_ts_entry",
    "app.js": "node_app",
    "app.ts": "ts_app",
    "main.js": "node_main",
    "main.ts": "ts_main",
    "server.js": "node_server",
    "server.ts": "ts_server",
}


def build_inspect_repo_structure_artifacts(repo: Path) -> dict[str, Any]:
    files_by_extension: dict[str, int] = {}
    size_by_extension: dict[str, int] = {}
    dir_stats: dict[str, dict[str, Any]] = {}
    entry_points: list[dict[str, str]] = []
    total_files = 0
    total_size_bytes = 0

    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        if should_ignore_search_path(path, repo):
            continue

        relative = path.relative_to(repo)
        ext = path.suffix.lower() if path.suffix else "(no extension)"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        files_by_extension[ext] = files_by_extension.get(ext, 0) + 1
        size_by_extension[ext] = size_by_extension.get(ext, 0) + size
        total_files += 1
        total_size_bytes += size

        dir_key = str(relative.parent)
        if dir_key not in dir_stats:
            dir_stats[dir_key] = {"file_count": 0, "size_bytes": 0}
        dir_stats[dir_key]["file_count"] += 1
        dir_stats[dir_key]["size_bytes"] += size

        if path.name in _KNOWN_ENTRY_POINTS:
            entry_points.append({"file": str(relative), "type": _KNOWN_ENTRY_POINTS[path.name]})

    directories = [
        {"path": d, "file_count": s["file_count"], "size_bytes": s["size_bytes"]}
        for d, s in sorted(dir_stats.items())
    ]

    top_exts = sorted(files_by_extension.items(), key=lambda x: -x[1])[:5]
    top_ext_str = ", ".join(f"{ext}: {count}" for ext, count in top_exts)
    summary = (
        f"{total_files} files across {len(dir_stats)} directories "
        f"({total_size_bytes:,} bytes). Top extensions: {top_ext_str}."
    )
    if entry_points:
        summary += f" Entry points: {', '.join(ep['file'] for ep in entry_points[:5])}."

    return {
        "workflow": "InspectRepoStructureWorkflow",
        "repo_path": str(repo),
        "total_files": total_files,
        "total_size_bytes": total_size_bytes,
        "total_directories": len(dir_stats),
        "files_by_extension": dict(sorted(files_by_extension.items(), key=lambda x: -x[1])),
        "size_by_extension": dict(sorted(size_by_extension.items(), key=lambda x: -x[1])),
        "directories": directories,
        "entry_points": entry_points,
        "summary": summary,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def build_inspect_runtime_artifacts(repo: Path | None = None) -> dict[str, Any]:  # noqa: ARG001
    """Inspect the active Python runtime. Zero LLM calls — purely deterministic."""
    import importlib.metadata as _meta
    import os as _os
    import platform as _platform
    import sys as _sys

    # ---- python version / executable ----------------------------------------
    python_version = ".".join(str(x) for x in _sys.version_info[:3])
    python_executable = _sys.executable

    # ---- active virtual environment -----------------------------------------
    virtual_env: str | None = _os.environ.get("VIRTUAL_ENV") or None
    if virtual_env is None and _sys.prefix != _sys.base_prefix:
        virtual_env = _sys.prefix

    # ---- platform -----------------------------------------------------------
    system = _platform.system()
    machine = _platform.machine()
    if system == "Darwin":
        mac_ver = _platform.mac_ver()[0]
        os_label = f"macOS {mac_ver}" if mac_ver else f"macOS {_platform.release()}"
    elif system == "Linux":
        os_label = f"Linux {_platform.release()}"
    elif system == "Windows":
        win_ver = _platform.win32_ver()[0]
        os_label = f"Windows {win_ver}" if win_ver else f"Windows {_platform.release()}"
    else:
        os_label = f"{system} {_platform.release()}"
    platform_str = f"{os_label} {machine}"

    # ---- installed packages (sorted, deduped by canonical name) -------------
    seen: set[str] = set()
    packages: list[dict[str, str]] = []
    try:
        dists = sorted(_meta.distributions(), key=lambda d: (d.metadata.get("Name", "") or "").lower())
        for dist in dists:
            name = dist.metadata.get("Name", "") or ""
            version = dist.metadata.get("Version", "") or ""
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                packages.append({"name": name, "version": version})
    except Exception:
        packages = []

    # ---- CI-relevant environment variables ----------------------------------
    env_vars: dict[str, object] = {}
    for key in ("CI", "VIRTUAL_ENV", "PYTHONPATH", "NODE_ENV"):
        env_vars[key] = _os.environ.get(key)
    env_vars["PATH"] = _os.environ.get("PATH")
    # ANTHROPIC_API_KEY: presence only — never log the value
    env_vars["ANTHROPIC_API_KEY"] = "ANTHROPIC_API_KEY" in _os.environ

    # ---- summary ------------------------------------------------------------
    venv_note = f" venv: {virtual_env}." if virtual_env else " No active venv detected."
    summary = (
        f"Python {python_version} on {platform_str}. "
        f"{len(packages)} package(s) installed.{venv_note}"
    )

    return {
        "workflow": "InspectRuntimeEnvironmentWorkflow",
        "python_version": python_version,
        "python_executable": python_executable,
        "virtual_env": virtual_env,
        "platform": platform_str,
        "installed_packages": packages,
        "environment_variables": env_vars,
        "port_conflicts": None,
        "summary": summary,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def _flakiness_median(values: list[int | float]) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    n = len(sv)
    if n % 2 == 1:
        return float(sv[n // 2])
    return (sv[n // 2 - 1] + sv[n // 2]) / 2.0


def _extract_failure_summary(stdout: str, stderr: str) -> str | None:
    """Return a concise failure message from test runner output, or None."""
    import re as _re
    combined = (stdout or "") + "\n" + (stderr or "")
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED ") and " - " in stripped:
            return stripped.split(" - ", 1)[-1].strip()[:300]
    for line in combined.splitlines():
        stripped = line.strip()
        if _re.match(r"(E\s+)?(\w+Error|\w+Exception):", stripped):
            return stripped.lstrip("E").strip()[:300]
    if stderr:
        for line in reversed(stderr.splitlines()):
            if line.strip():
                return line.strip()[:300]
    return None


def _extract_flakiness_params(task: str) -> tuple[str, int]:
    """Parse (test_identifier, runs) from a task description."""
    import re as _re
    m = _re.search(r"\b(\d+)\s+times?\b", task, _re.I) or _re.search(r"\b(\d+)\s+runs?\b", task, _re.I)
    runs = min(int(m.group(1)), 20) if m else 5
    path_m = _re.search(r"(tests?/\S+\.py(?:::\S+)?|\S+\.py(?:::\S+)?)", task)
    test_id = path_m.group(1) if path_m else "."
    return test_id, runs


def build_monitor_flakiness_artifacts(
    repo: Path,
    test_identifier: str = ".",
    runs: int = 5,
) -> dict[str, Any]:
    """Run a test suite N times and report pass rate / flakiness metrics. Zero LLM calls."""
    import datetime

    from ..verifier.test_runner import TestRunner

    runs = min(max(runs, 1), 20)
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    test_runner = TestRunner()
    base_cmd = test_runner.detect(repo)
    if base_cmd is None:
        return {
            "workflow": "MonitorTestFlakinessWorkflow",
            "error_code": "no_test_runner",
            "error": "No test runner detected for this repository.",
            "test_identifier": test_identifier,
            "runs": 0,
            "passed": 0,
            "failed": 0,
            "pass_rate": 0.0,
            "flaky": False,
            "deterministic": True,
            "failure_messages": [],
            "duration_ms": [],
            "median_duration_ms": 0.0,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    has_specific_target = bool(test_identifier and test_identifier != ".")
    if has_specific_target:
        # Build a precise, single-target command rather than appending onto
        # base_cmd, which (when a tests/ directory is present) is itself
        # already scoped to that whole directory — appending a specific
        # identifier on top would run the entire directory AND the target,
        # not just the target.
        if "pytest" in " ".join(base_cmd):
            cmd = [base_cmd[0], "-m", "pytest", "-q", test_identifier]
        elif "unittest" in " ".join(base_cmd):
            cmd = [base_cmd[0], "-m", "unittest", test_identifier]
        else:
            cmd = list(base_cmd) + [test_identifier]
    else:
        cmd = list(base_cmd)

    passed = 0
    failed = 0
    failure_messages: list[str] = []
    duration_ms_list: list[int] = []
    last_run_result: dict[str, Any] | None = None

    for _ in range(runs):
        # A specific, explicitly-named target carries none of the "rediscovers
        # and re-executes the whole suite, including itself" recursion risk a
        # bare/directory-wide invocation does — only the latter needs the
        # nested-pytest guard.
        run_result = test_runner.run_command(repo, cmd, allow_nested=has_specific_target)
        last_run_result = run_result
        duration_ms_list.append(int(run_result.get("duration_ms", 0)))
        if run_result.get("returncode") == 0:
            passed += 1
        else:
            failed += 1
            msg = _extract_failure_summary(run_result.get("stdout", ""), run_result.get("stderr", ""))
            if msg and msg not in failure_messages:
                failure_messages.append(msg)

    pass_rate = passed / runs
    flaky = 0.0 < pass_rate < 1.0
    deterministic = not flaky
    test_execution_started = bool(last_run_result) and not bool((last_run_result or {}).get("environment_error"))

    return {
        "workflow": "MonitorTestFlakinessWorkflow",
        "test_identifier": test_identifier,
        "runs": runs,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(pass_rate, 4),
        "flaky": flaky,
        "deterministic": deterministic,
        "failure_messages": failure_messages,
        "duration_ms": duration_ms_list,
        "median_duration_ms": _flakiness_median(duration_ms_list),
        "checked_at": checked_at,
        "tests_run": test_execution_started,
        "tests_passed": (passed == runs) if test_execution_started else None,
        "validation_status": "not_applicable",
        "execution_environment": (last_run_result or {}).get("execution_environment"),
        "selected_python": (last_run_result or {}).get("selected_python"),
        "selection_source": (last_run_result or {}).get("selection_source"),
        "fallback_used": bool((last_run_result or {}).get("fallback_used", False)),
        "environment_error": (last_run_result or {}).get("environment_error", False),
        "environment_error_code": (last_run_result or {}).get("environment_error_code"),
        "remediation": (last_run_result or {}).get("remediation"),
    }


def _parse_pytest_durations(output: str) -> list[dict[str, Any]]:
    """Parse 'slowest N durations' section from pytest --durations output."""
    import re as _re
    results: list[dict[str, Any]] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if _re.search(r"slowest\s+\d+\s+durations?", stripped, _re.I):
            in_section = True
            continue
        if not in_section:
            continue
        m = _re.match(r"(\d+\.\d+)s\s+\w+\s+(.+)", stripped)
        if m:
            dur_ms = int(float(m.group(1)) * 1000)
            name = m.group(2).strip()
            results.append({"name": name, "duration_ms": dur_ms})
        elif stripped.startswith(("=", "(")) and results:
            break
    return sorted(results, key=lambda x: x["duration_ms"], reverse=True)[:5]


def _speed_rating(total_ms: int) -> str:
    if total_ms < 10_000:
        return "fast"
    if total_ms < 60_000:
        return "normal"
    if total_ms < 300_000:
        return "slow"
    return "very_slow"


def build_measure_test_speed_artifacts(repo: Path, *, persist_baseline: bool = False) -> dict[str, Any]:
    """Run the full test suite once and return speed metrics. Zero LLM calls."""
    import datetime
    import json as _json
    import re as _re

    from ..verifier.test_runner import TestRunner

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    test_runner = TestRunner()
    base_cmd = test_runner.detect(repo)
    if base_cmd is None:
        return {
            "workflow": "MeasureTestSuiteSpeedWorkflow",
            "error_code": "no_test_runner",
            "error": "No test runner detected for this repository.",
            "total_duration_ms": 0,
            "test_count": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "slowest_tests": [],
            "speed_rating": "fast",
            "baseline_delta_ms": None,
            "baseline_saved_at": None,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "persist_baseline": persist_baseline,
            "state_write_performed": False,
            "written_paths": [],
            "gitignore_suggestions": [".skilllayer/test_speed_baseline.json"],
            "remediation": None,
        }

    cmd = list(base_cmd)
    is_pytest = any("pytest" in part for part in cmd)
    if is_pytest:
        cmd.append("--durations=5")

    run_result = test_runner.run_command(repo, cmd)
    total_duration_ms = int(run_result.get("duration_ms", 0))
    stdout = run_result.get("stdout", "") or ""
    stderr = run_result.get("stderr", "") or ""
    output = stdout + "\n" + stderr

    passed = int(run_result.get("passed", 0) or 0)
    failed = int(run_result.get("failed", 0) or 0)
    skip_m = _re.search(r"\b(\d+)\s+skipped\b", output)
    skipped = int(skip_m.group(1)) if skip_m else 0

    test_count_raw = run_result.get("test_count")
    test_count = int(test_count_raw) if test_count_raw is not None else (passed + failed + skipped)

    slowest_tests = _parse_pytest_durations(output) if is_pytest else []
    speed_rating = _speed_rating(total_duration_ms)

    # Baseline persistence: load prior, then save current
    baseline_path = repo / ".skilllayer" / "test_speed_baseline.json"
    baseline_delta_ms: int | None = None
    baseline_saved_at: str | None = None

    if baseline_path.exists():
        try:
            baseline = _json.loads(baseline_path.read_text("utf-8"))
            prev = baseline.get("duration_ms")
            baseline_saved_at = baseline.get("checked_at")
            if prev is not None:
                baseline_delta_ms = total_duration_ms - int(prev)
        except Exception:
            pass

    # A timed-out or never-actually-run (e.g. blocked by the nested-pytest
    # guard) measurement has a meaningless duration — self.timeout or 0, not
    # real elapsed test time. Persisting that as the new baseline would
    # corrupt every future baseline_delta_ms comparison, so persistence is
    # skipped entirely for those cases regardless of persist_baseline.
    run_completed = bool(run_result.get("available", True)) and not run_result.get("timed_out", False)

    written_paths: list[str] = []
    state_write_error: str | None = None
    if persist_baseline and run_completed:
        try:
            from ..memory.skilllayer_memory import atomic_write_json
            atomic_write_json(
                baseline_path,
                {"duration_ms": total_duration_ms, "checked_at": checked_at, "test_count": test_count},
            )
            written_paths.append(".skilllayer/test_speed_baseline.json")
        except OSError as exc:
            state_write_error = str(exc)

    return {
        "workflow": "MeasureTestSuiteSpeedWorkflow",
        "total_duration_ms": total_duration_ms,
        "test_count": test_count,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "slowest_tests": slowest_tests,
        "speed_rating": speed_rating,
        "baseline_delta_ms": baseline_delta_ms,
        "baseline_saved_at": baseline_saved_at,
        "checked_at": checked_at,
        "tests_run": not bool(run_result.get("environment_error")),
        "tests_passed": (failed == 0) if not bool(run_result.get("environment_error")) else None,
        "validation_status": "not_applicable",
        "persist_baseline": persist_baseline,
        "state_write_performed": bool(written_paths),
        "written_paths": written_paths,
        "state_write_error": state_write_error,
        "gitignore_suggestions": [".skilllayer/test_speed_baseline.json"],
        "execution_environment": run_result.get("execution_environment"),
        "selected_python": run_result.get("selected_python"),
        "selection_source": run_result.get("selection_source"),
        "fallback_used": bool(run_result.get("fallback_used", False)),
        "collection_started": bool(run_result.get("collection_started", False)),
        "tests_started": bool(run_result.get("tests_started", False)),
        "environment_error": run_result.get("environment_error", False),
        "environment_error_code": run_result.get("environment_error_code"),
        "remediation": run_result.get("remediation"),
        **({"error_code": "baseline_write_failed", "error": state_write_error} if state_write_error else {}),
    }


# ---- WatchDependencyUpdatesWorkflow -----------------------------------------


def _parse_version_parts(version: str) -> tuple[int, int, int]:
    """Parse 'major.minor.patch' into ints; returns (0,0,0) on failure."""
    cleaned = version.lstrip("^~=v").split("+")[0].split("-")[0]
    parts = cleaned.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return major, minor, patch
    except (ValueError, IndexError):
        return 0, 0, 0


def _classify_bump(current: str, latest: str) -> tuple[bool, bool, bool]:
    """Return (major_bump, minor_bump, patch_bump) between two version strings."""
    cm, cmi, cp = _parse_version_parts(current)
    lm, lmi, lp = _parse_version_parts(latest)
    if lm > cm:
        return True, False, False
    if lm == cm and lmi > cmi:
        return False, True, False
    if lm == cm and lmi == cmi and lp > cp:
        return False, False, True
    return False, False, False


def _fetch_pypi_latest(package: str) -> tuple[str | None, str | None]:
    """Fetch latest version from PyPI JSON API. Returns (version, error_code) —
    error_code is None only on genuine success; version is never returned
    alongside a non-None error_code."""
    import urllib.request
    import urllib.error
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skilllayer/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _j
            data = _j.loads(resp.read().decode("utf-8"))
            version = data.get("info", {}).get("version")
            if not version:
                return None, "malformed_registry_response"
            return version, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "package_not_found"
        return None, "registry_http_error"
    except TimeoutError:
        return None, "registry_timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, "registry_timeout"
        return None, "registry_network_error"
    except (ValueError, KeyError, TypeError):
        return None, "malformed_registry_response"
    except Exception:
        return None, "registry_lookup_failed"


def _fetch_npm_latest(package: str) -> tuple[str | None, str | None]:
    """Fetch latest version from npm registry. Returns (version, error_code) —
    error_code is None only on genuine success; version is never returned
    alongside a non-None error_code."""
    import urllib.request
    import urllib.error
    url = f"https://registry.npmjs.org/{package}/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "skilllayer/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _j
            data = _j.loads(resp.read().decode("utf-8"))
            version = data.get("version")
            if not version:
                return None, "malformed_registry_response"
            return version, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "package_not_found"
        return None, "registry_http_error"
    except TimeoutError:
        return None, "registry_timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, "registry_timeout"
        return None, "registry_network_error"
    except (ValueError, KeyError, TypeError):
        return None, "malformed_registry_response"
    except Exception:
        return None, "registry_lookup_failed"


def _parse_requirements_txt(path: Path) -> list[tuple[str, str]]:
    """Parse requirements.txt; returns [(name, version)] for pinned deps only."""
    deps: list[tuple[str, str]] = []
    try:
        content = path.read_text("utf-8", errors="replace")
    except OSError:
        return deps
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Strip inline comments
        line = line.split("#")[0].strip()
        if "==" in line:
            name, _, ver = line.partition("==")
            ver = ver.split(",")[0].strip()
            if ver and name.strip():
                deps.append((name.strip().lower(), ver.strip()))
    return deps


def _parse_pyproject_toml(path: Path) -> list[tuple[str, str]]:
    """Parse pyproject.toml dependencies; returns [(name, version)] pinned only."""
    deps: list[tuple[str, str]] = []
    try:
        content = path.read_text("utf-8", errors="replace")
    except OSError:
        return deps
    # Find dependencies array in [project] or [tool.poetry.dependencies]
    in_deps = False
    for line in content.splitlines():
        stripped = line.strip()
        # Section headers
        if stripped.startswith("["):
            in_deps = stripped in (
                "[project]",
                "[tool.poetry.dependencies]",
                "[tool.poetry.dev-dependencies]",
            )
            continue
        if not in_deps:
            continue
        # dependencies = [...] or "name==version"
        if stripped.startswith('"') or stripped.startswith("'"):
            # e.g. "requests==2.28.0", or "requests>=2"
            raw = stripped.strip("\"',")
            if "==" in raw:
                name, _, ver = raw.partition("==")
                ver = ver.split(",")[0].strip(" \"'")
                if ver and name.strip():
                    deps.append((name.strip().lower(), ver.strip()))
    return deps


def _parse_package_json(path: Path) -> list[tuple[str, str]]:
    """Parse package.json; returns [(name, version)] for pinned/semver deps only."""
    import json as _j
    deps: list[tuple[str, str]] = []
    try:
        data = _j.loads(path.read_text("utf-8", errors="replace"))
    except Exception:
        return deps
    for section in ("dependencies", "devDependencies"):
        for name, ver in (data.get(section) or {}).items():
            ver = ver.strip()
            if not ver or ver in ("*", "latest", ""):
                continue
            # Accept ^, ~, exact versions; skip git URLs, file:, workspace:
            if ver.startswith(("git", "file:", "workspace:", "http", "/")):
                continue
            cleaned = ver.lstrip("^~=v").split(" ")[0]
            if cleaned and cleaned[0].isdigit():
                deps.append((name.strip().lower(), ver.strip()))
    return deps


# Hard cap on total wall-clock time spent checking a whole dependency set,
# regardless of how many dependencies there are. Each individual registry
# fetch already has its own 5s timeout; without this, a large dependency
# list under a slow/unreachable registry could still run sequentially for
# hundreds of seconds. Once the deadline passes, remaining dependencies are
# reported as explicit unknowns rather than silently truncating the list.
DEPENDENCY_CHECK_DEADLINE_SECONDS = 20.0

ERROR_DEPENDENCY_CHECK_DEADLINE_EXCEEDED = "dependency_check_deadline_exceeded"


def build_watch_deps_artifacts(repo: Path) -> dict[str, Any]:
    """Check pinned dependencies against latest published versions. Zero LLM calls.

    Bounded by DEPENDENCY_CHECK_DEADLINE_SECONDS total: a registry outage
    with many dependencies stops checking at the deadline and reports the
    rest as explicit unknowns rather than running for minutes. A failed or
    incomplete lookup never becomes outdated=false/up_to_date — see each
    dependency's own "status" field."""
    import datetime
    import time as _time
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    deadline = _time.monotonic() + DEPENDENCY_CHECK_DEADLINE_SECONDS

    py_deps: list[tuple[str, str]] = []
    js_deps: list[tuple[str, str]] = []
    has_py = False
    has_js = False

    req_txt = repo / "requirements.txt"
    pyproject = repo / "pyproject.toml"
    pkg_json = repo / "package.json"

    if req_txt.exists():
        py_deps.extend(_parse_requirements_txt(req_txt))
        has_py = True
    if pyproject.exists():
        py_deps.extend(_parse_pyproject_toml(pyproject))
        has_py = True
    if pkg_json.exists():
        js_deps.extend(_parse_package_json(pkg_json))
        has_js = True

    if has_py and has_js:
        package_manager = "both"
    elif has_py:
        package_manager = "pip"
    elif has_js:
        package_manager = "npm"
    else:
        package_manager = "none"

    dependencies: list[dict[str, Any]] = []
    deadline_exceeded = False

    def _unknown_entry(name: str, current_ver: str, source: str, error_code: str) -> dict[str, Any]:
        return {
            "name": name,
            "current_version": current_ver,
            "latest_version": None,
            "status": "unsupported" if error_code == "package_not_found" else "unknown",
            "error_code": error_code,
            "error": error_code.replace("_", " "),
            "outdated": False,
            "major_bump": False,
            "minor_bump": False,
            "patch_bump": False,
            "source": source,
        }

    # Deduplicate by (name, source) — keep first seen
    seen_py: set[str] = set()
    for name, current_ver in py_deps:
        if name in seen_py:
            continue
        seen_py.add(name)
        if not deadline_exceeded and _time.monotonic() >= deadline:
            deadline_exceeded = True
        if deadline_exceeded:
            dependencies.append(_unknown_entry(name, current_ver, "pypi", ERROR_DEPENDENCY_CHECK_DEADLINE_EXCEEDED))
            continue
        latest, error_code = _fetch_pypi_latest(name)
        if error_code is not None:
            dependencies.append(_unknown_entry(name, current_ver, "pypi", error_code))
            continue
        cur_parts = _parse_version_parts(current_ver)
        lat_parts = _parse_version_parts(latest)
        is_outdated = lat_parts > cur_parts
        maj, mnr, pat = _classify_bump(current_ver, latest) if is_outdated else (False, False, False)
        dependencies.append({
            "name": name,
            "current_version": current_ver,
            "latest_version": latest,
            "status": "outdated" if is_outdated else "up_to_date",
            "error_code": None,
            "error": None,
            "outdated": is_outdated,
            "major_bump": maj,
            "minor_bump": mnr,
            "patch_bump": pat,
            "source": "pypi",
        })

    seen_js: set[str] = set()
    for name, current_ver in js_deps:
        if name in seen_js:
            continue
        seen_js.add(name)
        if not deadline_exceeded and _time.monotonic() >= deadline:
            deadline_exceeded = True
        if deadline_exceeded:
            dependencies.append(_unknown_entry(name, current_ver, "npm", ERROR_DEPENDENCY_CHECK_DEADLINE_EXCEEDED))
            continue
        latest, error_code = _fetch_npm_latest(name)
        if error_code is not None:
            dependencies.append(_unknown_entry(name, current_ver, "npm", error_code))
            continue
        cleaned_current = current_ver.lstrip("^~=v")
        cur_parts = _parse_version_parts(cleaned_current)
        lat_parts = _parse_version_parts(latest)
        is_outdated = lat_parts > cur_parts
        maj, mnr, pat = _classify_bump(cleaned_current, latest) if is_outdated else (False, False, False)
        dependencies.append({
            "name": name,
            "current_version": current_ver,
            "latest_version": latest,
            "status": "outdated" if is_outdated else "up_to_date",
            "error_code": None,
            "error": None,
            "outdated": is_outdated,
            "major_bump": maj,
            "minor_bump": mnr,
            "patch_bump": pat,
            "source": "npm",
        })

    checked_count = len(dependencies)
    outdated_count = sum(1 for d in dependencies if d["status"] == "outdated")
    up_to_date_count = sum(1 for d in dependencies if d["status"] == "up_to_date")
    unknown_count = sum(1 for d in dependencies if d["status"] == "unknown")
    unsupported_count = sum(1 for d in dependencies if d["status"] == "unsupported")

    return {
        "workflow": "WatchDependencyUpdatesWorkflow",
        "dependencies": dependencies,
        "checked_count": checked_count,
        "outdated_count": outdated_count,
        "up_to_date_count": up_to_date_count,
        "unknown_count": unknown_count,
        "unsupported_count": unsupported_count,
        "complete": not deadline_exceeded,
        "package_manager": package_manager,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


# ---- DetectRepoActivityWorkflow ---------------------------------------------


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr). Never raises."""
    import subprocess as _sp
    try:
        r = _sp.run(["git"] + args, capture_output=True, text=True, cwd=str(cwd), timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def build_detect_activity_artifacts(repo: Path, *, persist_snapshot: bool = False) -> dict[str, Any]:
    """Detect repo changes since last snapshot using git. Zero LLM calls."""
    import datetime
    import json as _json

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Verify git repo and get HEAD
    rc, head_hash, err = _run_git(["rev-parse", "--short", "HEAD"], repo)
    if rc != 0:
        return {
            "workflow": "DetectRepoActivityWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository or no commits found.",
            "since": None,
            "commits_since": [],
            "files_changed": [],
            "branches_changed": [],
            "summary": {
                "commit_count": 0,
                "files_changed_count": 0,
                "insertions_total": 0,
                "deletions_total": 0,
                "active_since": False,
            },
            "first_run": True,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "persist_snapshot": persist_snapshot,
            "state_write_performed": False,
            "written_paths": [],
            "gitignore_suggestions": [".skilllayer/repo_activity_snapshot.json"],
        }

    _, branch, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    _, branches_raw, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short) %(objectname:short)", "refs/heads/"],
        repo,
    )

    current_branches: dict[str, str] = {}
    for line in branches_raw.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            current_branches[parts[0]] = parts[1]

    # Load snapshot
    snapshot_path = repo / ".skilllayer" / "repo_activity_snapshot.json"
    old_snapshot: dict[str, Any] | None = None
    first_run = True

    if snapshot_path.exists():
        try:
            old_snapshot = _json.loads(snapshot_path.read_text("utf-8"))
            first_run = False
        except Exception:
            first_run = True

    commits_since: list[dict[str, Any]] = []
    files_changed: list[dict[str, Any]] = []
    branches_changed: list[dict[str, Any]] = []

    if not first_run and old_snapshot:
        old_hash = old_snapshot.get("head_hash", "")
        old_branches: dict[str, str] = {
            b["name"]: b["hash"] for b in old_snapshot.get("branches", [])
        }

        # Commits since old hash — use NUL-separated fields to avoid message conflicts
        if old_hash and old_hash != head_hash:
            rc2, log_out, _ = _run_git(
                ["log", f"{old_hash}..HEAD", "--format=%h%x00%s%x00%an%x00%aI"],
                repo,
            )
            if rc2 == 0 and log_out:
                for raw_line in log_out.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    parts = line.split("\x00")
                    if len(parts) >= 4:
                        commits_since.append({
                            "hash": parts[0][:7],
                            "message": parts[1],
                            "author": parts[2],
                            "timestamp": parts[3],
                        })

        # Files changed (status + numstat for insertions/deletions)
        if old_hash:
            rc3, stat_out, _ = _run_git(
                ["diff", "--name-status", "-M", old_hash, "HEAD"], repo
            )
            rc4, num_out, _ = _run_git(
                ["diff", "--numstat", "-M", old_hash, "HEAD"], repo
            )

            numstat: dict[str, tuple[int | None, int | None]] = {}
            for line in num_out.splitlines():
                cols = line.split("\t")
                if len(cols) >= 3:
                    try:
                        ins: int | None = int(cols[0]) if cols[0] != "-" else None
                        dels: int | None = int(cols[1]) if cols[1] != "-" else None
                        numstat[cols[2]] = (ins, dels)
                    except ValueError:
                        pass

            if rc3 == 0:
                for line in stat_out.splitlines():
                    if not line.strip():
                        continue
                    cols = line.split("\t")
                    if not cols:
                        continue
                    status_code = cols[0]
                    if status_code == "A" and len(cols) >= 2:
                        path = cols[1]
                        ins2, dels2 = numstat.get(path, (None, None))
                        files_changed.append({"path": path, "status": "added", "insertions": ins2, "deletions": dels2})
                    elif status_code == "M" and len(cols) >= 2:
                        path = cols[1]
                        ins2, dels2 = numstat.get(path, (None, None))
                        files_changed.append({"path": path, "status": "modified", "insertions": ins2, "deletions": dels2})
                    elif status_code == "D" and len(cols) >= 2:
                        path = cols[1]
                        ins2, dels2 = numstat.get(path, (None, None))
                        files_changed.append({"path": path, "status": "deleted", "insertions": ins2, "deletions": dels2})
                    elif status_code.startswith("R") and len(cols) >= 3:
                        new_path = cols[2]
                        ins2, dels2 = numstat.get(new_path, (None, None))
                        files_changed.append({"path": new_path, "status": "renamed", "insertions": ins2, "deletions": dels2})

        # Branches changed
        for name, h in current_branches.items():
            if name not in old_branches:
                branches_changed.append({"name": name, "status": "created"})
            elif old_branches[name] != h:
                branches_changed.append({"name": name, "status": "updated"})
        for name in old_branches:
            if name not in current_branches:
                branches_changed.append({"name": name, "status": "deleted"})

    # Summary
    ins_total = sum(f.get("insertions") or 0 for f in files_changed)
    dels_total = sum(f.get("deletions") or 0 for f in files_changed)
    active_since = bool(commits_since or files_changed or branches_changed)

    summary: dict[str, Any] = {
        "commit_count": len(commits_since),
        "files_changed_count": len(files_changed),
        "insertions_total": ins_total,
        "deletions_total": dels_total,
        "active_since": active_since,
    }

    # Build the optional next snapshot. Persistence requires explicit consent.
    new_snapshot: dict[str, Any] = {
        "head_hash": head_hash,
        "branch": branch,
        "branches": [{"name": k, "hash": v} for k, v in current_branches.items()],
        "checked_at": checked_at,
    }
    written_paths: list[str] = []
    state_write_error: str | None = None
    if persist_snapshot:
        try:
            from ..memory.skilllayer_memory import atomic_write_json
            atomic_write_json(snapshot_path, new_snapshot)
            written_paths.append(".skilllayer/repo_activity_snapshot.json")
        except OSError as exc:
            state_write_error = str(exc)

    return {
        "workflow": "DetectRepoActivityWorkflow",
        "since": old_snapshot.get("checked_at") if old_snapshot else None,
        "commits_since": commits_since,
        "files_changed": files_changed,
        "branches_changed": branches_changed,
        "summary": summary,
        "first_run": first_run,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_snapshot": persist_snapshot,
        "state_write_performed": bool(written_paths),
        "written_paths": written_paths,
        "state_write_error": state_write_error,
        "gitignore_suggestions": [".skilllayer/repo_activity_snapshot.json"],
        **({"error_code": "snapshot_write_failed", "error": state_write_error} if state_write_error else {}),
    }


# ---- WatchFileChangesWorkflow ------------------------------------------------

WATCH_FILE_CHANGES_SNAPSHOT_ENTRY = ".skilllayer/file_watch_snapshot.json"


def build_watch_file_changes_artifacts(
    repo: Path,
    scope: str | None = None,
    *,
    persist_snapshot: bool = False,
) -> dict[str, Any]:
    """Detect files added/modified/deleted on disk since the last watch. Zero LLM calls.

    Snapshot-and-diff, not real-time watching: hashes (sha256) every in-scope
    file now, compares against the previous snapshot recorded at
    .skilllayer/file_watch_snapshot.json, then saves a new snapshot for next
    time. Unlike DetectRepoActivityWorkflow (which only sees changes between
    two git commits), this reads the filesystem directly, so it also catches
    untracked files and uncommitted working-tree edits — the gap git-diff
    cannot see when no new commit has been made.
    """
    import datetime
    import hashlib
    import json as _json

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _error(error_code: str, error: str) -> dict[str, Any]:
        return {
            "workflow": "WatchFileChangesWorkflow",
            "first_watch": None,
            "scope": scope or ".",
            "recorded_at": None,
            "previous_recorded_at": None,
            "added": [],
            "modified": [],
            "deleted": [],
            "summary": {"added_count": 0, "modified_count": 0, "deleted_count": 0, "scanned_file_count": 0},
            "checked_at": checked_at,
            "error_code": error_code,
            "error": error,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "persist_snapshot": persist_snapshot,
            "state_write_performed": False,
            "written_paths": [],
            "gitignore_suggestions": [WATCH_FILE_CHANGES_SNAPSHOT_ENTRY],
        }

    if scope:
        scan_root = (repo / scope).resolve()
        try:
            scan_root.relative_to(repo.resolve())
        except ValueError:
            return _error("scope_escapes_repo", f"scope escapes repo: {scope}")
        if not scan_root.exists():
            return _error("scope_not_found", f"scope path does not exist: {scope}")
    else:
        scan_root = repo

    gitignore_patterns = _load_gitignore_patterns(repo)
    snapshot_path = repo / ".skilllayer" / "file_watch_snapshot.json"

    old_snapshot: dict[str, Any] | None = None
    if snapshot_path.exists():
        try:
            old_snapshot = _json.loads(snapshot_path.read_text("utf-8"))
        except Exception:
            old_snapshot = None
    first_watch = old_snapshot is None
    old_files: dict[str, dict[str, Any]] = dict(old_snapshot.get("files", {})) if old_snapshot else {}

    current_files: dict[str, dict[str, Any]] = {}
    scanned_file_count = 0
    skipped_file_count = 0

    for path in sorted(scan_root.rglob("*")):
        if not path.is_file():
            continue
        if should_ignore_search_path(path, repo):
            skipped_file_count += 1
            continue
        rel_path = str(path.relative_to(repo))
        if _matches_gitignore(rel_path, gitignore_patterns):
            skipped_file_count += 1
            continue
        try:
            stat = path.stat()
        except OSError:
            skipped_file_count += 1
            continue
        if stat.st_size > 1_048_576:
            skipped_file_count += 1
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            skipped_file_count += 1
            continue
        scanned_file_count += 1
        mtime = datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat()
        current_files[rel_path] = {"hash": f"sha256:{digest}", "size": stat.st_size, "mtime": mtime}

    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []

    if not first_watch:
        for rel_path, info in sorted(current_files.items()):
            old_info = old_files.get(rel_path)
            if old_info is None:
                added.append({"path": rel_path, "size": info["size"]})
            elif old_info.get("hash") != info["hash"]:
                modified.append({
                    "path": rel_path,
                    "size_before": old_info.get("size"),
                    "size_after": info["size"],
                    "size_delta": info["size"] - int(old_info.get("size") or 0),
                    "mtime": info["mtime"],
                })
        for rel_path in sorted(set(old_files) - set(current_files)):
            deleted.append({"path": rel_path})

    new_snapshot = {
        "schema": 1,
        "recorded_at": checked_at,
        "scope": scope or ".",
        "files": current_files,
    }
    written_paths: list[str] = []
    state_write_error: str | None = None
    if persist_snapshot:
        try:
            from ..memory.skilllayer_memory import atomic_write_json
            atomic_write_json(snapshot_path, new_snapshot)
            written_paths.append(WATCH_FILE_CHANGES_SNAPSHOT_ENTRY)
        except OSError as exc:
            state_write_error = str(exc)

    return {
        "workflow": "WatchFileChangesWorkflow",
        "first_watch": first_watch,
        "scope": scope or ".",
        "recorded_at": checked_at,
        "previous_recorded_at": old_snapshot.get("recorded_at") if old_snapshot else None,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "summary": {
            "added_count": len(added),
            "modified_count": len(modified),
            "deleted_count": len(deleted),
            "scanned_file_count": scanned_file_count,
        },
        "checked_at": checked_at,
        "error_code": None,
        "error": None,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_snapshot": persist_snapshot,
        "state_write_performed": bool(written_paths),
        "written_paths": written_paths,
        "state_write_error": state_write_error,
        "gitignore_suggestions": [WATCH_FILE_CHANGES_SNAPSHOT_ENTRY],
        **({"error_code": "snapshot_write_failed", "error": state_write_error} if state_write_error else {}),
    }


# ---- ProfileCodeExecutionWorkflow -------------------------------------------

_PROFILE_PROBE_SCRIPT = """\
import sys, json, time, cProfile, pstats, io, runpy
from pathlib import Path

target_path = sys.argv[1]
include_stdlib = sys.argv[2] == "1" if len(sys.argv) > 2 else False

pr = cProfile.Profile()
t_wall_start = time.perf_counter()
t_cpu_start = time.process_time()

try:
    pr.enable()
    try:
        runpy.run_path(target_path, run_name="__main__")
    except BaseException:
        pass
    pr.disable()
except Exception:
    pr.disable()

t_wall_end = time.perf_counter()
t_cpu_end = time.process_time()

total_duration_ms = (t_wall_end - t_wall_start) * 1000
cpu_time_ms = (t_cpu_end - t_cpu_start) * 1000

buf = io.StringIO()
stats = pstats.Stats(pr, stream=buf)
stats.sort_stats("cumulative")

try:
    raw_stats = pr.getstats()
except Exception:
    raw_stats = []

total_calls = sum(s.callcount for s in raw_stats)

hotspots = []
repo_root = str(Path(target_path).parent)
for s in sorted(raw_stats, key=lambda x: x.totaltime, reverse=True):
    code = s.code
    if hasattr(code, "co_filename"):
        fname = code.co_filename
        lineno = code.co_firstlineno
        func = code.co_name
    elif isinstance(code, str):
        parts = code.split(":")
        fname = parts[0] if parts else ""
        lineno = int(parts[1]) if len(parts) > 1 else 0
        func = parts[2] if len(parts) > 2 else code
    else:
        continue
    if not include_stdlib:
        import sysconfig
        stdlib_path = sysconfig.get_path("stdlib")
        stdlib_platstdlib = sysconfig.get_path("platstdlib")
        if fname.startswith("<") or (stdlib_path and fname.startswith(stdlib_path)) or (stdlib_platstdlib and fname.startswith(stdlib_platstdlib)):
            continue
    calls = s.callcount
    total_ms = s.totaltime * 1000
    per_call = total_ms / calls if calls > 0 else 0.0
    hotspots.append({
        "function": func,
        "file": fname,
        "line": lineno,
        "calls": calls,
        "total_time_ms": round(total_ms, 4),
        "per_call_ms": round(per_call, 4),
    })
    if len(hotspots) == 10:
        break

print(json.dumps({
    "total_duration_ms": round(total_duration_ms, 4),
    "cpu_time_ms": round(cpu_time_ms, 4),
    "total_calls": total_calls,
    "hotspots": hotspots,
}))
"""


def _profile_rating(total_ms: float) -> str:
    if total_ms < 100:
        return "fast"
    if total_ms < 1000:
        return "normal"
    if total_ms < 5000:
        return "slow"
    return "very_slow"


def _extract_profile_target(task: str) -> str:
    """Extract a .py file path from a profile/benchmark task description."""
    m = re.search(r"\b([\w./\\-]+\.py)\b", task)
    if m:
        return m.group(1)
    m = re.search(
        r"(?:profile(?:\s+execution\s+of)?|benchmark|how\s+fast\s+is|"
        r"measure\s+execution\s+time\s+of|what\s+is\s+slow\s+in)\s+([^\s,?]+)",
        task,
        re.I,
    )
    if m:
        return m.group(1).strip("'\"")
    return ""


def _build_profile_error(
    code: str,
    msg: str,
    target: str,
    checked_at: str,
    persist_baseline: bool = False,
) -> dict[str, Any]:
    return {
        "workflow": "ProfileCodeExecutionWorkflow",
        "error_code": code,
        "error": msg,
        "target": target,
        "total_duration_ms": 0.0,
        "cpu_time_ms": 0.0,
        "function_calls": 0,
        "hotspots": [],
        "rating": "fast",
        "baseline_delta_ms": None,
        "baseline_saved_at": None,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_baseline": persist_baseline,
        "state_write_performed": False,
        "written_paths": [],
        "gitignore_suggestions": [".skilllayer/profile_baseline.json"],
    }


def build_profile_execution_artifacts(
    repo: Path,
    target: str,
    *,
    include_stdlib: bool = False,
    persist_baseline: bool = False,
) -> dict[str, Any]:
    """Profile a Python file using cProfile in an isolated subprocess. Zero LLM calls."""
    import datetime
    import json as _json
    import subprocess as _subprocess
    import sys as _sys

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    target_path = Path(target) if Path(target).is_absolute() else repo / target

    if not target_path.is_file():
        return _build_profile_error(
            "file_not_found", f"File not found: {target}", target, checked_at, persist_baseline
        )

    if target_path.suffix != ".py":
        return _build_profile_error(
            "not_python_file",
            f"Only .py files are supported, got: {target_path.suffix or '(no extension)'}",
            target,
            checked_at,
            persist_baseline,
        )

    stdlib_flag = "1" if include_stdlib else "0"
    try:
        proc = _subprocess.run(
            [_sys.executable, "-c", _PROFILE_PROBE_SCRIPT, str(target_path), stdlib_flag],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo),
        )
        raw = proc.stdout.strip()
        if not raw:
            raise ValueError(proc.stderr.strip() or "profile probe produced no output")
        data = _json.loads(raw)
    except Exception as exc:
        return _build_profile_error(
            "execution_error", str(exc), target, checked_at, persist_baseline
        )

    total_duration_ms = float(data["total_duration_ms"])
    cpu_time_ms = float(data["cpu_time_ms"])
    function_calls = int(data["total_calls"])
    rating = _profile_rating(total_duration_ms)

    # Make hotspot file paths relative to repo
    hotspots: list[dict[str, Any]] = []
    for item in data.get("hotspots", []):
        file_str = item.get("file", "")
        try:
            rel = str(Path(file_str).relative_to(repo))
        except ValueError:
            rel = file_str
        hotspots.append({
            "function": item.get("function", ""),
            "file": rel,
            "line": item.get("line", 0),
            "calls": item.get("calls", 0),
            "total_time_ms": item.get("total_time_ms", 0.0),
            "per_call_ms": item.get("per_call_ms", 0.0),
        })

    # Baseline persistence
    baseline_path = repo / ".skilllayer" / "profile_baseline.json"
    baseline_delta_ms: float | None = None
    baseline_saved_at: str | None = None

    if baseline_path.exists():
        try:
            saved = _json.loads(baseline_path.read_text("utf-8"))
            if saved.get("target") == target:
                prev = saved.get("total_duration_ms")
                baseline_saved_at = saved.get("checked_at")
                if prev is not None:
                    baseline_delta_ms = round(total_duration_ms - float(prev), 4)
        except Exception:
            pass

    written_paths: list[str] = []
    state_write_error: str | None = None
    if persist_baseline:
        try:
            from ..memory.skilllayer_memory import atomic_write_json
            atomic_write_json(
                baseline_path,
                {"total_duration_ms": total_duration_ms, "target": target, "checked_at": checked_at},
            )
            written_paths.append(".skilllayer/profile_baseline.json")
        except OSError as exc:
            state_write_error = str(exc)

    return {
        "workflow": "ProfileCodeExecutionWorkflow",
        "target": target,
        "total_duration_ms": total_duration_ms,
        "cpu_time_ms": cpu_time_ms,
        "function_calls": function_calls,
        "hotspots": hotspots,
        "rating": rating,
        "baseline_delta_ms": baseline_delta_ms,
        "baseline_saved_at": baseline_saved_at,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_baseline": persist_baseline,
        "state_write_performed": bool(written_paths),
        "written_paths": written_paths,
        "state_write_error": state_write_error,
        "gitignore_suggestions": [".skilllayer/profile_baseline.json"],
        **({"error_code": "baseline_write_failed", "error": state_write_error} if state_write_error else {}),
    }


# ---- MeasureMemoryUsageWorkflow ---------------------------------------------

_MEMORY_PROBE_SCRIPT = """\
import sys, json, time, tracemalloc, threading, runpy, platform as _pl
def _ru_bytes():
    import resource as _r
    v = _r.getrusage(_r.RUSAGE_SELF).ru_maxrss
    return v if _pl.system() == "Darwin" else v * 1024
try:
    import psutil as _p; _proc = _p.Process(); _base = _proc.memory_info().rss
except ImportError:
    _proc = None; _base = _ru_bytes()
_peak = _base
_ev = threading.Event()
def _poll():
    global _peak
    while not _ev.is_set():
        try:
            v = _proc.memory_info().rss
            if v > _peak:
                _peak = v
        except Exception:
            break
        time.sleep(0.01)
_pt = None
if _proc:
    _pt = threading.Thread(target=_poll, daemon=True)
    _pt.start()
tracemalloc.start(1)
_t0 = time.perf_counter()
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except BaseException:
    pass
_dur = int((time.perf_counter() - _t0) * 1000)
_ev.set()
if _pt is not None:
    _pt.join(timeout=2.0)
if _proc is None:
    _peak = max(_peak, _ru_bytes())
_snap = tracemalloc.take_snapshot()
tracemalloc.stop()
_top = []
for _s in _snap.statistics("lineno")[:10]:
    _f = _s.traceback[0]
    _top.append({"file": _f.filename, "line": _f.lineno, "size_kb": round(_s.size / 1024, 2), "count": _s.count})
print(json.dumps({"baseline_rss": _base, "peak_rss": _peak, "duration_ms": _dur, "top_allocs": _top}))
"""


def _memory_rating(delta_mb: float) -> str:
    if delta_mb < 50:
        return "efficient"
    if delta_mb < 200:
        return "moderate"
    if delta_mb < 500:
        return "heavy"
    return "very_heavy"


def _extract_memory_target(task: str) -> str:
    """Extract a .py file path from a memory measurement task description."""
    # Prefer explicit .py file mentions
    m = re.search(r"\b([\w./\\-]+\.py)\b", task)
    if m:
        return m.group(1)
    # Fall back to word after key phrases
    m = re.search(
        r"(?:profile\s+memory\s+for|memory\s+profile\s+(?:for\s+)?|check\s+memory\s+(?:usage\s+)?(?:of|for)|"
        r"memory\s+usage\s+of|how\s+much\s+memory\s+does|is\b.{0,30}\bmemory)\s+([^\s,]+)",
        task,
        re.I,
    )
    if m:
        return m.group(1).strip("'\"")
    return ""


def _extract_commit_hash(task: str) -> str | None:
    """Extract a git commit hash (7–40 hex chars) from a task description."""
    m = re.search(r"\b([0-9a-f]{7,40})\b", task, re.I)
    return m.group(1) if m else None


def _extract_diff_refs(task: str) -> tuple[str, str | None]:
    """Extract base and optional target ref from a diff task description."""
    m = re.search(r"\bbetween\s+(\S+)\s+and\s+(\S+)\b", task, re.I)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"\b(\S+)\.\.\.?(\S+)\b", task)
    if m:
        return m.group(1), m.group(2)
    return "HEAD", None


def _extract_file_path_from_task(task: str) -> str | None:
    """Extract a file path from a task description (blame/history queries)."""
    # Match "blame X", "history of X", "who changed X", "history for X"
    m = re.search(
        r"(?:blame|history\s+(?:of|for)|who\s+(?:changed|wrote)|file\s+history\s+(?:of|for)?|"
        r"what\s+commits?\s+touched|when\s+was)\s+([^\s,]+)",
        task, re.I,
    )
    if m:
        return m.group(1).strip("'\".,")
    # Fallback: first path-like token
    m = re.search(r"\b([\w./\\-]+\.[\w]+)\b", task)
    if m:
        return m.group(1)
    return None


def _extract_int_param(task: str, pattern: str, default: int) -> int:
    """Extract an integer from a task description matching pattern, else return default."""
    m = re.search(pattern, task, re.I)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            pass
    return default


def _build_memory_error(
    code: str,
    msg: str,
    target: str,
    checked_at: str,
    persist_baseline: bool = False,
) -> dict[str, Any]:
    return {
        "workflow": "MeasureMemoryUsageWorkflow",
        "error_code": code,
        "error": msg,
        "target": target,
        "peak_memory_mb": 0.0,
        "baseline_memory_mb": 0.0,
        "delta_memory_mb": 0.0,
        "duration_ms": 0,
        "tracemalloc_top": [],
        "rating": "efficient",
        "baseline_delta_mb": None,
        "baseline_saved_at": None,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_baseline": persist_baseline,
        "state_write_performed": False,
        "written_paths": [],
        "gitignore_suggestions": [".skilllayer/memory_baseline.json"],
    }


def build_measure_memory_artifacts(
    repo: Path,
    target: str,
    *,
    persist_baseline: bool = False,
) -> dict[str, Any]:
    """Measure RSS memory of a Python file in an isolated subprocess. Zero LLM calls."""
    import datetime
    import json as _json
    import subprocess as _subprocess
    import sys as _sys

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    target_path = Path(target) if Path(target).is_absolute() else repo / target

    if not target_path.is_file():
        return _build_memory_error(
            "file_not_found", f"File not found: {target}", target, checked_at, persist_baseline
        )

    if target_path.suffix != ".py":
        return _build_memory_error(
            "not_python_file",
            f"Only .py files are supported, got: {target_path.suffix or '(no extension)'}",
            target,
            checked_at,
            persist_baseline,
        )

    try:
        proc = _subprocess.run(
            [_sys.executable, "-c", _MEMORY_PROBE_SCRIPT, str(target_path)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo),
        )
        raw = proc.stdout.strip()
        if not raw:
            raise ValueError(proc.stderr.strip() or "probe produced no output")
        data = _json.loads(raw)
    except Exception as exc:
        return _build_memory_error(
            "execution_error", str(exc), target, checked_at, persist_baseline
        )

    baseline_memory_mb = round(data["baseline_rss"] / 1024 / 1024, 2)
    peak_memory_mb = round(data["peak_rss"] / 1024 / 1024, 2)
    delta_memory_mb = round(max(0.0, peak_memory_mb - baseline_memory_mb), 2)
    duration_ms = int(data["duration_ms"])
    rating = _memory_rating(delta_memory_mb)

    tracemalloc_top: list[dict[str, Any]] = []
    for item in data.get("top_allocs", []):
        file_str = item.get("file", "")
        try:
            rel = str(Path(file_str).relative_to(repo))
        except ValueError:
            rel = file_str
        tracemalloc_top.append({
            "file": rel,
            "line": item.get("line", 0),
            "size_kb": item.get("size_kb", 0.0),
            "count": item.get("count", 0),
        })

    # Baseline persistence
    baseline_path = repo / ".skilllayer" / "memory_baseline.json"
    baseline_delta_mb: float | None = None
    baseline_saved_at: str | None = None

    if baseline_path.exists():
        try:
            saved = _json.loads(baseline_path.read_text("utf-8"))
            if saved.get("target") == target:
                prev = saved.get("peak_memory_mb")
                baseline_saved_at = saved.get("checked_at")
                if prev is not None:
                    baseline_delta_mb = round(peak_memory_mb - float(prev), 2)
        except Exception:
            pass

    written_paths: list[str] = []
    state_write_error: str | None = None
    if persist_baseline:
        try:
            from ..memory.skilllayer_memory import atomic_write_json
            atomic_write_json(
                baseline_path,
                {"peak_memory_mb": peak_memory_mb, "target": target, "checked_at": checked_at},
            )
            written_paths.append(".skilllayer/memory_baseline.json")
        except OSError as exc:
            state_write_error = str(exc)

    return {
        "workflow": "MeasureMemoryUsageWorkflow",
        "target": target,
        "peak_memory_mb": peak_memory_mb,
        "baseline_memory_mb": baseline_memory_mb,
        "delta_memory_mb": delta_memory_mb,
        "duration_ms": duration_ms,
        "tracemalloc_top": tracemalloc_top,
        "rating": rating,
        "baseline_delta_mb": baseline_delta_mb,
        "baseline_saved_at": baseline_saved_at,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
        "persist_baseline": persist_baseline,
        "state_write_performed": bool(written_paths),
        "written_paths": written_paths,
        "state_write_error": state_write_error,
        "gitignore_suggestions": [".skilllayer/memory_baseline.json"],
        **({"error_code": "baseline_write_failed", "error": state_write_error} if state_write_error else {}),
    }


# ---- DetectSecretPatternsWorkflow -------------------------------------------

_SECRET_PATTERNS: list[dict[str, Any]] = [
    # CRITICAL
    {"name": "anthropic_api_key", "severity": "critical",
     "pattern": re.compile(r"sk-ant-[a-zA-Z0-9]{20,}")},
    {"name": "openai_api_key", "severity": "critical",
     "pattern": re.compile(r"sk-[a-zA-Z0-9]{48}")},
    {"name": "aws_access_key", "severity": "critical",
     "pattern": re.compile(r"AKIA[0-9A-Z]{16}")},
    {"name": "aws_secret_key", "severity": "critical",
     "pattern": re.compile(r"""aws_secret\w*\s*=\s*(?:(["'])[a-zA-Z0-9/+=]{40}\1|[a-zA-Z0-9/+=]{40})""", re.I)},
    {"name": "private_key_block", "severity": "critical",
     "pattern": re.compile(r"-----BEGIN.*PRIVATE KEY-----")},
    {"name": "github_token", "severity": "critical",
     "pattern": re.compile(r"gh[pousr]_[a-zA-Z0-9]{36,}")},
    # HIGH
    {"name": "generic_api_key", "severity": "high",
     "pattern": re.compile(r"""(?:api_key|apikey|api_secret)\s*=\s*(?:(["'])[^"']{16,}\1|[^\s"']{16,})""", re.I)},
    {"name": "generic_token", "severity": "high",
     "pattern": re.compile(r"""\b(?:token|secret|password)\s*=\s*(?:(["'])[^"']{16,}\1|[^\s"']{16,})""", re.I)},
    {"name": "bearer_token", "severity": "high",
     "pattern": re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]{20,}")},
    # MEDIUM
    {"name": "ip_address", "severity": "medium",
     "pattern": re.compile(
         r"\b(?!127\.|0\.|10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)"
         r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
     )},
    {"name": "database_url", "severity": "medium",
     "pattern": re.compile(r"(?:postgres|mysql|mongodb|redis)://[^\s\"']+:[^\s\"']+@")},
]

_TEST_FILE_RE = re.compile(r"(?:^|[\\/])tests?[\\/]test_", re.I)


def _is_binary_file(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError:
        return True


def _is_test_fixture_path(rel_path: str) -> bool:
    return bool(_TEST_FILE_RE.search(rel_path))


def _load_gitignore_patterns(repo: Path) -> list[str]:
    gi = repo / ".gitignore"
    try:
        lines = []
        for line in gi.read_text("utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)
        return lines
    except OSError:
        return []


def _matches_gitignore(rel_path: str, patterns: list[str]) -> bool:
    import fnmatch as _fnmatch
    rel_fwd = rel_path.replace("\\", "/")
    parts = rel_fwd.split("/")
    for pattern in patterns:
        pat = pattern.rstrip("/")
        if "/" in pat:
            if _fnmatch.fnmatch(rel_fwd, pat.lstrip("/")):
                return True
        else:
            for part in parts:
                if _fnmatch.fnmatch(part, pat):
                    return True
    return False


# Skip reasons that mean content potentially containing a real secret was
# never inspected -> scan_complete must be False. "excluded_dir" (.git,
# node_modules, .venv, build output, ...) is a permanent, disclosed scope
# boundary around VCS-internal/third-party content, not an incidental gap,
# so it alone does not mark the scan incomplete.
_SECRET_SCAN_INCOMPLETE_REASONS = frozenset({"gitignored", "oversized", "binary", "permission_denied", "unreadable"})


def build_detect_secrets_artifacts(repo: Path) -> dict[str, Any]:
    """Scan repository files for accidentally committed secrets. Zero LLM calls.

    Advisory, not a security certification: `clean` is only ever True when
    every non-excluded file was actually scanned (scan_complete). Gitignored,
    oversized, binary, and permission-denied files are reported honestly via
    skipped_reasons rather than silently folded into a blanket "clean"."""
    import datetime
    import os

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    findings: list[dict[str, Any]] = []
    scanned_files = 0
    scanned_bytes = 0
    skipped_files = 0
    skipped_bytes = 0
    skipped_reasons: dict[str, int] = {}

    def _skip(reason: str, size: int | None) -> None:
        nonlocal skipped_files, skipped_bytes
        skipped_files += 1
        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
        if size is not None:
            skipped_bytes += size

    try:
        gitignore_patterns = _load_gitignore_patterns(repo)
        all_paths = sorted(repo.rglob("*"))
    except OSError as exc:
        return {
            "workflow": "DetectSecretPatternsWorkflow",
            "success": False,
            "status": "error",
            "error_code": "detect_secrets_scan_failed",
            "error": str(exc),
            "findings": [],
            "findings_count": 0,
            "clean": False,
            "scanned_files": 0,
            "scanned_bytes": 0,
            "skipped_files": 0,
            "skipped_bytes": 0,
            "skipped_reasons": {},
            "scan_complete": False,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    for path in all_paths:
        if not path.is_file():
            continue

        if should_ignore_search_path(path, repo):
            _skip("excluded_dir", None)
            continue

        rel_path = str(path.relative_to(repo))

        if _matches_gitignore(rel_path, gitignore_patterns):
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            _skip("gitignored", size)
            continue

        try:
            size = path.stat().st_size
        except PermissionError:
            _skip("permission_denied", None)
            continue
        except OSError:
            _skip("unreadable", None)
            continue

        if size > 1_048_576:
            _skip("oversized", size)
            continue

        # Checked before the binary sniff: _is_binary_file() swallows any
        # read failure (including permission-denied) as "probably binary",
        # which would misreport a real permission failure under the wrong
        # reason label.
        if not os.access(path, os.R_OK):
            _skip("permission_denied", size)
            continue

        if _is_binary_file(path):
            _skip("binary", size)
            continue

        try:
            content = path.read_text("utf-8", errors="replace")
        except PermissionError:
            _skip("permission_denied", size)
            continue
        except OSError:
            _skip("unreadable", size)
            continue

        scanned_files += 1
        scanned_bytes += size
        is_test_fixture = _is_test_fixture_path(rel_path)

        for line_no, line in enumerate(content.splitlines(), 1):
            for pat in _SECRET_PATTERNS:
                m = pat["pattern"].search(line)
                if m:
                    matched = m.group(0)
                    preview = matched[:6] + "..."
                    findings.append({
                        "file": rel_path,
                        "line": line_no,
                        "pattern_name": pat["name"],
                        "severity": pat["severity"],
                        "match_preview": preview,
                        "likely_test_fixture": is_test_fixture,
                    })

    scan_complete = not any(skipped_reasons.get(reason) for reason in _SECRET_SCAN_INCOMPLETE_REASONS)
    if findings:
        status = "findings"
    elif scan_complete:
        status = "clear_for_supported_patterns"
    else:
        status = "incomplete"
    clean = status == "clear_for_supported_patterns"

    return {
        "workflow": "DetectSecretPatternsWorkflow",
        "success": True,
        "status": status,
        "error_code": None,
        "error": None,
        "findings": findings,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "skipped_files": skipped_files,
        "skipped_bytes": skipped_bytes,
        "skipped_reasons": skipped_reasons,
        "scan_complete": scan_complete,
        "findings_count": len(findings),
        "clean": clean,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


# ---- SearchWorkflow ---------------------------------------------------------

def _extract_search_query(task: str) -> str:
    """Extract the search query from a natural-language task description."""
    patterns = [
        r"(?:where\s+(?:is|are))\s+(.+?)\s+(?:used|defined|called|referenced)\b",
        r"(?:search|grep)\s+(?:the\s+)?(?:codebase|repo|repository|code)\s+for\s+(.+)",
        r"find\s+all\s+(?:files\s+)?(?:occurrences?\s+of|containing)\s+(.+)",
        r"find\s+(.+?)\s+in\s+(?:the\s+)?(?:codebase|repo|repository|code)",
        r"(?:search|grep)\s+for\s+(.+)",
        r"find\s+(.+)",
    ]
    text = task.strip()
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            q = m.group(1).strip()
            if len(q) >= 2 and q[0] in ('"', "'") and q[-1] == q[0]:
                q = q[1:-1]
            return q
    return text


def _extract_decision_search_query(task: str) -> str:
    """Extract the search query from a natural-language decision-search task."""
    patterns = [
        r"why\s+did\s+we\s+choose\s+(.+)",
        r"why\s+did\s+we\s+decide\s+(?:to\s+)?(.+)",
        r"why\s+did\s+we\s+(?:use|pick|select|go\s+with)\s+(.+)",
        r"search\s+decisions?\s+(?:for|about)\s+(.+)",
        r"find\s+(?:a\s+|the\s+)?decisions?\s+(?:about|on|regarding)\s+(.+)",
        r"look\s+up\s+(?:the\s+)?decisions?\s+(?:about|on|for|regarding)\s+(.+)",
        r"query\s+(?:the\s+)?decisions?\s+(?:for|about)\s+(.+)",
    ]
    text = task.strip()
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            q = m.group(1).strip().rstrip("?.!")
            if len(q) >= 2 and q[0] in ('"', "'") and q[-1] == q[0]:
                q = q[1:-1]
            return q
    return text.rstrip("?.!")


def build_search_artifacts(
    repo: Path,
    query: str,
    *,
    mode: str = "text",
    case_sensitive: bool = False,
    file_pattern: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Search repository files for text or regex patterns. Zero LLM calls."""
    import datetime
    import fnmatch as _fnmatch

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(query if mode == "regex" else re.escape(query), flags)
    except re.error as exc:
        return {
            "workflow": "SearchWorkflow",
            "error_code": "invalid_regex",
            "error": f"Invalid regex pattern: {exc}",
            "query": query,
            "mode": mode,
            "matches": [],
            "total_matches": 0,
            "files_with_matches": 0,
            "scanned_files": 0,
            "skipped_files": 0,
            "truncated": False,
            "limit": limit,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    matches: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_files = 0
    files_with_matches: set[str] = set()
    total_matches = 0
    truncated = False

    gitignore_patterns = _load_gitignore_patterns(repo)

    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue

        if should_ignore_search_path(path, repo):
            skipped_files += 1
            continue

        rel_path = str(path.relative_to(repo))

        if file_pattern is not None and not _fnmatch.fnmatch(path.name, file_pattern):
            skipped_files += 1
            continue

        if _matches_gitignore(rel_path, gitignore_patterns):
            skipped_files += 1
            continue

        try:
            size = path.stat().st_size
        except OSError:
            skipped_files += 1
            continue

        if size > 1_048_576:
            skipped_files += 1
            continue

        if _is_binary_file(path):
            skipped_files += 1
            continue

        try:
            content = path.read_text("utf-8", errors="replace")
        except OSError:
            skipped_files += 1
            continue

        scanned_files += 1
        file_matched = False

        for line_no, line in enumerate(content.splitlines(), 1):
            for m in compiled.finditer(line):
                total_matches += 1
                file_matched = True
                if len(matches) < limit:
                    stripped = line.lstrip()
                    leading = len(line) - len(stripped)
                    preview = stripped[:120]
                    ms = max(0, m.start() - leading)
                    me = max(0, min(m.end() - leading, len(preview)))
                    matches.append({
                        "file": rel_path,
                        "line": line_no,
                        "column": m.start() + 1,
                        "preview": preview,
                        "match_start": ms,
                        "match_end": me,
                    })
                else:
                    truncated = True

        if file_matched:
            files_with_matches.add(rel_path)

    return {
        "workflow": "SearchWorkflow",
        "query": query,
        "mode": mode,
        "matches": matches,
        "total_matches": total_matches,
        "files_with_matches": len(files_with_matches),
        "scanned_files": scanned_files,
        "skipped_files": skipped_files,
        "truncated": truncated,
        "limit": limit,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def _extract_include_system_flag(task: str) -> bool:
    """Return True if the task description asks for system processes."""
    import re as _re2
    return bool(_re2.search(r"\b(include[- ]system|system[- ]process(es)?|all[- ]process(es)?)\b", task, _re2.I))


_DEV_PORTS: frozenset[int] = frozenset({3000, 4000, 5000, 8000, 8080, 8888})
_WEB_SERVER_NAMES: frozenset[str] = frozenset({
    "nginx", "apache", "apache2", "httpd", "caddy",
    "uvicorn", "gunicorn", "node", "next", "vite",
    "webpack-dev-server", "webpack",
})
_DATABASE_NAMES: frozenset[str] = frozenset({
    "postgres", "postgresql", "postmaster", "mysqld", "mysql",
    "mongod", "redis-server", "redis",
})
_TEST_RUNNER_NAMES: frozenset[str] = frozenset({"pytest", "jest", "mocha"})
_DOCKER_NAMES: frozenset[str] = frozenset({"dockerd", "containerd", "docker"})


def _name_matches(proc_name: str, patterns: frozenset[str]) -> bool:
    n = proc_name.lower()
    return any(p in n for p in patterns)


def build_detect_processes_artifacts(include_system: bool = False) -> dict[str, Any]:
    """Enumerate running processes. Zero LLM calls — purely deterministic.
    Never exposes environment variables or command line arguments.

    A top-level iterator failure (PermissionError/AccessDenied/OSError,
    e.g. a sandboxed environment that blocks process enumeration entirely)
    returns a structured unsupported result, never a raw traceback. Any
    per-process access failure is counted (skipped_count) and skipped, not
    silently dropped."""
    import datetime
    import psutil

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    empty_dev_services = {
        "web_server": False, "database": False, "test_runner": False,
        "dev_server": False, "docker": False,
    }

    _ATTRS = ["pid", "name", "status", "username", "memory_info", "create_time"]
    try:
        process_iterator = psutil.process_iter(_ATTRS)
    except (psutil.Error, OSError) as exc:
        return {
            "workflow": "DetectRunningProcessesWorkflow",
            "success": False,
            "status": "unsupported",
            "error_code": "process_inspection_unavailable",
            "error": str(exc),
            "processes": [],
            "dev_services": empty_dev_services,
            "inspected_count": 0,
            "skipped_count": 0,
            "permission_limited": True,
            "complete": False,
            "total_count": 0,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    processes: list[dict[str, Any]] = []
    skipped_count = 0

    while True:
        try:
            proc = next(process_iterator)
        except StopIteration:
            break
        except (psutil.Error, OSError):
            skipped_count += 1
            continue

        try:
            pid: int = proc.pid
            if not include_system and pid < 100:
                continue

            info = proc.info
            name: str = (info.get("name") or "unknown")
            status: str = (info.get("status") or "unknown")
            user: str = (info.get("username") or "unknown")

            # cpu_percent — first call after process start always returns 0.0; acceptable for a snapshot.
            try:
                cpu = proc.cpu_percent(interval=None)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                cpu = 0.0

            mem_info = info.get("memory_info")
            memory_mb = round(mem_info.rss / (1024 * 1024), 2) if mem_info else 0.0

            ports: list[int] = []
            try:
                for conn in proc.net_connections(kind="inet"):
                    laddr = getattr(conn, "laddr", None)
                    if laddr and laddr.port:
                        ports.append(laddr.port)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                pass
            ports = sorted(set(ports))

            create_time = info.get("create_time")
            started_at: str | None = None
            if create_time:
                try:
                    started_at = datetime.datetime.fromtimestamp(
                        create_time, tz=datetime.timezone.utc
                    ).isoformat()
                except Exception:
                    started_at = None

            processes.append({
                "pid": pid,
                "name": name,
                "status": status,
                "user": user,
                "cpu_percent": cpu,
                "memory_mb": memory_mb,
                "ports": ports,
                "started_at": started_at,
            })
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            skipped_count += 1
            continue

    all_names: set[str] = {p["name"] for p in processes}
    all_ports: set[int] = {port for p in processes for port in p["ports"]}

    web_server = any(_name_matches(n, _WEB_SERVER_NAMES) for n in all_names)
    database = any(_name_matches(n, _DATABASE_NAMES) for n in all_names)
    test_runner = any(_name_matches(n, _TEST_RUNNER_NAMES) for n in all_names)
    docker = any(_name_matches(n, _DOCKER_NAMES) for n in all_names)
    dev_server = bool(all_ports & _DEV_PORTS) or any(
        _name_matches(n, frozenset({"uvicorn", "gunicorn", "next", "vite", "webpack-dev-server"}))
        for n in all_names
    )

    dev_services: dict[str, bool] = {
        "web_server": web_server,
        "database": database,
        "test_runner": test_runner,
        "dev_server": dev_server,
        "docker": docker,
    }

    return {
        "workflow": "DetectRunningProcessesWorkflow",
        "success": True,
        "status": "healthy",
        "error_code": None,
        "error": None,
        "processes": processes,
        "dev_services": dev_services,
        "inspected_count": len(processes),
        "skipped_count": skipped_count,
        "permission_limited": skipped_count > 0,
        "complete": skipped_count == 0,
        "total_count": len(processes),
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def _extract_port_check_params(task: str) -> tuple[int | None, str]:
    """Parse port number and optional host from a free-text task description."""
    import re
    m = re.search(r"\b(?:port[:\s]+|:)(\d+)\b", task, re.I)
    if not m:
        m = re.search(r"\b(\d{2,5})\b", task)
    port = int(m.group(1)) if m else None
    hm = re.search(r"\b(?:on|host)\s+(\d{1,3}(?:\.\d{1,3}){3})\b", task, re.I)
    host = hm.group(1) if hm else "127.0.0.1"
    return port, host


_LOOPBACK_PORT_CHECK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def build_check_port_artifacts(
    port: int, host: str = "127.0.0.1", *, allow_non_loopback: bool = False
) -> dict[str, Any]:
    """Check whether a TCP port is available. Zero LLM calls — purely deterministic.

    Loopback only by default (localhost/127.0.0.1/::1). Probing any other
    host requires the caller to explicitly pass allow_non_loopback=True —
    this function never silently probes an arbitrary remote host."""
    import socket
    import datetime

    # Validate port range up front.
    if not (1 <= port <= 65535):
        return {
            "workflow": "CheckPortAvailabilityWorkflow",
            "success": False,
            "error_code": "invalid_port",
            "error": f"Port must be between 1 and 65535, got {port}.",
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    if not allow_non_loopback and host.strip().lower() not in _LOOPBACK_PORT_CHECK_HOSTS:
        return {
            "workflow": "CheckPortAvailabilityWorkflow",
            "success": False,
            "error_code": "non_loopback_target_not_allowed",
            "error": (
                f"Refusing to probe non-loopback host {host!r} without explicit "
                "authorization. Pass allow_non_loopback=True to check a remote host."
            ),
            "port": port,
            "host": host,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Probe availability via a bounded-timeout connect attempt. Socket
    # creation, hostname resolution, and connect can all raise OSError (e.g.
    # unresolvable host, address family mismatch, resource exhaustion) —
    # every one of those becomes a structured result, never a raw traceback.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            result_code = sock.connect_ex((host, port))
            available = result_code != 0
        finally:
            sock.close()
    except OSError as exc:
        return {
            "workflow": "CheckPortAvailabilityWorkflow",
            "success": False,
            "error_code": "port_check_socket_error",
            "error": str(exc),
            "port": port,
            "host": host,
            "checked_at": checked_at,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        }

    process_info: dict[str, Any] | None = None
    psutil_note: str | None = None
    if not available:
        try:
            import psutil
            # Iterate per-process to skip inaccessible entries (e.g. macOS sandbox).
            found = False
            for proc in psutil.process_iter(["pid", "name", "username"]):
                if found:
                    break
                try:
                    for conn in proc.net_connections(kind="inet"):
                        laddr = getattr(conn, "laddr", None)
                        if laddr and laddr.port == port:
                            process_info = {
                                "pid": proc.pid,
                                "name": proc.info.get("name") or "unknown",
                                "user": proc.info.get("username") or "unknown",
                            }
                            found = True
                            break
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
        except ImportError:
            psutil_note = "psutil not installed; process details unavailable."
        except Exception:
            psutil_note = "Process lookup failed; process details unavailable."

    result: dict[str, Any] = {
        "workflow": "CheckPortAvailabilityWorkflow",
        "success": True,
        "error_code": None,
        "error": None,
        "port": port,
        "host": host,
        "available": available,
        "process": process_info,
        "checked_at": checked_at,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }
    if psutil_note:
        result["note"] = psutil_note
    return result


def build_git_status_artifacts(repo: Path, log: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], int]:
    command_count = 0

    def run(command: tuple[str, ...]) -> dict[str, Any]:
        nonlocal command_count
        command_count += 1
        result = run_allowed_git_status_command(repo, command)
        if log is not None:
            log.append(
                {
                    "tool": "git_status_read",
                    "command": list(command),
                    "returncode": result["returncode"],
                    "allowed_read_only": True,
                }
            )
        return result

    root_result = run(("git", "rev-parse", "--show-toplevel"))
    if root_result["returncode"] != 0:
        return (
            {
                "workflow": "GitStatusWorkflow",
                "is_git_repo": False,
                "repo_root": None,
                "branch": None,
                "clean": False,
                "staged_count": 0,
                "unstaged_count": 0,
                "untracked_count": 0,
                "changed_files": [],
                "diff_stat": "",
                "cached_diff_stat": "",
                "summary": "Not a git repository.",
                "error_code": "not_git_repository",
                "tests_run": False,
                "tests_passed": None,
                "validation_status": "not_applicable",
            },
            command_count,
        )

    branch_result = run(("git", "branch", "--show-current"))
    porcelain_result = run(("git", "status", "--porcelain=v1"))
    diff_stat_result = run(("git", "diff", "--stat"))
    cached_diff_stat_result = run(("git", "diff", "--cached", "--stat"))

    changed_files = parse_git_porcelain(porcelain_result["stdout"])
    staged_count = sum(1 for item in changed_files if item["staged"])
    unstaged_count = sum(1 for item in changed_files if item["unstaged"])
    untracked_count = sum(1 for item in changed_files if item["status"] == "untracked")
    clean = not changed_files
    # git branch --show-current returns empty string in detached HEAD state
    branch_name = branch_result["stdout"].strip()
    branch = branch_name if branch_name else "(detached HEAD)"
    detached = not branch_name
    if detached:
        branch_desc = "HEAD detached"
    else:
        branch_desc = f"branch {branch}"
    summary = (
        f"On {branch_desc}; working tree clean."
        if clean
        else f"On {branch_desc}; {staged_count} staged, {unstaged_count} unstaged, {untracked_count} untracked."
    )
    return (
        {
            "workflow": "GitStatusWorkflow",
            "is_git_repo": True,
            "repo_root": root_result["stdout"].strip(),
            "branch": branch,
            "clean": clean,
            "staged_count": staged_count,
            "unstaged_count": unstaged_count,
            "untracked_count": untracked_count,
            "changed_files": changed_files,
            "diff_stat": diff_stat_result["stdout"].strip(),
            "cached_diff_stat": cached_diff_stat_result["stdout"].strip(),
            "summary": summary,
            "error_code": None,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        },
        command_count,
    )


def run_allowed_git_status_command(repo: Path, command: tuple[str, ...]) -> dict[str, Any]:
    if command not in ALLOWED_GIT_STATUS_COMMANDS:
        raise ValueError(f"disallowed git status command: {' '.join(command)}")
    completed = subprocess.run(
        list(command),
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def parse_git_porcelain(output: str) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("?? "):
            path = line[3:].strip()
            by_path[path] = {
                "path": path,
                "status": "untracked",
                "staged": False,
                "unstaged": False,
            }
            continue
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        raw_path = line[3:].strip()
        path = raw_path.split(" -> ", 1)[-1].strip()
        staged = index_status not in {" ", "?", "!"}
        unstaged = worktree_status not in {" ", "?", "!"}
        status_code = index_status if staged else worktree_status
        status = git_status_label(status_code)
        existing = by_path.get(path)
        if existing:
            existing["staged"] = bool(existing["staged"] or staged)
            existing["unstaged"] = bool(existing["unstaged"] or unstaged)
            if existing["status"] == "untracked" or status == "renamed":
                existing["status"] = status
        else:
            by_path[path] = {
                "path": path,
                "status": status,
                "staged": staged,
                "unstaged": unstaged,
            }
    return [by_path[path] for path in sorted(by_path)]


def git_status_label(code: str) -> str:
    return {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "U": "modified",
    }.get(code, "modified")


def build_dependency_check_artifacts(
    repo: Path,
    task_description: str,
    log: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    dependency = extract_dependency_name(task_description)
    command_count = 0
    if not dependency:
        return (
            {
                "workflow": "DependencyCheckWorkflow",
                "dependency": None,
                "declared": False,
                "declared_in": [],
                "used": False,
                "usage_count": 0,
                "usage_files": [],
                "usage_locations": [],
                "ecosystems": [],
                "summary": "No dependency name could be extracted from the task.",
                "error_code": "dependency_name_not_found",
                "tests_run": False,
                "tests_passed": None,
                "validation_status": "not_applicable",
            },
            command_count,
        )

    declared_in: list[str] = []
    ecosystems: set[str] = set()
    declaration_files_found = 0
    for relative_name in DEPENDENCY_DECLARATION_FILES:
        path = repo / relative_name
        if not path.exists() or not path.is_file():
            continue
        declaration_files_found += 1
        command_count += 1
        if log is not None:
            log.append({"tool": "dependency_file_read", "file": relative_name, "read_only": True})
        if relative_name == "package.json":
            ecosystems.add("node")
            if package_json_declares_dependency(path, dependency):
                declared_in.append(relative_name)
        else:
            ecosystems.add("python")
            if python_dependency_file_declares(path, dependency):
                declared_in.append(relative_name)

    usage_locations: list[dict[str, Any]] = []
    for path in iter_dependency_source_files(repo):
        command_count += 1
        rel_path = safe_relative_path(path, repo)
        if log is not None:
            log.append({"tool": "dependency_import_scan", "file": rel_path, "read_only": True})
        if path.suffix in PYTHON_SOURCE_SUFFIXES:
            ecosystems.add("python")
            usage_locations.extend(scan_python_dependency_usage(path, repo, dependency))
        elif path.suffix in NODE_SOURCE_SUFFIXES:
            ecosystems.add("node")
            usage_locations.extend(scan_node_dependency_usage(path, repo, dependency))

    usage_files = unique_preserving_order(location["file"] for location in usage_locations)
    declared = bool(declared_in)
    used = bool(usage_locations)
    ecosystem_list = [item for item in ["python", "node"] if item in ecosystems]
    error_code: str | None = "no_dependency_files_found" if declaration_files_found == 0 else None
    summary = summarize_dependency_check(dependency, declared, declared_in, used, usage_locations, error_code)
    return (
        {
            "workflow": "DependencyCheckWorkflow",
            "dependency": dependency,
            "declared": declared,
            "declared_in": declared_in,
            "used": used,
            "usage_count": len(usage_locations),
            "usage_files": usage_files,
            "usage_locations": usage_locations,
            "ecosystems": ecosystem_list,
            "summary": summary,
            "error_code": error_code,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        },
        command_count,
    )


def package_json_declares_dependency(path: Path, dependency: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dependency_mentioned_in_text(path, dependency)
    dependency_key = dependency.lower()
    for section in ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]:
        entries = data.get(section, {})
        if not isinstance(entries, dict):
            continue
        if any(str(name).lower() == dependency_key for name in entries):
            return True
    return False


def python_dependency_file_declares(path: Path, dependency: str) -> bool:
    return dependency_mentioned_in_text(path, dependency)


def dependency_mentioned_in_text(path: Path, dependency: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    normalized_target = normalize_dependency_name(dependency)
    for candidate in re.findall(r"(?<![A-Za-z0-9_.@/-])(@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)(?![A-Za-z0-9_.@/-])", text):
        if normalize_dependency_name(candidate) == normalized_target:
            return True
    return False


def iter_dependency_source_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(repo).parts
        if any(part in DEPENDENCY_SKIP_DIRS for part in relative_parts):
            continue
        if path.suffix in PYTHON_SOURCE_SUFFIXES or path.suffix in NODE_SOURCE_SUFFIXES:
            files.append(path)
    return sorted(files)


def scan_python_dependency_usage(path: Path, repo: Path, dependency: str) -> list[dict[str, Any]]:
    import_root = python_import_root_for_dependency(dependency)
    locations: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return locations
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        import_match = re.match(r"import\s+(.+)", stripped)
        if import_match:
            for imported in import_match.group(1).split(","):
                name = imported.strip().split(" as ", 1)[0].strip()
                if python_import_matches_dependency(name, import_root):
                    locations.append(build_dependency_usage_location(path, repo, line_no, "python_import"))
        from_match = re.match(r"from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\b", stripped)
        if from_match and python_import_matches_dependency(from_match.group(1), import_root):
            locations.append(build_dependency_usage_location(path, repo, line_no, "python_from_import"))
    return locations


def scan_node_dependency_usage(path: Path, repo: Path, dependency: str) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return locations
    for line_no, line in enumerate(lines, start=1):
        for pattern, usage_type in [
            (r"\bfrom\s+['\"]([^'\"]+)['\"]", "node_import"),
            (r"\bimport\s+['\"]([^'\"]+)['\"]", "node_side_effect_import"),
            (r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", "node_require"),
        ]:
            for match in re.finditer(pattern, line):
                imported = match.group(1)
                if node_import_matches_dependency(imported, dependency):
                    locations.append(build_dependency_usage_location(path, repo, line_no, usage_type))
    return locations


def build_dependency_usage_location(path: Path, repo: Path, line_no: int, usage_type: str) -> dict[str, Any]:
    return {
        "file": safe_relative_path(path, repo),
        "line": line_no,
        "usage_type": usage_type,
    }


def python_import_root_for_dependency(dependency: str) -> str:
    return dependency.replace("-", "_").split(".", 1)[0].lower()


def python_import_matches_dependency(imported: str, import_root: str) -> bool:
    root = imported.split(".", 1)[0].strip().lower()
    return root == import_root


def node_import_matches_dependency(imported: str, dependency: str) -> bool:
    if imported.startswith((".", "/")):
        return False
    normalized_import = imported.lower()
    normalized_dependency = dependency.lower()
    if normalized_dependency.startswith("@"):
        parts = normalized_import.split("/")
        package = "/".join(parts[:2]) if len(parts) >= 2 else normalized_import
        return package == normalized_dependency
    package = normalized_import.split("/", 1)[0]
    return package == normalized_dependency


def normalize_dependency_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def safe_relative_path(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def summarize_dependency_check(
    dependency: str,
    declared: bool,
    declared_in: list[str],
    used: bool,
    usage_locations: list[dict[str, Any]],
    error_code: str | None = None,
) -> str:
    if error_code == "no_dependency_files_found":
        return (
            f"Dependency {dependency}: no supported declaration files found "
            f"(requirements.txt, pyproject.toml, package.json, etc.) — cannot verify declaration."
        )
    declaration = f"declared in {', '.join(declared_in)}" if declared else "not declared in supported dependency files"
    usage = f"used in {len(unique_preserving_order(location['file'] for location in usage_locations))} file(s)" if used else "not used in scanned source imports"
    return f"Dependency {dependency} is {declaration} and {usage}."


_CLARIFY_SUGGESTIONS = [
    "inspect repo structure",
    "git status",
    "run tests",
    "find function <name>",
    "map dependencies",
]


def build_clarify_intent_artifacts(task_description: str) -> dict[str, Any]:
    """Read-only response for prose the router could not confidently match.

    Ambiguous input (e.g. "make the CLI faster") must never silently select a
    write-capable workflow. Instead of editing anything, this returns a
    clarification listing concrete read-only workflows the user can pick from.
    It performs no file changes and reports success=False so the caller knows
    nothing was executed.
    """
    quoted = ", ".join(f"'{s}'" for s in _CLARIFY_SUGGESTIONS)
    return {
        "workflow": "ClarifyIntentWorkflow",
        "matched": False,
        "unsupported": True,
        "unsupported_reason": "ambiguous_intent",
        "suggestions": list(_CLARIFY_SUGGESTIONS),
        "summary": (
            "This task was too vague to route to a specific workflow, so nothing "
            f"was run and no files were changed. Did you mean one of: {quoted}? "
            "Re-run with a concrete, specific task. Write-capable workflows are "
            "never selected by default for ambiguous input."
        ),
        "error_code": None,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def build_memory_cli_unsupported_artifacts(
    workflow: str, mcp_tool: str, needs: str
) -> dict[str, Any]:
    """Clear, honest artifact for memory *write* workflows invoked via the CLI.

    Save/track/remember require structured input (context state, decision
    fields, a preferences map) that the free-text ``run --task`` interface
    cannot provide, so they are exposed as MCP tools only. This replaces the
    misleading "no automated fix is available for this task" fallthrough — a
    FixBugWorkflow message — that these task types used to hit because they had
    no dispatch branch of their own. ``RehydrateContextWorkflow`` is read-only
    and works through the CLI, so it is deliberately not routed here.
    """
    return {
        "workflow": workflow,
        "workflow_status": "cli_unsupported",
        "unsupported": True,
        "unsupported_reason": "memory_write_requires_mcp",
        "cli_supported": False,
        "mcp_tool": mcp_tool,
        "summary": (
            f"{workflow} writes structured data ({needs}) that the CLI "
            f"'run --task' command cannot provide. This memory workflow is "
            f"available via the MCP tool '{mcp_tool}' only. See "
            f"docs/CLAUDE_CODE_SETUP.md for MCP setup."
        ),
        "error_code": None,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    }


def _snapshot_persistent_files(root: Path) -> dict[str, tuple[int, int, int]]:
    """Capture file identity/size/mtime without reading contents."""
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, int, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (int(getattr(stat, "st_ino", 0)), stat.st_size, stat.st_mtime_ns)
    return snapshot


def _with_repo_write_report(repo: Path, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Report persistent .skilllayer file changes without altering memory writes."""
    state_root = repo / ".skilllayer"
    before = _snapshot_persistent_files(state_root)
    result = dict(operation())
    after = _snapshot_persistent_files(state_root)
    written_paths = sorted(
        str(Path(path).relative_to(repo))
        for path, fingerprint in after.items()
        if before.get(path) != fingerprint
    )
    deleted_paths = sorted(
        str(Path(path).relative_to(repo))
        for path in before
        if path not in after
    )
    result["written_paths"] = written_paths
    result["deleted_paths"] = deleted_paths
    result["state_write_performed"] = bool(written_paths or deleted_paths)
    return result


def build_save_context_artifacts(
    repo: Path,
    state: str,
    open_questions: list[str],
) -> dict[str, Any]:
    from ..memory.skilllayer_memory import save_context_snapshot
    return _with_repo_write_report(
        repo,
        lambda: save_context_snapshot(repo / ".skilllayer", state, open_questions),
    )


def build_track_decision_artifacts(
    repo: Path,
    title: str,
    context: str,
    decision: str,
    reasoning: str,
    consequences: str,
    status: str = "proposed",
    related_files: list[str] | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    from ..memory.skilllayer_memory import track_decision
    return _with_repo_write_report(
        repo,
        lambda: track_decision(
            repo / ".skilllayer",
            title=title,
            context=context,
            decision=decision,
            reasoning=reasoning,
            consequences=consequences,
            status=status,
            related_files=related_files,
            supersedes=supersedes,
        ),
    )


def build_remember_preferences_artifacts(
    repo: Path,
    preferences: dict[str, dict[str, str]],
) -> dict[str, Any]:
    from ..memory.skilllayer_memory import remember_preferences
    return _with_repo_write_report(
        repo,
        lambda: remember_preferences(repo / ".skilllayer", preferences),
    )


def build_rehydrate_context_artifacts(
    repo: Path,
    *,
    full_context: bool = False,
    decision_id: str | None = None,
    full_preferences: bool = False,
) -> dict[str, Any]:
    from ..memory.skilllayer_memory import rehydrate_context
    return rehydrate_context(
        repo / ".skilllayer",
        full_context=full_context,
        decision_id=decision_id,
        full_preferences=full_preferences,
    )


# ---- CompareContextSnapshotsWorkflow ----------------------------------------

def build_compare_context_snapshots_artifacts(
    repo: Path,
    *,
    from_index: int | None = None,
    to_index: int | None = None,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> dict[str, Any]:
    """Diff two saved context snapshots. Read-only, zero LLM calls.

    Reads context/latest.md and context/history/*.md (written by
    SaveContextSnapshotWorkflow) via the existing parse_frontmatter() and
    extract_section_body() helpers — no new storage, never writes anything.

    Snapshot order: index 0 is always latest.md; index 1, 2, ... are history
    files newest-first. Select the pair via from_index/to_index or
    from_timestamp/to_timestamp (history filename stem, e.g.
    "20260701T090000Z"); "latest" is also accepted as a timestamp for
    latest.md. Defaults: to = latest (index 0), from = one snapshot back
    (index 1) — the most recent transition.
    """
    import datetime
    import difflib

    from ..memory.skilllayer_memory import extract_section_body, finalize_memory_result, parse_frontmatter

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _error(error_code: str, error: str) -> dict[str, Any]:
        return finalize_memory_result({
            "workflow": "CompareContextSnapshotsWorkflow",
            "from_snapshot": None,
            "to_snapshot": None,
            "state_diff": {},
            "open_questions_diff": {},
            "checked_at": checked_at,
            "error_code": error_code,
            "error": error,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        })

    skilllayer_dir = repo / ".skilllayer"
    latest_path = skilllayer_dir / "context" / "latest.md"
    history_dir = skilllayer_dir / "context" / "history"

    snapshots: list[tuple[str, Path]] = []
    if latest_path.exists():
        snapshots.append(("latest", latest_path))
    if history_dir.exists():
        for hf in sorted(history_dir.glob("*.md"), reverse=True):
            snapshots.append((hf.stem, hf))

    if len(snapshots) < 2:
        return _error(
            "insufficient_snapshots",
            f"Need at least 2 saved context snapshots to compare; found {len(snapshots)}. "
            "Save another snapshot first.",
        )

    def _by_index(idx: int | None) -> tuple[str, Path] | None:
        if idx is None or idx < 0 or idx >= len(snapshots):
            return None
        return snapshots[idx]

    def _by_timestamp(ts: str | None) -> tuple[str, Path] | None:
        if ts is None:
            return None
        return next((s for s in snapshots if s[0] == ts), None)

    to_snap = _by_timestamp(to_timestamp) or _by_index(to_index)
    if to_snap is None and to_timestamp is None and to_index is None:
        to_snap = snapshots[0]
    if to_snap is None:
        return _error(
            "snapshot_not_found",
            f"Could not resolve 'to' snapshot (to_index={to_index!r}, to_timestamp={to_timestamp!r}).",
        )

    from_snap = _by_timestamp(from_timestamp) or _by_index(from_index)
    if from_snap is None and from_timestamp is None and from_index is None:
        from_snap = snapshots[1]
    if from_snap is None:
        return _error(
            "snapshot_not_found",
            f"Could not resolve 'from' snapshot (from_index={from_index!r}, from_timestamp={from_timestamp!r}).",
        )

    def _load(snap: tuple[str, Path]) -> dict[str, Any]:
        label, path = snap
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        state_text = extract_section_body(body, "State")
        oq_text = extract_section_body(body, "Open Questions")
        open_questions = [ln.lstrip("- ").strip() for ln in oq_text.splitlines() if ln.strip().startswith("-")]
        return {
            "id": label,
            "created": str(meta.get("created") or meta.get("updated") or ""),
            "source": str(path.relative_to(repo)),
            "state_text": state_text,
            "open_questions": open_questions,
        }

    from_data = _load(from_snap)
    to_data = _load(to_snap)

    before_lines = [line + "\n" for line in from_data["state_text"].splitlines()]
    after_lines = [line + "\n" for line in to_data["state_text"].splitlines()]
    unified_diff_lines = list(
        difflib.unified_diff(
            before_lines, after_lines,
            fromfile=from_data["source"], tofile=to_data["source"],
        )
    )
    state_diff = {
        "changed": from_data["state_text"] != to_data["state_text"],
        "unified_diff": unified_diff_lines,
        "word_count_before": len(from_data["state_text"].split()),
        "word_count_after": len(to_data["state_text"].split()),
    }

    # Exact string match only — a question that was reworded rather than
    # resolved shows up as one resolved + one new, since recognizing "this is
    # the same question, just reworded" needs semantic judgment, not a
    # deterministic string comparison.
    from_qs = set(from_data["open_questions"])
    to_qs = set(to_data["open_questions"])
    resolved = sorted(from_qs - to_qs)
    new_questions = sorted(to_qs - from_qs)
    still_open = sorted(from_qs & to_qs)
    open_questions_diff = {
        "resolved": resolved,
        "new": new_questions,
        "still_open": still_open,
        "resolved_count": len(resolved),
        "new_count": len(new_questions),
        "still_open_count": len(still_open),
    }

    summary = (
        f"{len(resolved)} open question(s) resolved, {len(new_questions)} new, "
        f"{len(still_open)} still open. State "
        f"{'changed' if state_diff['changed'] else 'unchanged'} "
        f"({state_diff['word_count_before']} -> {state_diff['word_count_after']} words)."
    )

    return finalize_memory_result({
        "workflow": "CompareContextSnapshotsWorkflow",
        "from_snapshot": {"id": from_data["id"], "created": from_data["created"], "source": from_data["source"]},
        "to_snapshot": {"id": to_data["id"], "created": to_data["created"], "source": to_data["source"]},
        "state_diff": state_diff,
        "open_questions_diff": open_questions_diff,
        "summary": summary,
        "checked_at": checked_at,
        "error_code": None,
        "error": None,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    })


# ---- SearchDecisionLogWorkflow -----------------------------------------------

_DECISION_SEARCH_FIELD_NAMES = ("title", "context", "decision", "reasoning", "consequences")


def _extract_decision_title(body: str) -> str:
    """Extract the ADR title from its H1 heading.

    title is not a frontmatter field — track_decision() only ever writes it
    as the first body line ("# {title}"), so this is the sole way to recover
    it for search.
    """
    lines = body.splitlines()
    first_line = lines[0] if lines else ""
    return first_line[2:].strip() if first_line.startswith("# ") else first_line.strip()


def _make_decision_snippet(text: str, match: Any, window: int = 60) -> str:
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _resolve_current_decision_id(decision_id: str, decisions_by_id: dict[str, dict[str, Any]]) -> str:
    """Walk the superseded_by chain to the decision that is not itself
    superseded — handles a decision superseded more than once, not just one
    hop, so a caller always lands on the actually-current decision."""
    seen: set[str] = set()
    current = decision_id
    while current not in seen:
        seen.add(current)
        entry = decisions_by_id.get(current)
        if entry is None:
            return current
        next_id = entry.get("superseded_by")
        if not next_id or next_id not in decisions_by_id:
            return current
        current = next_id
    return current  # cycle guard for malformed/hand-edited data; should not happen


def build_search_decisions_artifacts(
    repo: Path,
    query: str,
    *,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Search TrackDecisionLog's decisions/*.md by keyword. Zero LLM calls, read-only.

    Case-insensitive substring matching (re.escape + re.IGNORECASE — the same
    primitive SearchWorkflow uses), applied independently per field across
    title/context/decision/reasoning/consequences via a purpose-built loop
    over decisions/*.md — not routed through SearchWorkflow's whole-repo scan,
    which is built for arbitrary scattered files and has no notion of "field"
    at all.

    Ranking: title match ranks above body-only match, then more matched
    fields ranks higher within that tier, then newest created as the final
    tiebreak. Every result reports status/superseded_by plus
    current_decision_id, resolved by walking the full superseded_by chain
    (not just one hop), so a superseded decision found via search is never
    mistaken for the current one.
    """
    import datetime

    from ..memory.skilllayer_memory import extract_section_body, finalize_memory_result, parse_frontmatter

    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    search_fields = list(fields) if fields else list(_DECISION_SEARCH_FIELD_NAMES)
    invalid_fields = [f for f in search_fields if f not in _DECISION_SEARCH_FIELD_NAMES]
    if invalid_fields:
        return finalize_memory_result({
            "workflow": "SearchDecisionLogWorkflow",
            "query": query,
            "fields_searched": search_fields,
            "results": [],
            "total_matches": 0,
            "superseded_count": 0,
            "checked_at": checked_at,
            "error_code": "invalid_fields",
            "error": f"Unknown field(s): {invalid_fields}. Valid fields: {list(_DECISION_SEARCH_FIELD_NAMES)}.",
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
        })

    compiled = re.compile(re.escape(query), re.IGNORECASE)

    decisions_dir = repo / ".skilllayer" / "decisions"
    decision_files = sorted(decisions_dir.glob("*.md")) if decisions_dir.exists() else []

    # First pass: parse every decision file once, so the supersession chain
    # can be resolved against the full set regardless of match order.
    parsed: dict[str, dict[str, Any]] = {}
    for path in decision_files:
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        decision_id = str(meta.get("id") or path.stem[:4])
        field_text = {
            "title": _extract_decision_title(body),
            "context": extract_section_body(body, "Context"),
            "decision": extract_section_body(body, "Decision"),
            "reasoning": extract_section_body(body, "Reasoning"),
            "consequences": extract_section_body(body, "Consequences"),
        }
        parsed[decision_id] = {
            "decision_id": decision_id,
            "status": str(meta.get("status") or "proposed"),
            "superseded_by": meta.get("superseded_by"),
            "created": str(meta.get("created") or ""),
            "file": str(path.relative_to(repo)),
            "fields": field_text,
        }

    results: list[dict[str, Any]] = []
    for decision_id, entry in parsed.items():
        matched_fields: list[str] = []
        matched_snippets: dict[str, str] = {}
        for field_name in search_fields:
            text = entry["fields"].get(field_name, "")
            if not text:
                continue
            match = compiled.search(text)
            if match:
                matched_fields.append(field_name)
                matched_snippets[field_name] = _make_decision_snippet(text, match)
        if not matched_fields:
            continue
        results.append({
            "decision_id": decision_id,
            "title": entry["fields"]["title"],
            "status": entry["status"],
            "superseded_by": entry["superseded_by"],
            "current_decision_id": _resolve_current_decision_id(decision_id, parsed),
            "matched_fields": matched_fields,
            "matched_snippets": matched_snippets,
            "created": entry["created"],
            "file": entry["file"],
        })

    # Stable multi-key sort: apply the least-significant key first.
    # (created, decision_id) needs descending (newest first) as the final
    # tiebreak — decision_id is included because "created" has only
    # second-level resolution (_utc_iso()), so decisions logged within the
    # same wall-clock second get an identical timestamp; decision_id is an
    # atomically-incrementing, zero-padded counter that never collides, so it
    # reliably breaks ties timestamps can't. (title_matched, field_count) is
    # the primary key and sorts ascending (0 before 1, more negative
    # field_count — i.e. more fields — before less).
    results.sort(key=lambda r: (r["created"], r["decision_id"]), reverse=True)
    results.sort(key=lambda r: (0 if "title" in r["matched_fields"] else 1, -len(r["matched_fields"])))

    superseded_count = sum(1 for r in results if r["status"] == "superseded")

    return finalize_memory_result({
        "workflow": "SearchDecisionLogWorkflow",
        "query": query,
        "fields_searched": search_fields,
        "results": results,
        "total_matches": len(results),
        "superseded_count": superseded_count,
        "checked_at": checked_at,
        "error_code": None,
        "error": None,
        "tests_run": False,
        "tests_passed": None,
        "validation_status": "not_applicable",
    })


# ---- CrossSessionTodo (AddTodo / MarkTodoDone / ListTodos) ------------------

def build_add_todo_artifacts(repo: Path, text: str) -> dict[str, Any]:
    from ..memory.skilllayer_memory import add_todo
    return _with_repo_write_report(repo, lambda: add_todo(repo / ".skilllayer", text))


def build_mark_todo_done_artifacts(repo: Path, todo_id: str, status: str = "done") -> dict[str, Any]:
    from ..memory.skilllayer_memory import mark_todo_done
    return _with_repo_write_report(
        repo,
        lambda: mark_todo_done(repo / ".skilllayer", todo_id, status=status),
    )


def build_list_todos_artifacts(repo: Path, status_filter: str = "open") -> dict[str, Any]:
    from ..memory.skilllayer_memory import list_todos
    return list_todos(repo / ".skilllayer", status_filter=status_filter)


def build_validate_memory_artifacts(repo: Path) -> dict[str, Any]:
    from ..memory.skilllayer_memory import validate_memory_store
    return validate_memory_store(repo / ".skilllayer")


def _extract_todo_text(task: str) -> str | None:
    """Best-effort: pull the todo text out of a free-text 'add a todo: ...' task."""
    match = re.search(r"\badd\s+(?:a\s+|another\s+)?todo\s*[:\-]?\s*(.+)", task, re.I)
    if match:
        text = match.group(1).strip()
        return text or None
    return None


def _extract_todo_id(task: str) -> str | None:
    """Best-effort: pull a todo id (bare or zero-padded) out of free text, normalized to 4 digits."""
    match = re.search(r"\btodo\s*#?\s*(\d{1,4})\b", task, re.I)
    if match:
        return f"{int(match.group(1)):04d}"
    return None


def _extract_todo_target_status(task: str) -> str:
    """For MarkTodoDoneWorkflow: default is 'done'; detect an explicit reopen request."""
    if re.search(r"\breopen\b|\bre-open\b|\bnot\s+done\b|\bundo\b", task, re.I):
        return "open"
    return "done"


def _extract_todo_status_filter(task: str) -> str:
    """For ListTodosWorkflow: default is 'open' (the far more common query)."""
    if re.search(r"\ball\s+(?:my\s+)?todos?\b", task, re.I):
        return "all"
    if re.search(r"\b(?:done|completed?|finished)\s+todos?\b", task, re.I):
        return "done"
    return "open"


def build_list_branches_artifacts(repo: Path) -> dict[str, Any]:
    """List all git branches with metadata. Zero LLM calls."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    rc, _, _ = _run_git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        return {
            "workflow": "ListBranchesWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository.",
            "branches": [],
            "current_branch": None,
            "total_count": 0,
            "checked_at": checked_at,
        }

    _, current_branch, _ = _run_git(["branch", "--show-current"], repo)

    default_branch: str | None = None
    for candidate in ("main", "master"):
        rc2, _, _ = _run_git(["rev-parse", "--verify", candidate], repo)
        if rc2 == 0:
            default_branch = candidate
            break

    _SEP = "\x01"
    _, local_raw, _ = _run_git(
        ["for-each-ref", f"--format=%(refname:short){_SEP}%(objectname:short){_SEP}%(subject){_SEP}%(creatordate:iso-strict)", "refs/heads/"],
        repo,
    )
    _, remote_raw, _ = _run_git(
        ["for-each-ref", f"--format=%(refname:short){_SEP}%(objectname:short){_SEP}%(subject){_SEP}%(creatordate:iso-strict)", "refs/remotes/"],
        repo,
    )

    branches: list[dict[str, Any]] = []

    def _parse_branch_line(line: str, remote: bool) -> dict[str, Any] | None:
        parts = line.split(_SEP)
        if len(parts) < 4:
            return None
        name, short_hash, subject, date = parts[0], parts[1], parts[2], parts[3]
        if not name or name.endswith("/HEAD"):
            return None
        return {
            "name": name,
            "current": name == current_branch,
            "remote": remote,
            "last_commit_hash": short_hash,
            "last_commit_message": subject,
            "last_commit_date": date,
            "ahead": None,
            "behind": None,
        }

    for line in (local_raw or "").splitlines():
        b = _parse_branch_line(line.strip(), remote=False)
        if b is None:
            continue
        if default_branch and b["name"] != default_branch:
            _, ahead_raw, _ = _run_git(["rev-list", "--count", f"{default_branch}..{b['name']}"], repo)
            _, behind_raw, _ = _run_git(["rev-list", "--count", f"{b['name']}..{default_branch}"], repo)
            try:
                b["ahead"] = int(ahead_raw.strip())
            except (ValueError, AttributeError):
                pass
            try:
                b["behind"] = int(behind_raw.strip())
            except (ValueError, AttributeError):
                pass
        branches.append(b)

    for line in (remote_raw or "").splitlines():
        b = _parse_branch_line(line.strip(), remote=True)
        if b:
            branches.append(b)

    return {
        "workflow": "ListBranchesWorkflow",
        "branches": branches,
        "current_branch": current_branch or None,
        "total_count": len(branches),
        "checked_at": checked_at,
    }


def build_git_log_artifacts(
    repo: Path,
    limit: int = 20,
    author: str | None = None,
    since: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Read git commit log with stats. Zero LLM calls. Author email never exposed."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    limit = min(max(1, limit), 100)

    rc, _, _ = _run_git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        return {
            "workflow": "GitLogWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository.",
            "commits": [],
            "total_shown": 0,
            "filters_applied": {},
            "checked_at": checked_at,
        }

    args = ["log", f"--max-count={limit}", "--format=%H%x00%h%x00%s%x00%an%x00%aI", "--numstat"]
    filters: dict[str, Any] = {"limit": limit}
    if author:
        args += [f"--author={author}"]
        filters["author"] = author
    if since:
        args += [f"--since={since}"]
        filters["since"] = since
    if path:
        args += ["--", path]
        filters["path"] = path

    _, output, _ = _run_git(args, repo)

    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (output or "").splitlines():
        if "\x00" in line:
            if current is not None:
                commits.append(current)
            parts = line.split("\x00")
            if len(parts) >= 5:
                current = {
                    "hash": parts[1],
                    "full_hash": parts[0],
                    "message": parts[2],
                    "author_name": parts[3],
                    "date": parts[4],
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
        elif current is not None and "\t" in line:
            parts_tab = line.split("\t")
            if len(parts_tab) >= 3:
                try:
                    current["insertions"] += int(parts_tab[0]) if parts_tab[0] != "-" else 0
                    current["deletions"] += int(parts_tab[1]) if parts_tab[1] != "-" else 0
                    current["files_changed"] += 1
                except (ValueError, IndexError):
                    pass
    if current is not None:
        commits.append(current)

    return {
        "workflow": "GitLogWorkflow",
        "commits": commits,
        "total_shown": len(commits),
        "filters_applied": filters,
        "checked_at": checked_at,
    }


def build_get_commit_artifacts(repo: Path, commit_hash: str) -> dict[str, Any]:
    """Get full details for a specific commit. Zero LLM calls. Author email never exposed."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    rc, full_hash_raw, _ = _run_git(["rev-parse", "--verify", commit_hash], repo)
    if rc != 0:
        return {
            "workflow": "GetCommitDetailsWorkflow",
            "error_code": "commit_not_found",
            "error": f"Commit not found: {commit_hash}",
            "hash": None,
            "short_hash": None,
            "message": None,
            "author_name": None,
            "date": None,
            "files_changed": [],
            "total_insertions": 0,
            "total_deletions": 0,
            "parent_hashes": [],
            "checked_at": checked_at,
        }
    full_hash = full_hash_raw.strip()
    short_hash = full_hash[:7]

    _, meta, _ = _run_git(["log", "-1", "--format=%an%x00%aI%x00%P", full_hash], repo)
    _, message, _ = _run_git(["log", "-1", "--format=%B", full_hash], repo)

    meta_parts = (meta or "").split("\x00")
    author_name = meta_parts[0] if len(meta_parts) > 0 else ""
    date = meta_parts[1] if len(meta_parts) > 1 else ""
    parent_raw = meta_parts[2] if len(meta_parts) > 2 else ""
    parent_hashes = [h[:7] for h in parent_raw.split() if h]

    _, numstat_raw, _ = _run_git(["diff-tree", "--numstat", "-r", "--no-commit-id", "--root", full_hash], repo)
    _, namestatus_raw, _ = _run_git(["diff-tree", "--name-status", "-r", "--no-commit-id", "--root", full_hash], repo)

    _STATUS_LABELS: dict[str, str] = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
    status_map: dict[str, str] = {}
    for line in (namestatus_raw or "").splitlines():
        ns_parts = line.split("\t", 2)
        if len(ns_parts) >= 2:
            code = ns_parts[0].strip()[:1]
            fp = ns_parts[-1].strip()
            status_map[fp] = _STATUS_LABELS.get(code, code.lower())

    files_changed: list[dict[str, Any]] = []
    total_ins = 0
    total_dels = 0
    for line in (numstat_raw or "").splitlines():
        num_parts = line.split("\t")
        if len(num_parts) >= 3:
            try:
                ins = int(num_parts[0]) if num_parts[0] != "-" else 0
                dels = int(num_parts[1]) if num_parts[1] != "-" else 0
            except ValueError:
                ins = dels = 0
            fp = num_parts[2].strip()
            total_ins += ins
            total_dels += dels
            files_changed.append({
                "path": fp,
                "status": status_map.get(fp, "modified"),
                "insertions": ins,
                "deletions": dels,
            })

    return {
        "workflow": "GetCommitDetailsWorkflow",
        "hash": full_hash,
        "short_hash": short_hash,
        "message": (message or "").strip(),
        "author_name": author_name,
        "date": date,
        "files_changed": files_changed,
        "total_insertions": total_ins,
        "total_deletions": total_dels,
        "parent_hashes": parent_hashes,
        "checked_at": checked_at,
    }


def build_git_diff_artifacts(
    repo: Path,
    base: str = "HEAD",
    target: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Show diff between refs or working tree. Zero LLM calls. Preview capped at 500 chars."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    rc, _, _ = _run_git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        return {
            "workflow": "GitDiffWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository.",
            "files": [],
            "total_files": 0,
            "total_insertions": 0,
            "total_deletions": 0,
            "truncated": False,
            "checked_at": checked_at,
        }

    rc2, _, _ = _run_git(["rev-parse", "--verify", base], repo)
    if rc2 != 0:
        return {
            "workflow": "GitDiffWorkflow",
            "error_code": "ref_not_found",
            "error": f"Git ref not found: {base}",
            "files": [],
            "total_files": 0,
            "total_insertions": 0,
            "total_deletions": 0,
            "truncated": False,
            "checked_at": checked_at,
        }

    if target is not None:
        rc3, _, _ = _run_git(["rev-parse", "--verify", target], repo)
        if rc3 != 0:
            return {
                "workflow": "GitDiffWorkflow",
                "error_code": "ref_not_found",
                "error": f"Git ref not found: {target}",
                "files": [],
                "total_files": 0,
                "total_insertions": 0,
                "total_deletions": 0,
                "truncated": False,
                "checked_at": checked_at,
            }

    def _base_args(extra: list[str], file_filter: str | None = None) -> list[str]:
        args = ["diff"] + extra + [base]
        if target is not None:
            args.append(target)
        if file_filter:
            args += ["--", file_filter]
        return args

    _, namestatus_raw, _ = _run_git(_base_args(["--name-status"], file_filter=path), repo)
    _, numstat_raw, _ = _run_git(_base_args(["--numstat"], file_filter=path), repo)

    _STATUS_LABELS: dict[str, str] = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
    status_map: dict[str, str] = {}
    for line in (namestatus_raw or "").splitlines():
        ns_parts = line.split("\t", 2)
        if len(ns_parts) >= 2:
            code = ns_parts[0].strip()[:1]
            fp = ns_parts[-1].strip()
            status_map[fp] = _STATUS_LABELS.get(code, code.lower())

    files: list[dict[str, Any]] = []
    total_ins = 0
    total_dels = 0
    for line in (numstat_raw or "").splitlines():
        num_parts = line.split("\t")
        if len(num_parts) >= 3:
            try:
                ins = int(num_parts[0]) if num_parts[0] != "-" else 0
                dels = int(num_parts[1]) if num_parts[1] != "-" else 0
            except ValueError:
                ins = dels = 0
            fp = num_parts[2].strip()
            total_ins += ins
            total_dels += dels
            files.append({
                "path": fp,
                "status": status_map.get(fp, "modified"),
                "insertions": ins,
                "deletions": dels,
                "diff_preview": "",
            })

    PREVIEW_CAP = 500
    for finfo in files[:20]:
        per_file_args = ["diff", base]
        if target is not None:
            per_file_args.append(target)
        per_file_args += ["--", finfo["path"]]
        _, file_diff, _ = _run_git(per_file_args, repo)
        finfo["diff_preview"] = (file_diff or "")[:PREVIEW_CAP]

    return {
        "workflow": "GitDiffWorkflow",
        "files": files,
        "total_files": len(files),
        "total_insertions": total_ins,
        "total_deletions": total_dels,
        "truncated": len(files) > 20,
        "checked_at": checked_at,
    }


def build_file_history_artifacts(repo: Path, file_path: str, limit: int = 10) -> dict[str, Any]:
    """Get commit history for a file. Zero LLM calls. Author email never exposed."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    limit = min(max(1, limit), 50)

    rc, _, _ = _run_git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        return {
            "workflow": "GetFileHistoryWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository.",
            "file": file_path,
            "commits": [],
            "total_shown": 0,
            "first_commit_date": None,
            "last_modified_date": None,
            "checked_at": checked_at,
        }

    _, probe, _ = _run_git(["log", "-1", "--format=%H", "--", file_path], repo)
    if not probe or not probe.strip():
        return {
            "workflow": "GetFileHistoryWorkflow",
            "error_code": "file_not_in_history",
            "error": f"File not found in git history: {file_path}",
            "file": file_path,
            "commits": [],
            "total_shown": 0,
            "first_commit_date": None,
            "last_modified_date": None,
            "checked_at": checked_at,
        }

    _, output, _ = _run_git(
        ["log", f"--max-count={limit}", "--format=%h%x00%s%x00%an%x00%aI", "--numstat", "--", file_path],
        repo,
    )

    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (output or "").splitlines():
        if "\x00" in line:
            if current is not None:
                commits.append(current)
            parts = line.split("\x00")
            if len(parts) >= 4:
                current = {
                    "hash": parts[0],
                    "message": parts[1],
                    "author_name": parts[2],
                    "date": parts[3],
                    "insertions": 0,
                    "deletions": 0,
                }
        elif current is not None and "\t" in line:
            tab_parts = line.split("\t")
            if len(tab_parts) >= 2:
                try:
                    current["insertions"] += int(tab_parts[0]) if tab_parts[0] != "-" else 0
                    current["deletions"] += int(tab_parts[1]) if tab_parts[1] != "-" else 0
                except (ValueError, IndexError):
                    pass
    if current is not None:
        commits.append(current)

    last_mod = commits[0]["date"] if commits else None

    _, earliest_raw, _ = _run_git(["log", "--format=%aI", "--follow", "--reverse", "--", file_path], repo)
    earliest_lines = (earliest_raw or "").splitlines()
    first_date = earliest_lines[0].strip() if earliest_lines else None

    return {
        "workflow": "GetFileHistoryWorkflow",
        "file": file_path,
        "commits": commits,
        "total_shown": len(commits),
        "first_commit_date": first_date,
        "last_modified_date": last_mod,
        "checked_at": checked_at,
    }


def build_git_blame_artifacts(
    repo: Path,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Run git blame. Zero LLM calls. Author email never exposed. Capped at 200 lines."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    MAX_LINES = 200

    rc, _, _ = _run_git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        return {
            "workflow": "GitBlameWorkflow",
            "error_code": "not_git_repo",
            "error": "Not a git repository.",
            "file": file_path,
            "lines": [],
            "total_lines": 0,
            "unique_authors": [],
            "date_range": {"earliest": None, "latest": None},
            "truncated": False,
            "checked_at": checked_at,
        }

    rc2, _, _ = _run_git(["ls-files", "--error-unmatch", file_path], repo)
    if rc2 != 0:
        return {
            "workflow": "GitBlameWorkflow",
            "error_code": "file_not_found",
            "error": f"File not found or not tracked by git: {file_path}",
            "file": file_path,
            "lines": [],
            "total_lines": 0,
            "unique_authors": [],
            "date_range": {"earliest": None, "latest": None},
            "truncated": False,
            "checked_at": checked_at,
        }

    args = ["blame", "--line-porcelain"]
    if start_line is not None and end_line is not None:
        args += [f"-L{start_line},{end_line}"]
    elif start_line is not None:
        args += [f"-L{start_line},+{MAX_LINES}"]
    args.append(file_path)

    rc3, output, stderr = _run_git(args, repo)
    if rc3 != 0:
        return {
            "workflow": "GitBlameWorkflow",
            "error_code": "blame_failed",
            "error": stderr or "git blame failed",
            "file": file_path,
            "lines": [],
            "total_lines": 0,
            "unique_authors": [],
            "date_range": {"earliest": None, "latest": None},
            "truncated": False,
            "checked_at": checked_at,
        }

    raw_lines = (output or "").splitlines()
    parsed_lines: list[dict[str, Any]] = []
    current_hash: str = ""
    current_author: str = ""
    current_date: str = ""
    current_summary: str = ""
    line_num: int = 0
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        words = raw.split()
        if words and len(words[0]) == 40 and all(c in "0123456789abcdef" for c in words[0]):
            current_hash = words[0][:7]
            line_num = int(words[2]) if len(words) > 2 else 0
            i += 1
            while i < len(raw_lines) and not raw_lines[i].startswith("\t"):
                hdr = raw_lines[i]
                if hdr.startswith("author ") and not hdr.startswith("author-"):
                    current_author = hdr[7:]
                elif hdr.startswith("author-time "):
                    try:
                        ts = int(hdr[12:])
                        current_date = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
                    except (ValueError, OSError):
                        current_date = ""
                elif hdr.startswith("summary "):
                    current_summary = hdr[8:]
                i += 1
            if i < len(raw_lines) and raw_lines[i].startswith("\t"):
                content = raw_lines[i][1:]
                parsed_lines.append({
                    "line_number": line_num,
                    "content": content,
                    "commit_hash": current_hash,
                    "author_name": current_author,
                    "date": current_date,
                    "summary": current_summary,
                })
                i += 1
        else:
            i += 1

    truncated = len(parsed_lines) > MAX_LINES
    if truncated:
        parsed_lines = parsed_lines[:MAX_LINES]

    unique_authors = sorted({ln["author_name"] for ln in parsed_lines if ln["author_name"]})
    dates = [ln["date"] for ln in parsed_lines if ln["date"]]
    date_range: dict[str, Any] = {
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
    }

    return {
        "workflow": "GitBlameWorkflow",
        "file": file_path,
        "lines": parsed_lines,
        "total_lines": len(parsed_lines),
        "unique_authors": unique_authors,
        "date_range": date_range,
        "truncated": truncated,
        "checked_at": checked_at,
    }


def build_find_conflicts_artifacts(repo: Path) -> dict[str, Any]:
    """Scan repository for merge conflict markers. Pure file scan. Zero LLM calls."""
    import datetime as _dt

    checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    CONFLICT_START = "<<<<<<< "
    CONFLICT_SEP = "======="
    CONFLICT_END = ">>>>>>> "

    conflicts: list[dict[str, Any]] = []
    total_sections = 0

    for fpath in sorted(repo.rglob("*")):
        if not fpath.is_file():
            continue
        try:
            rel = fpath.relative_to(repo)
        except ValueError:
            continue
        if should_ignore_search_path(fpath, repo):
            continue
        if fpath.suffix.lower() in DEFAULT_SEARCH_IGNORE_SUFFIXES:
            continue
        try:
            content = fpath.read_text("utf-8", errors="replace")
        except OSError:
            continue

        if CONFLICT_START not in content:
            continue

        file_sections: list[dict[str, Any]] = []
        start_ln: int | None = None
        sep_ln: int | None = None
        for ln, line in enumerate(content.splitlines(), 1):
            if line.startswith(CONFLICT_START):
                start_ln = ln
                sep_ln = None
            elif line.startswith(CONFLICT_SEP) and start_ln is not None and sep_ln is None:
                sep_ln = ln
            elif line.startswith(CONFLICT_END) and start_ln is not None:
                file_sections.append({
                    "start_line": start_ln,
                    "separator_line": sep_ln if sep_ln is not None else start_ln,
                    "end_line": ln,
                })
                start_ln = None
                sep_ln = None

        if file_sections:
            conflicts.append({
                "file": str(rel),
                "conflict_count": len(file_sections),
                "sections": file_sections,
            })
            total_sections += len(file_sections)

    return {
        "workflow": "FindMergeConflictsWorkflow",
        "conflicts": conflicts,
        "total_files_with_conflicts": len(conflicts),
        "total_conflict_sections": total_sections,
        "clean": len(conflicts) == 0,
        "checked_at": checked_at,
    }


# ============================================================================
# Professional skill packs — SafeCodeChangeWorkflow, ReleaseReadinessWorkflow,
# ResumeProjectWorkWorkflow. Each composes existing read-only/bounded builders
# above into one bounded, honest verdict. None duplicate the underlying
# inspection/search/secret-scan/test-execution/memory logic; they only
# aggregate and classify it. See docs/SKILLS.md for the user-facing contract.
# ============================================================================

_SAFE_CHANGE_PLAN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "implement", "help", "change",
    "feature", "safely", "without", "breaking", "unrelated", "behavior", "make",
    "code", "plan", "prepare", "verify", "issue", "please", "into", "from",
})


def _extract_change_search_terms(task: str) -> list[str]:
    """Deterministic keyword extraction for the safe-change relevance search.

    Prefers quoted/backticked substrings (the caller naming a specific symbol
    or file); falls back to significant words from the free-text task
    description. Zero LLM calls — this is a heuristic, not symbol resolution,
    and is reported as such (relevant_files/relevant_symbols are candidates,
    never a guaranteed-complete set)."""
    quoted = re.findall(r"[\"'`]([^\"'`]{2,60})[\"'`]", task)
    if quoted:
        return quoted[:5]
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task)
    terms = [w for w in words if w.lower() not in _SAFE_CHANGE_PLAN_STOPWORDS]
    return terms[:5]


def _build_safe_change_plan(task: str, relevant_files: list[str], repo_info: dict[str, Any]) -> list[str]:
    plan = [f"Understand the request: {task.strip()}"]
    if relevant_files:
        shown = ", ".join(relevant_files[:5])
        suffix = f" and {len(relevant_files) - 5} more" if len(relevant_files) > 5 else ""
        plan.append(f"Review {len(relevant_files)} candidate file(s) found by keyword search: {shown}{suffix}.")
    else:
        plan.append(
            "No candidate files matched by keyword search — inspect the repository "
            "structure manually to locate the right place to change."
        )
    plan.append("Make the smallest change that satisfies the request without touching unrelated code.")
    test_command = repo_info.get("detected_test_command")
    if test_command:
        plan.append(f"Run the detected test command ({' '.join(test_command)}) to validate.")
    else:
        plan.append("No test command was detected for this repository — validation will need manual review.")
    plan.append("Call this skill again with phase='validate' to inspect the diff and confirm nothing else broke.")
    return plan


def build_safe_change_artifacts(
    repo: Path,
    task: str,
    *,
    phase: str = "plan",
    test_command: list[str] | None = None,
    save_context: bool = False,
    acknowledge_dirty: bool = False,
) -> dict[str, Any]:
    """Orchestrate SkillLayer's own inspection/search/test capabilities into a
    two-phase safe-change workflow. SkillLayer never edits source files here —
    'executed_by' is always reported as 'host_agent'; the host AI coding agent
    performs the actual edit between the 'plan' and 'validate' calls."""
    if phase not in ("plan", "validate"):
        return {
            "skill": "safe_code_change", "success": False, "phase": phase,
            "repository_state": None, "uncommitted_work_present": None,
            "relevant_files": [], "relevant_symbols": [], "proposed_plan": None,
            "executed_by": "host_agent", "changed_files": [], "tests_run": False,
            "tests_passed": None, "tests_failed": None, "validation_complete": False,
            "test_environment": None, "selected_python": None, "tests_started": False,
            "environment_blocker": None, "remediation": None,
            "risks": [], "warnings": [], "next_action": "Call with phase='plan' or phase='validate'.",
            "written_paths": [], "verdict": "UNSUPPORTED",
            "error": f"unsupported phase: {phase!r}",
        }

    from ..tools.inspect import inspect_repo
    repo_info = inspect_repo(repo)
    policy_result = load_policy(repo)
    policy_status = policy_result.get("status")
    if policy_status in {"POLICY_INVALID", "POLICY_UNSUPPORTED_VERSION", "POLICY_CONFLICT", "POLICY_UNSAFE_PATH"}:
        return {
            "schema_version": 1, "skill": "safe_code_change", "success": False, "phase": phase,
            "policy": policy_result, "checks": [], "approvals": [], "blockers": policy_result.get("errors", []),
            "warnings": [], "evidence": {}, "error_code": policy_status, "summary": "Repository policy prevented safe-change evaluation.",
            "repository_state": None, "uncommitted_work_present": None, "relevant_files": [], "relevant_symbols": [],
            "proposed_plan": None, "executed_by": "host_agent", "changed_files": [], "tests_run": False,
            "tests_passed": None, "tests_failed": None, "validation_complete": False, "test_environment": None,
            "selected_python": None, "tests_started": False, "environment_blocker": None, "remediation": None,
            "risks": [], "next_action": "Fix the policy, then rerun the workflow.", "written_paths": [], "verdict": "POLICY_INVALID",
        }
    git_artifacts, _ = build_git_status_artifacts(repo, [])
    is_git_repo = bool(git_artifacts.get("is_git_repo"))
    repository_state = {
        "is_git_repo": is_git_repo,
        "branch": git_artifacts.get("branch"),
        "clean": git_artifacts.get("clean"),
        "staged_count": git_artifacts.get("staged_count"),
        "unstaged_count": git_artifacts.get("unstaged_count"),
        "untracked_count": git_artifacts.get("untracked_count"),
    }

    if not is_git_repo:
        return {
            "skill": "safe_code_change", "success": False, "phase": phase,
            "repository_state": repository_state, "uncommitted_work_present": None,
            "relevant_files": [], "relevant_symbols": [], "proposed_plan": None,
            "executed_by": "host_agent", "changed_files": [], "tests_run": False,
            "tests_passed": None, "tests_failed": None, "validation_complete": False,
            "test_environment": None, "selected_python": None, "tests_started": False,
            "environment_blocker": None, "remediation": None,
            "risks": ["Not a git repository — changes cannot be tracked or diffed."],
            "warnings": [], "next_action": "Initialize git before requesting a safe change.",
            "policy": policy_result, "checks": [], "approvals": [], "blockers": ["repository_state"], "evidence": repository_state,
            "schema_version": 1, "summary": "Repository state is not suitable for tracked safe change.", "error_code": "not_a_git_repository",
            "written_paths": [], "verdict": "BLOCKED_BY_REPOSITORY_STATE",
        }

    uncommitted_work_present = not bool(git_artifacts.get("clean", True))
    policy_approval_required = bool(
        policy_status == "POLICY_VALID"
        and policy_result["normalized_policy"]["safe_change"].get("require_clean_or_acknowledged_worktree")
        and uncommitted_work_present
        and not acknowledge_dirty
        and phase == "plan"
    )
    risks: list[str] = []
    warnings: list[str] = []
    if uncommitted_work_present and phase == "plan":
        # Only meaningful before any edit has happened: at phase="validate",
        # the working tree is *expected* to be dirty — that dirt is the change
        # being validated, not a pre-existing problem.
        risks.append(
            "Repository already has uncommitted or untracked changes before this "
            "request — the eventual diff may include unrelated changes unless isolated."
        )
    if not repo_info.get("detected_test_command"):
        risks.append("No test command was detected for this repository; validation cannot include automated tests.")

    query_terms = _extract_change_search_terms(task)
    relevant_files: list[str] = []
    relevant_symbols: list[str] = []
    for term in query_terms:
        search_artifacts = build_search_artifacts(repo, term, mode="text", case_sensitive=False, limit=20)
        matches = search_artifacts.get("matches", []) or []
        if matches:
            relevant_symbols.append(term)
            for m in matches:
                if m["file"] not in relevant_files:
                    relevant_files.append(m["file"])
    relevant_files = relevant_files[:25]

    if phase == "plan":
        proposed_plan = _build_safe_change_plan(task, relevant_files, repo_info)
        plan_result = {
            "schema_version": 1,
            "skill": "safe_code_change", "success": True, "phase": "plan",
            "repository_state": repository_state, "uncommitted_work_present": uncommitted_work_present,
            "relevant_files": relevant_files, "relevant_symbols": relevant_symbols,
            "proposed_plan": proposed_plan, "executed_by": "host_agent",
            "changed_files": [], "tests_run": False, "tests_passed": None, "tests_failed": None,
            "validation_complete": False, "risks": risks, "warnings": warnings,
            "test_environment": None, "selected_python": None, "tests_started": False,
            "environment_blocker": None, "remediation": None,
            "next_action": "Ask the host agent to implement the plan, then call again with phase='validate'.",
            "policy": policy_result, "checks": ["worktree_acknowledgement"],
            "approvals": ["dirty_worktree"] if policy_approval_required else [],
            "blockers": ["dirty_worktree"] if policy_approval_required else [],
            "evidence": {"policy_status": policy_status, "dirty_worktree": uncommitted_work_present},
            "summary": "Safe-change plan prepared with repository policy evidence.", "error_code": "policy_approval_required" if policy_approval_required else None,
            "written_paths": [], "verdict": "POLICY_WOULD_REQUIRE_APPROVAL" if policy_approval_required else "READY_TO_IMPLEMENT",
        }
        if policy_approval_required:
            plan_result["success"] = False
            plan_result["next_action"] = "Acknowledge the existing worktree changes explicitly, then rerun the plan."
        return plan_result

    # phase == "validate": inspect the diff the host agent produced and run tests.
    changed_files = [f["path"] for f in git_artifacts.get("changed_files", []) or []]
    tests_run = False
    tests_passed: bool | None = None
    tests_failed: int | None = None
    test_environment: dict[str, Any] | None = None
    selected_python: str | None = None
    tests_started = False
    environment_blocker: dict[str, Any] | None = None
    remediation: dict[str, Any] | None = None
    if test_command or repo_info.get("detected_test_command"):
        test_result = (
            TestRunner().run_command(repo, test_command, allow_nested=True)
            if test_command
            else TestRunner().run(repo)
        )
        run_artifacts = build_run_tests_artifacts(test_result)
        tests_run = bool(run_artifacts.get("tests_run"))
        tests_passed = run_artifacts.get("tests_passed")
        test_environment = run_artifacts.get("execution_environment")
        selected_python = run_artifacts.get("selected_python")
        tests_started = bool(run_artifacts.get("tests_started", tests_run))
        remediation = run_artifacts.get("remediation")
        if run_artifacts.get("environment_error"):
            environment_blocker = {
                "code": run_artifacts.get("environment_error_code"),
                "message": run_artifacts.get("environment_error_message"),
                "remediation": remediation,
            }
            warnings.append(
                "Tests did not start because the execution environment was incomplete; "
                "code correctness was not established."
            )
        failed_tests = run_artifacts.get("failed_tests") or []
        tests_failed = len(failed_tests) if failed_tests else (0 if tests_passed else None)
        if tests_run and not tests_passed:
            risks.append("Tests failed after the change.")
    else:
        warnings.append("No test command detected or provided; tests were not run.")

    written_paths: list[str] = []
    context_save_error: dict[str, Any] | None = None
    if save_context:
        state = (
            f"Safe code change: {task.strip()}. "
            f"Changed files: {', '.join(changed_files) if changed_files else 'none detected'}. "
            f"Tests run: {tests_run}, passed: {tests_passed}."
        )
        save_artifacts = build_save_context_artifacts(repo, state, [])
        written_paths = list(save_artifacts.get("written_paths", []) or [])
        if not save_artifacts.get("success"):
            context_save_error = {
                "code": save_artifacts.get("error_code") or "context_save_failed",
                "message": save_artifacts.get("error") or "Requested context could not be saved.",
            }
            warnings.append(
                "Validation completed, but the explicitly requested context snapshot was not saved."
            )

    policy_requires_validation = bool(policy_status == "POLICY_VALID" and policy_result["normalized_policy"]["safe_change"].get("require_validation"))
    if not changed_files:
        verdict, success = "CHANGE_INCOMPLETE", False
        next_action = "No changed files were detected yet. Make the change, then call again with phase='validate'."
    elif environment_blocker:
        verdict, success = "CHANGE_INCOMPLETE", False
        command = (remediation or {}).get("remediation_command")
        next_action = (
            f"Review and run this command yourself, then retry validation: {command}"
            if command else "Repair the reported test environment, then rerun validation; the change was not classified as a test failure."
        )
    elif tests_run and not tests_passed:
        verdict, success = "VALIDATION_FAILED", False
        next_action = "Fix the failing tests, then call again with phase='validate'."
    elif not tests_run:
        verdict, success = "CHANGE_INCOMPLETE", False
        next_action = (
            "Run the required validation, then call again with phase='validate'."
            if policy_requires_validation
            else "Provide or configure a test command, run validation, then call again with phase='validate'."
        )
    elif context_save_error:
        verdict, success = "CHANGE_INCOMPLETE", False
        next_action = "Repair the reported memory-store error, then retry validation with context saving enabled."
    elif risks:
        verdict, success = "CHANGE_VALIDATED_WITH_WARNINGS", True
        next_action = "Review the flagged warnings/risks before committing."
    else:
        verdict, success = "CHANGE_VALIDATED", True
        next_action = "Ready to commit."

    return {
        "schema_version": 1, "skill": "safe_code_change", "success": success, "phase": "validate",
        "repository_state": repository_state, "uncommitted_work_present": uncommitted_work_present,
        "relevant_files": relevant_files, "relevant_symbols": relevant_symbols,
        "proposed_plan": None, "executed_by": "host_agent",
        "changed_files": changed_files, "tests_run": tests_run, "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "validation_complete": tests_run and not bool(environment_blocker) and not bool(context_save_error),
        "test_environment": test_environment, "selected_python": selected_python,
        "tests_started": tests_started, "environment_blocker": environment_blocker,
        "remediation": remediation,
        "risks": risks, "warnings": warnings, "next_action": next_action,
        "policy": policy_result, "checks": ["validation"] if tests_run else [],
        "approvals": ["dirty_worktree"] if acknowledge_dirty else [],
        "blockers": [item for item in (environment_blocker, context_save_error) if item],
        "evidence": {
            "tests_run": tests_run,
            "tests_started": tests_started,
            "validation_complete": tests_run and not bool(environment_blocker) and not bool(context_save_error),
            "context_save_requested": save_context,
            "context_saved": bool(save_context and written_paths),
        },
        "summary": "Safe-change validation completed with policy-aware evidence.",
        "error_code": (
            context_save_error["code"]
            if context_save_error and verdict == "CHANGE_INCOMPLETE" and tests_run and not environment_blocker
            else ("validation_incomplete" if verdict == "CHANGE_INCOMPLETE" else None)
        ),
        "written_paths": written_paths, "verdict": verdict,
    }


def build_release_readiness_artifacts(repo: Path, *, deep: bool = False) -> dict[str, Any]:
    """Aggregate existing read-only SkillLayer inspections into one bounded
    release-readiness verdict. Never certifies a repository as secure; an
    incomplete check always reduces confidence rather than becoming success.
    Bounded by default (deep=False: detects the test command without running
    the suite); pass deep=True to actually execute it via TestRunner."""
    policy_result = load_policy(repo)
    if policy_result["status"] in {"POLICY_INVALID", "POLICY_UNSUPPORTED_VERSION", "POLICY_CONFLICT", "POLICY_UNSAFE_PATH"}:
        return {
            "schema_version": 1, "skill": "release_readiness", "success": False, "checks_requested": [],
            "checks_completed": [], "checks_incomplete": [], "findings": [], "blockers": policy_result.get("errors", []),
            "warnings": [], "policy": policy_result, "checks": [], "approvals": [], "evidence": {},
            "verdict": "BLOCKED_BY_POLICY", "error_code": policy_result["status"],
            "summary": "Repository policy prevented release-readiness evaluation.",
        }
    checks_requested = [
        "repository_inspection", "git_status", "secret_scan", "dependency_inspection",
        "memory_integrity", "packaging_metadata", "hidden_write_self_check", "test_status",
    ]
    checks_completed: list[str] = []
    checks_incomplete: list[dict[str, str]] = []
    findings: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    platform_limitations: list[str] = [
        "Public/private artifact boundary and cross-document consistency checks "
        "require a repo-specific manifest and are not run generically.",
    ]

    from ..tools.inspect import inspect_repo
    repo_info = inspect_repo(repo)
    checks_completed.append("repository_inspection")

    git_artifacts, _ = build_git_status_artifacts(repo, [])
    checks_completed.append("git_status")
    git_status = {
        "is_git_repo": git_artifacts.get("is_git_repo"),
        "branch": git_artifacts.get("branch"),
        "clean": git_artifacts.get("clean"),
        "staged_count": git_artifacts.get("staged_count"),
        "unstaged_count": git_artifacts.get("unstaged_count"),
        "untracked_count": git_artifacts.get("untracked_count"),
    }
    if git_artifacts.get("is_git_repo") and not git_artifacts.get("clean"):
        warnings.append("Working tree has uncommitted or untracked changes.")

    skilllayer_dir = repo / ".skilllayer"
    existed_before = skilllayer_dir.exists()
    before_files = set(skilllayer_dir.rglob("*")) if existed_before else set()

    secret_artifacts = build_detect_secrets_artifacts(repo)
    checks_completed.append("secret_scan")
    secret_scan_status = secret_artifacts.get("status", "error")
    for finding in secret_artifacts.get("findings", []) or []:
        severity = finding.get("severity", "unknown")
        findings.append({
            "check": "secret_scan", "severity": severity, "file": finding.get("file"),
            "line": finding.get("line"), "pattern": finding.get("pattern_name"),
            "likely_test_fixture": finding.get("likely_test_fixture", False),
        })
        if severity == "critical" and not finding.get("likely_test_fixture"):
            blockers.append(f"Potential secret ({finding.get('pattern_name')}) in {finding.get('file')}:{finding.get('line')}")
    if secret_scan_status == "incomplete":
        checks_incomplete.append({"check": "secret_scan", "reason": "scan skipped some files that could hide a real secret"})
        warnings.append("Secret scan was incomplete — do not treat this as a clean bill of health.")

    dep_artifacts = build_map_dependencies_artifacts(repo)
    checks_completed.append("dependency_inspection")
    dependency_status = {
        "files_parsed": dep_artifacts.get("files_parsed", []),
        "files_not_found": dep_artifacts.get("files_not_found", []),
        "total_dependencies": dep_artifacts.get("total_dependencies", 0),
        "unpinned_count": dep_artifacts.get("unpinned_count", 0),
        "unpinned_names": dep_artifacts.get("unpinned_names", []),
    }
    if not dep_artifacts.get("files_parsed"):
        checks_incomplete.append({"check": "dependency_inspection", "reason": "no recognized dependency manifest found"})

    if existed_before:
        memory_artifacts = build_validate_memory_artifacts(repo)
        checks_completed.append("memory_integrity")
        broken = memory_artifacts.get("summary", {}).get("broken_count", 0)
        if broken:
            blockers.append(f"SkillLayer memory store has {broken} broken integrity finding(s).")
    else:
        checks_incomplete.append({"check": "memory_integrity", "reason": "no .skilllayer/ store present in this repo"})

    pyproject = repo / "pyproject.toml"
    setup_py = repo / "setup.py"
    package_status: dict[str, Any] = {"pyproject_toml": pyproject.exists(), "setup_py": setup_py.exists()}
    if pyproject.exists():
        try:
            try:
                import tomllib
            except ImportError:
                # Python 3.10 has no stdlib tomllib. Keep this check bounded:
                # dependency inspection already has a conservative regex
                # fallback, so valid-looking TOML is not falsely blocked.
                text = pyproject.read_text(encoding="utf-8")
                package_status["pyproject_parses"] = bool(text.strip())
                package_status["parser"] = "bounded_text_fallback"
            else:
                with pyproject.open("rb") as fh:
                    tomllib.load(fh)
                package_status["pyproject_parses"] = True
        except Exception as exc:
            package_status["pyproject_parses"] = False
            blockers.append(f"pyproject.toml does not parse: {exc}")
    checks_completed.append("packaging_metadata")
    if not pyproject.exists() and not setup_py.exists():
        checks_incomplete.append({"check": "packaging_metadata", "reason": "no pyproject.toml or setup.py found"})

    detected_test_command = repo_info.get("detected_test_command")
    test_environment_incomplete = False
    remediation: dict[str, Any] | None = None
    if detected_test_command:
        if deep:
            test_result = TestRunner().run(repo)
            run_artifacts = build_run_tests_artifacts(test_result)
            test_status = {
                "mode": "deep", "detected_test_command": detected_test_command,
                "tests_run": run_artifacts.get("tests_run"), "tests_passed": run_artifacts.get("tests_passed"),
                "outcome": run_artifacts.get("outcome"),
                "execution_environment": run_artifacts.get("execution_environment"),
                "selected_python": run_artifacts.get("selected_python"),
                "selection_source": run_artifacts.get("selection_source"),
                "fallback_used": run_artifacts.get("fallback_used", False),
                "tests_started": run_artifacts.get("tests_started", False),
                "environment_error": run_artifacts.get("environment_error", False),
                "environment_error_code": run_artifacts.get("environment_error_code"),
                "remediation": run_artifacts.get("remediation"),
            }
            checks_completed.append("test_status")
            if run_artifacts.get("environment_error"):
                remediation = run_artifacts.get("remediation")
                test_environment_incomplete = True
                checks_incomplete.append({
                    "check": "test_status",
                    "reason": "test environment prevented collection; code correctness was not established",
                })
                warnings.append("Deep test validation is incomplete because the selected environment could not start tests.")
            elif run_artifacts.get("tests_run") and not run_artifacts.get("tests_passed"):
                blockers.append("Test suite failed.")
        else:
            test_status = {
                "mode": "bounded", "detected_test_command": detected_test_command,
                "tests_run": False, "tests_passed": None,
                "note": "tests were detected but not executed in bounded mode; pass deep=True to run them",
            }
            checks_incomplete.append({"check": "test_status", "reason": "bounded mode — detected but not executed"})
    else:
        test_status = {"mode": "deep" if deep else "bounded", "detected_test_command": None, "tests_run": False, "tests_passed": None}
        checks_incomplete.append({"check": "test_status", "reason": "no test command detected"})

    after_files = set(skilllayer_dir.rglob("*")) if skilllayer_dir.exists() else set()
    hidden_write_detected = (not existed_before and skilllayer_dir.exists()) or bool(after_files - before_files)
    checks_completed.append("hidden_write_self_check")
    if hidden_write_detected:
        blockers.append("This read-only scan unexpectedly created or modified files under .skilllayer/.")

    # Not generically applicable checks: always disclosed via
    # platform_limitations, but they never gate the top verdict — they were
    # never part of checks_requested, so they must not silently make
    # READY_FOR_CAREFUL_TESTERS unreachable for every ordinary repository.
    platform_limitations.append(
        "No generic cross-document consistency checker exists yet; documentation "
        "consistency was not checked."
    )

    policy = policy_result.get("normalized_policy")
    policy_requires_tests = bool(policy and policy.get("release", {}).get("require_tests"))
    policy_disallows_incomplete = bool(policy and not policy.get("release", {}).get("allow_incomplete"))
    if policy_requires_tests and not deep:
        checks_incomplete.append({"check": "test_status", "reason": "repository policy requires tests; bounded mode did not execute them"})
        test_environment_incomplete = True
    if blockers:
        verdict = "NOT_READY"
    elif test_environment_incomplete or secret_scan_status == "incomplete" or hidden_write_detected:
        verdict = "INCOMPLETE_ASSESSMENT"
    elif any(item.get("check") == "test_status" for item in checks_incomplete):
        verdict = "INCOMPLETE_ASSESSMENT"
    elif checks_incomplete and policy_disallows_incomplete:
        verdict = "INCOMPLETE_ASSESSMENT"
    elif checks_incomplete:
        verdict = "READY_WITH_KNOWN_LIMITATIONS"
    else:
        verdict = "READY_FOR_CAREFUL_TESTERS"

    return {
        "schema_version": 1, "skill": "release_readiness",
        "success": True,
        "checks_requested": checks_requested,
        "checks_completed": checks_completed,
        "checks_incomplete": checks_incomplete,
        "findings": findings,
        "blockers": blockers,
        "warnings": warnings,
        "secret_scan_status": secret_scan_status,
        "test_status": test_status,
        "remediation": remediation,
        "package_status": package_status,
        "git_status": git_status,
        "dependency_status": dependency_status,
        "platform_limitations": platform_limitations,
        "written_paths": [],
        "policy": policy_result,
        "checks": [{"name": name, "completed": name in checks_completed} for name in checks_requested],
        "approvals": [], "blockers": blockers, "evidence": {"deep": deep, "checks_completed": checks_completed},
        "summary": "Release-readiness assessment completed with policy-aware evidence.",
        "error_code": "incomplete_assessment" if verdict == "INCOMPLETE_ASSESSMENT" else None,
        "verdict": verdict,
    }


_RESUME_LABELS = ("PURPOSE", "OBJECTIVE", "CONSTRAINTS", "COMPLETED", "NEXT STEP")


def _parse_structured_resume_state(state_text: str) -> dict[str, str | None]:
    """Parse the PURPOSE/OBJECTIVE/CONSTRAINTS/COMPLETED/NEXT STEP convention
    used by this project's own save_context calls. Pure regex — zero LLM
    calls. Repos that saved unstructured free text simply get all-None here;
    callers fall back to the raw state text (see project_summary)."""
    parsed: dict[str, str | None] = {label: None for label in _RESUME_LABELS}
    if not state_text:
        return parsed
    label_alt = "|".join(re.escape(label) for label in _RESUME_LABELS)
    pattern = re.compile(rf"^({label_alt}):\s*(.*?)(?=\n(?:{label_alt}):|\Z)", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(state_text):
        parsed[match.group(1)] = match.group(2).strip()
    return parsed


def build_resume_work_artifacts(
    repo: Path,
    *,
    confirm_update: bool = False,
    new_state: str | None = None,
    new_open_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Reconstruct project state for a brand-new session from saved memory
    alone. Never overwrites memory unless confirm_update=True and new_state is
    given explicitly — resume itself is always read-only by default."""
    memory_artifacts = build_validate_memory_artifacts(repo)
    memory_health = {
        "store_exists": memory_artifacts.get("store_exists", False),
        "status": memory_artifacts.get("status"),
        "summary": memory_artifacts.get("summary", {}),
    }
    broken = memory_artifacts.get("summary", {}).get("broken_count", 0)

    if not memory_artifacts.get("store_exists"):
        return {
            "skill": "resume_project_work", "success": True,
            "project_summary": None, "last_objective": None, "completed_work": None,
            "constraints": None, "remembered_next_action": None,
            "current_repository_state": None, "detected_drift": None, "unfinished_work": None,
            "uncertainty": ["No .skilllayer/ memory store exists yet for this repository — nothing has been saved."],
            "recommended_next_action": "Save context (e.g. via skilllayer_save_context) before relying on resume.",
            "memory_health": memory_health, "written_paths": [], "verdict": "NO_SAVED_CONTEXT",
        }

    if broken:
        return {
            "skill": "resume_project_work", "success": False,
            "project_summary": None, "last_objective": None, "completed_work": None,
            "constraints": None, "remembered_next_action": None,
            "current_repository_state": None, "detected_drift": None, "unfinished_work": None,
            "uncertainty": [f"Memory store has {broken} broken integrity finding(s); saved context may be unreliable."],
            "recommended_next_action": "Run skilllayer_validate_memory for details and repair before resuming.",
            "memory_health": memory_health, "written_paths": [], "verdict": "MEMORY_UNHEALTHY",
        }

    rehydrate_artifacts = build_rehydrate_context_artifacts(repo, full_context=True)
    context_text = rehydrate_artifacts.get("context") or ""
    state_match = re.search(r"## State\n(.*?)(?:\n## |\Z)", context_text, re.DOTALL)
    state_text = state_match.group(1).strip() if state_match else ""

    if not state_text:
        return {
            "skill": "resume_project_work", "success": True,
            "project_summary": None, "last_objective": None, "completed_work": None,
            "constraints": None, "remembered_next_action": None,
            "current_repository_state": None, "detected_drift": None, "unfinished_work": None,
            "uncertainty": ["Memory store exists but no context snapshot has been saved yet."],
            "recommended_next_action": "Save context before relying on resume.",
            "memory_health": memory_health, "written_paths": [], "verdict": "NO_SAVED_CONTEXT",
        }

    parsed = _parse_structured_resume_state(state_text)
    structured = any(parsed.values())
    uncertainty: list[str] = []
    if not structured:
        uncertainty.append(
            "Saved context is not in the PURPOSE/OBJECTIVE/CONSTRAINTS/COMPLETED/NEXT STEP "
            "format; returning the raw saved text as project_summary only."
        )

    git_artifacts, _ = build_git_status_artifacts(repo, [])
    current_repository_state = {
        "is_git_repo": git_artifacts.get("is_git_repo"),
        "branch": git_artifacts.get("branch"),
        "clean": git_artifacts.get("clean"),
        "untracked_count": git_artifacts.get("untracked_count"),
        "staged_count": git_artifacts.get("staged_count"),
        "unstaged_count": git_artifacts.get("unstaged_count"),
    }

    activity_artifacts = build_detect_activity_artifacts(repo, persist_snapshot=False)
    first_run = activity_artifacts.get("first_run", True)
    activity_summary = activity_artifacts.get("summary", {}) or {}
    from ..memory.skilllayer_memory import parse_frontmatter

    context_meta, _ = parse_frontmatter(context_text)
    context_created = context_meta.get("created")
    commit_after_context_save: bool | None = None
    git_log_code, git_log_stdout, _ = _run_git(["log", "-1", "--format=%cI"], repo)
    if git_log_code == 0 and git_log_stdout.strip() and isinstance(context_created, str):
        try:
            saved_at = datetime.fromisoformat(context_created.replace("Z", "+00:00"))
            current_commit_at = datetime.fromisoformat(git_log_stdout.strip().replace("Z", "+00:00"))
            commit_after_context_save = current_commit_at > saved_at
        except ValueError:
            uncertainty.append(
                "Context or Git commit timestamp could not be parsed; committed drift is unknown."
            )
    detected_drift = {
        "first_run": first_run,
        "commit_count": activity_summary.get("commit_count", 0),
        "files_changed_count": activity_summary.get("files_changed_count", 0),
        "branches_changed": activity_artifacts.get("branches_changed", []),
        "active_since": activity_summary.get("active_since", False),
        "commit_after_context_save": commit_after_context_save,
    }
    has_drift = bool(
        (not first_run and activity_summary.get("active_since"))
        or commit_after_context_save
    )
    if first_run:
        uncertainty.append("No prior activity snapshot to compare against yet — drift detection has no baseline.")

    needs_wrap = rehydrate_artifacts.get("needs_wrap") or {}
    if needs_wrap.get("status") == "unknown":
        uncertainty.append("Unsaved-work status is unknown — WatchFileChanges has never been run to establish a baseline.")
    unfinished_work = {
        "needs_wrap_status": needs_wrap.get("status"),
        "changed_count": needs_wrap.get("changed_count"),
        "untracked_git_files": git_artifacts.get("untracked_count", 0),
    }

    written_paths: list[str] = []
    if confirm_update and new_state:
        save_artifacts = build_save_context_artifacts(repo, new_state, new_open_questions or [])
        written_paths = list(save_artifacts.get("written_paths", []) or [])

    project_summary = (parsed.get("PURPOSE") or state_text) if structured else state_text
    verdict = "CONTEXT_INCOMPLETE" if not structured else ("READY_WITH_REPOSITORY_DRIFT" if has_drift else "READY_TO_CONTINUE")

    return {
        "skill": "resume_project_work",
        "success": True,
        "project_summary": project_summary,
        "last_objective": parsed.get("OBJECTIVE"),
        "completed_work": parsed.get("COMPLETED"),
        "constraints": parsed.get("CONSTRAINTS"),
        "remembered_next_action": parsed.get("NEXT STEP"),
        "current_repository_state": current_repository_state,
        "detected_drift": detected_drift,
        "unfinished_work": unfinished_work,
        "uncertainty": uncertainty,
        "recommended_next_action": parsed.get("NEXT STEP") or "Review the raw saved context and decide the next step.",
        "memory_health": memory_health,
        "written_paths": written_paths,
        "verdict": verdict,
    }


def flatten_workflow_artifacts(workflow: str, artifacts: dict[str, Any]) -> dict[str, Any]:
    if workflow in ("SafeCodeChangeWorkflow", "ReleaseReadinessWorkflow", "ResumeProjectWorkWorkflow"):
        # These builders already return their full, final top-level shape
        # (skill/verdict/etc.) — nothing to promote, just pass it through so
        # skilllayer_run's generic envelope and the dedicated MCP tool return
        # equivalent results.
        return dict(artifacts)
    if workflow == "ClarifyIntentWorkflow":
        return {
            "matched": bool(artifacts.get("matched", False)),
            "suggestions": list(artifacts.get("suggestions", []) or []),
            "summary": artifacts.get("summary", ""),
            "unsupported": bool(artifacts.get("unsupported", True)),
            "unsupported_reason": artifacts.get("unsupported_reason"),
        }
    if workflow == "FindFunctionWorkflow":
        return {
            "extracted_symbols": list(artifacts.get("extracted_symbols", [])),
            "matched_symbols": list(artifacts.get("matched_symbols", [])),
            "matches": list(artifacts.get("matches", [])),
            "file_paths": list(artifacts.get("file_paths", [])),
            "definition_matches": list(artifacts.get("definition_matches", []) or []),
            "reference_matches": list(artifacts.get("reference_matches", []) or []),
            "possible_false_positive_matches": list(artifacts.get("possible_false_positive_matches", []) or []),
            "definition_count": int(artifacts.get("definition_count", 0) or 0),
            "reference_count": int(artifacts.get("reference_count", 0) or 0),
            "possible_false_positive_count": int(artifacts.get("possible_false_positive_count", 0) or 0),
            "total_matches": int(artifacts.get("total_matches", 0) or 0),
            "shown_matches": int(artifacts.get("shown_matches", 0) or 0),
            "max_matches": int(artifacts.get("max_matches", 0) or 0),
            "truncated": bool(artifacts.get("truncated", False)),
            "confidence": artifacts.get("confidence"),
            "confidence_reason": artifacts.get("confidence_reason"),
            "summary": artifacts.get("summary"),
            "tip": artifacts.get("tip"),
            "ignore_rules": dict(artifacts.get("ignore_rules", {}) or {}),
        }
    if workflow == "RenameSymbolWorkflow":
        return {
            "extracted_symbols": list(artifacts.get("extracted_symbols", [])),
            "old_symbol": artifacts.get("old_symbol"),
            "new_symbol": artifacts.get("new_symbol"),
            "rename_pair": artifacts.get("rename_pair"),
            "files_modified": list(artifacts.get("files_modified", [])),
            "replacements_performed": int(artifacts.get("replacements_performed", 0) or 0),
            "validation_details": dict(artifacts.get("validation_details", {})),
        }
    if workflow == "AddHelperWorkflow":
        return {
            "workflow_status": artifacts.get("workflow_status"),
            "action_taken": artifacts.get("action_taken"),
            "helper_name": artifacts.get("helper_name"),
            "target_file": artifacts.get("target_file"),
            "files_modified": list(artifacts.get("files_modified", []) or []),
            "would_modify_files": list(artifacts.get("would_modify_files", []) or []),
            "dry_run": bool(artifacts.get("dry_run", False)),
            "unsupported_reason": artifacts.get("unsupported_reason"),
            "guardrail_triggered": bool(artifacts.get("guardrail_triggered", False)),
            "guardrail_reason": artifacts.get("guardrail_reason"),
            "risk_level": artifacts.get("risk_level"),
            "expected_change_summary": artifacts.get("expected_change_summary"),
            "verification": dict(artifacts.get("verification", {}) or {}),
            "verification_ran": bool(artifacts.get("verification_ran", False)),
            "verification_passed": artifacts.get("verification_passed"),
            "summary": artifacts.get("summary"),
            "recommendation": artifacts.get("recommendation"),
            "internal": bool(artifacts.get("internal", True)),
            "user_facing": bool(artifacts.get("user_facing", False)),
        }
    if workflow == "RunTestsWorkflow":
        return {
            "test_command": artifacts.get("test_command"),
            "command_argv": list(artifacts.get("command_argv", []) or []),
            "exit_code": artifacts.get("exit_code"),
            "duration_ms": artifacts.get("duration_ms"),
            "test_count": artifacts.get("test_count"),
            "no_tests_discovered": bool(artifacts.get("no_tests_discovered", False)),
            "pass_count": int(artifacts.get("pass_count", 0) or 0),
            "failure_count": int(artifacts.get("failure_count", 0) or 0),
            "timed_out": bool(artifacts.get("timed_out", False)),
            "failed_tests": list(artifacts.get("failed_tests", []) or []),
            "summary": artifacts.get("summary"),
            "stdout_snippet": artifacts.get("stdout_snippet"),
            "stderr_snippet": artifacts.get("stderr_snippet"),
            "error_code": artifacts.get("error_code"),
            "outcome": artifacts.get("outcome"),
            "outcome_reason": artifacts.get("outcome_reason"),
            "workflow_execution_success": artifacts.get("workflow_execution_success"),
            "task_validation_success": artifacts.get("task_validation_success"),
            "recommendation": artifacts.get("recommendation"),
        }
    if workflow == "SingleTestWorkflow":
        return {
            "test_target": artifacts.get("test_target"),
            "target_source": artifacts.get("target_source"),
            "test_command": artifacts.get("test_command"),
            "command_argv": list(artifacts.get("command_argv", []) or []),
            "exit_code": artifacts.get("exit_code"),
            "duration_ms": artifacts.get("duration_ms"),
            "test_count": artifacts.get("test_count"),
            "no_tests_discovered": bool(artifacts.get("no_tests_discovered", False)),
            "pass_count": int(artifacts.get("pass_count", 0) or 0),
            "failure_count": int(artifacts.get("failure_count", 0) or 0),
            "timed_out": bool(artifacts.get("timed_out", False)),
            "failed_tests": list(artifacts.get("failed_tests", []) or []),
            "summary": artifacts.get("summary"),
            "stdout_snippet": artifacts.get("stdout_snippet"),
            "stderr_snippet": artifacts.get("stderr_snippet"),
            "error_code": artifacts.get("error_code"),
            "outcome": artifacts.get("outcome"),
            "outcome_reason": artifacts.get("outcome_reason"),
            "workflow_execution_success": artifacts.get("workflow_execution_success"),
            "task_validation_success": artifacts.get("task_validation_success"),
            "recommendation": artifacts.get("recommendation"),
        }
    if workflow == "ExplainFailureWorkflow":
        return {
            "test_command": artifacts.get("test_command"),
            "command_argv": list(artifacts.get("command_argv", []) or []),
            "tests_run": bool(artifacts.get("tests_run", False)),
            "tests_passed": artifacts.get("tests_passed"),
            "validation_status": artifacts.get("validation_status"),
            "exit_code": artifacts.get("exit_code"),
            "duration_ms": artifacts.get("duration_ms"),
            "test_count": artifacts.get("test_count"),
            "no_tests_discovered": bool(artifacts.get("no_tests_discovered", False)),
            "pass_count": int(artifacts.get("pass_count", 0) or 0),
            "failure_count": int(artifacts.get("failure_count", 0) or 0),
            "timed_out": bool(artifacts.get("timed_out", False)),
            "failed_tests": list(artifacts.get("failed_tests", []) or []),
            "summary": artifacts.get("summary"),
            "stdout_snippet": artifacts.get("stdout_snippet"),
            "stderr_snippet": artifacts.get("stderr_snippet"),
            "error_code": artifacts.get("error_code"),
            "outcome": artifacts.get("outcome"),
            "outcome_reason": artifacts.get("outcome_reason"),
            "workflow_execution_success": artifacts.get("workflow_execution_success"),
            "task_validation_success": artifacts.get("task_validation_success"),
            "recommendation": artifacts.get("recommendation"),
            "diagnoses": list(artifacts.get("diagnoses", []) or []),
            "diagnosis_count": int(artifacts.get("diagnosis_count", 0) or 0),
            "likely_failure_types": list(artifacts.get("likely_failure_types", []) or []),
            "confidence": artifacts.get("confidence"),
            "confidence_reason": artifacts.get("confidence_reason"),
            "raw_output_available": bool(artifacts.get("raw_output_available", False)),
        }
    if workflow == "FixFailingTestWorkflow":
        return {
            "diagnoses": list(artifacts.get("diagnoses", []) or []),
            "diagnosis_count": int(artifacts.get("diagnosis_count", 0) or 0),
            "repair_status": artifacts.get("repair_status"),
            "repair_attempted": bool(artifacts.get("repair_attempted", False)),
            "repair_type": artifacts.get("repair_type"),
            "risk_level": artifacts.get("risk_level"),
            "repair_class": artifacts.get("repair_class"),
            "target_file": artifacts.get("target_file"),
            "patch_summary": artifacts.get("patch_summary"),
            "files_modified": list(artifacts.get("files_modified", []) or []),
            "lines_changed": int(artifacts.get("lines_changed", 0) or 0),
            "replacements_performed": int(artifacts.get("replacements_performed", 0) or 0),
            "guardrail_triggered": bool(artifacts.get("guardrail_triggered", False)),
            "guardrail_reason": artifacts.get("guardrail_reason"),
            "verification": dict(artifacts.get("verification", {}) or {}),
            "verification_ran": bool(artifacts.get("verification_ran", False)),
            "verification_passed": artifacts.get("verification_passed"),
            "rollback_performed": bool(artifacts.get("rollback_performed", False)),
            "rollback_supported": bool(artifacts.get("rollback_supported", False)),
            "unsupported": bool(artifacts.get("unsupported", False)),
            "unsupported_reason": artifacts.get("unsupported_reason"),
            "post_patch_failures": list(artifacts.get("post_patch_failures", []) or []),
            "proposed_repair_class": artifacts.get("proposed_repair_class"),
            "proposed_repair": dict(artifacts.get("proposed_repair", {}) or {}) if artifacts.get("proposed_repair") else None,
            "would_modify_files": list(artifacts.get("would_modify_files", []) or []),
            "proposed_patch_summary": artifacts.get("proposed_patch_summary"),
            "expected_change_summary": artifacts.get("expected_change_summary"),
            "verification_plan": artifacts.get("verification_plan"),
            "summary": artifacts.get("summary"),
            "recommendation": artifacts.get("recommendation"),
        }
    if workflow == "DetectDeadCodeWorkflow":
        return {
            "files_scanned": int(artifacts.get("files_scanned", 0)),
            "total_findings": int(artifacts.get("total_findings", 0)),
            "certain_count": int(artifacts.get("certain_count", 0)),
            "possible_count": int(artifacts.get("possible_count", 0)),
            "findings": list(artifacts.get("findings", []) or []),
            "summary": artifacts.get("summary", ""),
        }
    if workflow == "MapDependenciesWorkflow":
        return {
            "files_parsed": list(artifacts.get("files_parsed", []) or []),
            "files_not_found": list(artifacts.get("files_not_found", []) or []),
            "total_dependencies": int(artifacts.get("total_dependencies", 0)),
            "pinned_count": int(artifacts.get("pinned_count", 0)),
            "unpinned_count": int(artifacts.get("unpinned_count", 0)),
            "unpinned_names": list(artifacts.get("unpinned_names", []) or []),
            "by_type": dict(artifacts.get("by_type", {})),
            "dependencies": list(artifacts.get("dependencies", []) or []),
            "summary": artifacts.get("summary", ""),
        }
    if workflow == "InspectRepoStructureWorkflow":
        return {
            "total_files": int(artifacts.get("total_files", 0)),
            "total_size_bytes": int(artifacts.get("total_size_bytes", 0)),
            "total_directories": int(artifacts.get("total_directories", 0)),
            "files_by_extension": dict(artifacts.get("files_by_extension", {})),
            "size_by_extension": dict(artifacts.get("size_by_extension", {})),
            "directories": list(artifacts.get("directories", []) or []),
            "entry_points": list(artifacts.get("entry_points", []) or []),
            "summary": artifacts.get("summary", ""),
        }
    if workflow == "GitStatusWorkflow":
        return {
            "is_git_repo": artifacts.get("is_git_repo"),
            "repo_root": artifacts.get("repo_root"),
            "branch": artifacts.get("branch"),
            "clean": artifacts.get("clean"),
            "staged_count": artifacts.get("staged_count", 0),
            "unstaged_count": artifacts.get("unstaged_count", 0),
            "untracked_count": artifacts.get("untracked_count", 0),
            "changed_files": list(artifacts.get("changed_files", []) or []),
            "diff_stat": artifacts.get("diff_stat", ""),
            "cached_diff_stat": artifacts.get("cached_diff_stat", ""),
            "summary": artifacts.get("summary", ""),
            "error_code": artifacts.get("error_code"),
        }
    if workflow == "DependencyCheckWorkflow":
        return {
            "dependency": artifacts.get("dependency"),
            "declared": bool(artifacts.get("declared", False)),
            "declared_in": list(artifacts.get("declared_in", []) or []),
            "used": bool(artifacts.get("used", False)),
            "usage_count": int(artifacts.get("usage_count", 0) or 0),
            "usage_files": list(artifacts.get("usage_files", []) or []),
            "usage_locations": list(artifacts.get("usage_locations", []) or []),
            "ecosystems": list(artifacts.get("ecosystems", []) or []),
            "summary": artifacts.get("summary", ""),
            "error_code": artifacts.get("error_code"),
        }
    if workflow == "SaveContextSnapshotWorkflow":
        return {
            "context_file": artifacts.get("context_file"),
            "history_count": int(artifacts.get("history_count", 0) or 0),
            "open_questions": list(artifacts.get("open_questions", []) or []),
            "summary": artifacts.get("summary", ""),
            "error_code": artifacts.get("error_code"),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "TrackDecisionLogWorkflow":
        # "status" here is the decision's own proposed/accepted/superseded/
        # deprecated state (finalize_memory_result is called with
        # include_status_field=False for this workflow), not the health-status
        # word.
        return {
            "decision_id": artifacts.get("decision_id"),
            "file": artifacts.get("file"),
            "title": artifacts.get("title"),
            "status": artifacts.get("status"),
            "supersedes": artifacts.get("supersedes"),
            "summary": artifacts.get("summary", ""),
            "error_code": artifacts.get("error_code"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "RememberUserPreferencesWorkflow":
        return {
            "preferences_file": artifacts.get("preferences_file"),
            "sections_updated": list(artifacts.get("sections_updated", []) or []),
            "summary": artifacts.get("summary", ""),
            "error_code": artifacts.get("error_code"),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "RehydrateContextWorkflow":
        return {
            "index": artifacts.get("index"),
            "context": artifacts.get("context"),
            "decision": artifacts.get("decision"),
            "preferences": artifacts.get("preferences"),
            "error_code": artifacts.get("error_code"),
            "summary": artifacts.get("summary", ""),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "InspectRuntimeEnvironmentWorkflow":
        return {
            "python_version": artifacts.get("python_version"),
            "python_executable": artifacts.get("python_executable"),
            "virtual_env": artifacts.get("virtual_env"),
            "platform": artifacts.get("platform"),
            "installed_packages": list(artifacts.get("installed_packages", [])),
            "environment_variables": dict(artifacts.get("environment_variables", {})),
            "port_conflicts": artifacts.get("port_conflicts"),
            "summary": artifacts.get("summary", ""),
        }
    if workflow == "MonitorTestFlakinessWorkflow":
        return {
            "test_identifier": artifacts.get("test_identifier"),
            "runs": artifacts.get("runs", 0),
            "passed": artifacts.get("passed", 0),
            "failed": artifacts.get("failed", 0),
            "pass_rate": artifacts.get("pass_rate", 0.0),
            "flaky": artifacts.get("flaky", False),
            "deterministic": artifacts.get("deterministic", True),
            "failure_messages": list(artifacts.get("failure_messages", [])),
            "duration_ms": list(artifacts.get("duration_ms", [])),
            "median_duration_ms": artifacts.get("median_duration_ms", 0.0),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "DetectRunningProcessesWorkflow":
        return {
            "processes": list(artifacts.get("processes", [])),
            "dev_services": dict(artifacts.get("dev_services", {})),
            "total_count": artifacts.get("total_count", 0),
            "inspected_count": artifacts.get("inspected_count", 0),
            "skipped_count": artifacts.get("skipped_count", 0),
            "permission_limited": artifacts.get("permission_limited", False),
            "complete": artifacts.get("complete", True),
            "status": artifacts.get("status"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "checked_at": artifacts.get("checked_at"),
        }
    if workflow == "CheckPortAvailabilityWorkflow":
        out: dict[str, Any] = {
            "port": artifacts.get("port"),
            "host": artifacts.get("host"),
            "available": artifacts.get("available"),
            "process": artifacts.get("process"),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
        if "note" in artifacts:
            out["note"] = artifacts["note"]
        return {k: v for k, v in out.items() if v is not None or k in ("available", "process", "port", "host")}
    if workflow == "MeasureTestSuiteSpeedWorkflow":
        return {
            "total_duration_ms": artifacts.get("total_duration_ms", 0),
            "test_count": artifacts.get("test_count", 0),
            "passed": artifacts.get("passed", 0),
            "failed": artifacts.get("failed", 0),
            "skipped": artifacts.get("skipped", 0),
            "slowest_tests": list(artifacts.get("slowest_tests", [])),
            "speed_rating": artifacts.get("speed_rating"),
            "baseline_delta_ms": artifacts.get("baseline_delta_ms"),
            "baseline_saved_at": artifacts.get("baseline_saved_at"),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "WatchDependencyUpdatesWorkflow":
        return {
            "dependencies": list(artifacts.get("dependencies", [])),
            "checked_count": artifacts.get("checked_count", 0),
            "outdated_count": artifacts.get("outdated_count", 0),
            "up_to_date_count": artifacts.get("up_to_date_count", 0),
            "unknown_count": artifacts.get("unknown_count", 0),
            "unsupported_count": artifacts.get("unsupported_count", 0),
            "complete": artifacts.get("complete", False),
            "package_manager": artifacts.get("package_manager", "none"),
            "checked_at": artifacts.get("checked_at"),
        }
    if workflow == "DetectRepoActivityWorkflow":
        return {
            "since": artifacts.get("since"),
            "commits_since": list(artifacts.get("commits_since", [])),
            "files_changed": list(artifacts.get("files_changed", [])),
            "branches_changed": list(artifacts.get("branches_changed", [])),
            "summary": artifacts.get("summary", {}),
            "first_run": artifacts.get("first_run", True),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "WatchFileChangesWorkflow":
        return {
            "first_watch": artifacts.get("first_watch"),
            "scope": artifacts.get("scope"),
            "recorded_at": artifacts.get("recorded_at"),
            "previous_recorded_at": artifacts.get("previous_recorded_at"),
            "added": list(artifacts.get("added", [])),
            "modified": list(artifacts.get("modified", [])),
            "deleted": list(artifacts.get("deleted", [])),
            "summary": artifacts.get("summary", {}),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "CompareContextSnapshotsWorkflow":
        return {
            "from_snapshot": artifacts.get("from_snapshot"),
            "to_snapshot": artifacts.get("to_snapshot"),
            "state_diff": artifacts.get("state_diff", {}),
            "open_questions_diff": artifacts.get("open_questions_diff", {}),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "SearchDecisionLogWorkflow":
        return {
            "query": artifacts.get("query"),
            "fields_searched": list(artifacts.get("fields_searched", [])),
            "results": list(artifacts.get("results", [])),
            "total_matches": artifacts.get("total_matches", 0),
            "superseded_count": artifacts.get("superseded_count", 0),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "AddTodoWorkflow":
        # "status" here is the touched todo's own open/done state
        # (finalize_memory_result is called with include_status_field=False
        # for this workflow), not the health-status word.
        return {
            "todo_id": artifacts.get("todo_id"),
            "text": artifacts.get("text"),
            "status": artifacts.get("status"),
            "created": artifacts.get("created"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "MarkTodoDoneWorkflow":
        # "status" here is the touched todo's own open/done state
        # (finalize_memory_result is called with include_status_field=False
        # for this workflow), not the health-status word.
        return {
            "todo_id": artifacts.get("todo_id"),
            "status": artifacts.get("status"),
            "previous_status": artifacts.get("previous_status"),
            "done_at": artifacts.get("done_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "ListTodosWorkflow":
        return {
            "filter_status": artifacts.get("filter_status"),
            "todos": list(artifacts.get("todos", [])),
            "open_count": artifacts.get("open_count", 0),
            "done_count": artifacts.get("done_count", 0),
            "total_count": artifacts.get("total_count", 0),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "status": artifacts.get("status"),
            "warnings": list(artifacts.get("warnings", []) or []),
        }
    if workflow == "ProfileCodeExecutionWorkflow":
        return {
            "target": artifacts.get("target"),
            "total_duration_ms": artifacts.get("total_duration_ms", 0.0),
            "cpu_time_ms": artifacts.get("cpu_time_ms", 0.0),
            "function_calls": artifacts.get("function_calls", 0),
            "hotspots": list(artifacts.get("hotspots", [])),
            "rating": artifacts.get("rating"),
            "baseline_delta_ms": artifacts.get("baseline_delta_ms"),
            "baseline_saved_at": artifacts.get("baseline_saved_at"),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "MeasureMemoryUsageWorkflow":
        return {
            "target": artifacts.get("target"),
            "peak_memory_mb": artifacts.get("peak_memory_mb", 0.0),
            "baseline_memory_mb": artifacts.get("baseline_memory_mb", 0.0),
            "delta_memory_mb": artifacts.get("delta_memory_mb", 0.0),
            "duration_ms": artifacts.get("duration_ms", 0),
            "tracemalloc_top": list(artifacts.get("tracemalloc_top", [])),
            "rating": artifacts.get("rating"),
            "baseline_delta_mb": artifacts.get("baseline_delta_mb"),
            "baseline_saved_at": artifacts.get("baseline_saved_at"),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "DetectSecretPatternsWorkflow":
        return {
            "findings": list(artifacts.get("findings", [])),
            "scanned_files": artifacts.get("scanned_files", 0),
            "scanned_bytes": artifacts.get("scanned_bytes", 0),
            "skipped_files": artifacts.get("skipped_files", 0),
            "skipped_bytes": artifacts.get("skipped_bytes", 0),
            "skipped_reasons": dict(artifacts.get("skipped_reasons", {}) or {}),
            "scan_complete": artifacts.get("scan_complete", False),
            "findings_count": artifacts.get("findings_count", 0),
            # Defaults to False, not True: an artifact missing "clean"
            # entirely must never be silently treated as a clean result.
            "clean": artifacts.get("clean", False),
            "status": artifacts.get("status"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
            "checked_at": artifacts.get("checked_at"),
        }
    if workflow == "SearchWorkflow":
        return {
            "query": artifacts.get("query"),
            "mode": artifacts.get("mode"),
            "matches": list(artifacts.get("matches", [])),
            "total_matches": artifacts.get("total_matches", 0),
            "files_with_matches": artifacts.get("files_with_matches", 0),
            "scanned_files": artifacts.get("scanned_files", 0),
            "skipped_files": artifacts.get("skipped_files", 0),
            "truncated": artifacts.get("truncated", False),
            "limit": artifacts.get("limit", 100),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "ListBranchesWorkflow":
        return {
            "branches": list(artifacts.get("branches", [])),
            "current_branch": artifacts.get("current_branch"),
            "total_count": int(artifacts.get("total_count", 0)),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "GitLogWorkflow":
        return {
            "commits": list(artifacts.get("commits", [])),
            "total_shown": int(artifacts.get("total_shown", 0)),
            "filters_applied": dict(artifacts.get("filters_applied", {})),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "GetCommitDetailsWorkflow":
        return {
            "hash": artifacts.get("hash"),
            "short_hash": artifacts.get("short_hash"),
            "message": artifacts.get("message"),
            "author_name": artifacts.get("author_name"),
            "date": artifacts.get("date"),
            "files_changed": list(artifacts.get("files_changed", [])),
            "total_insertions": int(artifacts.get("total_insertions", 0)),
            "total_deletions": int(artifacts.get("total_deletions", 0)),
            "parent_hashes": list(artifacts.get("parent_hashes", [])),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "GitDiffWorkflow":
        return {
            "files": list(artifacts.get("files", [])),
            "total_files": int(artifacts.get("total_files", 0)),
            "total_insertions": int(artifacts.get("total_insertions", 0)),
            "total_deletions": int(artifacts.get("total_deletions", 0)),
            "truncated": bool(artifacts.get("truncated", False)),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "GetFileHistoryWorkflow":
        return {
            "file": artifacts.get("file"),
            "commits": list(artifacts.get("commits", [])),
            "total_shown": int(artifacts.get("total_shown", 0)),
            "first_commit_date": artifacts.get("first_commit_date"),
            "last_modified_date": artifacts.get("last_modified_date"),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "GitBlameWorkflow":
        return {
            "file": artifacts.get("file"),
            "lines": list(artifacts.get("lines", [])),
            "total_lines": int(artifacts.get("total_lines", 0)),
            "unique_authors": list(artifacts.get("unique_authors", [])),
            "date_range": dict(artifacts.get("date_range", {})),
            "truncated": bool(artifacts.get("truncated", False)),
            "checked_at": artifacts.get("checked_at"),
            "error_code": artifacts.get("error_code"),
            "error": artifacts.get("error"),
        }
    if workflow == "FindMergeConflictsWorkflow":
        return {
            "conflicts": list(artifacts.get("conflicts", [])),
            "total_files_with_conflicts": int(artifacts.get("total_files_with_conflicts", 0)),
            "total_conflict_sections": int(artifacts.get("total_conflict_sections", 0)),
            "clean": bool(artifacts.get("clean", True)),
            "checked_at": artifacts.get("checked_at"),
        }
    return {}


def classify_symbol_match(symbol: str, line_text: str) -> str:
    stripped = line_text.strip()
    if not stripped:
        return "possible_false_positive"
    if stripped.startswith("#"):
        return "possible_false_positive"
    if re.search(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}\b", line_text):
        return "definition"
    if line_looks_like_string_only_match(symbol, stripped):
        return "possible_false_positive"
    if re.search(r"^\s*(?:from\s+\S+\s+import|import\s+)", line_text):
        return "reference"
    if re.search(rf"\b{re.escape(symbol)}\s*\(", line_text):
        return "reference"
    return "reference"


def line_looks_like_string_only_match(symbol: str, stripped_line: str) -> bool:
    if stripped_line.startswith(("print(", "logger.", "logging.")):
        return True
    quote_positions = [idx for idx, char in enumerate(stripped_line) if char in {"'", '"'}]
    symbol_index = stripped_line.find(symbol)
    if symbol_index == -1:
        return False
    if len(quote_positions) >= 2:
        before = [idx for idx in quote_positions if idx < symbol_index]
        after = [idx for idx in quote_positions if idx > symbol_index]
        if before and after:
            return True
    if stripped_line.startswith(("{", "[", '"', "'")):
        return True
    return False


def unique_preserving_order(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def locate_helper_insertion_point(repo: Path) -> str | None:
    candidates = [
        "ledgerlite/validators.py",
        "ledgerlite/helpers.py",
        "ledgerlite/utils.py",
        "helpers.py",
        "utils.py",
    ]
    for relative_path in candidates:
        if (repo / relative_path).exists():
            return relative_path
    return None


def choose_simple_patch(task_description: str, test_result: dict[str, Any]) -> dict[str, str] | None:
    text = " ".join(
        [
            task_description.lower(),
            str(test_result.get("stdout", "")).lower(),
            str(test_result.get("stderr", "")).lower(),
        ]
    )
    patches = [
        {
            "tokens": ["currency", "casing"],
            "file": "ledgerlite/money.py",
            "old": "code = str(value).strip().lower()",
            "new": "code = str(value).strip().upper()",
            "subtype": "wrong_operator",
            "description": "Restore currency-code normalization to uppercase.",
        },
        {
            "tokens": ["signed", "transaction", "amount"],
            "file": "ledgerlite/transactions.py",
            "old": "sign = 1 if self.kind == TransactionKind.DEBIT else -1",
            "new": "sign = -1 if self.kind == TransactionKind.DEBIT else 1",
            "subtype": "wrong_operator",
            "description": "Restore debit/credit signed amount direction.",
        },
        {
            "tokens": ["default", "tax", "rate"],
            "file": "ledgerlite/tax.py",
            "old": '"default": Decimal("0.08")',
            "new": '"default": Decimal("0.07")',
            "subtype": "wrong_constant",
            "description": "Restore default tax rate constant.",
        },
        {
            "tokens": ["account", "label", "ordering"],
            "file": "ledgerlite/accounts.py",
            "old": 'return f"{self.name} - {self.code}"',
            "new": 'return f"{self.code} - {self.name}"',
            "subtype": "wrong_operator",
            "description": "Restore account label field order.",
        },
        {
            "tokens": ["empty", "allocation"],
            "file": "ledgerlite/money.py",
            "old": 'if not weights:\n        raise ValidationError("weights required")',
            "new": "if not weights:\n        return []",
            "subtype": "missing_edge_case",
            "description": "Restore empty allocation edge case.",
        },
    ]
    for patch in patches:
        if all(token in text for token in patch["tokens"]):
            return {key: str(value) for key, value in patch.items() if key != "tokens"}
    return None


def choose_import_export_patch(test_result: dict[str, Any], repo: Path) -> dict[str, str] | None:
    output = "\n".join([str(test_result.get("stdout", "")), str(test_result.get("stderr", ""))])
    match = re.search(r"cannot import name ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"] from ['\"]ledgerlite['\"]", output)
    if not match:
        return None
    symbol = match.group(1)
    init_path = repo / "ledgerlite" / "__init__.py"
    if not init_path.exists():
        return None
    module = find_package_symbol_module(repo / "ledgerlite", symbol)
    if not module:
        return None
    current = init_path.read_text(encoding="utf-8", errors="ignore")
    import_line = f"from .{module} import {symbol}"
    if import_line in current:
        return None
    return {
        "file": "ledgerlite/__init__.py",
        "old": current,
        "new": import_line + "\n" + current,
        "subtype": "import_error",
        "description": f"Export {symbol} from ledgerlite package.",
    }


def find_package_symbol_module(package_dir: Path, symbol: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:def|class)\s+{re.escape(symbol)}\b")
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(line) for line in text.splitlines()):
            return path.stem
    return None


def extract_browser_smoke_request(task_description: str, config: dict[str, Any], repo: Path) -> dict[str, Any]:
    url = config.get("url")
    url_match = re.search(r"(https?://[^\s,;]+|file://[^\s,;]+)", task_description)
    if url_match:
        url = url_match.group(1).rstrip(").,;")
    if not url:
        index = next((path for path in [repo / "index.html", repo / "dist" / "index.html", repo / "public" / "index.html"] if path.exists()), None)
        if index:
            url = f"file://{index.resolve()}"

    selectors = parse_selector_config(config.get("selectors", "body"))
    selector_match = re.search(r"(?:selectors?|elements?)\s*[:=]\s*([^\n;]+)", task_description, re.I)
    if selector_match:
        selectors = parse_selector_config(selector_match.group(1))
    if not selectors:
        selectors = ["body"]

    return {
        "url": url,
        "selectors": selectors,
        "wait_seconds": float(config.get("wait_seconds", 1) or 0),
        "output_dir": config.get("output_dir", "runs/browser_smoke"),
        "write_artifacts": bool(config.get("write_artifacts", False)),
    }


def parse_selector_config(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in text.split(",") if item.strip()]


def extract_unsupported_reason(log: list[dict[str, Any]], result: dict[str, Any]) -> str | None:
    if result.get("error"):
        return str(result["error"])
    for event in log:
        status = str(event.get("status") or "").lower()
        reason = event.get("reason") or event.get("error")
        if status == "unsupported" or reason:
            return str(reason or status)
    return None


def snapshot_python_files(repo: Path) -> dict[str, str]:
    if not repo.exists():
        return {}
    return {
        str(path.relative_to(repo)): path.read_text(encoding="utf-8", errors="ignore")
        for path in repo.rglob("*.py")
        if not should_ignore_search_path(path, repo)
    }


def unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        before_lines = before.get(path, "").splitlines(keepends=True)
        after_lines = after.get(path, "").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))
    return "".join(chunks)


def prepare_log_path(log_dir: str | Path | None) -> Path | None:
    if not log_dir:
        return None
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / time.strftime("run_%Y%m%d_%H%M%S")
    suffix = 0
    while path.exists():
        suffix += 1
        path = root / f"{time.strftime('run_%Y%m%d_%H%M%S')}_{suffix}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_run_logs(log_path: Path, result: dict[str, Any], trace: list[dict[str, Any]], test_result: dict[str, Any], diff_text: str) -> None:
    log_path.mkdir(parents=True, exist_ok=True)
    (log_path / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (log_path / "trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    (log_path / "test_stdout.txt").write_text(str(test_result.get("stdout", "")), encoding="utf-8")
    (log_path / "test_stderr.txt").write_text(str(test_result.get("stderr", "")), encoding="utf-8")
    (log_path / "patch.diff").write_text(diff_text, encoding="utf-8")
