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
from .claude_code_prep_check import run_claude_code_prep_check
from .config import load_config
from .config.defaults import COMMAND_METADATA, MACROS, WORKFLOWS, WORKFLOW_METADATA
from .router import SkillRouter
from .security import (
    blocked_workflow_reason,
    internal_execution_allowed_via_env,
    workflow_execution_blocked,
)
from .feedback_registry import ALLOWED_PLATFORMS, ALLOWED_STATUSES, build_feedback_report
from .security_check import run_security_check
from .telemetry import record_cli_activity, record_tool_invocation, summarize_telemetry
from .tester_check import run_tester_check
from .tools import inspect_repo


COMMANDS = {
    "run",
    "inspect",
    "workflows",
    "skills",
    "doctor",
    "stats",
    "tester-check",
    "claude-code-prep-check",
    "security-check",
    "feedback-status",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        if "--repo" in argv and "--task" in argv:
            argv = ["run", *argv]
        else:
            argv = ["--help", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    started = time.perf_counter()
    command_name = str(getattr(args, "command", None) or argv[0] or "unknown")
    try:
        exit_code = int(args.handler(args))
    except Exception:
        record_cli_activity_best_effort(command_name, success=False, started=started)
        raise
    record_cli_activity_best_effort(command_name, success=exit_code == 0, started=started)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skilllayer", description="Production-oriented SkillLayer CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Route and execute a coding task.")
    run_parser.add_argument("--repo", required=True, help="Repository path.")
    run_parser.add_argument("--task", required=True, help="Task description.")
    run_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    run_parser.add_argument("--dry-run", action="store_true", help="Route and plan without modifying files.")
    run_parser.add_argument("--config", type=Path, default=None, help="Path to skilllayer.yaml or JSON config.")
    run_parser.add_argument("--log-dir", type=Path, default=None, help="Directory for run logs.")
    run_parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    run_parser.add_argument("--verbose", action="store_true", help="Print extra human-readable details.")
    run_parser.add_argument(
        "--allow-internal",
        action="store_true",
        help="Maintainer opt-in to run INTERNAL-stability workflows (also via SKILLLAYER_ALLOW_INTERNAL=1). Blocked by default.",
    )
    run_parser.set_defaults(handler=handle_run)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a repository.")
    inspect_parser.add_argument("--repo", required=True, help="Repository path.")
    inspect_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    inspect_parser.set_defaults(handler=handle_inspect)

    workflows_parser = subparsers.add_parser("workflows", help="List available workflows.")
    workflows_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    workflows_parser.set_defaults(handler=handle_workflows)

    skills_parser = subparsers.add_parser("skills", help="List available macros and primitive tools.")
    skills_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    skills_parser.set_defaults(handler=handle_skills)

    doctor_parser = subparsers.add_parser("doctor", help="Check local SkillLayer runtime readiness.")
    doctor_parser.add_argument("--repo", default=None, help="Optional repository path to check.")
    doctor_parser.add_argument("--config", type=Path, default=None, help="Optional config path.")
    doctor_parser.add_argument("--log-dir", type=Path, default=None, help="Optional log directory to check.")
    doctor_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    doctor_parser.set_defaults(handler=handle_doctor)

    stats_parser = subparsers.add_parser("stats", help="Show SkillLayer telemetry and adoption metrics.")
    stats_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    stats_parser.set_defaults(handler=handle_stats)

    tester_parser = subparsers.add_parser("tester-check", help="Run local validation checks.")
    tester_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    tester_parser.set_defaults(handler=handle_tester_check)

    claude_parser = subparsers.add_parser("claude-code-prep-check", help="Run Claude Code tester-prep validation.")
    claude_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    claude_parser.set_defaults(handler=handle_claude_code_prep_check)

    security_parser = subparsers.add_parser("security-check", help="Report SkillLayer local-first security posture and docs readiness.")
    security_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    security_parser.set_defaults(handler=handle_security_check)

    feedback_parser = subparsers.add_parser("feedback-status", help="Summarize local tester feedback registry status.")
    feedback_parser.add_argument("--status", default=None, help="Filter by OPEN, IN_PROGRESS, FIXED, WAITING_VALIDATION, VALIDATED, or REJECTED.")
    feedback_parser.add_argument("--source", default=None, help="Filter by tester/source id, for example tester_002.")
    feedback_parser.add_argument("--platform", default=None, help="Filter by platform/client such as cursor, windows, macos, linux, codex, or claude_code.")
    feedback_parser.add_argument("--needs-validation", action="store_true", help="Show fixed items that still need original-tester revalidation.")
    feedback_parser.add_argument("--open-only", action="store_true", help="Show OPEN items only.")
    feedback_parser.add_argument("--waiting-validation", action="store_true", help="Show items waiting for external tester confirmation.")
    feedback_parser.add_argument("--stale", action="store_true", help="Show stale open or stale waiting-validation items.")
    feedback_parser.add_argument("--recently-validated", action="store_true", help="Show recently validated items.")
    feedback_parser.add_argument("--stale-threshold-days", type=int, default=30, help="Days before open/waiting feedback is considered stale.")
    feedback_parser.add_argument("--id", default=None, help="Show full details for one feedback item, for example F-001.")
    feedback_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    feedback_parser.set_defaults(handler=handle_feedback_status)
    return parser


def handle_run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    config = load_config(args.config)
    dry_run = bool(args.dry_run or config.get("execution", {}).get("dry_run", False))
    log_dir = args.log_dir or config.get("logging", {}).get("log_dir", "runs/skilllayer_logs")

    # Enforce stability before doing any work. INTERNAL workflows are blocked
    # unless a maintainer opts in via --allow-internal or SKILLLAYER_ALLOW_INTERNAL.
    # Routing is read-only, so this cannot modify the repository.
    allow_internal = bool(getattr(args, "allow_internal", False)) or internal_execution_allowed_via_env()
    try:
        routed_workflow = SkillRouter().route(args.task).workflow
    except Exception:
        routed_workflow = None
    if workflow_execution_blocked(routed_workflow, allow_internal=allow_internal):
        reason = blocked_workflow_reason(routed_workflow)
        error_result = {
            "success": False,
            "repo_path": str(Path(args.repo)),
            "task": args.task,
            "workflow": routed_workflow,
            "macro_sequence": [],
            "tool_calls": 0,
            "llm_calls": 0,
            "tests_run": False,
            "tests_passed": None,
            "validation_status": "not_applicable",
            "dry_run": dry_run,
            "logs_path": None,
            "unsupported": True,
            "unsupported_reason": "workflow_stability_blocked",
            "error": reason,
        }
        if args.json:
            print(json.dumps(error_result, indent=2))
        else:
            print(f"Blocked: {reason}", file=sys.stderr)
            print("Re-run with --allow-internal (or SKILLLAYER_ALLOW_INTERNAL=1) only if you understand the risk.", file=sys.stderr)
        return 1

    raw_result = SkillLayer().run(
        args.repo, args.task, dry_run=dry_run, log_dir=log_dir, config=config, allow_internal=allow_internal
    )
    result = {
        "success": bool(raw_result["success"]),
        "repo_path": str(Path(args.repo)),
        "task": args.task,
        "workflow": raw_result["workflow"],
        "macro_sequence": raw_result["macro_sequence"],
        "tool_calls": raw_result["tool_calls"],
        "llm_calls": raw_result["llm_calls"],
        "tests_run": bool(raw_result.get("tests_run", False)),
        "tests_passed": raw_result["tests_passed"],
        "validation_status": raw_result.get("validation_status", "not_applicable"),
        "dry_run": dry_run,
        "logs_path": raw_result.get("logs_path"),
    }
    result.update(extract_optional_run_fields(raw_result))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human_run(result, verbose=args.verbose)
    record_tool_invocation_best_effort(
        tool_name="skilllayer_run",
        repo_path=str(Path(args.repo)),
        task=args.task,
        workflow=result.get("workflow"),
        success=bool(result["success"]),
        duration_ms=(time.perf_counter() - started) * 1000.0,
        tool_calls=int(result.get("tool_calls", 0) or 0),
        macro_sequence=list(result.get("macro_sequence", []) or []),
    )
    return 0 if result["success"] else 1


def handle_inspect(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    result = with_not_applicable_validation(inspect_repo(args.repo))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer repository inspection")
        print(f"  repo_path: {result['repo_path']}")
        print(f"  file_count: {result['file_count']}")
        print(f"  python_file_count: {result['python_file_count']}")
        print(f"  loc_estimate: {result['loc_estimate']}")
        print(f"  test_files: {len(result['test_files'])}")
        print(f"  detected_test_command: {result['detected_test_command']}")
        print(f"  validation: {format_validation_status(str(result['validation_status']))}")
    record_tool_invocation_best_effort(
        tool_name="skilllayer_inspect_repo",
        repo_path=str(Path(args.repo)),
        task=None,
        workflow=None,
        success=True,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        tool_calls=0,
        macro_sequence=[],
    )
    return 0


def handle_workflows(args: argparse.Namespace) -> int:
    result = {
        "workflows": [
            {
                "name": name,
                "stability": WORKFLOW_METADATA.get(name, {}).get("stability", "experimental"),
                "summary": WORKFLOW_METADATA.get(name, {}).get("summary", ""),
                "macro_sequence": list(macros),
            }
            for name, macros in WORKFLOWS.items()
        ],
        "commands": [{"name": name, **metadata} for name, metadata in COMMAND_METADATA.items()],
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for workflow in result["workflows"]:
            print(f"{workflow['name']} [{workflow['stability']}]: {' -> '.join(workflow['macro_sequence'])}")
        print("Commands")
        for command in result["commands"]:
            print(f"  {command['name']} [{command['stability']}]: {command['summary']}")
    return 0


def handle_skills(args: argparse.Namespace) -> int:
    primitive_tools = sorted({tool for tools in MACROS.values() for tool in tools})
    result = {
        "macros": [{"name": name, "tools": list(tools)} for name, tools in MACROS.items()],
        "primitive_tools": primitive_tools,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Macros")
        for macro in result["macros"]:
            print(f"  {macro['name']}: {', '.join(macro['tools'])}")
        print("Primitive tools")
        for tool in primitive_tools:
            print(f"  {tool}")
    return 0


def handle_doctor(args: argparse.Namespace) -> int:
    pytest_available = is_pytest_available()
    mcp_available = module_available("mcp")
    required_checks: dict[str, Any] = {
        "python_version": {
            "value": ".".join(str(part) for part in sys.version_info[:3]),
            "ok": sys.version_info >= (3, 10),
            "required": True,
        },
        "repo_path": {
            "value": str(args.repo) if args.repo else None,
            "ok": Path(args.repo).exists() if args.repo else True,
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
            "value": mcp_available,
            "ok": mcp_available,
            "required": False,
            "warning": None if mcp_available else "MCP SDK is optional for CLI use. Install with python -m pip install -e \".[mcp]\" --no-build-isolation for MCP clients.",
        },
    }
    config_ok = True
    config_error = None
    try:
        config = load_config(args.config)
    except Exception as exc:
        config = {}
        config_ok = False
        config_error = str(exc)
    required_checks["config_valid"] = {"value": str(args.config) if args.config else "default", "ok": config_ok, "error": config_error, "required": True}
    backend = config.get("router", {}).get("backend")
    if backend == "ollama":
        required_checks["ollama_available"] = {
            "value": shutil.which("ollama") is not None,
            "ok": shutil.which("ollama") is not None,
            "required": True,
        }
    log_dir = args.log_dir or config.get("logging", {}).get("log_dir", "runs/skilllayer_logs")
    required_checks["log_dir_writable"] = {"value": str(log_dir), "ok": is_writable_dir(Path(log_dir)), "required": True}
    checks = {**required_checks, **optional_checks}
    warnings = [check["warning"] for check in optional_checks.values() if check.get("warning")]
    result = with_not_applicable_validation(
        {
            "success": all(item["ok"] for item in required_checks.values()),
            "ok": all(item["ok"] for item in required_checks.values()),
            "checks": checks,
            "required_checks": required_checks,
            "optional_checks": optional_checks,
            "warnings": warnings,
        }
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer doctor")
        print("  required checks:")
        for name, check in required_checks.items():
            status = "ok" if check["ok"] else "fail"
            print(f"  {name}: {status} ({check.get('value')})")
        print("  optional checks:")
        for name, check in optional_checks.items():
            status = "ok" if check["ok"] else "warning"
            print(f"  {name}: {status} ({check.get('value')})")
        if warnings:
            print("  warnings:")
            for warning in warnings:
                print(f"    {warning}")
        print(f"  validation: {format_validation_status(str(result['validation_status']))}")
    return 0 if result["ok"] else 1


def handle_stats(args: argparse.Namespace) -> int:
    result = summarize_telemetry()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer telemetry stats")
        print(f"  total_runs: {result['total_runs']}")
        print(f"  successful_runs: {result['successful_runs']}")
        print(f"  tool_invocations_total: {result['tool_invocations_total']}")
        print(f"  estimated_llm_calls_saved: {result['estimated_llm_calls_saved']}")
        print(f"  estimated_tool_steps_saved: {result['estimated_tool_steps_saved']}")
        print(f"  estimated_savings_only: {result['estimated_savings_only']}")
        print("  workflow_breakdown:")
        for workflow, count in result["workflow_breakdown"].items():
            print(f"    {workflow}: {count}")
        print("  tool_usage_breakdown:")
        for tool, count in result["tool_usage_breakdown"].items():
            print(f"    {tool}: {count}")
    return 0


def handle_tester_check(args: argparse.Namespace) -> int:
    result = run_tester_check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print(f"SkillLayer tester-check v{result.get('version', 'unknown')}")
        print(f"  ok: {summary['ok']}")
        print(f"  passed_count: {summary['passed_count']}")
        print(f"  failed_count: {summary['failed_count']}")
        print(f"  package_import_ok: {summary['package_import_ok']}")
        print(f"  tester_files_exist: {summary['tester_files_exist']}")
        print(f"  cli_available: {summary['cli_available']}")
        print(f"  doctor_command_works: {summary['doctor_command_works']}")
        print(f"  doctor_warning_count: {summary.get('doctor_warning_count', 0)}")
        for warning in summary.get("doctor_warnings", [])[:5]:
            print(f"    warning: {warning}")
        print(f"  workflows_command_works: {summary['workflows_command_works']}")
        print("  no_network_requests: true")
        print("  automatic_upload: false")
        print("  automatic_telemetry: false")
        print("  report: runs/tester_check/report.json")
    return 0 if result["summary"]["ok"] else 1


def handle_claude_code_prep_check(args: argparse.Namespace) -> int:
    result = run_claude_code_prep_check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print("SkillLayer Claude Code tester prep")
        print(f"  success: {result['success']}")
        print(f"  claude_code_status: {result['claude_code_status']}")
        print(f"  claude_code_validated: {result['claude_code_validated']}")
        print(f"  setup_doc_exists: {summary['setup_doc_exists']}")
        print(f"  config_generator_has_claude_section: {summary['config_generator_has_claude_section']}")
        print(f"  config_has_mcp_server_block: {summary['config_has_mcp_server_block']}")
        print(f"  mcp_tool_listing_works: {summary['mcp_tool_listing_works']}")
        print(f"  registered_tool_count: {summary['registered_tool_count']}")
        print(f"  tester_check_passes: {summary['tester_check_passes']}")
        print("  limitations:")
        for item in result["limitations"]:
            print(f"    {item}")
        print("  report: runs/claude_code_tester_prep/report.json")
    return 0 if result["success"] else 1


def handle_security_check(args: argparse.Namespace) -> int:
    result = run_security_check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        check = result["security_check"]
        print("SkillLayer security check")
        print(f"  success: {result['success']}")
        print(f"  security_review_exists: {summary['security_review_exists']}")
        print(f"  security_checklist_exists: {summary['security_checklist_exists']}")
        print(f"  safe_mode_exists: {summary['safe_mode_exists']}")
        print(f"  network_upload_enabled: {check['network_upload_enabled']}")
        print(f"  automatic_telemetry_upload: {check['automatic_telemetry_upload']}")
        print(f"  mcp_server_auto_start: {check['mcp_server_auto_start']}")
        print(f"  local_telemetry_dir: {check['local_telemetry_dir']}")
        print("  read_only_workflows:")
        for workflow in check["read_only_workflows"]:
            print(f"    {workflow}")
        print("  modifying_workflows:")
        for workflow in check["modifying_workflows"]:
            print(f"    {workflow}")
        print("  formal_third_party_security_audit: false")
        print("  report: runs/security_review_kit/report.json")
    return 0 if result["success"] else 1


def handle_feedback_status(args: argparse.Namespace) -> int:
    result = build_feedback_report(
        status=args.status,
        source=args.source,
        platform=args.platform,
        needs_validation=bool(args.needs_validation),
        open_only=bool(args.open_only),
        waiting_validation=bool(args.waiting_validation),
        stale=bool(args.stale),
        recently_validated=bool(args.recently_validated),
        item_id=args.id,
        stale_threshold_days=int(args.stale_threshold_days),
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not result["success"]:
            print("Feedback Status Error")
            for error in result["errors"]:
                print(f"  - {error}")
            print(f"Allowed statuses: {', '.join(ALLOWED_STATUSES)}")
            print(f"Allowed platforms: {', '.join(ALLOWED_PLATFORMS)}")
            return 1
        print_feedback_human(result)
    return 0 if result["success"] else 1


def print_feedback_human(result: dict[str, Any]) -> None:
    summary = result["summary"]
    filters = result["filters"]
    print("Feedback Summary")
    print(f"Open: {summary['open']}")
    print(f"In Progress: {summary['in_progress']}")
    print(f"Fixed: {summary['fixed']}")
    print(f"Waiting Validation: {summary['waiting_validation']}")
    print(f"Validated: {summary['validated']}")
    print(f"Rejected: {summary['rejected']}")
    print(f"Needs Validation: {summary['needs_validation']}")
    print(f"Stale Open: {summary['stale_open']}")
    print(f"Stale Waiting Validation: {summary['stale_validation_wait']}")
    print(f"Matching Items: {summary['matching_items']}")
    print(f"Total: {summary['total_items']}")
    metrics = summary.get("metrics") or {}
    print(f"Validation Rate: {metrics.get('validation_rate')}")
    print(f"Average Days To Fix: {metrics.get('average_days_to_fix')}")
    print(f"Average Days To Validation: {metrics.get('average_days_to_validation')}")
    active_filters = {
        key: value
        for key, value in filters.items()
        if value not in (None, False) and (key != "stale_threshold_days" or filters.get("stale"))
    }
    if active_filters:
        print("")
        print("Filters:")
        for key, value in active_filters.items():
            print(f"  {key}: {value}")

    if filters.get("id"):
        print("")
        print("Feedback Item Detail")
        for item in result["items"]:
            print_feedback_item_detail(item)
    else:
        print("")
        print("Open items:")
        if result["open_items"]:
            for item in result["open_items"][:5]:
                print_feedback_item_line(item)
        else:
            print("  none")
        print("")
        print("Waiting validation:")
        waiting_items = result.get("waiting_validation_items") or result.get("needs_validation_items") or []
        if waiting_items:
            for item in waiting_items[:8]:
                print_feedback_item_line(item)
                print("    Waiting for external tester confirmation.")
        else:
            print("  none")
        print("")
        print("Oldest open:")
        if result.get("oldest_open"):
            print_feedback_item_line(result["oldest_open"])
        else:
            print("  none")
        print("")
        print("Oldest waiting validation:")
        if result.get("oldest_waiting_validation"):
            print_feedback_item_line(result["oldest_waiting_validation"])
        else:
            print("  none")
        print("")
        print("Recently validated:")
        recently_validated = result.get("recently_validated_items") or []
        if recently_validated:
            for item in recently_validated[:5]:
                print_feedback_item_line(item)
        else:
            print("  none")
        stale_items = list(result.get("stale_open_items") or []) + list(result.get("stale_validation_wait_items") or [])
        print("")
        print("Stale items:")
        if stale_items:
            for item in stale_items[:8]:
                print_feedback_item_line(item)
        else:
            print("  none")
        print("")
        print("Most recent items:")
        for item in result["recent_items"]:
            print_feedback_item_line(item)
        print("Source of truth: FEEDBACK_STATUS.md")
        print("Machine registry: feedback/feedback_registry.json")


def print_feedback_item_line(item: dict[str, Any]) -> None:
    fixed_in = f" fixed_in={item.get('fixed_in')}" if item.get("fixed_in") else ""
    marker = " needs_validation=true" if item.get("needs_validation") else ""
    age = f" age_days={item.get('age_days')}" if item.get("age_days") is not None else ""
    waiting = (
        f" days_waiting_validation={item.get('days_waiting_validation')}"
        if item.get("days_waiting_validation") is not None
        else ""
    )
    print(f"  {item['id']} [{item['status']}] {item['source']} ({item['platform']}){fixed_in}{marker}")
    if age or waiting:
        print(f"    {age.strip()} {waiting.strip()}".rstrip())
    print(f"    {item['description']}")


def print_feedback_item_detail(item: dict[str, Any]) -> None:
    print(f"  id: {item.get('id')}")
    print(f"  source: {item.get('source')}")
    if item.get("source_label"):
        print(f"  source_label: {item.get('source_label')}")
    print(f"  platform: {item.get('platform')}")
    print(f"  date: {item.get('date')}")
    print(f"  created_date: {item.get('created_date')}")
    print(f"  fixed_date: {item.get('fixed_date')}")
    print(f"  validated_date: {item.get('validated_date')}")
    print(f"  status: {item.get('status')}")
    print(f"  severity: {item.get('severity')}")
    print(f"  category: {item.get('category')}")
    print(f"  fixed_in: {item.get('fixed_in')}")
    print(f"  validation_confirmed: {item.get('validation_confirmed')}")
    print(f"  needs_validation: {item.get('needs_validation')}")
    print(f"  age_days: {item.get('age_days')}")
    print(f"  days_to_fix: {item.get('days_to_fix')}")
    print(f"  days_to_validation: {item.get('days_to_validation')}")
    print(f"  days_waiting_validation: {item.get('days_waiting_validation')}")
    print(f"  stale_open: {item.get('stale_open')}")
    print(f"  stale_validation_wait: {item.get('stale_validation_wait')}")
    print("  description:")
    print(f"    {item.get('description')}")
    if item.get("validation_notes"):
        print("  validation_notes:")
        print(f"    {item.get('validation_notes')}")
    related_files = list(item.get("related_files") or [])
    if related_files:
        print("  related_files:")
        for file_path in related_files:
            print(f"    {file_path}")


_ERROR_CODE_MESSAGES: dict[str, str] = {
    "no_memory_store": (
        "No .skilllayer/ store found. Run 'Save context snapshot' first "
        "to initialize memory for this repository."
    ),
    "no_test_command_detected": (
        "No test runner detected. Add test files or configure pytest in pyproject.toml."
    ),
    "test_run_timed_out": "Test run timed out.",
    "no_tests_discovered": "Tests ran but no tests were discovered.",
    "command_error": "Test command exited with an error.",
    "unsupported_test_runner": "Test runner is not supported.",
    "single_test_not_supported": "Running a single test is not supported for this repo.",
    "no_failed_test_available": "No failing test found to target.",
    "test_target_not_specified": "Test target not specified.",
    "test_file_not_found": "Test file not found.",
    "ambiguous_test_target": "Multiple test files match; specify the full path.",
    "test_target_not_found": "Test target not found in the test file.",
    "not_git_repository": "This directory is not a git repository.",
    "dependency_name_not_found": "Could not determine the dependency name from the task.",
    "no_dependency_files_found": "No dependency files found in this repository.",
}


def print_human_run(result: dict[str, Any], verbose: bool = False) -> None:
    print("SkillLayer run summary")
    print(f"  success: {result['success']}")
    print(f"  workflow: {result['workflow']}")
    print(f"  macro_sequence: {' -> '.join(result['macro_sequence'])}")
    print(f"  validation: {format_validation_status(str(result.get('validation_status', 'not_applicable')))}")
    print(f"  tests_run: {result.get('tests_run', False)}")
    print(f"  tests_passed: {result.get('tests_passed')}")
    print(f"  dry_run: {result['dry_run']}")
    print(f"  tool_calls: {result['tool_calls']}")
    print(f"  llm_calls: {result['llm_calls']}")
    print(f"  logs_path: {result['logs_path']}")
    if result.get("workflow") == "BrowserSmokeWorkflow":
        print(f"  console_errors: {result.get('console_errors')}")
        print(f"  network_errors: {result.get('network_errors')}")
        print(f"  elements_verified: {result.get('elements_verified')}")
        print(f"  screenshot_path: {result.get('screenshot_path')}")
    if result.get("workflow") == "FindFunctionWorkflow":
        matches = list(result.get("matches", []) or [])
        print(f"  confidence: {result.get('confidence')}")
        if result.get("confidence_reason"):
            print(f"  confidence_reason: {result.get('confidence_reason')}")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        total_matches = result.get("total_matches", len(matches))
        shown_matches = result.get("shown_matches", len(matches))
        print(f"  matches: {shown_matches} shown / {total_matches} total")
        for match in matches[:8]:
            location = f"{match.get('file')}:{match.get('line')}" if match.get("line") else str(match.get("file"))
            print(f"    {location} {match.get('symbol')} [{match.get('match_type')}]")
            if match.get("text"):
                print(f"      {match.get('text')}")
        if len(matches) > 8 or result.get("truncated"):
            hidden = max(int(total_matches or 0) - 8, 0)
            print(f"    ... {hidden} more")
            print("    Use --json for full structured output.")
            if result.get("logs_path"):
                print(f"    logs_path: {result.get('logs_path')}")
    if result.get("workflow") == "RenameSymbolWorkflow":
        files_modified = list(result.get("files_modified", []) or [])
        print(f"  files_modified: {len(files_modified)}")
        for file_path in files_modified[:8]:
            print(f"    {file_path}")
        if len(files_modified) > 8:
            print(f"    ... {len(files_modified) - 8} more")
        print(f"  replacements_performed: {result.get('replacements_performed', 0)}")
        validation = result.get("validation_details") or {}
        if isinstance(validation, dict) and validation:
            print(
                "  validation_details: "
                f"tests_run={validation.get('tests_run')} "
                f"tests_passed={validation.get('tests_passed')} "
                f"returncode={validation.get('returncode')}"
            )
    if result.get("workflow") == "RunTestsWorkflow":
        print(f"  test_command: {result.get('test_command')}")
        if result.get("outcome"):
            print(f"  outcome: {result.get('outcome')}")
        if result.get("outcome_reason"):
            print(f"  outcome_reason: {result.get('outcome_reason')}")
        print(f"  exit_code: {result.get('exit_code')}")
        print(f"  test_count: {result.get('test_count')}")
        if result.get("no_tests_discovered"):
            print("  warning: No tests were discovered. This is not considered a passed test run.")
        print(f"  failure_count: {result.get('failure_count', 0)}")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        if result.get("recommendation"):
            print(f"  recommendation: {result.get('recommendation')}")
        failed_tests = list(result.get("failed_tests", []) or [])
        if failed_tests:
            print("  top_failed_tests:")
            for failure in failed_tests[:5]:
                location = failure.get("file") or "unknown"
                if failure.get("line"):
                    location = f"{location}:{failure.get('line')}"
                print(f"    {failure.get('test_name') or failure.get('kind')}: {location}")
                if failure.get("snippet"):
                    print(f"      {failure.get('snippet')}")
    if result.get("workflow") == "SingleTestWorkflow":
        print("Single Test Report")
        print(f"  Target: {result.get('test_target')}")
        print(f"  Result: {format_validation_status(str(result.get('validation_status', 'not_applicable')))}")
        print(f"  Duration: {result.get('duration_ms')} ms")
        print(f"  exit_code: {result.get('exit_code')}")
        print(f"  test_command: {result.get('test_command')}")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        if result.get("error_code"):
            print(f"  error_code: {result.get('error_code')}")
        print(f"  Failures: {result.get('failure_count', 0)}")
        failed_tests = list(result.get("failed_tests", []) or [])
        if failed_tests:
            print("  top_failed_tests:")
            for failure in failed_tests[:5]:
                location = failure.get("file") or "unknown"
                if failure.get("line"):
                    location = f"{location}:{failure.get('line')}"
                print(f"    {failure.get('test_name') or failure.get('kind')}: {location}")
        print("  tip: Use --json for full structured artifacts.")
    if result.get("workflow") == "ExplainFailureWorkflow":
        print("Explain Failure Report")
        if result.get("outcome"):
            print(f"  Outcome: {result.get('outcome')}")
        if result.get("outcome_reason"):
            print(f"  outcome_reason: {result.get('outcome_reason')}")
        print(f"  test_command: {result.get('test_command')}")
        print(f"  exit_code: {result.get('exit_code')}")
        print(f"  failure_count: {result.get('failure_count', 0)}")
        print(f"  diagnosis_count: {result.get('diagnosis_count', 0)}")
        if result.get("summary"):
            print(f"  Summary: {result.get('summary')}")
        diagnoses = list(result.get("diagnoses", []) or [])
        if diagnoses:
            top = diagnoses[0]
            location = top.get("file") or "unknown"
            if top.get("line"):
                location = f"{location}:{top.get('line')}"
            print("  top_diagnoses:")
            print(f"  Top diagnosis: {top.get('failure_type')} ({top.get('test_name') or location})")
            print(f"  Confidence: {top.get('confidence')}")
            if top.get("confidence_reason"):
                print(f"  confidence_reason: {top.get('confidence_reason')}")
            print(f"  likely_cause: {top.get('likely_cause')}")
            steps = list(top.get("suggested_next_steps", []) or [])
            if steps:
                print("  suggested_next_steps:")
                for step in steps[:3]:
                    print(f"    - {step}")
            if len(diagnoses) > 1:
                print(f"  additional_diagnoses: {len(diagnoses) - 1}")
                print("  tip: Use --json for full structured artifacts.")
        else:
            print(f"  no_diagnosis_reason: {result.get('confidence_reason') or 'No diagnosis was produced.'}")
    if result.get("workflow") == "FixFailingTestWorkflow":
        print("Fix Failing Test Report")
        if result.get("repair_status"):
            print(f"  Repair status: {result.get('repair_status')}")
        print(f"  unsupported: {result.get('unsupported', False)}")
        if result.get("unsupported_reason"):
            print(f"  unsupported_reason: {result.get('unsupported_reason')}")
        print(f"  Repair attempted: {result.get('repair_attempted', False)}")
        if result.get("risk_level"):
            print(f"  Risk level: {result.get('risk_level')}")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        if result.get("recommendation"):
            print(f"  Recommendation: {result.get('recommendation')}")
        if result.get("repair_class") or result.get("proposed_repair_class"):
            print(f"  repair_class: {result.get('repair_class') or result.get('proposed_repair_class')}")
        if result.get("target_file"):
            print(f"  target_file: {result.get('target_file')}")
        if result.get("patch_summary") or result.get("proposed_patch_summary"):
            print(f"  patch_summary: {result.get('patch_summary') or result.get('proposed_patch_summary')}")
        if result.get("guardrail_triggered"):
            print(f"  guardrail_triggered: {result.get('guardrail_triggered')}")
            print(f"  guardrail_reason: {result.get('guardrail_reason')}")
        files_modified = list(result.get("files_modified", []) or [])
        if files_modified:
            print("  files_modified:")
            for file_path in files_modified[:8]:
                print(f"    {file_path}")
        would_modify = list(result.get("would_modify_files", []) or [])
        if would_modify:
            print("  would_modify_files:")
            for file_path in would_modify[:8]:
                print(f"    {file_path}")
        verification = result.get("verification") or {}
        if isinstance(verification, dict) and verification:
            print(
                "  verification: "
                f"tests_run={verification.get('tests_run')} "
                f"tests_passed={verification.get('tests_passed')} "
                f"validation_status={verification.get('validation_status')}"
            )
        if result.get("rollback_supported") is not None:
            print(f"  rollback_supported: {result.get('rollback_supported')}")
            print(f"  rollback_performed: {result.get('rollback_performed')}")
        diagnoses = list(result.get("diagnoses", []) or [])
        if diagnoses:
            top = diagnoses[0]
            print(f"  failure_diagnosis: {top.get('failure_type')} ({top.get('confidence')})")
            print(f"    likely_cause: {top.get('likely_cause')}")
    if result.get("workflow") == "AddHelperWorkflow":
        print("Add Helper Report")
        print("  visibility: internal")
        if result.get("workflow_status"):
            print(f"  workflow_status: {result.get('workflow_status')}")
        if result.get("action_taken"):
            print(f"  action_taken: {result.get('action_taken')}")
        if result.get("helper_name"):
            print(f"  helper_name: {result.get('helper_name')}")
        if result.get("target_file"):
            print(f"  target_file: {result.get('target_file')}")
        print(f"  dry_run: {result.get('dry_run', False)}")
        if result.get("unsupported_reason"):
            print(f"  unsupported_reason: {result.get('unsupported_reason')}")
        if result.get("risk_level"):
            print(f"  risk_level: {result.get('risk_level')}")
        files_modified = list(result.get("files_modified", []) or [])
        if files_modified:
            print("  files_modified:")
            for file_path in files_modified[:8]:
                print(f"    {file_path}")
        would_modify = list(result.get("would_modify_files", []) or [])
        if would_modify:
            print("  would_modify_files:")
            for file_path in would_modify[:8]:
                print(f"    {file_path}")
        verification = result.get("verification") or {}
        if isinstance(verification, dict) and verification:
            print(
                "  verification: "
                f"tests_run={verification.get('tests_run')} "
                f"tests_passed={verification.get('tests_passed')} "
                f"validation_status={verification.get('validation_status')}"
            )
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        if result.get("recommendation"):
            print(f"  recommendation: {result.get('recommendation')}")
    if result.get("workflow") == "GitStatusWorkflow":
        print(f"  branch: {result.get('branch')}")
        print(f"  clean: {result.get('clean')}")
        print(f"  staged_count: {result.get('staged_count', 0)}")
        print(f"  unstaged_count: {result.get('unstaged_count', 0)}")
        print(f"  untracked_count: {result.get('untracked_count', 0)}")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        changed_files = list(result.get("changed_files", []) or [])
        if changed_files:
            print("  changed_files:")
            for item in changed_files[:12]:
                flags = []
                if item.get("staged"):
                    flags.append("staged")
                if item.get("unstaged"):
                    flags.append("unstaged")
                print(f"    {item.get('status')}: {item.get('path')} ({', '.join(flags) or 'working tree'})")
            if len(changed_files) > 12:
                print(f"    ... {len(changed_files) - 12} more")
    if result.get("workflow") == "DependencyCheckWorkflow":
        print(f"  dependency: {result.get('dependency')}")
        print(f"  declared: {result.get('declared')}")
        declared_in = list(result.get("declared_in", []) or [])
        if declared_in:
            print("  declared_in:")
            for file_path in declared_in[:8]:
                print(f"    {file_path}")
        print(f"  used: {result.get('used')}")
        print(f"  usage_count: {result.get('usage_count', 0)}")
        usage_files = list(result.get("usage_files", []) or [])
        if usage_files:
            print("  usage_files:")
            for file_path in usage_files[:8]:
                print(f"    {file_path}")
            if len(usage_files) > 8:
                print(f"    ... {len(usage_files) - 8} more")
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
    if result.get("workflow") in {
        "SaveContextSnapshotWorkflow",
        "TrackDecisionLogWorkflow",
        "RememberUserPreferencesWorkflow",
        "RehydrateContextWorkflow",
    }:
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
    if result.get("workflow") == "MonitorTestFlakinessWorkflow":
        tid = result.get("test_identifier", ".")
        runs = result.get("runs", 0)
        passed = result.get("passed", 0)
        pr = result.get("pass_rate")
        flaky = result.get("flaky")
        print(f"  test: {tid}")
        print(f"  runs: {runs}  passed: {passed}  failed: {result.get('failed', 0)}")
        if pr is not None:
            print(f"  pass_rate: {pr:.0%}")
        if flaky is not None:
            verdict = "FLAKY" if flaky else ("deterministic" if result.get("deterministic") else "unstable")
            print(f"  verdict: {verdict}")
        msgs = result.get("failure_messages") or []
        if msgs:
            print(f"  failure: {msgs[0][:120]}")
        med = result.get("median_duration_ms")
        if med is not None:
            print(f"  median_duration_ms: {med:.0f}")
    if result.get("workflow") == "MeasureTestSuiteSpeedWorkflow":
        dur = result.get("total_duration_ms", 0)
        rating = result.get("speed_rating", "unknown")
        print(f"  total_duration_ms: {dur}")
        print(f"  speed_rating: {rating}")
        tc = result.get("test_count", 0)
        passed = result.get("passed", 0)
        failed = result.get("failed", 0)
        skipped = result.get("skipped", 0)
        print(f"  tests: {tc} total  passed: {passed}  failed: {failed}  skipped: {skipped}")
        delta = result.get("baseline_delta_ms")
        if delta is not None:
            sign = "+" if delta >= 0 else ""
            direction = "slower" if delta > 0 else ("faster" if delta < 0 else "same")
            print(f"  baseline_delta_ms: {sign}{delta} ({direction})")
        else:
            print("  baseline_delta_ms: null (no prior baseline)")
        slowest = result.get("slowest_tests") or []
        if slowest:
            print("  slowest_tests:")
            for t in slowest[:5]:
                print(f"    {t.get('duration_ms')}ms  {t.get('name')}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "SearchWorkflow":
        query = result.get("query", "")
        mode = result.get("mode", "text")
        total = result.get("total_matches", 0)
        files = result.get("files_with_matches", 0)
        print(f"  query: {query!r}  mode: {mode}  matches: {total}  files: {files}")
        if result.get("truncated"):
            print(f"  truncated: true (showing first {result.get('limit', 100)})")
        for m in (result.get("matches") or [])[:20]:
            print(f"  {m.get('file')}:{m.get('line')}:{m.get('column')}  {m.get('preview', '')[:80]}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "DetectSecretPatternsWorkflow":
        clean = result.get("clean", True)
        fc = result.get("findings_count", 0)
        sc = result.get("scanned_files", 0)
        sk = result.get("skipped_files", 0)
        print(f"  clean: {clean}  findings: {fc}  scanned: {sc}  skipped: {sk}")
        findings = result.get("findings") or []
        if findings:
            by_sev: dict[str, list[dict]] = {}
            for f in findings:
                sev = f.get("severity", "unknown")
                by_sev.setdefault(sev, []).append(f)
            for sev in ("critical", "high", "medium"):
                group = by_sev.get(sev, [])
                if group:
                    print(f"  {sev}:")
                    for f in group[:10]:
                        fixture = " [test fixture]" if f.get("likely_test_fixture") else ""
                        print(f"    {f.get('file')}:{f.get('line')}  {f.get('pattern_name')}  {f.get('match_preview')}{fixture}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "WatchDependencyUpdatesWorkflow":
        pm = result.get("package_manager", "none")
        out = result.get("outdated_count", 0)
        up = result.get("up_to_date_count", 0)
        unk = result.get("unknown_count", 0)
        print(f"  package_manager: {pm}  outdated: {out}  up_to_date: {up}  unknown: {unk}")
        for dep in (result.get("dependencies") or []):
            bump = ""
            if dep.get("major_bump"):
                bump = " [MAJOR]"
            elif dep.get("minor_bump"):
                bump = " [minor]"
            elif dep.get("patch_bump"):
                bump = " [patch]"
            latest = dep.get("latest_version") or "?"
            status = "OUTDATED" if dep.get("outdated") else ("unknown" if dep.get("latest_version") is None else "ok")
            print(f"  {dep.get('name'):40s}  {dep.get('current_version'):12s} → {latest:12s}  {status}{bump}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "DetectRepoActivityWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            since = result.get("since") or "none"
            first_run = result.get("first_run", False)
            summary = result.get("summary") or {}
            active = summary.get("active_since", False)
            print(f"  since: {since}  first_run: {first_run}  active: {active}")
            print(f"  commits: {summary.get('commit_count', 0)}  "
                  f"files: {summary.get('files_changed_count', 0)}  "
                  f"+{summary.get('insertions_total', 0)}/-{summary.get('deletions_total', 0)}")
            for c in (result.get("commits_since") or [])[:5]:
                print(f"  [{c.get('hash')}] {c.get('message', '')[:60]}  ({c.get('author')})")
            for f in (result.get("files_changed") or [])[:10]:
                ins = f.get("insertions")
                dels = f.get("deletions")
                stat = f"+{ins}/{dels}" if ins is not None else ""
                print(f"  {f.get('status', '?'):8s}  {f.get('path')}  {stat}")
            for b in (result.get("branches_changed") or []):
                print(f"  branch {b.get('status')}: {b.get('name')}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "ProfileCodeExecutionWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            target = result.get("target", "")
            total_ms = result.get("total_duration_ms", 0.0)
            cpu_ms = result.get("cpu_time_ms", 0.0)
            calls = result.get("function_calls", 0)
            rating = result.get("rating", "unknown")
            print(f"  target: {target}")
            print(f"  total_duration_ms: {total_ms:.2f}  cpu_time_ms: {cpu_ms:.2f}  function_calls: {calls}  rating: {rating}")
            hotspots = result.get("hotspots") or []
            if hotspots:
                print("  hotspots:")
                for h in hotspots[:5]:
                    print(f"    {h.get('total_time_ms', 0.0):.2f}ms  {h.get('function')}  {h.get('file')}:{h.get('line')}  calls={h.get('calls')}")
            bline_delta = result.get("baseline_delta_ms")
            if bline_delta is not None:
                sign = "+" if bline_delta >= 0 else ""
                print(f"  baseline_delta_ms: {sign}{bline_delta:.2f}")
            else:
                print("  baseline_delta_ms: null (no prior baseline)")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "MeasureMemoryUsageWorkflow":
        target = result.get("target", "")
        peak = result.get("peak_memory_mb", 0.0)
        base = result.get("baseline_memory_mb", 0.0)
        delta = result.get("delta_memory_mb", 0.0)
        rating = result.get("rating", "unknown")
        dur = result.get("duration_ms", 0)
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  target: {target}")
            print(f"  peak_memory_mb: {peak:.2f}  baseline_memory_mb: {base:.2f}  delta_memory_mb: {delta:.2f}  rating: {rating}")
            print(f"  duration_ms: {dur}")
            top = result.get("tracemalloc_top") or []
            if top:
                print("  tracemalloc_top:")
                for t in top[:5]:
                    print(f"    {t.get('size_kb', 0.0):.1f}KB  {t.get('file')}:{t.get('line')}")
            bline_delta = result.get("baseline_delta_mb")
            if bline_delta is not None:
                sign = "+" if bline_delta >= 0 else ""
                print(f"  baseline_delta_mb: {sign}{bline_delta:.2f}")
            else:
                print("  baseline_delta_mb: null (no prior baseline)")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "DetectRunningProcessesWorkflow":
        total = result.get("total_count", 0)
        print(f"  total_count: {total} process(es)")
        dev = result.get("dev_services") or {}
        active = [k for k, v in dev.items() if v]
        if active:
            print(f"  dev_services: {', '.join(active)}")
        else:
            print("  dev_services: none detected")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
        if total:
            print("  tip: Use --json for the full process list.")
    if result.get("workflow") == "CheckPortAvailabilityWorkflow":
        port = result.get("port")
        host = result.get("host", "127.0.0.1")
        available = result.get("available")
        if port is not None and available is not None:
            status = "available" if available else "in use"
            print(f"  port: {port}")
            print(f"  host: {host}")
            print(f"  status: {status}")
            proc = result.get("process")
            if proc:
                print(f"  process: pid={proc.get('pid')} name={proc.get('name')} user={proc.get('user')}")
            if result.get("note"):
                print(f"  note: {result['note']}")
            if result.get("checked_at"):
                print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "InspectRuntimeEnvironmentWorkflow":
        if result.get("python_version"):
            print(f"  python_version: {result['python_version']}")
        if result.get("python_executable"):
            print(f"  python_executable: {result['python_executable']}")
        venv = result.get("virtual_env")
        print(f"  virtual_env: {venv if venv else '(none)'}")
        if result.get("platform"):
            print(f"  platform: {result['platform']}")
        pkgs = result.get("installed_packages")
        if isinstance(pkgs, list):
            print(f"  installed_packages: {len(pkgs)} package(s)")
        if result.get("summary"):
            print(f"  summary: {result['summary']}")
    if result.get("workflow") == "ListBranchesWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            cur = result.get("current_branch") or "(detached)"
            total = result.get("total_count", 0)
            print(f"  current_branch: {cur}  total: {total}")
            for b in (result.get("branches") or []):
                marker = "* " if b.get("current") else "  "
                tag = " [remote]" if b.get("remote") else ""
                ahead = b.get("ahead")
                behind = b.get("behind")
                ab = ""
                if ahead is not None and behind is not None:
                    ab = f"  ↑{ahead} ↓{behind}"
                print(f"{marker}{b.get('name')}{tag}{ab}  [{b.get('last_commit_hash')}] {b.get('last_commit_message', '')[:50]}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "GitLogWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            total = result.get("total_shown", 0)
            filters = result.get("filters_applied") or {}
            print(f"  commits: {total}  filters: {filters}")
            for c in (result.get("commits") or []):
                print(f"  [{c.get('hash')}] {c.get('message', '')[:60]}  ({c.get('author_name')})  +{c.get('insertions', 0)}/-{c.get('deletions', 0)}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "GetCommitDetailsWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  commit: {result.get('short_hash')}  author: {result.get('author_name')}  date: {result.get('date')}")
            msg = (result.get("message") or "").splitlines()
            print(f"  message: {msg[0][:80] if msg else ''}")
            print(f"  insertions: +{result.get('total_insertions', 0)}  deletions: -{result.get('total_deletions', 0)}")
            for f in (result.get("files_changed") or [])[:10]:
                print(f"  {f.get('status', '?'):10s}  {f.get('path')}  +{f.get('insertions', 0)}/-{f.get('deletions', 0)}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "GitDiffWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            total_f = result.get("total_files", 0)
            total_i = result.get("total_insertions", 0)
            total_d = result.get("total_deletions", 0)
            truncated = result.get("truncated", False)
            print(f"  files: {total_f}  +{total_i}/-{total_d}{'  [truncated]' if truncated else ''}")
            for f in (result.get("files") or [])[:15]:
                print(f"  {f.get('status', '?'):10s}  {f.get('path')}  +{f.get('insertions', 0)}/-{f.get('deletions', 0)}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "GetFileHistoryWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  file: {result.get('file')}  commits: {result.get('total_shown', 0)}")
            print(f"  first_commit: {result.get('first_commit_date')}  last_modified: {result.get('last_modified_date')}")
            for c in (result.get("commits") or []):
                print(f"  [{c.get('hash')}] {c.get('message', '')[:60]}  ({c.get('author_name')})  +{c.get('insertions', 0)}/-{c.get('deletions', 0)}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "GitBlameWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            total_l = result.get("total_lines", 0)
            trunc = result.get("truncated", False)
            authors = result.get("unique_authors") or []
            print(f"  file: {result.get('file')}  lines: {total_l}{'  [truncated]' if trunc else ''}")
            print(f"  authors: {', '.join(authors)}")
            for ln in (result.get("lines") or [])[:10]:
                print(f"  {ln.get('line_number'):5d}  [{ln.get('commit_hash')}] {ln.get('author_name')}: {ln.get('content', '')[:60]}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "FindMergeConflictsWorkflow":
        clean = result.get("clean", True)
        total_f = result.get("total_files_with_conflicts", 0)
        total_s = result.get("total_conflict_sections", 0)
        print(f"  clean: {clean}  files_with_conflicts: {total_f}  total_sections: {total_s}")
        for c in (result.get("conflicts") or []):
            print(f"  {c.get('file')}  ({c.get('conflict_count')} conflict(s))")
            for sec in (c.get("sections") or [])[:5]:
                print(f"    lines {sec.get('start_line')}–{sec.get('end_line')}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    # Surface error_code as a human-readable message for any workflow failure.
    if not result["success"]:
        error_code = result.get("error_code")
        if error_code:
            human_msg = _ERROR_CODE_MESSAGES.get(str(error_code))
            if human_msg:
                print(f"  error: {human_msg}")
            else:
                print(f"  error_code: {error_code}")
    if verbose:
        print(f"  repo_path: {result['repo_path']}")
        print(f"  task: {result['task']}")
    if result.get("artifacts"):
        print("  tip: Use --json for full structured artifacts.")


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
        "python_version",
        "python_executable",
        "virtual_env",
        "platform",
        "installed_packages",
        "environment_variables",
        "port_conflicts",
        "port",
        "host",
        "available",
        "process",
        "checked_at",
        "note",
        "processes",
        "dev_services",
        "total_count",
        "test_identifier",
        "runs",
        "passed",
        "failed",
        "pass_rate",
        "flaky",
        "deterministic",
        "failure_messages",
        "duration_ms",
        "median_duration_ms",
        "total_duration_ms",
        "skipped",
        "slowest_tests",
        "speed_rating",
        "baseline_delta_ms",
        "baseline_saved_at",
        "findings",
        "scanned_files",
        "skipped_files",
        "findings_count",
        "clean",
        "matches",
        "total_matches",
        "files_with_matches",
        "truncated",
        "limit",
        "peak_memory_mb",
        "baseline_memory_mb",
        "delta_memory_mb",
        "tracemalloc_top",
        "rating",
        "baseline_delta_mb",
        "total_duration_ms",
        "cpu_time_ms",
        "function_calls",
        "hotspots",
        "commits_since",
        "files_changed",
        "branches_changed",
        "summary",
        "first_run",
        "dependencies",
        "outdated_count",
        "up_to_date_count",
        "unknown_count",
        "package_manager",
        "branches",
        "current_branch",
        "total_count",
        "commits",
        "total_shown",
        "filters_applied",
        "hash",
        "short_hash",
        "message",
        "author_name",
        "date",
        "files_changed",
        "total_insertions",
        "total_deletions",
        "parent_hashes",
        "files",
        "total_files",
        "file",
        "first_commit_date",
        "last_modified_date",
        "lines",
        "total_lines",
        "unique_authors",
        "date_range",
        "conflicts",
        "total_files_with_conflicts",
        "total_conflict_sections",
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
        "virtual_env",
        "port_conflicts",
        "process",
    }
    return {
        key: result_source[key]
        for key in optional_keys
        if key in result_source and (result_source[key] is not None or key in nullable_optional_keys)
    }


def record_cli_activity_best_effort(command_name: str, *, success: bool, started: float) -> None:
    try:
        record_cli_activity(
            command_name=command_name,
            success=success,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            client="cli",
        )
    except Exception:
        pass


def format_validation_status(status: str) -> str:
    labels = {
        "passed": "Passed",
        "failed": "Failed",
        "not_applicable": "Not Applicable",
    }
    return labels.get(status, status.replace("_", " ").title())


def with_not_applicable_validation(result: dict[str, Any]) -> dict[str, Any]:
    updated = dict(result)
    updated.setdefault("tests_run", False)
    updated.setdefault("tests_passed", None)
    updated.setdefault("validation_status", "not_applicable")
    return updated


def record_tool_invocation_best_effort(
    *,
    tool_name: str,
    repo_path: str | None,
    task: str | None,
    workflow: str | None,
    success: bool,
    duration_ms: float,
    tool_calls: int,
    macro_sequence: list[str],
) -> None:
    try:
        record_tool_invocation(
            tool_name=tool_name,
            repo_path=repo_path,
            task=task,
            workflow=workflow,
            success=success,
            duration_ms=duration_ms,
            tool_calls=tool_calls,
            macro_sequence=macro_sequence,
        )
    except Exception:
        pass


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def is_pytest_available() -> bool:
    if os.environ.get("SKILLLAYER_FORCE_PYTEST_MISSING") == "1":
        return False
    return shutil.which("pytest") is not None or module_available("pytest")


def is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".skilllayer_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
