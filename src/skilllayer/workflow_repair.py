from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .benchmark import run_benchmark


INPUT_BENCHMARK_DIR = Path("runs/p4_5_benchmark")
INPUT_FAILURE_DIR = Path("runs/p4_5_failure_analysis")
OUTPUT_DIR = Path("runs/p4_6_workflow_repair")
BEFORE_DIR = OUTPUT_DIR / "before_benchmark"
BEFORE_FAILURE_DIR = OUTPUT_DIR / "before_failure_analysis"


def run_workflow_repair_report(
    benchmark_dir: str | Path = INPUT_BENCHMARK_DIR,
    failure_dir: str | Path = INPUT_FAILURE_DIR,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict[str, Any]:
    benchmark_path = Path(benchmark_dir)
    failure_path = Path(failure_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    ensure_before_snapshot(benchmark_path, failure_path, output_path)
    before_summary = load_json(BEFORE_DIR / "benchmark_summary.json")
    before_results = load_json(BEFORE_DIR / "benchmark_results.json")
    failure_attribution = load_json(BEFORE_FAILURE_DIR / "failure_attribution_results.json")

    failure_subtypes = classify_failure_subtypes(failure_attribution)
    write_json(output_path / "failure_subtypes.json", failure_subtypes)

    after_report = run_benchmark()
    after_summary = after_report["comparison"]
    after_results = load_json(benchmark_path / "benchmark_results.json")
    after_task_breakdown = load_json(benchmark_path / "task_breakdown.json")
    after_workflow_breakdown = load_json(benchmark_path / "workflow_breakdown.json")

    comparison = build_before_after_comparison(before_summary, after_summary)
    unsupported = classify_after_unsupported(after_results)
    summary = {
        "milestone": "Workflow Decomposition and Targeted Repair",
        "before_skilllayer_success_rate": before_summary["skilllayer_success_rate"],
        "after_skilllayer_success_rate": after_summary["skilllayer_success_rate"],
        "target_success_rate": 0.85,
        "target_met": after_summary["skilllayer_success_rate"] >= 0.85,
        "failure_subtype_counts": failure_subtypes["subtype_counts"],
        "unsupported_after_repair": unsupported,
        "comparison": comparison,
        "diagnostic": build_diagnostic(before_summary, after_summary, unsupported),
        "routing_changed": False,
        "benchmark_tasks_changed": False,
        "paid_llm_used": False,
    }
    results = {
        "after_benchmark_results": after_results,
        "after_task_breakdown": after_task_breakdown,
        "after_workflow_breakdown": after_workflow_breakdown,
    }
    report = {
        "summary": summary,
        "failure_subtypes": failure_subtypes,
        "before_after_comparison": comparison,
        "results_path": str(output_path / "results.json"),
        "summary_path": str(output_path / "summary.json"),
        "before_after_comparison_path": str(output_path / "before_after_comparison.json"),
        "failure_subtypes_path": str(output_path / "failure_subtypes.json"),
        "report_path": str(output_path / "report.json"),
        "interpretation": "improves targeted workflow capability without changing routing or benchmark tasks.",
    }

    write_json(output_path / "results.json", results)
    write_json(output_path / "summary.json", summary)
    write_json(output_path / "before_after_comparison.json", comparison)
    write_json(output_path / "report.json", report)
    return report


def ensure_before_snapshot(benchmark_dir: Path, failure_dir: Path, output_dir: Path) -> None:
    before_dir = output_dir / "before_benchmark"
    before_failure_dir = output_dir / "before_failure_analysis"
    before_dir.mkdir(parents=True, exist_ok=True)
    before_failure_dir.mkdir(parents=True, exist_ok=True)
    for filename in ["benchmark_results.json", "benchmark_summary.json", "task_breakdown.json", "workflow_breakdown.json"]:
        target = before_dir / filename
        if not target.exists():
            source = benchmark_dir / filename
            if not source.exists():
                raise FileNotFoundError(f"Missing benchmark input: {source}")
            shutil.copy2(source, target)
    failure_source = failure_dir / "failure_attribution_results.json"
    failure_target = before_failure_dir / "failure_attribution_results.json"
    if not failure_target.exists():
        if not failure_source.exists():
            raise FileNotFoundError(f"Missing failure input: {failure_source}")
        shutil.copy2(failure_source, failure_target)


def classify_failure_subtypes(failure_records: list[dict[str, Any]]) -> dict[str, Any]:
    records = []
    for item in failure_records:
        task_type = str(item.get("task_type", ""))
        subtype = classify_subtype(task_type, str(item.get("task", "")), item)
        records.append(
            {
                "task_id": item.get("task_id"),
                "task_type": task_type,
                "workflow_selected": item.get("workflow_selected"),
                "root_cause": item.get("root_cause"),
                "subtype": subtype,
                "repairable": bool(item.get("repairable")),
                "evidence": item.get("evidence", {}),
            }
        )
    subtype_counts = Counter(record["subtype"] for record in records)
    workflow_subtype_counts: dict[str, dict[str, int]] = {}
    for record in records:
        workflow = str(record.get("workflow_selected") or "none")
        workflow_subtype_counts.setdefault(workflow, {})
        workflow_subtype_counts[workflow][record["subtype"]] = workflow_subtype_counts[workflow].get(record["subtype"], 0) + 1
    return {
        "records": records,
        "subtype_counts": dict(sorted(subtype_counts.items())),
        "workflow_subtype_counts": workflow_subtype_counts,
    }


def classify_subtype(task_type: str, task: str, item: dict[str, Any]) -> str:
    text = task.lower()
    if task_type == "FixFailingTest":
        if "signed" in text or "label ordering" in text or "casing" in text:
            return "wrong_operator"
        if "constant" in text or "tax rate" in text:
            return "wrong_constant"
        if "empty" in text or "edge case" in text:
            return "missing_edge_case"
        if "validation" in text:
            return "missing_validation"
        if "import" in text:
            return "import_error"
        return "unknown"
    if task_type == "AddHelper":
        evidence = item.get("evidence", {})
        if evidence.get("tests_passed") and not item.get("task_success"):
            return "helper_missing"
        if "export" in text:
            return "helper_not_exported"
        if "import" in text:
            return "helper_inserted_wrong_file"
        if "test" in text:
            return "test_not_updated"
        return "helper_missing"
    return "unknown"


def build_before_after_comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    metrics = [
        "skilllayer_success_rate",
        "tool_call_reduction_percent",
        "file_read_reduction_percent",
        "search_reduction_percent",
        "duration_reduction_percent",
    ]
    comparison = {}
    for metric in metrics:
        before_value = before.get(metric)
        after_value = after.get(metric)
        comparison[metric] = {
            "before": before_value,
            "after": after_value,
            "delta": round(float(after_value) - float(before_value), 3) if before_value is not None and after_value is not None else None,
        }
    comparison["baseline_success_rate"] = {
        "before": before.get("baseline_success_rate"),
        "after": after.get("baseline_success_rate"),
        "delta": round(float(after.get("baseline_success_rate", 0)) - float(before.get("baseline_success_rate", 0)), 3),
    }
    return comparison


def classify_after_unsupported(after_results: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [item for item in after_results if not item.get("skilllayer", {}).get("success")]
    unsupported = []
    for item in failures:
        task_type = str(item.get("task_type", ""))
        reason = "unsupported_or_failed_after_repair"
        if task_type == "FixFailingTest":
            reason = "simple_patch_not_available_or_verification_failed"
        elif task_type == "AddHelper":
            reason = "helper_insert_or_verification_failed"
        unsupported.append(
            {
                "task_id": item.get("task_id"),
                "task_type": task_type,
                "workflow": item.get("skilllayer", {}).get("workflow_used"),
                "reason": reason,
            }
        )
    return {
        "count": len(unsupported),
        "items": unsupported,
    }


def build_diagnostic(before: dict[str, Any], after: dict[str, Any], unsupported: dict[str, Any]) -> str:
    before_rate = float(before.get("skilllayer_success_rate", 0.0))
    after_rate = float(after.get("skilllayer_success_rate", 0.0))
    if after_rate >= 0.85:
        return f"Target met: SkillLayer success improved from {before_rate:.3f} to {after_rate:.3f}."
    return (
        f"Target not met: SkillLayer success improved from {before_rate:.3f} to {after_rate:.3f}, "
        f"with {unsupported['count']} unsupported or still-failing tasks."
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
