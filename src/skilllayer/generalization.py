from __future__ import annotations

import ast
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SkillLayer
from .benchmark import count_trace_operations
from .tools import inspect_repo
from .tools.inspect import is_ignored


OUTPUT_DIR = Path("runs/p4_7_generalization")
WORKDIR = OUTPUT_DIR / "workdirs"
LOG_DIR = OUTPUT_DIR / "logs"
LEDGERLITE_REPO = Path("runs/a3_2_repo")


@dataclass(frozen=True)
class RepoCandidate:
    repo_id: str
    path: Path
    origin: str
    generated_by_skilllayer: bool = False


def run_generalization_validation() -> dict[str, Any]:
    """Run without changing routing, workflows, or benchmark tasks."""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    candidates = discover_repo_candidates()
    inventories = [build_repo_inventory(candidate) for candidate in candidates]
    selected = [item for item in inventories if item["selected"]]
    task_catalog = build_task_catalog(selected)
    repo_results = run_repo_tasks(selected, task_catalog)
    comparison = build_cross_repo_comparison(inventories, repo_results)
    summary = build_generalization_summary(inventories, task_catalog, repo_results, comparison)
    report = build_report(summary, comparison)

    outputs = {
        "repo_inventory.json": inventories,
        "task_catalog.json": task_catalog,
        "repo_results.json": repo_results,
        "generalization_summary.json": summary,
        "cross_repo_comparison.json": comparison,
        "report.json": report,
    }
    for filename, payload in outputs.items():
        write_json(OUTPUT_DIR / filename, payload)
    return report


def discover_repo_candidates() -> list[RepoCandidate]:
    candidates = [
        RepoCandidate("ledgerlite", LEDGERLITE_REPO, "controlled_baseline", generated_by_skilllayer=True),
    ]
    extra = os.environ.get("SKILLLAYER_GENERALIZATION_REPOS", "").strip()
    if extra:
        for index, raw_path in enumerate(extra.split(os.pathsep), start=1):
            path = Path(raw_path).expanduser()
            if path:
                candidates.append(RepoCandidate(f"external_env_{index}", path, "external_env"))
    return dedupe_candidates(candidates)


def dedupe_candidates(candidates: list[RepoCandidate]) -> list[RepoCandidate]:
    seen: set[Path] = set()
    result = []
    for candidate in candidates:
        resolved = candidate.path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    return result


def build_repo_inventory(candidate: RepoCandidate) -> dict[str, Any]:
    repo = candidate.path.expanduser()
    if not repo.exists():
        return {
            "repo_id": candidate.repo_id,
            "repo_path": str(repo),
            "origin": candidate.origin,
            "generated_by_skilllayer": candidate.generated_by_skilllayer,
            "selected": False,
            "suitability": "missing",
            "rejection_reasons": ["repo_path_missing"],
        }

    try:
        inventory = inspect_repo(repo)
    except Exception as exc:
        return {
            "repo_id": candidate.repo_id,
            "repo_path": str(repo),
            "origin": candidate.origin,
            "generated_by_skilllayer": candidate.generated_by_skilllayer,
            "selected": False,
            "suitability": "inspection_failed",
            "rejection_reasons": [f"inspection_failed: {exc}"],
        }

    reasons = rejection_reasons(inventory, candidate)
    selected = not reasons
    warning_reasons = soft_warnings(inventory, candidate)
    return {
        "repo_id": candidate.repo_id,
        "repo_path": inventory["repo_path"],
        "origin": candidate.origin,
        "generated_by_skilllayer": candidate.generated_by_skilllayer,
        "selected": selected,
        "suitability": "selected" if selected else "rejected",
        "rejection_reasons": reasons,
        "warnings": warning_reasons,
        "file_count": inventory["file_count"],
        "python_file_count": inventory["python_file_count"],
        "loc_estimate": inventory["loc_estimate"],
        "test_file_count": len(inventory["test_files"]),
        "test_files": inventory["test_files"],
        "detected_test_command": inventory["detected_test_command"],
    }


def rejection_reasons(inventory: dict[str, Any], candidate: RepoCandidate) -> list[str]:
    if candidate.generated_by_skilllayer:
        if Path(inventory["repo_path"]).exists():
            return []
        return ["ledgerlite_baseline_missing"]

    reasons = []
    if inventory["loc_estimate"] < 500:
        reasons.append("loc_below_500")
    if inventory["loc_estimate"] > 5000:
        reasons.append("loc_above_5000")
    if inventory["python_file_count"] < 2:
        reasons.append("not_multiple_python_modules")
    if len(inventory["test_files"]) < 1:
        reasons.append("no_tests_detected")
    if not inventory["detected_test_command"]:
        reasons.append("no_runnable_pytest_or_unittest_command_detected")
    return reasons


def soft_warnings(inventory: dict[str, Any], candidate: RepoCandidate) -> list[str]:
    warnings = []
    if not candidate.generated_by_skilllayer and inventory["python_file_count"] < 5:
        warnings.append("very_small_external_repo")
    if not candidate.generated_by_skilllayer and len(inventory["test_files"]) == 1:
        warnings.append("single_test_file")
    return warnings


def build_task_catalog(selected_repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for repo in selected_repos:
        repo_path = Path(repo["repo_path"])
        functions = discover_functions(repo_path)
        repo_tasks: list[dict[str, Any]] = []
        for index, function in enumerate(functions[:2], start=1):
            repo_tasks.append(
                {
                    "task_id": f"{repo['repo_id']}_find_{index:02d}",
                    "repo_id": repo["repo_id"],
                    "task_type": "FindFunctionWorkflow",
                    "workflow": "FindFunctionWorkflow",
                    "description": f"Find function {function['name']}",
                    "symbol": function["name"],
                    "source_file": function["file"],
                    "supported_workflow_only": True,
                }
            )

        for index, function in enumerate(rename_candidates(functions)[:2], start=1):
            new_symbol = f"{function['name']}_p47"
            repo_tasks.append(
                {
                    "task_id": f"{repo['repo_id']}_rename_{index:02d}",
                    "repo_id": repo["repo_id"],
                    "task_type": "RenameSymbolWorkflow",
                    "workflow": "RenameSymbolWorkflow",
                    "description": f"Rename {function['name']} to {new_symbol}",
                    "old_symbol": function["name"],
                    "new_symbol": new_symbol,
                    "source_file": function["file"],
                    "supported_workflow_only": True,
                }
            )

        helper_name = f"p47_{repo['repo_id']}_helper"
        repo_tasks.append(
            {
                "task_id": f"{repo['repo_id']}_helper_01",
                "repo_id": repo["repo_id"],
                "task_type": "AddHelperWorkflow",
                "workflow": "AddHelperWorkflow",
                "description": f"Add helper function {helper_name}",
                "helper_name": helper_name,
                "supported_workflow_only": True,
            }
        )

        browser_task = make_browser_task(repo_path, repo["repo_id"])
        if browser_task:
            repo_tasks.append(browser_task)

        if not repo_tasks:
            repo_tasks.append(
                {
                    "task_id": f"{repo['repo_id']}_no_supported_tasks",
                    "repo_id": repo["repo_id"],
                    "task_type": "Skipped",
                    "workflow": None,
                    "description": "No supported workflow task could be generated from repository structure.",
                    "skip_reason": "no_supported_task_candidates",
                    "supported_workflow_only": True,
                }
            )
        tasks.extend(repo_tasks)
    return tasks


def discover_functions(repo_path: Path) -> list[dict[str, str]]:
    functions: list[dict[str, str]] = []
    for path in sorted(repo_path.rglob("*.py")):
        relative = path.relative_to(repo_path)
        if is_ignored(relative) or is_test_path(relative):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                functions.append({"name": node.name, "file": str(relative), "line": str(node.lineno)})
    return functions


def rename_candidates(functions: list[dict[str, str]]) -> list[dict[str, str]]:
    blocked = {"main", "run", "test", "setup"}
    return [function for function in functions if function["name"] not in blocked and not function["name"].startswith("test_")]


def make_browser_task(repo_path: Path, repo_id: str) -> dict[str, Any] | None:
    for candidate in [repo_path / "index.html", repo_path / "public" / "index.html", repo_path / "dist" / "index.html"]:
        if candidate.exists():
            url = "file://" + str(candidate.resolve())
            return {
                "task_id": f"{repo_id}_browser_01",
                "repo_id": repo_id,
                "task_type": "BrowserSmokeWorkflow",
                "workflow": "BrowserSmokeWorkflow",
                "description": f"Run browser smoke test {url} selectors=body",
                "url": url,
                "supported_workflow_only": True,
            }
    return None


def is_test_path(relative: Path) -> bool:
    return "tests" in relative.parts or relative.name.startswith("test_") or relative.name.endswith("_test.py")


def run_repo_tasks(inventories: list[dict[str, Any]], task_catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repo_by_id = {repo["repo_id"]: repo for repo in inventories if repo["selected"]}
    results = []
    for task in task_catalog:
        if task.get("workflow") is None:
            results.append({"task": task, "skipped": True, "success": False, "failure_reason": task.get("skip_reason")})
            continue
        repo = repo_by_id[task["repo_id"]]
        task_repo = prepare_isolated_repo(Path(repo["repo_path"]), task)
        log_dir = LOG_DIR / task["task_id"]
        started = time.perf_counter()
        result = SkillLayer().run(task_repo, task["description"], config={}, run_tests=True, log_dir=log_dir, allow_internal=True)
        duration_ms = (time.perf_counter() - started) * 1000.0
        trace_counts = count_trace_operations(result.get("logs_path"))
        task_result = {
            "task_id": task["task_id"],
            "repo_id": task["repo_id"],
            "task_type": task["task_type"],
            "description": task["description"],
            "workflow_expected": task["workflow"],
            "workflow_used": result.get("workflow"),
            "success": bool(result.get("success")),
            "duration_ms": round(duration_ms, 3),
            "tool_calls": int(result.get("tool_calls", 0) or 0),
            "file_reads": int(trace_counts.get("file_reads", 0)),
            "search_operations": int(trace_counts.get("search_operations", 0)),
            "test_runs": int(trace_counts.get("test_runs", 0)),
            "tests_passed": bool(result.get("tests_passed")),
            "fallback_triggered": not bool(result.get("workflow")),
            "logs_path": result.get("logs_path"),
            "failure_reason": classify_failure_reason(result),
        }
        results.append(task_result)
    return results


def prepare_isolated_repo(source: Path, task: dict[str, Any]) -> Path:
    target = WORKDIR / task["repo_id"] / task["task_id"]
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"),
    )
    return target


def classify_failure_reason(result: dict[str, Any]) -> str | None:
    if result.get("success"):
        return None
    trace = read_trace(Path(result["logs_path"])) if result.get("logs_path") else []
    reasons = []
    for event in trace:
        if event.get("status") == "unsupported":
            reasons.append(str(event.get("reason", "unsupported")))
        if event.get("tool") == "browser_smoke" and event.get("error"):
            reasons.append(str(event["error"]))
    if reasons:
        if "no_safe_helper_target" in reasons:
            return "workflow_assumption_no_safe_helper_target"
        return "workflow_capability_error:" + ",".join(sorted(set(reasons)))
    if result.get("workflow") and not result.get("tests_passed") and result.get("workflow") != "FindFunctionWorkflow":
        return "tests_failed_or_validation_failed"
    if not result.get("workflow"):
        return "fallback_or_routing_failure"
    return "workflow_failed_unknown"


def read_trace(log_dir: Path) -> list[dict[str, Any]]:
    path = log_dir / "trace.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def build_cross_repo_comparison(
    inventories: list[dict[str, Any]],
    repo_results: list[dict[str, Any]],
) -> dict[str, Any]:
    results_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in repo_results:
        if not result.get("skipped"):
            results_by_repo[result["repo_id"]].append(result)

    rows = []
    for repo in inventories:
        if not repo["selected"]:
            continue
        items = results_by_repo.get(repo["repo_id"], [])
        successes = sum(1 for item in items if item["success"])
        workflow_counts = Counter(item.get("workflow_used") or "none" for item in items)
        rows.append(
            {
                "repo_id": repo["repo_id"],
                "origin": repo["origin"],
                "generated_by_skilllayer": repo["generated_by_skilllayer"],
                "task_count": len(items),
                "success_count": successes,
                "success_rate": round(successes / len(items), 3) if items else 0.0,
                "tool_calls": sum(int(item["tool_calls"]) for item in items),
                "file_reads": sum(int(item["file_reads"]) for item in items),
                "search_operations": sum(int(item["search_operations"]) for item in items),
                "duration_ms": round(sum(float(item["duration_ms"]) for item in items), 3),
                "workflow_distribution": dict(sorted(workflow_counts.items())),
                "fallback_count": sum(1 for item in items if item["fallback_triggered"]),
                "failure_reasons": dict(sorted(Counter(item["failure_reason"] for item in items if item["failure_reason"]).items())),
                "warnings": repo.get("warnings", []),
            }
        )
    return {
        "repositories": rows,
        "selected_repo_count": len(rows),
        "selected_external_repo_count": sum(1 for row in rows if row["origin"].startswith("external")),
        "outside_ledgerlite_success_rate": outside_ledgerlite_success_rate(rows),
    }


def outside_ledgerlite_success_rate(rows: list[dict[str, Any]]) -> float:
    external = [row for row in rows if row["repo_id"] != "ledgerlite"]
    attempts = sum(row["task_count"] for row in external)
    successes = sum(row["success_count"] for row in external)
    return round(successes / attempts, 3) if attempts else 0.0


def build_generalization_summary(
    inventories: list[dict[str, Any]],
    task_catalog: list[dict[str, Any]],
    repo_results: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    attempted = [result for result in repo_results if not result.get("skipped")]
    workflow_summary = build_workflow_summary(attempted)
    outside_rate = comparison["outside_ledgerlite_success_rate"]
    limitations = []
    if comparison["selected_external_repo_count"] < 2:
        limitations.append(
            "Fewer than two suitable external small Python repositories were available locally; cannot fully satisfy the requested three-repository external diversity."
        )
    rejected = [repo for repo in inventories if not repo["selected"]]
    if rejected:
        limitations.append("Rejected candidates are preserved in repo_inventory.json with explicit rejection reasons.")

    failure_reasons = Counter(result["failure_reason"] for result in attempted if result.get("failure_reason"))
    workflow_assumption_failures = sum(
        count for reason, count in failure_reasons.items() if reason and (reason.startswith("workflow_assumption") or reason.startswith("workflow_capability"))
    )
    return {
        "milestone": "Generalization Validation",
        "validation_only": True,
        "routing_changed": False,
        "workflows_changed": False,
        "tasks_tuned": False,
        "selected_repo_count": comparison["selected_repo_count"],
        "selected_external_repo_count": comparison["selected_external_repo_count"],
        "task_count": len([task for task in task_catalog if task.get("workflow")]),
        "overall_success_rate": round(sum(1 for result in attempted if result["success"]) / len(attempted), 3) if attempted else 0.0,
        "outside_ledgerlite_success_rate": outside_rate,
        "outside_ledgerlite_above_85_percent": outside_rate >= 0.85,
        "workflow_summary": workflow_summary,
        "best_generalizing_workflows": best_generalizing_workflows(workflow_summary),
        "repository_specific_workflows": repository_specific_workflows(workflow_summary),
        "failure_reason_counts": dict(sorted(failure_reasons.items())),
        "workflow_assumption_failure_count": workflow_assumption_failures,
        "limitations": limitations,
        "answers": {
            "which_workflows_generalize_best": best_generalizing_workflows(workflow_summary),
            "which_workflows_are_repository_specific": repository_specific_workflows(workflow_summary),
            "does_success_remain_above_85_outside_ledgerlite": outside_rate >= 0.85,
            "are_failures_caused_by_workflow_assumptions": workflow_assumption_failures > 0,
        },
    }


def build_workflow_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result.get("workflow_used") or "none"].append(result)
    summary = {}
    for workflow, items in sorted(grouped.items()):
        successes = sum(1 for item in items if item["success"])
        repos = sorted({item["repo_id"] for item in items})
        external_items = [item for item in items if item["repo_id"] != "ledgerlite"]
        external_successes = sum(1 for item in external_items if item["success"])
        summary[workflow] = {
            "attempts": len(items),
            "successes": successes,
            "success_rate": round(successes / len(items), 3) if items else 0.0,
            "repo_count": len(repos),
            "repos": repos,
            "external_attempts": len(external_items),
            "external_success_rate": round(external_successes / len(external_items), 3) if external_items else None,
            "failure_reasons": dict(sorted(Counter(item["failure_reason"] for item in items if item["failure_reason"]).items())),
        }
    return summary


def best_generalizing_workflows(workflow_summary: dict[str, Any]) -> list[str]:
    return [
        workflow
        for workflow, stats in workflow_summary.items()
        if stats["repo_count"] > 1 and stats["success_rate"] >= 0.85 and (stats["external_success_rate"] is None or stats["external_success_rate"] >= 0.85)
    ]


def repository_specific_workflows(workflow_summary: dict[str, Any]) -> list[str]:
    specific = []
    for workflow, stats in workflow_summary.items():
        if stats["external_attempts"] == 0:
            specific.append(workflow)
        elif stats["external_success_rate"] is not None and stats["external_success_rate"] < 0.85:
            specific.append(workflow)
    return specific


def build_report(summary: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    external_count = summary["selected_external_repo_count"]
    if external_count < 2:
        interpretation = (
            "ran with limited external repository coverage. The result is useful as a smoke validation, "
            "but it is not enough to estimate broad transfer beyond LedgerLite."
        )
    elif summary["outside_ledgerlite_above_85_percent"]:
        interpretation = "SkillLayer maintained above-85% success outside LedgerLite on the selected local repositories."
    else:
        interpretation = "SkillLayer did not maintain above-85% success outside LedgerLite; workflow assumptions should be inspected before expansion."
    return {
        "summary": summary,
        "cross_repo_comparison": comparison,
        "interpretation": interpretation,
        "important_limitations": summary["limitations"],
        "outputs": {
            "repo_inventory": str(OUTPUT_DIR / "repo_inventory.json"),
            "task_catalog": str(OUTPUT_DIR / "task_catalog.json"),
            "repo_results": str(OUTPUT_DIR / "repo_results.json"),
            "generalization_summary": str(OUTPUT_DIR / "generalization_summary.json"),
            "cross_repo_comparison": str(OUTPUT_DIR / "cross_repo_comparison.json"),
        },
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
