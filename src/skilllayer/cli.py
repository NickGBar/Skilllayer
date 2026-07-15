from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import SkillLayer
from .ab_benchmark import run_ab_benchmark
from .analytics import build_analytics_report
from .benchmark import run_benchmark
from .benchmark_automation import run_benchmark_automation
from .bundle_review import review_telemetry_bundle
from .claude_code_prep_check import run_claude_code_prep_check
from .config import load_config
from .config.defaults import COMMAND_METADATA, MACROS, WORKFLOWS, WORKFLOW_METADATA
from .cost_tracking import USAGE_EVENTS_JSONL, aggregate_usage
from .cost_optimization import run_cost_optimization_report
from .cursor_mcp_validation import run_cursor_mcp_validation
from .demand_tracking import DEMAND_EVENTS_JSONL, build_demand_report
from .diagnostics import build_diagnostics
from .failure_analysis import analyze_failures
from .feedback_registry import ALLOWED_PLATFORMS, ALLOWED_STATUSES, build_feedback_report
from .generalization import run_generalization_validation
from .live_telemetry_validation import run_live_provider_validation
from .router import SkillRouter
from .security import (
    blocked_workflow_reason,
    internal_execution_allowed_via_env,
    workflow_execution_blocked,
)
from .memory.skilllayer_memory import status_for_error_code
from .mcp_config import build_config as build_mcp_config, render_config, validate_config
from .open_source_audit import run_open_source_audit
from .packaging_audit import run_packaging_audit
from .pricing import aggregate_costs
from .release_validation import run_release_check
from .run_tests_real_repo_smoke import run_smoke as run_tests_real_repo_smoke
from .security_check import run_security_check
from .telemetry import (
    automatic_telemetry_enabled,
    automatic_telemetry_paths,
    cli_activity_paths,
    record_cli_activity,
    record_tool_invocation,
    summarize_telemetry,
)
from .telemetry_export import export_anonymous_telemetry
from .tester_check import run_tester_check
from .tools import inspect_repo
from .token_savings_benchmark import run_token_savings_benchmark
from .workflow_repair import run_workflow_repair_report
from .version import product_version
from .runner.core import (
    build_release_readiness_artifacts,
    build_resume_work_artifacts,
    build_safe_change_artifacts,
)


COMMANDS = {
    "run",
    "inspect",
    "workflows",
    "skills",
    "doctor",
    "diagnostics",
    "mcp-config",
    "mcp-config-check",
    "safe-change", "release-readiness", "resume-work",
    "stats", "analytics", "benchmark", "analyze-failures", "repair-workflows-report",
    "optimize-cost-report", "generalization-report", "packaging-audit", "open-source-audit",
    "usage-report", "cost-report", "ab-benchmark", "validate-live-telemetry", "demand-report",
    "telemetry-export", "review-bundle", "tester-check", "cursor-mcp-validation",
    "claude-code-prep-check", "run-tests-smoke", "fix-tests-smoke", "security-check",
    "release-check", "feedback-status", "token-benchmark", "benchmark-automation",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print(product_version())
        return 0
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
    if automatic_telemetry_enabled() and command_name != "release-check":
        telemetry_paths = (
            automatic_telemetry_paths()
            if command_name in {"run", "inspect"}
            else cli_activity_paths()
        )
        if command_name == "run":
            telemetry_paths = [
                *telemetry_paths,
                str(DEMAND_EVENTS_JSONL),
                str(USAGE_EVENTS_JSONL),
            ]
        print(
            "SkillLayer automatic telemetry is enabled; writing: "
            + ", ".join(telemetry_paths),
            file=sys.stderr,
        )
    try:
        exit_code = int(args.handler(args))
    except Exception:
        if command_name != "release-check":
            record_cli_activity_best_effort(command_name, success=False, started=started)
        raise
    if command_name != "release-check":
        record_cli_activity_best_effort(command_name, success=exit_code == 0, started=started)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skilllayer", description="Production-oriented SkillLayer CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Route and execute a coding task.")
    run_parser.add_argument("--repo", default=None, help="Repository path. Defaults to the current working directory.")
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

    diagnostics_parser = subparsers.add_parser("diagnostics", help="Generate a local sanitized diagnostics report.")
    diagnostics_parser.add_argument("--repo", default=None, help="Optional project path; only this project's MCP config is inspected.")
    diagnostics_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    diagnostics_parser.add_argument("--output", type=Path, default=None, help="Write the report locally; refuses existing paths by default.")
    diagnostics_parser.add_argument("--force", action="store_true", help="Explicitly allow replacing --output.")
    diagnostics_parser.set_defaults(handler=handle_diagnostics)

    safe_change_parser = subparsers.add_parser("safe-change", help="Plan and validate a code change safely (host agent performs the edit).")
    safe_change_parser.add_argument("--repo", default=None, help="Repository path. Defaults to the current working directory.")
    safe_change_parser.add_argument("--task", required=True, help="Description of the change to make.")
    safe_change_parser.add_argument("--phase", choices=["plan", "validate"], default="plan", help="plan (default) or validate.")
    safe_change_parser.add_argument("--save-context", action="store_true", help="On phase=validate, persist a summary snapshot under .skilllayer/.")
    safe_change_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    safe_change_parser.set_defaults(handler=handle_safe_change)

    release_readiness_parser = subparsers.add_parser("release-readiness", help="Assess whether a repository is ready for careful external release.")
    release_readiness_parser.add_argument("--repo", default=None, help="Repository path. Defaults to the current working directory.")
    release_readiness_parser.add_argument("--deep", action="store_true", help="Also execute the repository's own test suite (bounded by default).")
    release_readiness_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    release_readiness_parser.set_defaults(handler=handle_release_readiness)

    resume_work_parser = subparsers.add_parser("resume-work", help="Reconstruct project state for a new session from saved memory alone.")
    resume_work_parser.add_argument("--repo", default=None, help="Repository path. Defaults to the current working directory.")
    resume_work_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    resume_work_parser.set_defaults(handler=handle_resume_work)

    mcp_config_parser = subparsers.add_parser("mcp-config", help="Generate the installed SkillLayer stdio MCP config.")
    mcp_config_parser.add_argument("--output", type=Path, default=None, help="Explicit path to write config JSON; otherwise print it.")
    mcp_config_parser.set_defaults(handler=handle_mcp_config)

    mcp_config_check_parser = subparsers.add_parser("mcp-config-check", help="Validate a SkillLayer MCP config and detect relocation.")
    mcp_config_check_parser.add_argument("config", type=Path, help="Generated MCP config JSON to validate.")
    mcp_config_check_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    mcp_config_check_parser.set_defaults(handler=handle_mcp_config_check)

    stats_parser = subparsers.add_parser("stats", help="Show SkillLayer telemetry and adoption metrics.")
    stats_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    stats_parser.set_defaults(handler=handle_stats)

    analytics_parser = subparsers.add_parser("analytics", help="Build SkillLayer coverage, ROI, and workflow miss analytics.")
    analytics_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    analytics_parser.set_defaults(handler=handle_analytics)

    benchmark_parser = subparsers.add_parser("benchmark", help="Run the baseline-vs-SkillLayer cost benchmark.")
    benchmark_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    benchmark_parser.set_defaults(handler=handle_benchmark)

    failures_parser = subparsers.add_parser("analyze-failures", help="Analyze benchmark failures without changing behavior.")
    failures_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    failures_parser.set_defaults(handler=handle_analyze_failures)

    repair_parser = subparsers.add_parser("repair-workflows-report", help="Run workflow repair benchmark and report before/after results.")
    repair_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    repair_parser.set_defaults(handler=handle_repair_workflows_report)

    optimize_parser = subparsers.add_parser("optimize-cost-report", help="Run cost optimization benchmark and report before/after results.")
    optimize_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    optimize_parser.set_defaults(handler=handle_optimize_cost_report)

    generalization_parser = subparsers.add_parser("generalization-report", help="Run cross-repository generalization validation.")
    generalization_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    generalization_parser.set_defaults(handler=handle_generalization_report)

    packaging_parser = subparsers.add_parser("packaging-audit", help="Run packaging, installability, and assumption audit.")
    packaging_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    packaging_parser.set_defaults(handler=handle_packaging_audit)

    open_source_parser = subparsers.add_parser("open-source-audit", help="Run open-source readiness audit.")
    open_source_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    open_source_parser.set_defaults(handler=handle_open_source_audit)

    usage_parser = subparsers.add_parser("usage-report", help="Aggregate raw provider/model usage events.")
    usage_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    usage_parser.set_defaults(handler=handle_usage_report)

    cost_parser = subparsers.add_parser("cost-report", help="Calculate estimated USD cost from observed usage events.")
    cost_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    cost_parser.set_defaults(handler=handle_cost_report)

    ab_parser = subparsers.add_parser("ab-benchmark", help="Run the controlled baseline-vs-SkillLayer cost benchmark.")
    ab_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    ab_parser.set_defaults(handler=handle_ab_benchmark)

    live_parser = subparsers.add_parser("validate-live-telemetry", help="Validate observed provider telemetry and cost-calculation readiness.")
    live_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    live_parser.set_defaults(handler=handle_validate_live_telemetry)

    demand_parser = subparsers.add_parser("demand-report", help="Build workflow demand statistics and opportunity ranking.")
    demand_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    demand_parser.set_defaults(handler=handle_demand_report)

    export_parser = subparsers.add_parser("telemetry-export", help="Create an opt-in anonymous local telemetry export.")
    export_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    export_parser.add_argument("--output", type=Path, default=None, help="Output export JSON path.")
    export_parser.add_argument("--include-raw-tasks", action="store_true", help="Include raw task text. Disabled by default.")
    export_parser.add_argument("--include-local-paths", action="store_true", help="Include local paths and browser URLs. Disabled by default.")
    export_parser.add_argument("--allow-unsafe", action="store_true", help="Write export even if privacy scan fails. Use only after manual review.")
    export_parser.set_defaults(handler=handle_telemetry_export)

    review_parser = subparsers.add_parser("review-bundle", help="Review a local directory of anonymous telemetry exports.")
    review_parser.add_argument("directory", help="Directory containing skilllayer_telemetry_export*.json files.")
    review_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    review_parser.set_defaults(handler=handle_review_bundle)

    tester_parser = subparsers.add_parser("tester-check", help="Run local validation checks.")
    tester_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    tester_parser.set_defaults(handler=handle_tester_check)

    cursor_parser = subparsers.add_parser("cursor-mcp-validation", help="Run Cursor MCP setup and schema validation.")
    cursor_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    cursor_parser.set_defaults(handler=handle_cursor_mcp_validation)

    claude_parser = subparsers.add_parser("claude-code-prep-check", help="Run Claude Code tester-prep validation.")
    claude_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    claude_parser.set_defaults(handler=handle_claude_code_prep_check)

    run_tests_smoke_parser = subparsers.add_parser("run-tests-smoke", help="Run RunTestsWorkflow real-repo smoke validation.")
    run_tests_smoke_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    run_tests_smoke_parser.set_defaults(handler=handle_run_tests_smoke)

    fix_tests_smoke_parser = subparsers.add_parser("fix-tests-smoke", help="Run FixFailingTestWorkflow v2 real-repo smoke validation.")
    fix_tests_smoke_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    fix_tests_smoke_parser.set_defaults(handler=handle_fix_tests_smoke)

    security_parser = subparsers.add_parser("security-check", help="Report SkillLayer local-first security posture and docs readiness.")
    security_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    security_parser.set_defaults(handler=handle_security_check)

    release_parser = subparsers.add_parser("release-check", help="Run read-only public export readiness validation.")
    release_parser.add_argument("--repo", default=None, help="Optional repository path. Defaults to this SkillLayer checkout.")
    release_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    release_parser.set_defaults(handler=handle_release_check)

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

    token_benchmark_parser = subparsers.add_parser("token-benchmark", help="Run token-savings proxy benchmark.")
    token_benchmark_parser.add_argument("--repo", default=None, help="Optional repository path. Defaults to this SkillLayer checkout.")
    token_benchmark_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    token_benchmark_parser.set_defaults(handler=handle_token_benchmark)

    benchmark_automation_parser = subparsers.add_parser("benchmark-automation", help="Run token-savings proxy benchmark across benchmark_repos.json.")
    benchmark_automation_parser.add_argument("--matrix", default=None, help="Optional benchmark matrix path. Defaults to benchmark_repos.json.")
    benchmark_automation_parser.add_argument("--json", action="store_true", help="Print strict JSON only.")
    benchmark_automation_parser.set_defaults(handler=handle_benchmark_automation)
    return parser


def handle_run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    # --repo is optional; default to the current working directory so read-only
    # tasks (git status, rehydrate context, ...) work without an explicit path.
    if not args.repo:
        args.repo = os.getcwd()
    config = load_config(args.config)
    dry_run = bool(args.dry_run or config.get("execution", {}).get("dry_run", False))
    log_dir = args.log_dir or config.get("logging", {}).get("log_dir")

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


def _resolve_repo_arg(repo_arg: str | None) -> Path:
    return Path(repo_arg or os.getcwd()).expanduser().resolve()


def _print_environment_remediation(remediation: dict[str, object] | None) -> None:
    if not remediation or remediation.get("remediation_type") in (None, "none"):
        return
    print(f"  environment: {remediation.get('remediation_summary')}")
    command = remediation.get("remediation_command")
    if command:
        print(f"  recommended_command: {command}")
    if remediation.get("mutates_environment"):
        print("  note: this command changes the target environment. SkillLayer did not run it; review it, run it yourself, then retry validation.")
    else:
        print("  note: SkillLayer did not run a remediation command. Review the environment, then retry validation.")


def handle_safe_change(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    repo = _resolve_repo_arg(args.repo)
    result = build_safe_change_artifacts(
        repo, args.task, phase=args.phase, save_context=bool(args.save_context),
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Safe code change — phase={result['phase']} verdict={result['verdict']}")
        print(f"  repository_state: {result['repository_state']}")
        if result["phase"] == "plan":
            print("  proposed_plan:")
            for step in result.get("proposed_plan") or []:
                print(f"    - {step}")
        else:
            print(f"  changed_files: {result['changed_files']}")
            print(f"  tests_run: {result['tests_run']}  tests_passed: {result['tests_passed']}")
            _print_environment_remediation(result.get("remediation"))
        if result.get("risks"):
            print(f"  risks: {result['risks']}")
        if result.get("warnings"):
            print(f"  warnings: {result['warnings']}")
        print(f"  next_action: {result['next_action']}")
    record_tool_invocation_best_effort(
        tool_name="skilllayer_safe_change", repo_path=str(repo), task=args.task,
        workflow="SafeCodeChangeWorkflow", success=bool(result.get("success")),
        duration_ms=(time.perf_counter() - started) * 1000.0, tool_calls=0, macro_sequence=[],
    )
    return 0 if result.get("success") else 1


def handle_release_readiness(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    repo = _resolve_repo_arg(args.repo)
    result = build_release_readiness_artifacts(repo, deep=bool(args.deep))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Release readiness — verdict={result['verdict']}")
        print(f"  checks_completed: {', '.join(result['checks_completed'])}")
        if result["checks_incomplete"]:
            print(f"  checks_incomplete: {[c['check'] for c in result['checks_incomplete']]}")
        if result["blockers"]:
            print(f"  blockers: {result['blockers']}")
        if result["warnings"]:
            print(f"  warnings: {result['warnings']}")
        _print_environment_remediation(result.get("remediation") or result.get("test_status", {}).get("remediation"))
    record_tool_invocation_best_effort(
        tool_name="skilllayer_release_readiness", repo_path=str(repo), task=None,
        workflow="ReleaseReadinessWorkflow", success=bool(result.get("success")),
        duration_ms=(time.perf_counter() - started) * 1000.0, tool_calls=0, macro_sequence=[],
    )
    return 0 if result.get("verdict") in ("READY_FOR_CAREFUL_TESTERS", "READY_WITH_KNOWN_LIMITATIONS") else 1


def handle_resume_work(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    repo = _resolve_repo_arg(args.repo)
    result = build_resume_work_artifacts(repo)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Resume project work — verdict={result['verdict']}")
        print(f"  project_summary: {result['project_summary']}")
        print(f"  remembered_next_action: {result['remembered_next_action']}")
        if result["uncertainty"]:
            print(f"  uncertainty: {result['uncertainty']}")
    record_tool_invocation_best_effort(
        tool_name="skilllayer_resume_work", repo_path=str(repo), task=None,
        workflow="ResumeProjectWorkWorkflow", success=bool(result.get("success")),
        duration_ms=(time.perf_counter() - started) * 1000.0, tool_calls=0, macro_sequence=[],
    )
    return 0 if result.get("success") else 1


def handle_workflows(args: argparse.Namespace) -> int:
    result = {
        "workflows": [
            {
                "name": name,
                **WORKFLOW_METADATA.get(name, {}),
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
            print(
                f"{workflow['name']} [{workflow['stability']}; "
                f"{workflow['write_behavior']}]: {' -> '.join(workflow['macro_sequence'])}"
            )
            if workflow["state_locations"]:
                print(f"  state_locations: {', '.join(workflow['state_locations'])}")
            if workflow["requires_write_consent"]:
                print("  requires_write_consent: true")
        print("Commands")
        for command in result["commands"]:
            print(
                f"  {command['name']} [{command['stability']}; "
                f"{command['write_behavior']}]: {command['summary']}"
            )
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
        "product_version": {
            "value": product_version(),
            "ok": product_version() != "unknown",
            "required": True,
        },
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
            "warning": None if mcp_available else "MCP SDK is optional for CLI use. Install with python -m pip install \".[mcp]\" --no-build-isolation for MCP clients.",
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
    log_dir = args.log_dir or config.get("logging", {}).get("log_dir")
    if log_dir:
        required_checks["log_dir_writable"] = {
            "value": str(log_dir),
            "ok": is_writable_dir(Path(log_dir)),
            "required": True,
            "method": "read_only_permission_check",
        }
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
    details = []
    for check_id, check in checks.items():
        required = bool(check.get("required"))
        ok = bool(check.get("ok"))
        status = "PASS" if ok else ("BLOCKED" if required else "INCOMPLETE")
        observed = check.get("value")
        warning = check.get("warning") or check.get("error")
        details.append({
            "id": check_id,
            "title": check_id.replace("_", " "),
            "status": status,
            "observed": observed,
            "impact": None if ok else ("Required installation condition is unavailable." if required else "This optional capability was not confirmed."),
            "usable": ok or not required,
            "next_action": warning,
            "remediation": warning,
            "mutates_environment": False,
            "requires_user_confirmation": False,
            "evidence": {"required": required, "ok": ok},
            "error_code": None if ok else check_id,
        })
    result["check_details"] = details
    result["overall_status"] = "PASS" if result["ok"] else "BLOCKED"
    result["recommended_next_action"] = next((item["next_action"] for item in details if item["next_action"]), None)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"SkillLayer doctor: {result['overall_status']}")
        for item in details:
            if item["status"] != "PASS":
                print(f"  {item['status']}: {item['title']} — {item['observed']}")
                if item["next_action"]:
                    print(f"    next action: {item['next_action']}")
        print(f"  evidence: {sum(item['status'] == 'PASS' for item in details)}/{len(details)} checks passed")
    return 0 if result["ok"] else 1


def handle_diagnostics(args: argparse.Namespace) -> int:
    doctor_args = argparse.Namespace(repo=args.repo, config=None, log_dir=None, json=True)
    # Build doctor data without printing it to the diagnostics stream.
    from contextlib import redirect_stdout
    import io
    sink = io.StringIO()
    with redirect_stdout(sink):
        handle_doctor(doctor_args)
    doctor = json.loads(sink.getvalue())
    try:
        tool_count = _mcp_tool_count()
    except Exception:
        tool_count = None
    result = build_diagnostics(args.repo, doctor, tool_count=tool_count)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        destination = args.output.expanduser().resolve()
        if destination.exists() and not args.force:
            print(f"diagnostics output exists: {destination}; use --force to replace it", file=sys.stderr)
            return 2
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        os.replace(temporary, destination)
        print(f"Diagnostics written locally: {destination}")
        print("Review this report before sharing. It is generated locally and is not uploaded automatically.")
        return 0
    if args.json:
        print(text, end="")
    else:
        print(f"SkillLayer diagnostics: {result['product_version']}")
        print(f"  doctor: {result['doctor_summary']['overall_status']}")
        print(f"  MCP tools: {result['mcp_tool_count']}")
        print("  Review this report before sharing. It is generated locally and is not uploaded automatically.")
    return 0


def _mcp_tool_count() -> int:
    # Import lazily so base CLI installs retain a useful missing-MCP-dependency
    # failure mode instead of failing every command at import time.
    from .mcp_server import mcp_tool_count

    return mcp_tool_count()


def handle_mcp_config(args: argparse.Namespace) -> int:
    config = build_mcp_config(tool_count=_mcp_tool_count())
    text = render_config(config)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


def handle_mcp_config_check(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result: dict[str, Any] = {
            "ok": False,
            "error": f"invalid MCP config: {exc}",
            "remediation": "Generate a new config with: skilllayer mcp-config --output <config.json>",
        }
    else:
        result = validate_config(payload, tool_count_provider=_mcp_tool_count)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer MCP configuration")
        print(f"  ok: {result['ok']}")
        if result.get("remediation"):
            print(f"  remediation: {result['remediation']}")
    return 0 if result["ok"] else 1


def _fmt_estimate(value: Any) -> str:
    """Render a savings estimate so it can never be mistaken for a measurement.

    The JSON keeps the raw number plus an `estimated_savings_only` flag, but the
    human-readable output must label the number itself as an estimate.
    """
    return f"~{value} (estimated, not measured)"


def handle_stats(args: argparse.Namespace) -> int:
    result = summarize_telemetry()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer telemetry stats")
        print(f"  total_runs: {result['total_runs']}")
        print(f"  successful_runs: {result['successful_runs']}")
        print(f"  tool_invocations_total: {result['tool_invocations_total']}")
        print(f"  estimated_llm_calls_saved: {_fmt_estimate(result['estimated_llm_calls_saved'])}")
        print(f"  estimated_tool_steps_saved: {_fmt_estimate(result['estimated_tool_steps_saved'])}")
        print(f"  estimated_savings_only: {result['estimated_savings_only']}")
        print("  workflow_breakdown:")
        for workflow, count in result["workflow_breakdown"].items():
            print(f"    {workflow}: {count}")
        print("  tool_usage_breakdown:")
        for tool, count in result["tool_usage_breakdown"].items():
            print(f"    {tool}: {count}")
    return 0


def handle_analytics(args: argparse.Namespace) -> int:
    result = build_analytics_report()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        coverage = result["coverage"]
        adoption = result["adoption"]
        roi = result["roi"]
        misses = result["workflow_misses"]
        print("SkillLayer analytics")
        print(f"  total_tasks_seen: {coverage['total_tasks_seen']}")
        print(f"  coverage_percent: {coverage['coverage_percent']}")
        print(f"  execution_percent: {coverage['execution_percent']}")
        print(f"  workflow_adoption_percent: {adoption['workflow_adoption_percent']}")
        print(f"  fallback_percent: {adoption['fallback_percent']}")
        print(f"  estimated_llm_calls_saved: {_fmt_estimate(roi['estimated_llm_calls_saved'])}")
        print(f"  estimated_tool_steps_saved: {_fmt_estimate(roi['estimated_tool_steps_saved'])}")
        print(f"  estimated_file_reads_saved: {_fmt_estimate(roi['estimated_file_reads_saved'])}")
        print(f"  estimated_search_operations_saved: {_fmt_estimate(roi['estimated_search_operations_saved'])}")
        print(f"  estimated_savings_only: {roi['estimated_savings_only']}")
        print("  top_missing_categories:")
        for item in misses["top_missing_categories"]:
            print(f"    {item['category']}: {item['count']}")
        print("  top_workflows:")
        for item in result["workflow_usage"]["top_workflows"]:
            print(f"    {item['workflow']}: {item['count']}")
    return 0


def handle_benchmark(args: argparse.Namespace) -> int:
    result = run_benchmark()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        comparison = result["comparison"]
        print("SkillLayer cost benchmark")
        print(f"  task_count: {result['task_count']}")
        print(f"  baseline_success_rate: {comparison['baseline_success_rate']}")
        print(f"  skilllayer_success_rate: {comparison['skilllayer_success_rate']}")
        print(f"  duration_reduction_percent: {comparison['duration_reduction_percent']}")
        print(f"  file_read_reduction_percent: {comparison['file_read_reduction_percent']}")
        print(f"  search_reduction_percent: {comparison['search_reduction_percent']}")
        print(f"  tool_call_reduction_percent: {comparison['tool_call_reduction_percent']}")
        print("  proxy_metrics: true")
        print("  estimated_impact: true")
        print("  report: runs/p4_5_benchmark/benchmark_report.json")
    return 0


def handle_analyze_failures(args: argparse.Namespace) -> int:
    result = analyze_failures()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["failure_attribution_summary"]
        pareto = result["pareto_analysis"]
        print("SkillLayer benchmark failure attribution")
        print(f"  skilllayer_failures: {summary['skilllayer_failures']}")
        print(f"  primary_failure_mode: {summary['primary_failure_mode']}")
        print(f"  diagnostic_conclusion: {summary['diagnostic_conclusion']}")
        print("  root_cause_counts:")
        for cause, count in summary["root_cause_counts"].items():
            print(f"    {cause}: {count}")
        print("  top_failure_contributors:")
        for item in pareto["contributors"][:3]:
            print(f"    {item['workflow']}: {item['failures']} failures")
        print("  report: runs/p4_5_failure_analysis/failure_analysis_report.json")
    return 0


def handle_repair_workflows_report(args: argparse.Namespace) -> int:
    result = run_workflow_repair_report()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        comparison = summary["comparison"]
        print("SkillLayer workflow repair report")
        print(f"  before_skilllayer_success_rate: {summary['before_skilllayer_success_rate']}")
        print(f"  after_skilllayer_success_rate: {summary['after_skilllayer_success_rate']}")
        print(f"  target_met: {summary['target_met']}")
        print(f"  unsupported_after_repair: {summary['unsupported_after_repair']['count']}")
        print(f"  duration_reduction_delta: {comparison['duration_reduction_percent']['delta']}")
        print(f"  tool_call_reduction_delta: {comparison['tool_call_reduction_percent']['delta']}")
        print(f"  report: runs/p4_6_workflow_repair/report.json")
    return 0


def handle_optimize_cost_report(args: argparse.Namespace) -> int:
    result = run_cost_optimization_report()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        comparison = summary["comparison"]
        print("SkillLayer cost optimization report")
        print(f"  p4_6_1_skilllayer_success_rate: {summary['p4_6_1_skilllayer_success_rate']}")
        print(f"  success_rate_preserved: {summary['success_rate_preserved']}")
        print(f"  tool_call_proxy_improved_vs_p4_6: {summary['tool_call_proxy_improved_vs_p4_6']}")
        print(f"  duplicate_operation_count: {summary['duplicate_operation_count']}")
        print(f"  test_run_count: {summary['test_run_count']}")
        print(f"  tool_call_reduction_percent: {comparison['tool_call_reduction_percent']['p4_6_1']}")
        print(f"  duration_reduction_percent: {comparison['duration_reduction_percent']['p4_6_1']}")
        print("  report: runs/p4_6_1_cost_optimization/report.json")
    return 0


def handle_generalization_report(args: argparse.Namespace) -> int:
    result = run_generalization_validation()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print("SkillLayer generalization validation")
        print(f"  selected_repo_count: {summary['selected_repo_count']}")
        print(f"  selected_external_repo_count: {summary['selected_external_repo_count']}")
        print(f"  task_count: {summary['task_count']}")
        print(f"  overall_success_rate: {summary['overall_success_rate']}")
        print(f"  outside_ledgerlite_success_rate: {summary['outside_ledgerlite_success_rate']}")
        print(f"  outside_ledgerlite_above_85_percent: {summary['outside_ledgerlite_above_85_percent']}")
        print(f"  routing_changed: {summary['routing_changed']}")
        print(f"  workflows_changed: {summary['workflows_changed']}")
        if summary["limitations"]:
            print("  limitations:")
            for limitation in summary["limitations"]:
                print(f"    {limitation}")
        print("  report: runs/p4_7_generalization/report.json")
    return 0


def handle_packaging_audit(args: argparse.Namespace) -> int:
    result = run_packaging_audit()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["installability_summary"]
        print("SkillLayer packaging and installability audit")
        print(f"  package_compiles: {summary['package_compiles']}")
        print(f"  cli_commands_work: {summary['cli_commands_work']}")
        print(f"  mcp_server_imports: {summary['mcp_server_imports']}")
        print(f"  mcp_tool_schemas_listed: {summary['mcp_tool_schemas_listed']}")
        print(f"  fresh_install_completed: {summary['fresh_install_completed']}")
        print(f"  installable_for_new_user: {summary['installable_for_new_user']}")
        print(f"  stable_workflows: {', '.join(summary['stable_workflows'])}")
        print(f"  experimental_workflows: {', '.join(summary['experimental_workflows'])}")
        print(f"  internal_workflows: {', '.join(summary.get('internal_workflows', []))}")
        print("  report: runs/p5_1_packaging_audit/report.json")
    return 0


def handle_open_source_audit(args: argparse.Namespace) -> int:
    result = run_open_source_audit()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        secret_scan = result["secret_scan"]
        counts = result["file_classification_counts"]
        print("SkillLayer open-source readiness audit")
        print(f"  safe_to_publish_now: {result['safe_to_publish_now']}")
        print(f"  secret_findings: {secret_scan['finding_count']}")
        print(f"  secret_counts_by_severity: {secret_scan['counts_by_severity']}")
        print("  file_classification_counts:")
        for category, count in counts.items():
            print(f"    {category}: {count}")
        print("  top_5_risks:")
        for item in result["top_5_risks"]:
            print(f"    {item['risk']} [{item['severity']}]: {item['evidence']}")
        print("  report: runs/p5_2_open_source_audit/report.json")
    return 0


def handle_usage_report(args: argparse.Namespace) -> int:
    result = aggregate_usage()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer usage report")
        print(f"  total_events: {result['total_events']}")
        print(f"  total_input_tokens: {result['total_input_tokens']}")
        print(f"  total_output_tokens: {result['total_output_tokens']}")
        print(f"  total_cached_tokens: {result['total_cached_tokens']}")
        print(f"  total_reasoning_tokens: {result['total_reasoning_tokens']}")
        print(f"  total_tokens: {result['total_tokens']}")
        print(f"  unknown_usage_events: {result['unknown_usage_events']}")
        print("  provider_breakdown:")
        for provider, item in result["provider_breakdown"].items():
            print(f"    {provider}: {item['events']} events, {item['total_tokens']} tokens")
        print("  cost_calculation_included: false")
        print("  roi_calculation_included: false")
        print("  dollar_calculation_included: false")
        print("  report: runs/skilllayer_cost/usage_summary.json")
    return 0


def handle_cost_report(args: argparse.Namespace) -> int:
    result = aggregate_costs()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer cost report")
        print(f"  total_events: {result['total_events']}")
        print(f"  cost_known_events: {result['cost_known_events']}")
        print(f"  cost_unknown_events: {result['cost_unknown_events']}")
        print(f"  total_cost_usd: {result['total_cost_usd']}")
        print("  provider_cost_breakdown:")
        for provider, item in result["provider_cost_breakdown"].items():
            print(f"    {provider}: {item['cost_known_events']} known events, ${item['total_cost_usd']}")
        print("  cost_calculation_scope: observed_usage_only")
        print("  savings_calculation_included: false")
        print("  roi_calculation_included: false")
        print("  dollar_savings_calculation_included: false")
        print("  report: runs/skilllayer_cost/cost_summary.json")
    return 0


def handle_ab_benchmark(args: argparse.Namespace) -> int:
    result = run_ab_benchmark()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        comparison = result["comparison"]
        cost_per_success = result["cost_per_successful_task"]
        print("SkillLayer controlled A/B cost benchmark")
        print(f"  task_count: {result['task_count']}")
        print(f"  baseline_success_rate: {comparison['baseline_success_rate']}")
        print(f"  skilllayer_success_rate: {comparison['skilllayer_success_rate']}")
        print(f"  cost_comparison_possible: {comparison['cost_comparison_possible']}")
        print(f"  token_comparison_possible: {comparison['token_comparison_possible']}")
        print(f"  honest_outcome: {comparison['honest_outcome']}")
        print(f"  baseline_total_cost_usd: {comparison['baseline_total_cost_usd']}")
        print(f"  skilllayer_total_cost_usd: {comparison['skilllayer_total_cost_usd']}")
        print(f"  baseline_total_tokens: {comparison['baseline_total_tokens']}")
        print(f"  skilllayer_total_tokens: {comparison['skilllayer_total_tokens']}")
        print(f"  duration_difference_percent: {comparison['duration_difference_percent']}")
        print(f"  cost_per_successful_task_baseline: {cost_per_success['cost_per_successful_task_baseline']}")
        print(f"  cost_per_successful_task_skilllayer: {cost_per_success['cost_per_successful_task_skilllayer']}")
        print("  missing_token_counts_inferred: false")
        print("  savings_estimated: false")
        print("  roi_calculated: false")
        print("  report: runs/p5_6_3_ab_benchmark/report.json")
    return 0


def handle_validate_live_telemetry(args: argparse.Namespace) -> int:
    result = run_live_provider_validation()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        readiness = result["readiness_summary"]
        health = result["pipeline_health"]
        telemetry = result["telemetry_completeness"]
        print("SkillLayer live provider telemetry validation")
        print(f"  readiness: {readiness['readiness']}")
        print(f"  usage_capture_ok: {health['usage_capture_ok']}")
        print(f"  adapter_ok: {health['adapter_ok']}")
        print(f"  pricing_ok: {health['pricing_ok']}")
        print(f"  cost_calculation_ok: {health['cost_calculation_ok']}")
        print(f"  cost_comparison_ready: {health['cost_comparison_ready']}")
        print(f"  real_provider_event_count: {telemetry['real_provider_event_count']}")
        print(f"  raw_provider_payload_count: {telemetry['raw_provider_payload_count']}")
        print(f"  cost_calculable_count: {telemetry['cost_calculable_count']}")
        print("  missing_requirements:")
        for item in health["missing_requirements"]:
            print(f"    {item}")
        print("  provider_payloads_fabricated: false")
        print("  cost_savings_claimed: false")
        print("  report: runs/p5_6_4_live_provider_validation/report.json")
    return 0


def handle_demand_report(args: argparse.Namespace) -> int:
    result = build_demand_report()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["demand_summary"]
        opportunities = result["opportunity_ranking"]["opportunities"]
        print("SkillLayer workflow demand report")
        print(f"  total_events: {summary['total_events']}")
        print(f"  recorded_demand_events: {summary['recorded_demand_events']}")
        print(f"  telemetry_derived_events: {summary['telemetry_derived_events']}")
        print(f"  workflow_found_count: {summary['workflow_found_count']}")
        print(f"  workflow_missing_count: {summary['workflow_missing_count']}")
        print(f"  workflow_executed_count: {summary['workflow_executed_count']}")
        print(f"  data_volume_level: {summary['data_volume_level']}")
        print(f"  ranking_reliable: {summary['ranking_reliable']}")
        print(f"  opportunity_ranking_status: {summary['opportunity_ranking_status']}")
        print(f"  use_for_roadmap_decisions: {summary['use_for_roadmap_decisions']}")
        print("  warnings:")
        for warning in summary["warnings"]:
            print(f"    {warning}")
        print("  top_categories:")
        for item in summary["top_categories"][:5]:
            print(f"    {item['category']}: {item['count']}")
        print("  top_missing_categories:")
        for item in summary["top_missing_categories"][:5]:
            print(f"    {item['category']}: {item['count']}")
        print("  top_opportunities:")
        for item in opportunities[:5]:
            print(f"    {item['candidate_workflow']}: {item['opportunity_score']}")
        print("  llm_used: false")
        print("  embeddings_used: false")
        print("  workflow_generation_included: false")
        print("  report: runs/p6_0_workflow_demand/report.json")
    return 0


def handle_telemetry_export(args: argparse.Namespace) -> int:
    result = export_anonymous_telemetry(
        output=args.output,
        include_raw_tasks=bool(args.include_raw_tasks),
        include_local_paths=bool(args.include_local_paths),
        allow_unsafe=bool(args.allow_unsafe),
    )
    if not result["success"]:
        print("SkillLayer anonymous telemetry export failed privacy scan.", file=sys.stderr)
        print(f"  error: {result['error']}", file=sys.stderr)
        print(f"  findings: {result['privacy_scan']['finding_count']}", file=sys.stderr)
        for finding in result["privacy_scan"].get("findings", []):
            print(f"    {finding['pattern_type']}: {finding['count']}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["success"]:
            print("SkillLayer anonymous telemetry export")
            print(f"  export_written: {result['export_written']}")
            print(f"  output_path: {result['output_path']}")
            print(f"  sources_included: {', '.join(result['sources_included']) or 'none'}")
            print(f"  sources_missing: {', '.join(result['sources_missing']) or 'none'}")
            print(f"  privacy_scan_passed: {result['privacy_scan']['passed']}")
            print(f"  findings: {result['privacy_scan']['finding_count']}")
            export_summary = result.get("export_summary", {})
            print("  exported_counts:")
            print(f"    workflow_runs: {export_summary.get('workflow_runs_exported', 0)}")
            print(f"    cli_commands: {export_summary.get('cli_commands_exported', 0)}")
            print(f"    demand_events: {export_summary.get('demand_events_exported', 0)}")
            print(f"    usage_events: {export_summary.get('usage_events_exported', 0)}")
            if export_summary.get("cli_activity_present_without_workflow_runs"):
                print("  note: No workflow runs found, but CLI activity was recorded.")
            print("  redactions:")
            for key, value in result["sanitization_report"].items():
                print(f"    {key}: {value}")
            print("  automatic_upload: false")
            print("  no_network_requests: true")
            print("  reminder: review the export before sharing it.")
    return 0 if result["success"] else 1


def handle_review_bundle(args: argparse.Namespace) -> int:
    result = review_telemetry_bundle(args.directory)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["bundle_summary"]
        validation = result["validation_report"]
        workflow = result["workflow_summary"]
        demand = result["demand_summary"]
        client = result["client_summary"]
        print("SkillLayer telemetry bundle review")
        print(f"  input_directory: {summary['input_directory']}")
        print(f"  total_exports: {summary['total_exports']}")
        print(f"  valid_export_count: {validation['valid_export_count']}")
        print(f"  invalid_export_count: {validation['invalid_export_count']}")
        print(f"  total_events: {summary['total_events']}")
        print(f"  data_volume_level: {summary['data_volume_level']}")
        print(f"  use_for_roadmap_decisions: {summary['use_for_roadmap_decisions']}")
        print("  warnings:")
        for warning in summary["warnings"]:
            print(f"    {warning}")
        print("  top_workflows:")
        for item in workflow["top_workflows"][:5]:
            print(f"    {item['workflow']}: {item['count']}")
        print("  top_missing_categories:")
        for item in demand["top_missing_categories"][:5]:
            print(f"    {item['category']}: {item['count']}")
        print("  client_distribution:")
        for key, value in client["client_distribution"].items():
            print(f"    {key}: {value}")
        print("  no_network_requests: true")
        print("  automatic_upload: false")
        print("  opportunity_ranking_generated: false")
        print("  roadmap_recommendations_generated: false")
        print("  report: runs/p6_2_bundle_review/report.json")
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
        print(f"  telemetry_export_command_works: {summary['telemetry_export_command_works']}")
        print("  no_network_requests: true")
        print("  automatic_upload: false")
        print("  automatic_telemetry: false")
        print("  report: runs/tester_check/report.json")
    return 0 if result["summary"]["ok"] else 1


def handle_cursor_mcp_validation(args: argparse.Namespace) -> int:
    result = run_cursor_mcp_validation()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("SkillLayer Cursor MCP validation")
        print(f"  success: {result['success']}")
        print(f"  cursor_support_status: {result['cursor_support_status']}")
        print(f"  cursor_ui_tool_discovery_validated: {result['cursor_ui_tool_discovery_validated']}")
        print("  validation_scope:")
        for item in result["validation_scope"]:
            print(f"    {item}")
        print("  not_validated:")
        for item in result["not_validated"]:
            print(f"    {item}")
        print("  report: runs/p7_5_cursor_validation/report.json")
    return 0 if result["success"] else 1


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
        print("  report: runs/p9_0_claude_code_tester_prep/report.json")
    return 0 if result["success"] else 1


def handle_run_tests_smoke(args: argparse.Namespace) -> int:
    result = run_tests_real_repo_smoke()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print("SkillLayer RunTestsWorkflow real repo smoke")
        print(f"  success: {result['success']}")
        print(f"  executed_repo_count: {summary['executed_repo_count']}")
        print(f"  skipped_repo_count: {summary['skipped_repo_count']}")
        print(f"  pytest_passing_ok: {summary['pytest_passing_ok']}")
        print(f"  pytest_failing_ok: {summary['pytest_failing_ok']}")
        print(f"  no_test_repo_ok: {summary['no_test_repo_ok']}")
        print(f"  node_passing_ok: {summary['node_passing_ok']}")
        print(f"  node_failing_ok: {summary['node_failing_ok']}")
        print(f"  read_only_confirmed: {summary['read_only_confirmed']}")
        print(f"  human_output_smoke_passed: {summary['human_output_smoke_passed']}")
        if result["limitations"]:
            print("  limitations:")
            for item in result["limitations"]:
                print(f"    {item}")
        print("  report: runs/p8_1_1_run_tests_real_repo_smoke/report.json")
    return 0 if result["success"] else 1


def handle_fix_tests_smoke(args: argparse.Namespace) -> int:
    from .fix_failing_test_real_repo_smoke import run_smoke as fix_failing_test_real_repo_smoke

    result = fix_failing_test_real_repo_smoke()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print("SkillLayer FixFailingTestWorkflow v2 real repo smoke")
        print(f"  success: {result['success']}")
        print(f"  executed_repo_count: {summary['executed_repo_count']}")
        print(f"  skipped_repo_count: {summary['skipped_repo_count']}")
        print(f"  supported_repairs_passed: {summary['supported_repairs_passed']}")
        print(f"  unsupported_cases_safe: {summary['unsupported_cases_safe']}")
        print(f"  dry_run_safe: {summary['dry_run_safe']}")
        print(f"  patch_scope_valid: {summary['patch_scope_valid']}")
        print(f"  human_output_useful: {summary['human_output_useful']}")
        print(f"  regressions_passed: {summary['regressions_passed']}")
        print(f"  llm_calls_used: {summary['llm_calls_used']}")
        if result["limitations"]:
            print("  limitations:")
            for item in result["limitations"]:
                print(f"    {item}")
        print("  report: runs/p8_3_1_fix_failing_test_real_repo_smoke/report.json")
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
        print("  report: runs/p9_1_security_review_kit/report.json")
    return 0 if result["success"] else 1


def handle_release_check(args: argparse.Namespace) -> int:
    result = run_release_check(args.repo)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Release Validation Summary")
        print(f"Code Validation: {result['code_validation']['status']}")
        print(f"Environment Validation: {result['environment_validation']['status']}")
        print(f"Documentation Validation: {result['documentation_validation']['status']}")
        print(f"Repository Hygiene: {result['repository_hygiene']['status']}")
        print(f"Public Export Validation: {result['public_export_validation']['status']}")
        print("")
        print(f"READY_FOR_PUBLIC_EXPORT = {str(result['ready_for_public_export']).lower()}")
        if result["errors"]:
            print("")
            print("Actionable failures:")
            for error in result["errors"][:20]:
                print(f"  - {error}")
            if len(result["errors"]) > 20:
                print(f"  ... {len(result['errors']) - 20} more")
        if result["warnings"]:
            print("")
            print("Warnings:")
            for warning in result["warnings"][:10]:
                print(f"  - {warning}")
            if len(result["warnings"]) > 10:
                print(f"  ... {len(result['warnings']) - 10} more")
        print("")
        print("Read-only: true")
        print("Push/commit/tag: not performed")
    return 0 if result["ready_for_public_export"] else 1


def handle_token_benchmark(args: argparse.Namespace) -> int:
    result = run_token_savings_benchmark(args.repo)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        summary = result["benchmark_summary"]
        print("SkillLayer Token Savings Benchmark")
        print(f"  repo_path: {result['repo_path']}")
        print(f"  scenarios: {summary['scenario_count']}")
        print(f"  average_step_reduction: {summary['average_step_reduction']}%")
        print(f"  total_step_reduction_percent: {summary['total_step_reduction_percent']}%")
        print(f"  average_estimated_context_reduction: {summary['average_estimated_context_reduction']}%")
        print(f"  workflow_success_rate: {summary['workflow_success_rate']}")
        print(f"  best_case: {summary['best_case']} ({summary['best_case_reduction_percent']}%)")
        print(f"  worst_case: {summary['worst_case']} ({summary['worst_case_reduction_percent']}%)")
        print("")
        print("Scenario results:")
        for item in result["scenario_results"]:
            print(
                "  "
                f"{item['scenario']}: "
                f"baseline_steps={item['baseline_steps']} "
                f"skilllayer_steps={item['skilllayer_steps']} "
                f"actions_saved={item['actions_saved']} "
                f"reduction={item['reduction_percent']}% "
                f"success={item['workflow_success']}"
            )
        print("")
        print("Interpretation: estimated proxy metrics only; this does not prove real token savings.")
        print("External Claude Code/Cursor validation is still required.")
        print("Report: runs/p12_0_token_savings_benchmark/report.json")
    return 0 if result["success"] else 1


def handle_benchmark_automation(args: argparse.Namespace) -> int:
    result = run_benchmark_automation(args.matrix)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        metrics = result["aggregate_metrics"]
        health = result["benchmark_health"]
        print("SkillLayer Benchmark Automation")
        print(f"  matrix_path: {result['matrix_path']}")
        print(f"  repositories_tested: {health['repositories_tested']}")
        print(f"  successful_benchmarks: {health['successful_benchmarks']}")
        print(f"  failed_benchmarks: {health['failed_benchmarks']}")
        print(f"  scenario_count: {metrics['scenario_count']}")
        print(f"  average_action_reduction: {metrics['average_action_reduction']}%")
        print(f"  median_action_reduction: {metrics['median_action_reduction']}%")
        print(f"  workflow_success_rate: {metrics['workflow_success_rate']}")
        print(f"  best_repo: {format_repo_score(metrics['best_repo'])}")
        print(f"  worst_repo: {format_repo_score(metrics['worst_repo'])}")
        if health["failed_repositories"]:
            print("")
            print("Failed repositories:")
            for item in health["failed_repositories"]:
                print(f"  - {item['name']}: {item['failure_reason']}")
        print("")
        print("Interpretation: estimated proxy metrics only; this does not prove real token savings.")
        print("No repositories are cloned and no network access is required.")
        print("Report: runs/p12_1_benchmark_automation/report.json")
    return 0 if result["success"] else 1


def format_repo_score(item: dict[str, Any] | None) -> str:
    if not item:
        return "n/a"
    return f"{item['name']} ({item['average_action_reduction']}%)"


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
        "No .skilllayer/ memory store found. Save a context snapshot first "
        "using the MCP tool 'skilllayer_save_context' (the CLI cannot create "
        "write operations — see docs/CLAUDE_CODE_SETUP.md for MCP setup)."
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


# Workflows that render their own detailed human-readable block below. Any
# workflow NOT listed here falls back to printing its `summary` artifact so that
# human-mode output is never just a success flag plus a "use --json" tip.
_WORKFLOWS_WITH_DETAILED_HUMAN_OUTPUT = frozenset({
    "ClarifyIntentWorkflow",
    "AddHelperWorkflow",
    "BrowserSmokeWorkflow",
    "CheckPortAvailabilityWorkflow",
    "DependencyCheckWorkflow",
    "DetectRepoActivityWorkflow",
    "WatchFileChangesWorkflow",
    "DetectRunningProcessesWorkflow",
    "DetectSecretPatternsWorkflow",
    "ExplainFailureWorkflow",
    "FindFunctionWorkflow",
    "FindMergeConflictsWorkflow",
    "FixFailingTestWorkflow",
    "GetCommitDetailsWorkflow",
    "GetFileHistoryWorkflow",
    "GitBlameWorkflow",
    "GitDiffWorkflow",
    "GitLogWorkflow",
    "GitStatusWorkflow",
    "InspectRuntimeEnvironmentWorkflow",
    "ListBranchesWorkflow",
    "MeasureMemoryUsageWorkflow",
    "MeasureTestSuiteSpeedWorkflow",
    "MonitorTestFlakinessWorkflow",
    "ProfileCodeExecutionWorkflow",
    "RenameSymbolWorkflow",
    "RunTestsWorkflow",
    "SearchWorkflow",
    "SingleTestWorkflow",
    "WatchDependencyUpdatesWorkflow",
    # Memory workflows render their own summary via a dedicated block below.
    "SaveContextSnapshotWorkflow",
    "TrackDecisionLogWorkflow",
    "RememberUserPreferencesWorkflow",
    "RehydrateContextWorkflow",
    "CompareContextSnapshotsWorkflow",
    "SearchDecisionLogWorkflow",
    "AddTodoWorkflow",
    "MarkTodoDoneWorkflow",
    "ListTodosWorkflow",
})


# Memory workflows reachable via `skilllayer run` carry the shared
# healthy/warning/broken/error result contract (see finalize_memory_result in
# memory/skilllayer_memory.py). In most of these, top-level "status" IS that
# health word. In the other three, "status" is a domain field (the decision's
# own proposed/accepted/... state, or the touched todo's own open/done state)
# and the health word must be derived from error_code instead — never printed
# under the same label as the domain value, to avoid the same collision the
# result contract itself works around.
_MEMORY_WORKFLOWS_HEALTH_STATUS_FIELD = frozenset({
    "SaveContextSnapshotWorkflow",
    "RememberUserPreferencesWorkflow",
    "RehydrateContextWorkflow",
    "CompareContextSnapshotsWorkflow",
    "SearchDecisionLogWorkflow",
    "ListTodosWorkflow",
})
_MEMORY_WORKFLOWS_STATUS_IS_DOMAIN_FIELD = frozenset({
    "TrackDecisionLogWorkflow",
    "AddTodoWorkflow",
    "MarkTodoDoneWorkflow",
})
_MEMORY_STATUS_LABELS = {
    "healthy": "SUCCESS",
    "warning": "WARNING",
    "broken": "BROKEN",
    "error": "ERROR",
}


def print_human_run(result: dict[str, Any], verbose: bool = False) -> None:
    print("SkillLayer run summary")
    print(f"  success: {result['success']}")
    workflow_name = result.get("workflow")
    if workflow_name in _MEMORY_WORKFLOWS_HEALTH_STATUS_FIELD:
        health_status = result.get("status") or status_for_error_code(result.get("error_code"))
        print(f"  status: {_MEMORY_STATUS_LABELS.get(health_status, health_status)}")
        for warning_text in result.get("warnings") or []:
            print(f"  warning: {warning_text}")
    elif workflow_name in _MEMORY_WORKFLOWS_STATUS_IS_DOMAIN_FIELD:
        health_status = status_for_error_code(result.get("error_code"))
        print(f"  status: {_MEMORY_STATUS_LABELS.get(health_status, health_status)}")
        for warning_text in result.get("warnings") or []:
            print(f"  warning: {warning_text}")
    print(f"  workflow: {result['workflow']}")
    print(f"  macro_sequence: {' -> '.join(result['macro_sequence'])}")
    print(f"  validation: {format_validation_status(str(result.get('validation_status', 'not_applicable')))}")
    print(f"  tests_run: {result.get('tests_run', False)}")
    print(f"  tests_passed: {result.get('tests_passed')}")
    print(f"  dry_run: {result['dry_run']}")
    print(f"  tool_calls: {result['tool_calls']}")
    print(f"  llm_calls: {result['llm_calls']}")
    print(f"  logs_path: {result['logs_path']}")
    if result.get("write_behavior"):
        print(f"  write_behavior: {result['write_behavior']}")
    if result.get("requires_write_consent"):
        performed = bool(result.get("state_write_performed") or result.get("written_paths"))
        print(f"  persistent_state_write: {'performed with explicit consent' if performed else 'not performed; explicit consent required'}")
    written_paths = list(result.get("written_paths") or [])
    if written_paths:
        print("  written_paths:")
        for path in written_paths:
            print(f"    - {path}")
    deleted_paths = list(result.get("deleted_paths") or [])
    if deleted_paths:
        print("  deleted_paths:")
        for path in deleted_paths:
            print(f"    - {path}")
    gitignore_suggestions = list(result.get("gitignore_suggestions") or [])
    if gitignore_suggestions:
        print("  gitignore_suggestions (not applied automatically):")
        for path in gitignore_suggestions:
            print(f"    - {path}")
    if result.get("workflow") == "ClarifyIntentWorkflow":
        if result.get("summary"):
            print(f"  summary: {result.get('summary')}")
        suggestions = list(result.get("suggestions", []) or [])
        if suggestions:
            print("  did you mean:")
            for suggestion in suggestions:
                print(f"    - {suggestion}")
    if result.get("workflow") == "BrowserSmokeWorkflow":
        print(f"  browser_executed: {result.get('browser_executed')}")
        print(f"  backend_used: {result.get('backend_used') or result.get('browser_backend')}")
        if not result.get("browser_executed"):
            print(f"  status: {result.get('status')}")
            if result.get("error_code"):
                print(f"  error_code: {result.get('error_code')}")
            for limitation in result.get("limitations") or []:
                print(f"  limitation: {limitation}")
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
        if "status" in result:
            print(f"  status: {result.get('status')}")
        if "scan_complete" in result:
            print(f"  scan_complete: {result.get('scan_complete')}")
        skipped_reasons = result.get("skipped_reasons") or {}
        if skipped_reasons:
            reasons_str = ", ".join(f"{reason}={count}" for reason, count in skipped_reasons.items())
            print(f"  skipped_reasons: {reasons_str}")
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
        unsup = result.get("unsupported_count", 0)
        print(f"  package_manager: {pm}  outdated: {out}  up_to_date: {up}  unknown: {unk}  unsupported: {unsup}")
        if "complete" in result:
            print(f"  complete: {result.get('complete')}")
        for dep in (result.get("dependencies") or []):
            bump = ""
            if dep.get("major_bump"):
                bump = " [MAJOR]"
            elif dep.get("minor_bump"):
                bump = " [minor]"
            elif dep.get("patch_bump"):
                bump = " [patch]"
            latest = dep.get("latest_version") or "?"
            status = dep.get("status") or ("OUTDATED" if dep.get("outdated") else ("unknown" if dep.get("latest_version") is None else "ok"))
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
    if result.get("workflow") == "WatchFileChangesWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            summary = result.get("summary") or {}
            print(f"  scope: {result.get('scope')}  first_watch: {result.get('first_watch', False)}  "
                  f"scanned: {summary.get('scanned_file_count', 0)}")
            print(f"  added: {summary.get('added_count', 0)}  "
                  f"modified: {summary.get('modified_count', 0)}  "
                  f"deleted: {summary.get('deleted_count', 0)}")
            for a in (result.get("added") or [])[:10]:
                print(f"  added     {a.get('path')}  ({a.get('size')} bytes)")
            for m in (result.get("modified") or [])[:10]:
                print(f"  modified  {m.get('path')}  {m.get('size_before')} -> {m.get('size_after')} bytes")
            for d in (result.get("deleted") or [])[:10]:
                print(f"  deleted   {d.get('path')}")
        if result.get("checked_at"):
            print(f"  checked_at: {result['checked_at']}")
    if result.get("workflow") == "CompareContextSnapshotsWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            from_s = result.get("from_snapshot") or {}
            to_s = result.get("to_snapshot") or {}
            print(f"  from: {from_s.get('id')} ({from_s.get('created')})  to: {to_s.get('id')} ({to_s.get('created')})")
            state_diff = result.get("state_diff") or {}
            print(f"  state_changed: {state_diff.get('changed', False)}  "
                  f"words: {state_diff.get('word_count_before', 0)} -> {state_diff.get('word_count_after', 0)}")
            oq = result.get("open_questions_diff") or {}
            print(f"  open_questions: {oq.get('resolved_count', 0)} resolved  "
                  f"{oq.get('new_count', 0)} new  {oq.get('still_open_count', 0)} still open")
            for q in (oq.get("resolved") or [])[:5]:
                print(f"  resolved:   {q}")
            for q in (oq.get("new") or [])[:5]:
                print(f"  new:        {q}")
    if result.get("workflow") == "SearchDecisionLogWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  query: {result.get('query')!r}  fields: {', '.join(result.get('fields_searched') or [])}")
            print(f"  total_matches: {result.get('total_matches', 0)}  superseded: {result.get('superseded_count', 0)}")
            for r in (result.get("results") or [])[:10]:
                flag = f" [SUPERSEDED -> {r.get('current_decision_id')}]" if r.get("status") == "superseded" else ""
                print(f"  {r.get('decision_id')}  {r.get('title')}  ({', '.join(r.get('matched_fields') or [])}){flag}")
    if result.get("workflow") == "AddTodoWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  todo_id: {result.get('todo_id')}  status: {result.get('status')}")
            print(f"  text: {result.get('text')!r}")
    if result.get("workflow") == "MarkTodoDoneWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  todo_id: {result.get('todo_id')}  status: {result.get('previous_status')} -> {result.get('status')}")
    if result.get("workflow") == "ListTodosWorkflow":
        if result.get("error_code"):
            print(f"  error: {result.get('error')}")
        else:
            print(f"  filter: {result.get('filter_status')}  open: {result.get('open_count', 0)}  "
                  f"done: {result.get('done_count', 0)}  total: {result.get('total_count', 0)}")
            for t in (result.get("todos") or [])[:20]:
                print(f"  {t.get('id')}  [{t.get('status')}]  {t.get('text')}")
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
        if result.get("status") == "unsupported":
            print(f"  status: unsupported  error_code: {result.get('error_code')}")
            print(f"  error: {result.get('error')}")
        total = result.get("total_count", 0)
        print(f"  total_count: {total} process(es)")
        if "skipped_count" in result:
            print(f"  skipped_count: {result.get('skipped_count')}  permission_limited: {result.get('permission_limited')}")
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
        elif result.get("error_code"):
            print(f"  error_code: {result.get('error_code')}")
            print(f"  error: {result.get('error')}")
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
    # Workflows without a dedicated block above must still show their summary
    # artifact, so human-mode output is never just a bare success flag + tip.
    if (
        result.get("workflow") not in _WORKFLOWS_WITH_DETAILED_HUMAN_OUTPUT
        and result.get("summary")
    ):
        print(f"  summary: {result['summary']}")
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
        "backend_used",
        "browser_executed",
        "static_inspection_performed",
        "limitations",
        "url",
        "error",
        "artifacts",
        "suggestions",
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
        "inspected_count",
        "skipped_count",
        "permission_limited",
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
        "scanned_bytes",
        "skipped_files",
        "skipped_bytes",
        "skipped_reasons",
        "scan_complete",
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
        "checked_count",
        "outdated_count",
        "up_to_date_count",
        "unknown_count",
        "unsupported_count",
        "complete",
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
        "write_behavior",
        "state_locations",
        "requires_write_consent",
        "may_dirty_worktree",
        "written_paths",
        "deleted_paths",
        "state_write_performed",
        "state_write_error",
        "persist_baseline",
        "persist_snapshot",
        "gitignore_suggestions",
        "run_log_written_paths",
        "automatic_state_written_paths",
        "artifact_writes_enabled",
        "external_side_effects_skipped",
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
    if not automatic_telemetry_enabled():
        return
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
    if not automatic_telemetry_enabled():
        return
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
    # Check the running interpreter's OWN environment (the venv, when invoked as
    # `python -m skilllayer`), not a system pytest on PATH. RunTests executes
    # `sys.executable -m pytest`, so whether *this* interpreter can import pytest
    # is what actually matters — a pytest on PATH from some other environment is
    # a false green light and must not count.
    return module_available("pytest")


def is_writable_dir(path: Path) -> bool:
    """Best-effort writability check that never creates a probe or directory."""
    candidate = path.expanduser()
    if candidate.exists():
        return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)
    parent = candidate.parent
    while parent != parent.parent and not parent.exists():
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


if __name__ == "__main__":
    raise SystemExit(main())
