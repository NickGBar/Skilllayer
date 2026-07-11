"""ReportRealSessionUsageWorkflow — read Claude Code's own local session logs
and emit a deterministic, read-only report of measured token usage and
estimated cost.

Data source (confirmed on Claude Code 2.1.197): ``~/.claude/projects/<slug>/
*.jsonl`` only. This module NEVER depends on stats-cache.json, usage-data/,
history.jsonl, or todos/ (confirmed absent). It is strictly read-only: it
opens ``*.jsonl`` files for reading and writes nothing.

Privacy — the extractor uses a STRICT ALLOWLIST. It reads only:
  type, timestamp, sessionId, message.model, the six message.usage.* numeric
  fields, and tool_use[].name.
It NEVER dereferences message.content[].text, .thinking, tool_use.input
values, tool_result.content, aiTitle, lastPrompt, or any user prompt body.
Every extracted record is a fresh dict built field-by-field from the allowlist;
the raw parsed object is never copied wholesale into any output.

Reports measured USAGE only — never a reduction claim or counterfactual
comparison. Zero LLM calls, zero network, stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .claude_code_pricing import (
    estimate_cost_usd,
    price_known,
    pricing_reference_meta,
)

WORKFLOW_NAME = "ReportRealSessionUsageWorkflow"

SKILLLAYER_TOOL_PREFIX = "mcp__skilllayer__"

# Entry ``type`` values we recognise. Anything else is counted under
# parse_health.unknown_types rather than causing a failure.
_KNOWN_ENTRY_TYPES = frozenset(
    {
        "assistant",
        "user",
        "attachment",
        "queue-operation",
        "ai-title",
        "last-prompt",
        "mode",
        "system",
        "summary",
    }
)

# Verbatim methodology text — present in every report.
METHODOLOGY = {
    "attribution_granularity": (
        "Usage is recorded per assistant MESSAGE (per API request), not per tool "
        "call. A message that calls a tool bundles that tool's request cost with "
        "all other generation in the same message."
    ),
    "tool_result_cost": (
        "Tool RESULTS carry no token counts. Their cost appears only in the NEXT "
        "message's input/cache figures and cannot be isolated."
    ),
    "no_baseline": (
        "This reports observed usage only. No counterfactual or comparison against "
        "an alternate run is possible from this data."
    ),
    "format_stability": (
        "Source is Claude Code's undocumented internal JSONL (observed app version "
        "2.1.197). Fields are parsed defensively and may drift without notice."
    ),
    "multi_tool_reconciliation": (
        "When one message invokes multiple tools, its tokens are attributed to each "
        "in per-tool views, so per-tool sums may exceed session totals. Deduped "
        "aggregates are provided separately."
    ),
}

# Verbatim attribution note for the SkillLayer-tool block.
SKILLLAYER_ATTRIBUTION_NOTE = (
    "Figures are the COMBINED cost of the assistant messages that invoked a "
    "SkillLayer tool — message-level, not isolated tool-level. The tool's result "
    "tokens are not included here; they surface in the following message's "
    "input/cache. This is not a per-tool cost and not a comparison against not "
    "using the tool."
)

# Exactly the fields the extractor is permitted to read — surfaced in the
# report's privacy block so the guarantee is auditable.
PRIVACY_FIELDS_READ = [
    "type",
    "timestamp",
    "sessionId",
    "message.model",
    "message.usage.*",
    "tool_use.name",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> int:
    """Coerce a token field to a non-negative int; anything odd -> 0."""
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return 0


def _zero_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}


def _add_tokens(acc: dict[str, int], tokens: dict[str, int]) -> None:
    for key in ("input", "output", "cache_write", "cache_read", "total"):
        acc[key] += tokens.get(key, 0)


def slug_for_cwd(cwd: str) -> str:
    """Transform an absolute path into Claude Code's project-dir slug.

    Claude Code names each project directory by replacing path separators (and
    dots) in the absolute cwd with ``-`` — e.g. ``/Users/x/Repo`` ->
    ``-Users-x-Repo``.
    """
    text = str(cwd)
    out = []
    for ch in text:
        out.append("-" if ch in ("/", "\\", ".") else ch)
    return "".join(out)


def resolve_claude_projects_dir(projects_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the Claude Code projects directory (read-only).

    Defaults to ``~/.claude/projects``. A supplied override is used verbatim.
    The path is NOT required to exist here; callers check existence and degrade
    gracefully.
    """
    if projects_dir is not None:
        return Path(projects_dir)
    return Path.home() / ".claude" / "projects"


def _looks_like_path(project: str) -> bool:
    return "/" in project or "\\" in project or project.startswith("~")


def _project_to_slug(project: str) -> str:
    """A ``project=`` argument may be an absolute path or an already-formed slug."""
    if _looks_like_path(project):
        return slug_for_cwd(str(Path(project).expanduser()))
    return project


# ---------------------------------------------------------------------------
# Scope resolution — which session files are in play
# ---------------------------------------------------------------------------

def resolve_session_files(
    *,
    cwd: str,
    scope: str = "current",
    project: str | None = None,
    projects_dir: str | os.PathLike[str] | None = None,
) -> tuple[list[tuple[str, Path]], dict[str, Any]]:
    """Return ``[(project_slug, jsonl_path), ...]`` plus a scope descriptor.

    Never raises for a missing directory: an absent projects dir or slug yields
    an empty list and a descriptive note.
    """
    base = resolve_claude_projects_dir(projects_dir)
    scope_info: dict[str, Any] = {
        "mode": scope,
        "projects_dir": str(base),
        "project_filter": project,
        "note": None,
    }

    if not base.exists() or not base.is_dir():
        scope_info["note"] = f"projects directory not found: {base}"
        return [], scope_info

    # Determine which project directories to scan.
    if project is not None:
        target_slugs = [_project_to_slug(project)]
        scope_info["mode"] = "project"
    elif scope == "all":
        target_slugs = None  # all subdirectories
    else:  # "current" (default)
        target_slugs = [slug_for_cwd(cwd)]
        scope_info["mode"] = "current"

    files: list[tuple[str, Path]] = []
    if target_slugs is None:
        for entry in sorted(base.iterdir()):
            if entry.is_dir():
                for jsonl in sorted(glob.glob(str(entry / "*.jsonl"))):
                    files.append((entry.name, Path(jsonl)))
    else:
        found_any = False
        for slug in target_slugs:
            proj_dir = base / slug
            if proj_dir.exists() and proj_dir.is_dir():
                found_any = True
                for jsonl in sorted(glob.glob(str(proj_dir / "*.jsonl"))):
                    files.append((slug, Path(jsonl)))
        if not found_any:
            scope_info["note"] = (
                f"no session logs found for project slug(s): {', '.join(target_slugs)}"
            )

    return files, scope_info


# ---------------------------------------------------------------------------
# Extraction — STRICT ALLOWLIST
# ---------------------------------------------------------------------------

def _extract_record(obj: dict[str, Any], project_slug: str) -> dict[str, Any] | None:
    """Build a fresh, allowlisted usage record from one parsed assistant entry.

    Returns None for non-assistant entries or assistant entries with no usable
    ``message.usage`` dict. The returned dict is constructed field-by-field —
    the raw ``obj`` is never embedded, so no free-text can leak downstream.
    """
    if obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    cache_creation = usage.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}

    input_tokens = _to_int(usage.get("input_tokens"))
    output_tokens = _to_int(usage.get("output_tokens"))
    cache_write = _to_int(usage.get("cache_creation_input_tokens"))
    cache_read = _to_int(usage.get("cache_read_input_tokens"))
    # The two nested cache tiers are read for completeness of the allowlist but
    # are a breakdown of cache_write and are NOT re-added to totals.
    _ = _to_int(cache_creation.get("ephemeral_1h_input_tokens"))
    _ = _to_int(cache_creation.get("ephemeral_5m_input_tokens"))

    tokens = {
        "input": input_tokens,
        "output": output_tokens,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "total": input_tokens + output_tokens + cache_write + cache_read,
    }

    # Tool names only — never the tool input/arguments.
    tool_names: list[str] = []
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    tool_names.append(name)

    model = message.get("model")
    if not isinstance(model, str):
        model = None

    session_id = obj.get("sessionId")
    session_id = session_id if isinstance(session_id, str) else "unknown"

    timestamp = obj.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else None

    return {
        "project": project_slug,
        "session_id": session_id,
        "timestamp": timestamp,
        "date": timestamp[:10] if timestamp else None,
        "model": model,
        "tokens": tokens,
        "tools": tool_names,
    }


def _in_date_window(date: str | None, since: str | None, until: str | None) -> bool:
    """Inclusive YYYY-MM-DD window. Records without a date are always kept."""
    if date is None:
        return True
    if since is not None and date < since:
        return False
    if until is not None and date > until:
        return False
    return True


def extract_records(
    files: Iterable[tuple[str, Path]],
    *,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the session files into allowlisted usage records.

    Fully defensive: malformed lines are skipped and counted; unreadable files
    are recorded. Never raises on bad data.
    """
    records: list[dict[str, Any]] = []
    parse_health: dict[str, Any] = {
        "lines_total": 0,
        "lines_parsed": 0,
        "lines_skipped": 0,
        "unknown_types": Counter(),
        "files_unreadable": [],
        "files_scanned": 0,
    }
    sessions_seen: set[str] = set()

    for project_slug, path in files:
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError as exc:
            parse_health["files_unreadable"].append({"path": str(path), "error": str(exc)})
            continue
        parse_health["files_scanned"] += 1
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                parse_health["lines_total"] += 1
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    parse_health["lines_skipped"] += 1
                    continue
                if not isinstance(obj, dict):
                    parse_health["lines_skipped"] += 1
                    continue
                parse_health["lines_parsed"] += 1

                entry_type = obj.get("type")
                if entry_type not in _KNOWN_ENTRY_TYPES:
                    parse_health["unknown_types"][str(entry_type)] += 1

                record = _extract_record(obj, project_slug)
                if record is None:
                    continue
                if not _in_date_window(record["date"], since, until):
                    continue
                records.append(record)
                sessions_seen.add(record["session_id"])

    parse_health["unknown_types"] = dict(parse_health["unknown_types"])
    parse_health["sessions_with_usage"] = len(sessions_seen)
    return records, parse_health


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _cost_or_none(tokens: dict[str, int], model: str | None) -> float | None:
    return estimate_cost_usd(tokens, model)


def _sum_cost(values: Iterable[float | None]) -> float | None:
    """Sum costs, ignoring None (unknown-model) entries. None if nothing known."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return round(sum(known), 6)


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll records up into totals / by_session / by_model / by_tool and the
    dedicated SkillLayer-tool block."""

    totals_tokens = _zero_tokens()
    totals_costs: list[float | None] = []

    by_session: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    tool_buckets: dict[str, dict[str, Any]] = {}

    models_priced: set[str] = set()
    models_unpriced: set[str] = set()

    # SkillLayer dedup accumulators
    sl_dedup_tokens = _zero_tokens()
    sl_dedup_costs: list[float | None] = []
    sl_messages = 0
    sl_multi_tool_messages = 0
    sl_tool_names: set[str] = set()

    for rec in records:
        tokens = rec["tokens"]
        model = rec["model"]
        cost = _cost_or_none(tokens, model)

        _add_tokens(totals_tokens, tokens)
        totals_costs.append(cost)

        model_key = model if model is not None else "unknown"
        if price_known(model):
            models_priced.add(model_key)
        else:
            models_unpriced.add(model_key)

        # by_session
        sess = by_session.get(rec["session_id"])
        if sess is None:
            sess = {
                "session_id": rec["session_id"],
                "project": rec["project"],
                "started_at": rec["timestamp"],
                "ended_at": rec["timestamp"],
                "assistant_messages": 0,
                "tokens": _zero_tokens(),
                "_costs": [],
            }
            by_session[rec["session_id"]] = sess
        sess["assistant_messages"] += 1
        _add_tokens(sess["tokens"], tokens)
        sess["_costs"].append(cost)
        ts = rec["timestamp"]
        if ts is not None:
            if sess["started_at"] is None or ts < sess["started_at"]:
                sess["started_at"] = ts
            if sess["ended_at"] is None or ts > sess["ended_at"]:
                sess["ended_at"] = ts

        # by_model
        mdl = by_model.get(model_key)
        if mdl is None:
            mdl = {
                "model": model_key,
                "pricing_known": price_known(model),
                "assistant_messages": 0,
                "tokens": _zero_tokens(),
                "_costs": [],
            }
            by_model[model_key] = mdl
        mdl["assistant_messages"] += 1
        _add_tokens(mdl["tokens"], tokens)
        mdl["_costs"].append(cost)

        # by_tool (message-level attribution; a message counts once per distinct tool)
        distinct_tools = list(dict.fromkeys(rec["tools"]))  # dedupe within a message
        for name in distinct_tools:
            bucket = tool_buckets.get(name)
            if bucket is None:
                bucket = {
                    "tool_name": name,
                    "invoking_messages": 0,
                    "message_level_tokens": _zero_tokens(),
                    "_costs": [],
                }
                tool_buckets[name] = bucket
            bucket["invoking_messages"] += 1
            _add_tokens(bucket["message_level_tokens"], tokens)
            bucket["_costs"].append(cost)

        # SkillLayer dedup block (message counted once even with 2 SL tools)
        sl_tools_in_msg = [t for t in distinct_tools if t.startswith(SKILLLAYER_TOOL_PREFIX)]
        if sl_tools_in_msg:
            sl_messages += 1
            _add_tokens(sl_dedup_tokens, tokens)
            sl_dedup_costs.append(cost)
            sl_tool_names.update(sl_tools_in_msg)
            if len(distinct_tools) > 1:
                sl_multi_tool_messages += 1

    # Finalise per-group cost + strip private accumulators
    def _finalise(group: dict[str, Any]) -> dict[str, Any]:
        costs = group.pop("_costs")
        group["estimated_cost_usd"] = _sum_cost(costs)
        return group

    by_session_list = [_finalise(s) for s in by_session.values()]
    by_session_list.sort(key=lambda s: (s["started_at"] or "", s["session_id"]))
    by_model_list = [_finalise(m) for m in by_model.values()]
    by_model_list.sort(key=lambda m: m["tokens"]["total"], reverse=True)

    skilllayer_tools = []
    other_tools = []
    for bucket in tool_buckets.values():
        bucket["message_level_cost_usd"] = _sum_cost(bucket.pop("_costs"))
        if bucket["tool_name"].startswith(SKILLLAYER_TOOL_PREFIX):
            skilllayer_tools.append(bucket)
        else:
            other_tools.append(bucket)
    skilllayer_tools.sort(key=lambda b: b["message_level_tokens"]["total"], reverse=True)
    other_tools.sort(key=lambda b: b["message_level_tokens"]["total"], reverse=True)

    totals = {
        "assistant_messages": len(records),
        "tokens": totals_tokens,
        "estimated_cost_usd": _sum_cost(totals_costs),
    }

    skilllayer_tool_usage = {
        "messages_invoking_a_skilllayer_tool": sl_messages,
        "distinct_skilllayer_tools": len(sl_tool_names),
        "messages_with_multiple_tools": sl_multi_tool_messages,
        "message_level_tokens": sl_dedup_tokens,
        "message_level_cost_usd": _sum_cost(sl_dedup_costs),
        "per_tool": [dict(b) for b in skilllayer_tools],
        "attribution_note": SKILLLAYER_ATTRIBUTION_NOTE,
    }

    return {
        "totals": totals,
        "by_session": by_session_list,
        "by_model": by_model_list,
        "by_tool": {"skilllayer_tools": skilllayer_tools, "other_tools": other_tools},
        "skilllayer_tool_usage": skilllayer_tool_usage,
        "models_priced": sorted(models_priced),
        "models_unpriced": sorted(models_unpriced),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _redact_project(slug: str) -> str:
    parts = [p for p in slug.split("-") if p]
    return parts[-1] if parts else slug


def build_session_usage_artifacts(
    *,
    cwd: str | None = None,
    scope: str = "current",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    projects_dir: str | os.PathLike[str] | None = None,
    redact_paths: bool = False,
) -> dict[str, Any]:
    """Produce the full ReportRealSessionUsageWorkflow report.

    Read-only, deterministic, zero LLM, zero network. Never raises on a missing
    directory or malformed data.
    """
    cwd = cwd if cwd is not None else os.getcwd()

    files, scope_info = resolve_session_files(
        cwd=cwd, scope=scope, project=project, projects_dir=projects_dir
    )
    sessions_scanned = len({str(p) for _, p in files})

    records, parse_health = extract_records(files, since=since, until=until)
    agg = aggregate(records)

    if redact_paths:
        scope_info["projects_dir"] = "<redacted>"
        for sess in agg["by_session"]:
            sess["project"] = _redact_project(sess["project"])

    sessions_included = len(agg["by_session"])

    note = scope_info.pop("note", None)
    if note is None and sessions_included == 0:
        note = "no assistant usage records found in scope"

    pricing_meta = pricing_reference_meta()
    pricing_reference = {
        **pricing_meta,
        "models_priced": agg.pop("models_priced"),
        "models_unpriced": agg.pop("models_unpriced"),
    }

    privacy = {
        "fields_read": list(PRIVACY_FIELDS_READ),
        "free_text_read": False,
        "prompt_or_response_text_in_output": False,
        "note": (
            "Only token counts, model ids, tool names, timestamps, and "
            "session/project identifiers are read. Message/thinking/tool-input/"
            "tool-result text is never accessed."
        ),
    }

    parse_health_out = {
        "lines_total": parse_health["lines_total"],
        "lines_parsed": parse_health["lines_parsed"],
        "lines_skipped": parse_health["lines_skipped"],
        "unknown_types": parse_health["unknown_types"],
        "files_unreadable": parse_health["files_unreadable"],
    }

    return {
        "workflow": WORKFLOW_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "mode": scope_info["mode"],
            "projects_dir": scope_info["projects_dir"],
            "project_filter": scope_info["project_filter"],
            "since": since,
            "until": until,
            "sessions_scanned": sessions_scanned,
            "sessions_included": sessions_included,
        },
        "note": note,
        "pricing_reference": pricing_reference,
        "totals": agg["totals"],
        "by_model": agg["by_model"],
        "by_session": agg["by_session"],
        "by_tool": agg["by_tool"],
        "skilllayer_tool_usage": agg["skilllayer_tool_usage"],
        "methodology": {k: v for k, v in METHODOLOGY.items()},
        "privacy": privacy,
        "parse_health": parse_health_out,
    }
