from __future__ import annotations

import json
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SkillLayer
from .runner.core import extract_symbols
from .tools.browser import run_browser_smoke
from .verifier import TestRunner


BENCHMARK_DIR = Path("runs/p4_5_benchmark")
LEDGER_REPO = Path("runs/a3_2_repo")
BROWSER_FIXTURE = Path("runs/browser_smoke/fixture")


@dataclass
class AgentMetrics:
    success: bool = False
    duration_ms: float = 0.0
    file_reads: int = 0
    search_operations: int = 0
    tool_calls: int = 0
    tests_executed: bool = False
    tests_passed: bool = False
    workflow_used: str | None = None
    fallback_triggered: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "duration_ms": round(self.duration_ms, 3),
            "file_reads": self.file_reads,
            "search_operations": self.search_operations,
            "tool_calls": self.tool_calls,
            "tests_executed": self.tests_executed,
            "tests_passed": self.tests_passed,
            "workflow_used": self.workflow_used,
            "fallback_triggered": self.fallback_triggered,
            **self.extra,
        }


class BaselineAgent:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self.metrics = AgentMetrics(workflow_used="baseline_manual")
        self.test_runner = TestRunner()

    def list_files(self) -> list[Path]:
        self.metrics.tool_calls += 1
        return sorted(path for path in self.repo_path.rglob("*.py") if "__pycache__" not in path.parts)

    def read_file(self, path: Path) -> str:
        self.metrics.tool_calls += 1
        self.metrics.file_reads += 1
        return path.read_text(encoding="utf-8", errors="ignore")

    def search_symbol(self, symbol: str) -> list[Path]:
        self.metrics.tool_calls += 1
        self.metrics.search_operations += 1
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        hits: list[Path] = []
        for path in self.list_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                hits.append(path)
        return hits

    def replace_token_all(self, old: str, new: str) -> bool:
        self.metrics.tool_calls += 1
        pattern = re.compile(rf"\b{re.escape(old)}\b")
        changed = False
        for path in self.list_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            updated, count = pattern.subn(new, text)
            if count:
                path.write_text(updated, encoding="utf-8")
                changed = True
        return changed

    def replace_text(self, relative_path: str, old: str, new: str) -> bool:
        self.metrics.tool_calls += 1
        path = self.repo_path / relative_path
        text = path.read_text(encoding="utf-8", errors="ignore")
        if old not in text:
            return False
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return True

    def insert_text(self, relative_path: str, text: str) -> bool:
        self.metrics.tool_calls += 1
        path = self.repo_path / relative_path
        current = self.read_file(path)
        if text.strip() in current:
            return False
        path.write_text(current.rstrip() + "\n\n" + text.strip() + "\n", encoding="utf-8")
        return True

    def run_tests(self) -> bool:
        self.metrics.tool_calls += 1
        self.metrics.tests_executed = True
        result = self.test_runner.run(self.repo_path)
        self.metrics.tests_passed = bool(result.get("available")) and result.get("returncode") == 0
        return self.metrics.tests_passed

    def run(self, task: dict[str, Any]) -> AgentMetrics:
        started = time.perf_counter()
        task_type = task["task_type"]
        if task_type == "FindFunction":
            symbol = task["symbol"]
            hits = self.search_symbol(symbol)
            if hits:
                self.read_file(hits[0])
            self.metrics.success = bool(hits)
        elif task_type == "RenameSymbol":
            hits = self.search_symbol(task["old_symbol"])
            changed = self.replace_token_all(task["old_symbol"], task["new_symbol"]) if hits else False
            tests_passed = self.run_tests()
            self.metrics.success = changed and tests_passed
        elif task_type == "FixFailingTest":
            self.run_tests()
            test_files = sorted((self.repo_path / "tests").glob("test_*.py"))
            if test_files:
                self.read_file(test_files[0])
            fixed = self.replace_text(task["bug_file"], task["broken_text"], task["fixed_text"]) if not self.metrics.tests_passed else True
            tests_passed = self.run_tests()
            self.metrics.success = fixed and tests_passed
        elif task_type == "AddHelper":
            helper_text = f"def {task['helper_name']}(value):\n    return value\n"
            inserted = self.insert_text("ledgerlite/validators.py", helper_text)
            tests_passed = self.run_tests()
            self.metrics.success = inserted and tests_passed
        elif task_type == "BrowserSmoke":
            result = run_browser_smoke(task["url"], task["selectors"], output_dir=BENCHMARK_DIR / "browser_smoke")
            trace = result.get("trace", [])
            self.metrics.tool_calls = len(trace)
            self.metrics.file_reads = 0
            self.metrics.search_operations = 0
            self.metrics.success = bool(result.get("success"))
            self.metrics.extra = {
                "console_errors": result.get("console_errors", 0),
                "network_errors": result.get("network_errors", 0),
                "screenshot_path": result.get("screenshot_path"),
            }
        else:
            self.metrics.success = False
            self.metrics.fallback_triggered = True
        self.metrics.duration_ms = (time.perf_counter() - started) * 1000.0
        return self.metrics


def run_benchmark(skilllayer_log_dir: Path | None = None) -> dict[str, Any]:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    workdir = BENCHMARK_DIR / "workdirs"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    tasks = create_task_dataset()
    results = []
    for task in tasks:
        baseline_repo = prepare_task_repo(workdir, task, "baseline")
        skilllayer_repo = prepare_task_repo(workdir, task, "skilllayer")
        task_for_baseline = materialize_task(task, baseline_repo)
        task_for_skilllayer = materialize_task(task, skilllayer_repo)

        baseline = BaselineAgent(baseline_repo).run(task_for_baseline)
        task_log_dir = skilllayer_log_dir / task["task_id"] if skilllayer_log_dir else None
        skilllayer = run_skilllayer_agent(skilllayer_repo, task_for_skilllayer, log_dir=task_log_dir)
        results.append(
            {
                "task_id": task["task_id"],
                "task_type": task["task_type"],
                "task": task_for_skilllayer["description"],
                "baseline": baseline.to_dict(),
                "skilllayer": skilllayer.to_dict(),
            }
        )

    summary = build_summary(results)
    task_breakdown = build_task_breakdown(results)
    workflow_breakdown = build_workflow_breakdown(results)
    report = {
        "benchmark_name": "Real Cost Benchmark",
        "task_count": len(tasks),
        "comparison": summary,
        "task_breakdown": task_breakdown,
        "workflow_breakdown": workflow_breakdown,
        "results_path": str(BENCHMARK_DIR / "benchmark_results.json"),
        "proxy_metrics_only": True,
        "interpretation": "This benchmark compares operational proxy metrics on controlled local tasks. It does not measure real token savings or intelligence.",
    }
    write_outputs(results, summary, task_breakdown, workflow_breakdown, report)
    return report


def run_skilllayer_agent(repo_path: Path, task: dict[str, Any], log_dir: Path | None = None) -> AgentMetrics:
    started = time.perf_counter()
    result = SkillLayer().run(repo_path, task["description"], config={}, run_tests=True, log_dir=log_dir, allow_internal=True)
    trace_counts = count_trace_operations(result.get("logs_path")) if log_dir else {}
    metrics = AgentMetrics(
        success=bool(result.get("success")),
        duration_ms=(time.perf_counter() - started) * 1000.0,
        file_reads=int(trace_counts.get("file_reads", estimate_skilllayer_file_reads(result))),
        search_operations=int(trace_counts.get("search_operations", estimate_skilllayer_searches(result))),
        tool_calls=int(result.get("tool_calls", 0) or 0),
        tests_executed=bool(trace_counts.get("test_runs", 0)) if log_dir else bool(result.get("tests_passed")) or result.get("workflow") in {"RenameSymbolWorkflow", "FixFailingTestWorkflow", "AddHelperWorkflow"},
        tests_passed=bool(result.get("tests_passed")),
        workflow_used=result.get("workflow"),
        fallback_triggered=not bool(result.get("workflow")),
        extra={
            "console_errors": result.get("console_errors"),
            "network_errors": result.get("network_errors"),
            "screenshot_path": result.get("screenshot_path"),
            "logs_path": result.get("logs_path"),
            "test_run_count": trace_counts.get("test_runs"),
        },
    )
    return metrics


def create_task_dataset() -> list[dict[str, Any]]:
    find_symbols = ["parse_decimal_money", "allocate_amount", "require_account_code", "make_debit", "export_transactions_json", "summarize_csv"]
    rename_symbols = [
        ("money_rule_00", "money_rule_bench_00"),
        ("validator_rule_00", "validator_rule_bench_00"),
        ("account_rule_00", "account_rule_bench_00"),
        ("transaction_rule_00", "transaction_rule_bench_00"),
        ("exporter_rule_00", "exporter_rule_bench_00"),
        ("parse_currency_code", "parse_currency_code_bench"),
    ]
    tasks: list[dict[str, Any]] = []
    for index, symbol in enumerate(find_symbols, start=1):
        tasks.append({"task_id": f"find_{index:02d}", "task_type": "FindFunction", "symbol": symbol, "description": f"Find function {symbol}"})
    for index, (old, new) in enumerate(rename_symbols, start=1):
        tasks.append({"task_id": f"rename_{index:02d}", "task_type": "RenameSymbol", "old_symbol": old, "new_symbol": new, "description": f"Rename {old} to {new}"})
    seeded_failures = [
        {
            "bug_file": "ledgerlite/money.py",
            "fixed_text": "code = str(value).strip().upper()",
            "broken_text": "code = str(value).strip().lower()",
            "description": "Fix failing test caused by currency code casing regression",
        },
        {
            "bug_file": "ledgerlite/transactions.py",
            "fixed_text": "sign = -1 if self.kind == TransactionKind.DEBIT else 1",
            "broken_text": "sign = 1 if self.kind == TransactionKind.DEBIT else -1",
            "description": "Fix failing test caused by signed transaction amount regression",
        },
        {
            "bug_file": "ledgerlite/tax.py",
            "fixed_text": '"default": Decimal("0.07")',
            "broken_text": '"default": Decimal("0.08")',
            "description": "Fix failing test caused by default tax rate regression",
        },
        {
            "bug_file": "ledgerlite/accounts.py",
            "fixed_text": 'return f"{self.code} - {self.name}"',
            "broken_text": 'return f"{self.name} - {self.code}"',
            "description": "Fix failing test caused by account label ordering regression",
        },
        {
            "bug_file": "ledgerlite/money.py",
            "fixed_text": "if not weights:\n        return []",
            "broken_text": "if not weights:\n        raise ValidationError(\"weights required\")",
            "description": "Fix failing test caused by empty allocation regression",
        },
    ]
    for index, failure in enumerate(seeded_failures, start=1):
        tasks.append({"task_id": f"test_{index:02d}", "task_type": "FixFailingTest", **failure})
    for index in range(1, 5):
        tasks.append({"task_id": f"helper_{index:02d}", "task_type": "AddHelper", "helper_name": f"benchmark_helper_{index:02d}", "description": f"Add helper function benchmark_helper_{index:02d}"})
    for index in range(1, 5):
        tasks.append(
            {
                "task_id": f"browser_{index:02d}",
                "task_type": "BrowserSmoke",
                "selectors": ["#app", "form", "input[name=email]"],
                "description": "Run browser smoke test {url} selectors=#app, form, input[name=email]",
            }
        )
    return tasks


def prepare_task_repo(workdir: Path, task: dict[str, Any], agent_name: str) -> Path:
    source = BROWSER_FIXTURE if task["task_type"] == "BrowserSmoke" else LEDGER_REPO
    target = workdir / f"{task['task_id']}_{agent_name}"
    shutil.copytree(source, target)
    if task["task_type"] == "FixFailingTest":
        apply_seeded_failure(target, task)
    return target


def materialize_task(task: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    materialized = dict(task)
    if task["task_type"] == "BrowserSmoke":
        url = "file://" + str((repo_path / "index.html").resolve())
        materialized["url"] = url
        materialized["description"] = task["description"].format(url=url)
    return materialized


def apply_seeded_failure(repo_path: Path, task: dict[str, Any]) -> None:
    path = repo_path / task["bug_file"]
    text = path.read_text(encoding="utf-8", errors="ignore")
    fixed = task["fixed_text"]
    broken = task["broken_text"]
    if fixed not in text:
        raise ValueError(f"Cannot seed benchmark failure in {path}: expected text not found")
    path.write_text(text.replace(fixed, broken, 1), encoding="utf-8")


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = aggregate_agent(results, "baseline")
    skilllayer = aggregate_agent(results, "skilllayer")
    return {
        "baseline_total_duration": baseline["duration_ms"],
        "skilllayer_total_duration": skilllayer["duration_ms"],
        "baseline_file_reads": baseline["file_reads"],
        "skilllayer_file_reads": skilllayer["file_reads"],
        "baseline_search_operations": baseline["search_operations"],
        "skilllayer_search_operations": skilllayer["search_operations"],
        "baseline_tool_calls": baseline["tool_calls"],
        "skilllayer_tool_calls": skilllayer["tool_calls"],
        "baseline_success_rate": baseline["success_rate"],
        "skilllayer_success_rate": skilllayer["success_rate"],
        "duration_reduction_percent": reduction_percent(baseline["duration_ms"], skilllayer["duration_ms"]),
        "file_read_reduction_percent": reduction_percent(baseline["file_reads"], skilllayer["file_reads"]),
        "search_reduction_percent": reduction_percent(baseline["search_operations"], skilllayer["search_operations"]),
        "tool_call_reduction_percent": reduction_percent(baseline["tool_calls"], skilllayer["tool_calls"]),
        "roi": {
            "estimated_llm_call_reduction": max(0, baseline["tool_calls"] - skilllayer["tool_calls"]),
            "estimated_file_read_reduction": baseline["file_reads"] - skilllayer["file_reads"],
            "estimated_search_reduction": baseline["search_operations"] - skilllayer["search_operations"],
            "proxy_metrics": True,
            "estimated_impact": True,
            "estimated_savings_only": True,
        },
        "honest_interpretation": "Negative reductions mean SkillLayer cost more than the baseline for that proxy metric.",
    }


def aggregate_agent(results: list[dict[str, Any]], agent_key: str) -> dict[str, Any]:
    count = len(results)
    successes = sum(1 for result in results if result[agent_key]["success"])
    return {
        "duration_ms": round(sum(float(result[agent_key]["duration_ms"]) for result in results), 3),
        "file_reads": sum(int(result[agent_key]["file_reads"]) for result in results),
        "search_operations": sum(int(result[agent_key]["search_operations"]) for result in results),
        "tool_calls": sum(int(result[agent_key]["tool_calls"]) for result in results),
        "success_rate": round(successes / count, 3) if count else 0.0,
    }


def build_task_breakdown(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result["task_type"]].append(result)
    return {task_type: build_summary(items) for task_type, items in sorted(grouped.items())}


def build_workflow_breakdown(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        workflow = result["skilllayer"].get("workflow_used") or "none"
        grouped[workflow].append(result)
    return {workflow: build_summary(items) for workflow, items in sorted(grouped.items())}


def write_outputs(
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    task_breakdown: dict[str, Any],
    workflow_breakdown: dict[str, Any],
    report: dict[str, Any],
) -> None:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "benchmark_results.json": results,
        "benchmark_summary.json": summary,
        "task_breakdown.json": task_breakdown,
        "workflow_breakdown.json": workflow_breakdown,
        "benchmark_report.json": report,
    }
    for filename, payload in outputs.items():
        (BENCHMARK_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def estimate_skilllayer_file_reads(result: dict[str, Any]) -> int:
    workflow = result.get("workflow")
    if workflow == "BrowserSmokeWorkflow":
        return 0
    if workflow == "FindFunctionWorkflow":
        return 1
    if workflow in {"RenameSymbolWorkflow", "FixBugWorkflow", "AddHelperWorkflow", "FixFailingTestWorkflow"}:
        return 1
    return 0


def estimate_skilllayer_searches(result: dict[str, Any]) -> int:
    workflow = result.get("workflow")
    if workflow in {"FindFunctionWorkflow", "RenameSymbolWorkflow", "FixBugWorkflow", "AddHelperWorkflow"}:
        return max(1, len(extract_symbols(str(result.get("task", "")))[:3]))
    return 0


def count_trace_operations(logs_path: str | None) -> dict[str, int]:
    if not logs_path:
        return {}
    trace_path = Path(logs_path) / "trace.json"
    if not trace_path.exists():
        return {}
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    counts = {"file_reads": 0, "search_operations": 0, "test_runs": 0}
    for event in trace:
        if event.get("cached") or event.get("skipped"):
            continue
        tool = event.get("tool")
        if tool in {"read_file", "open_file"}:
            counts["file_reads"] += 1
        elif tool == "search_symbol":
            counts["search_operations"] += 1
        elif tool == "run_tests":
            counts["test_runs"] += 1
    return counts


def reduction_percent(baseline_value: float, skilllayer_value: float) -> float:
    if baseline_value == 0:
        return 0.0
    return round(((baseline_value - skilllayer_value) / baseline_value) * 100.0, 3)
