from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import WORKFLOWS
from .telemetry import normalize_telemetry_entry, read_telemetry


DEMAND_DIR = Path("runs/skilllayer_demand")
DEMAND_EVENTS_JSONL = DEMAND_DIR / "demand_events.jsonl"
DEMAND_SUMMARY_JSON = DEMAND_DIR / "demand_summary.json"
DEMAND_OPPORTUNITY_JSON = DEMAND_DIR / "opportunity_ranking.json"

P6_OUTPUT_DIR = Path("runs/p6_0_workflow_demand")
P6_GUARDRAIL_DIR = Path("runs/p6_0_1_demand_guardrails")

ROADMAP_MINIMUM_EVENTS = 100
USABLE_MINIMUM_EVENTS = 500

WARNING_SMALL_SAMPLE = "Sample size is too small for roadmap decisions."
WARNING_RANKING_UNRELIABLE = "Opportunity ranking is not reliable until at least 100 events."
WARNING_REPEATED_SESSIONS = "Raw event counts may be dominated by repeated attempts from the same session."
WARNING_EARLY_OBSERVATION = "Use this report for debugging and early observation only."

DEMAND_CATEGORIES = [
    "git",
    "pr_review",
    "documentation",
    "database",
    "browser",
    "testing",
    "refactoring",
    "search",
    "api",
    "devops",
    "data_analysis",
    "other",
]

CATEGORY_WORKFLOW_CANDIDATES = {
    "git": "GitWorkflow",
    "pr_review": "PRReviewWorkflow",
    "documentation": "DocumentationWorkflow",
    "database": "DatabaseWorkflow",
    "browser": "BrowserSmokeWorkflow",
    "testing": "FixFailingTestWorkflow",
    "refactoring": "RenameSymbolWorkflow",
    "search": "FindFunctionWorkflow",
    "api": "APIWorkflow",
    "devops": "DevOpsWorkflow",
    "data_analysis": "DataAnalysisWorkflow",
    "other": "GeneralWorkflow",
}

SUPPORTED_CATEGORY_WORKFLOWS = {
    "browser": {"BrowserSmokeWorkflow"},
    "testing": {"FixFailingTestWorkflow"},
    "refactoring": {"RenameSymbolWorkflow", "FixBugWorkflow"},
    "search": {"FindFunctionWorkflow"},
}


def record_demand_event(
    *,
    task_text: str | None,
    normalized_task: str | None = None,
    category: str | None = None,
    workflow_found: bool = False,
    workflow_executed: bool = False,
    workflow_name: str | None = None,
    client: str | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
    output_path: str | Path = DEMAND_EVENTS_JSONL,
) -> dict[str, Any]:
    normalized = normalized_task or normalize_task_text(task_text)
    event = {
        "timestamp": timestamp or current_timestamp(),
        "task_text": task_text,
        "normalized_task": normalized,
        "category": category or classify_demand_category(task_text or normalized),
        "workflow_found": bool(workflow_found),
        "workflow_executed": bool(workflow_executed),
        "workflow_name": workflow_name,
        "client": client or "unknown",
        "source": source or "unknown",
        "metadata": dict(metadata or {}),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def build_demand_report() -> dict[str, Any]:
    recorded_events = read_demand_events()
    telemetry_events = demand_events_from_telemetry()
    events = [normalize_demand_event(event) for event in [*telemetry_events, *recorded_events]]
    guardrails = build_guardrail_summary(events)
    summary = build_demand_summary(
        events,
        recorded_event_count=len(recorded_events),
        telemetry_event_count=len(telemetry_events),
        guardrails=guardrails,
    )
    opportunity = build_opportunity_ranking(events, guardrails=guardrails)
    category_breakdown = build_category_breakdown(events)
    workflow_gap_analysis = build_workflow_gap_analysis(events)
    dedupe_report = build_dedupe_report(events)
    sample_size_report = build_sample_size_report(events, guardrails)
    report = {
        "milestone": "Workflow Demand Engine v1",
        "demand_summary": summary,
        "opportunity_ranking": opportunity,
        "category_breakdown": category_breakdown,
        "workflow_gap_analysis": workflow_gap_analysis,
        "guardrails": guardrails,
        "sample_size_report": sample_size_report,
        "dedupe_report": dedupe_report,
        "outputs": {
            "demand_events": str(DEMAND_EVENTS_JSONL),
            "demand_summary": str(DEMAND_SUMMARY_JSON),
            "opportunity_ranking": str(DEMAND_OPPORTUNITY_JSON),
            "dashboard_demand_summary": str(P6_OUTPUT_DIR / "demand_summary.json"),
            "dashboard_opportunity_ranking": str(P6_OUTPUT_DIR / "opportunity_ranking.json"),
            "dashboard_category_breakdown": str(P6_OUTPUT_DIR / "category_breakdown.json"),
            "dashboard_workflow_gap_analysis": str(P6_OUTPUT_DIR / "workflow_gap_analysis.json"),
            "dashboard_report": str(P6_OUTPUT_DIR / "report.json"),
            "guardrail_summary": str(P6_GUARDRAIL_DIR / "guardrail_summary.json"),
            "sample_size_report": str(P6_GUARDRAIL_DIR / "sample_size_report.json"),
            "dedupe_report": str(P6_GUARDRAIL_DIR / "dedupe_report.json"),
            "guardrail_report": str(P6_GUARDRAIL_DIR / "report.json"),
        },
        "classification": {
            "method": "deterministic_keyword_rules",
            "llm_used": False,
            "embeddings_used": False,
            "external_api_used": False,
        },
        "workflow_generation_included": False,
        "interpretation": "Demand rankings identify candidate workflow areas from observed tasks and misses. They do not create or validate new workflows.",
    }
    write_demand_outputs(summary, opportunity, category_breakdown, workflow_gap_analysis, guardrails, sample_size_report, dedupe_report, report)
    return report


def read_demand_events(path: str | Path = DEMAND_EVENTS_JSONL) -> list[dict[str, Any]]:
    event_path = Path(path)
    if not event_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in event_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def demand_events_from_telemetry() -> list[dict[str, Any]]:
    events = []
    for entry in read_telemetry():
        normalized = normalize_telemetry_entry(entry)
        if normalized.get("tool_name") != "skilllayer_run":
            continue
        task_text = normalized.get("task")
        workflow_name = normalized.get("workflow")
        fallback = bool(normalized.get("fallback_triggered"))
        workflow_found = bool(normalized.get("workflow_found"))
        workflow_executed = bool(normalized.get("workflow_executed")) and not fallback
        events.append(
            {
                "timestamp": normalized.get("timestamp") or current_timestamp(),
                "task_text": task_text,
                "normalized_task": normalize_task_text(task_text),
                "category": classify_demand_category(task_text),
                "workflow_found": workflow_found,
                "workflow_executed": workflow_executed,
                "workflow_name": workflow_name,
                "client": "skilllayer_cli_or_mcp",
                "source": "telemetry",
                "metadata": {
                    "success": bool(normalized.get("success")),
                    "fallback_triggered": fallback,
                    "reason": normalized.get("reason"),
                    "derived_from_telemetry": True,
                },
            }
        )
    return events


def build_demand_summary(
    events: list[dict[str, Any]],
    *,
    recorded_event_count: int,
    telemetry_event_count: int,
    guardrails: dict[str, Any],
) -> dict[str, Any]:
    category_counts = Counter(event["category"] for event in events)
    missing_events = [event for event in events if is_missing_or_unsupported(event)]
    workflow_counts = Counter(requested_workflow_name(event) for event in events)
    dedupe_report = build_dedupe_report(events)
    return {
        "total_events": len(events),
        "data_volume_level": guardrails["data_volume_level"],
        "ranking_reliable": guardrails["ranking_reliable"],
        "opportunity_ranking_status": guardrails["opportunity_ranking_status"],
        "minimum_events_required": guardrails["minimum_events_required"],
        "use_for_roadmap_decisions": guardrails["use_for_roadmap_decisions"],
        "warnings": guardrails["warnings"],
        "recorded_demand_events": recorded_event_count,
        "telemetry_derived_events": telemetry_event_count,
        "workflow_found_count": sum(1 for event in events if bool(event["workflow_found"])),
        "workflow_missing_count": len(missing_events),
        "workflow_executed_count": sum(1 for event in events if bool(event["workflow_executed"])),
        "top_categories": [{"category": key, "count": count} for key, count in category_counts.most_common()],
        "top_missing_categories": [
            {"category": key, "count": count}
            for key, count in Counter(event["category"] for event in missing_events).most_common()
        ],
        "top_requested_workflows": [
            {"workflow": key, "count": count}
            for key, count in workflow_counts.most_common()
        ],
        "category_breakdown": build_category_breakdown(events),
        "deduped_counts_by_category": dedupe_report["deduped_counts_by_category"],
        "unique_session_counts_by_category": dedupe_report["unique_session_counts_by_category"],
        "event_sources": dict(sorted(Counter(event["source"] for event in events).items())),
        "demand_events_path": str(DEMAND_EVENTS_JSONL),
    }


def build_opportunity_ranking(events: list[dict[str, Any]], guardrails: dict[str, Any] | None = None) -> dict[str, Any]:
    guardrails = guardrails or build_guardrail_summary(events)
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[requested_workflow_name(event)].append(event)

    opportunities = []
    max_count = max((len(items) for items in grouped.values()), default=1)
    for workflow, items in sorted(grouped.items()):
        demand_frequency = len(items)
        missing_count = sum(1 for item in items if is_missing_or_unsupported(item))
        missing_rate = missing_count / demand_frequency if demand_frequency else 0.0
        existing_support = workflow in WORKFLOWS
        frequency_component = (demand_frequency / max_count) * 45.0
        missing_component = missing_rate * 45.0
        support_component = 10.0 if not existing_support else 0.0
        score = round(frequency_component + missing_component + support_component, 3)
        opportunities.append(
            {
                "candidate_workflow": workflow,
                "category": dominant_category(items),
                "opportunity_score": score,
                "demand_frequency": demand_frequency,
                "workflow_missing_count": missing_count,
                "workflow_missing_rate": round(missing_rate, 3),
                "existing_support": existing_support,
                "recommendation": recommend_action(existing_support, missing_rate, demand_frequency),
            }
        )
    opportunities.sort(key=lambda item: (-item["opportunity_score"], item["candidate_workflow"]))
    return {
        "ranking_reliable": guardrails["ranking_reliable"],
        "ranking_status": guardrails["opportunity_ranking_status"],
        "opportunity_ranking_status": guardrails["opportunity_ranking_status"],
        "minimum_events_required": guardrails["minimum_events_required"],
        "current_events": guardrails["current_events"],
        "use_for_roadmap_decisions": guardrails["use_for_roadmap_decisions"],
        "warnings": guardrails["warnings"],
        "interpretation": guardrails["ranking_interpretation"],
        "opportunities": opportunities,
        "scoring_formula": {
            "demand_frequency": "up to 45 points, normalized by the most frequent requested workflow",
            "workflow_missing_rate": "up to 45 points",
            "existing_support": "10 point boost when no current workflow exists",
        },
        "llm_used": False,
        "embeddings_used": False,
    }


def build_category_breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["category"]].append(event)
    breakdown = {}
    for category, items in sorted(grouped.items()):
        missing_count = sum(1 for item in items if is_missing_or_unsupported(item))
        workflows = Counter(requested_workflow_name(item) for item in items)
        sessions = {session_id(item) for item in items}
        dedupe_keys = {dedupe_key(item) for item in items}
        breakdown[category] = {
            "raw_event_count": len(items),
            "unique_session_count": len(sessions),
            "deduped_event_count": len(dedupe_keys),
            "total_events": len(items),
            "workflow_found_count": sum(1 for item in items if bool(item["workflow_found"])),
            "workflow_executed_count": sum(1 for item in items if bool(item["workflow_executed"])),
            "workflow_missing_count": missing_count,
            "workflow_missing_rate": round(missing_count / len(items), 3) if items else 0.0,
            "top_requested_workflows": [{"workflow": workflow, "count": count} for workflow, count in workflows.most_common()],
        }
    for category in DEMAND_CATEGORIES:
        breakdown.setdefault(
            category,
            {
                "raw_event_count": 0,
                "unique_session_count": 0,
                "deduped_event_count": 0,
                "total_events": 0,
                "workflow_found_count": 0,
                "workflow_executed_count": 0,
                "workflow_missing_count": 0,
                "workflow_missing_rate": 0.0,
                "top_requested_workflows": [],
            },
        )
    return breakdown


def build_workflow_gap_analysis(events: list[dict[str, Any]]) -> dict[str, Any]:
    missing_events = [event for event in events if is_missing_or_unsupported(event)]
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in missing_events:
        grouped[requested_workflow_name(event)].append(event)
    return {
        "total_gap_events": len(missing_events),
        "gap_by_workflow": {
            workflow: {
                "count": len(items),
                "category": dominant_category(items),
                "sample_tasks": [item.get("task_text") for item in items[:5]],
                "reasons": dict(sorted(Counter(str((item.get("metadata") or {}).get("reason") or "unsupported_or_missing") for item in items).items())),
                "existing_support": workflow in WORKFLOWS,
            }
            for workflow, items in sorted(grouped.items())
        },
        "stable_existing_workflows": sorted(WORKFLOWS),
    }


def normalize_demand_event(event: dict[str, Any]) -> dict[str, Any]:
    task_text = event.get("task_text")
    category = event.get("category") or classify_demand_category(task_text)
    if category not in DEMAND_CATEGORIES:
        category = classify_demand_category(task_text)
    return {
        "timestamp": event.get("timestamp") or current_timestamp(),
        "task_text": task_text,
        "normalized_task": event.get("normalized_task") or normalize_task_text(task_text),
        "category": category,
        "workflow_found": bool(event.get("workflow_found")),
        "workflow_executed": bool(event.get("workflow_executed")),
        "workflow_name": event.get("workflow_name"),
        "client": event.get("client") or "unknown",
        "source": event.get("source") or "unknown",
        "metadata": dict(event.get("metadata") or {}),
    }


def build_guardrail_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    total_events = len(events)
    data_volume_level = classify_data_volume(total_events)
    status = classify_ranking_status(total_events)
    ranking_reliable = total_events >= ROADMAP_MINIMUM_EVENTS
    warnings = []
    if total_events < ROADMAP_MINIMUM_EVENTS:
        warnings.extend([WARNING_SMALL_SAMPLE, WARNING_RANKING_UNRELIABLE])
    warnings.extend([WARNING_REPEATED_SESSIONS, WARNING_EARLY_OBSERVATION])
    return {
        "current_events": total_events,
        "data_volume_level": data_volume_level,
        "ranking_reliable": ranking_reliable,
        "opportunity_ranking_status": status,
        "ranking_status": status,
        "minimum_events_required": ROADMAP_MINIMUM_EVENTS,
        "usable_events_required": USABLE_MINIMUM_EVENTS,
        "use_for_roadmap_decisions": bool(total_events >= ROADMAP_MINIMUM_EVENTS),
        "warnings": warnings,
        "ranking_interpretation": "Do not use this ranking to choose new workflows yet."
        if total_events < ROADMAP_MINIMUM_EVENTS
        else "Ranking has enough volume for tentative workflow prioritization, but still requires human review.",
    }


def build_sample_size_report(events: list[dict[str, Any]], guardrails: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_events": len(events),
        "data_volume_level": guardrails["data_volume_level"],
        "ranking_reliable": guardrails["ranking_reliable"],
        "opportunity_ranking_status": guardrails["opportunity_ranking_status"],
        "minimum_events_required": guardrails["minimum_events_required"],
        "usable_events_required": guardrails["usable_events_required"],
        "use_for_roadmap_decisions": guardrails["use_for_roadmap_decisions"],
        "warnings": guardrails["warnings"],
        "level_definitions": {
            "very_low": "total_events < 50",
            "low": "50 <= total_events < 100",
            "usable": "100 <= total_events < 500",
            "stronger": "total_events >= 500",
        },
    }


def build_dedupe_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["category"]].append(event)
    category_rows = {}
    for category in DEMAND_CATEGORIES:
        items = grouped.get(category, [])
        sessions = {session_id(item) for item in items}
        dedupe_keys = {dedupe_key(item) for item in items}
        category_rows[category] = {
            "raw_event_count": len(items),
            "unique_session_count": len(sessions),
            "deduped_event_count": len(dedupe_keys),
            "unknown_session_counted_as_one": "unknown" in sessions,
        }
    return {
        "total_raw_events": len(events),
        "total_unique_session_count": len({session_id(event) for event in events}),
        "total_deduped_event_count": len({dedupe_key(event) for event in events}),
        "dedupe_key_rule": "metadata.dedupe_key if present, otherwise category + normalized_task",
        "missing_session_id_rule": 'session_id = "unknown"; unknown sessions are not treated as unique users',
        "category_breakdown": category_rows,
        "deduped_counts_by_category": {
            category: row["deduped_event_count"] for category, row in category_rows.items()
        },
        "unique_session_counts_by_category": {
            category: row["unique_session_count"] for category, row in category_rows.items()
        },
    }


def classify_data_volume(total_events: int) -> str:
    if total_events < 50:
        return "very_low"
    if total_events < 100:
        return "low"
    if total_events < 500:
        return "usable"
    return "stronger"


def classify_ranking_status(total_events: int) -> str:
    if total_events < ROADMAP_MINIMUM_EVENTS:
        return "insufficient_data"
    if total_events < USABLE_MINIMUM_EVENTS:
        return "experimental"
    return "usable"


def session_id(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") or {}
    return str(metadata.get("session_id") or event.get("session_id") or "unknown")


def dedupe_key(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") or {}
    explicit = metadata.get("dedupe_key") or event.get("dedupe_key")
    if explicit:
        return str(explicit)
    return f"{event.get('category') or 'other'}::{event.get('normalized_task') or normalize_task_text(event.get('task_text'))}"


def classify_demand_category(task_text: str | None) -> str:
    text = normalize_task_text(task_text)
    rules = [
        ("git", r"\b(git|commit|branch|merge|rebase|push|pull|tag|stash|checkout)\b"),
        ("pr_review", r"\b(pr|pull request|review comment|review comments|code review|address feedback|review thread)\b"),
        ("documentation", r"\b(readme|docs?|documentation|quickstart|guide|changelog|docstring)\b"),
        ("database", r"\b(database|sql|postgres|sqlite|migration|schema|table|query|index)\b"),
        ("browser", r"\b(browser|frontend|front-end|page|form|button|console|network|smoke|ui|dom|screenshot)\b"),
        ("testing", r"\b(test|tests|pytest|unittest|failing test|coverage|assertion|fixture)\b"),
        ("refactoring", r"\b(refactor|rename|move|extract|cleanup|restructure|repository-wide|symbol)\b"),
        ("search", r"\b(find|locate|where is|definition|inspect|search|list files|repo structure)\b"),
        ("api", r"\b(api|endpoint|request|response|client|server|http|json schema|webhook)\b"),
        ("devops", r"\b(ci|cd|docker|deploy|deployment|github actions|workflow|pipeline|build|env|config|yaml)\b"),
        ("data_analysis", r"\b(data|csv|spreadsheet|analytics|dashboard|chart|metric|report|notebook)\b"),
    ]
    for category, pattern in rules:
        if re.search(pattern, text):
            return category
    return "other"


def requested_workflow_name(event: dict[str, Any]) -> str:
    workflow = event.get("workflow_name")
    if workflow:
        return str(workflow)
    return CATEGORY_WORKFLOW_CANDIDATES.get(event.get("category") or "other", "GeneralWorkflow")


def is_missing_or_unsupported(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") or {}
    reason = str(metadata.get("reason") or "").lower()
    unsupported = bool(metadata.get("unsupported")) or "unsupported" in reason
    fallback = bool(metadata.get("fallback_triggered"))
    return (not bool(event.get("workflow_found"))) or unsupported or fallback


def dominant_category(items: list[dict[str, Any]]) -> str:
    if not items:
        return "other"
    return Counter(item["category"] for item in items).most_common(1)[0][0]


def recommend_action(existing_support: bool, missing_rate: float, demand_frequency: int) -> str:
    if not existing_support and demand_frequency > 0:
        return "candidate_new_workflow"
    if existing_support and missing_rate >= 0.5:
        return "audit_existing_workflow_capability"
    if existing_support:
        return "monitor_existing_workflow"
    return "monitor_demand"


def normalize_task_text(task_text: str | None) -> str:
    text = (task_text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_demand_outputs(
    summary: dict[str, Any],
    opportunity: dict[str, Any],
    category_breakdown: dict[str, Any],
    workflow_gap_analysis: dict[str, Any],
    guardrails: dict[str, Any],
    sample_size_report: dict[str, Any],
    dedupe_report: dict[str, Any],
    report: dict[str, Any],
) -> None:
    DEMAND_DIR.mkdir(parents=True, exist_ok=True)
    P6_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    P6_GUARDRAIL_DIR.mkdir(parents=True, exist_ok=True)
    DEMAND_SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    DEMAND_OPPORTUNITY_JSON.write_text(json.dumps(opportunity, indent=2), encoding="utf-8")
    dashboard_outputs = {
        "demand_summary.json": summary,
        "opportunity_ranking.json": opportunity,
        "category_breakdown.json": category_breakdown,
        "workflow_gap_analysis.json": workflow_gap_analysis,
        "report.json": report,
    }
    for filename, payload in dashboard_outputs.items():
        (P6_OUTPUT_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    guardrail_outputs = {
        "guardrail_summary.json": guardrails,
        "sample_size_report.json": sample_size_report,
        "dedupe_report.json": dedupe_report,
        "report.json": {
            "milestone": "Demand Reporting Guardrails",
            "guardrails": guardrails,
            "sample_size_report": sample_size_report,
            "dedupe_report": dedupe_report,
            "opportunity_ranking_status": opportunity["opportunity_ranking_status"],
            "ranking_reliable": opportunity["ranking_reliable"],
            "use_for_roadmap_decisions": opportunity["use_for_roadmap_decisions"],
            "warnings": guardrails["warnings"],
        },
    }
    for filename, payload in guardrail_outputs.items():
        (P6_GUARDRAIL_DIR / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
