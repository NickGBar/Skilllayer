"""Tests for ReportRealSessionUsageWorkflow (build_session_usage_artifacts).

The single most important test here is TestPrivacyAllowlist — it feeds the
parser a session log stuffed with recognizable secret-like strings in every
free-text field and confirms none of them appear anywhere in the output.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.skilllayer.claude_code_pricing import (
    CLAUDE_CODE_MODEL_PRICING,
    estimate_cost_usd,
)
from src.skilllayer.config.defaults import WORKFLOW_METADATA, WORKFLOWS
from src.skilllayer.session_usage import (
    METHODOLOGY,
    SKILLLAYER_ATTRIBUTION_NOTE,
    build_session_usage_artifacts,
    slug_for_cwd,
)

FIXTURE = Path(__file__).parent / "fixtures" / "report_session_usage" / "sessions_sample.jsonl"

# Every planted secret string in the fixture's free-text fields.
PLANTED_SECRETS = [
    "PLANTED_THINKING_LEAK_TOPSECRET_AKIA1234",
    "PLANTED_TOOLINPUT_LEAK_hunter2password",
    "PLANTED_TOOLRESULT_LEAK_BEGIN_RSA_PRIVATE_KEY",
    "PLANTED_USERPROMPT_LEAK_do_not_leak_this_prompt_body",
    "PLANTED_MULTITOOL_LEAK_ghp_FAKETOKEN",
    "PLANTED_BASH_LEAK",
    "PLANTED_AITITLE_LEAK_secret_project_codename",
    "PLANTED_LASTPROMPT_LEAK_confidential_instruction",
    "PLANTED_UNKNOWNTYPE_LEAK",
]

CWD = "/Users/x/Demo"
SLUG = "-Users-x-Demo"


def _projects_dir_with_fixture(tmp_path: Path) -> Path:
    """Build a temp ~/.claude/projects layout containing the sample fixture."""
    base = tmp_path / "projects"
    proj = base / SLUG
    proj.mkdir(parents=True)
    shutil.copy(FIXTURE, proj / "sessions_sample.jsonl")
    return base


def _report(tmp_path: Path, **kwargs):
    base = _projects_dir_with_fixture(tmp_path)
    return build_session_usage_artifacts(cwd=CWD, projects_dir=str(base), **kwargs)


def _write_jsonl(path: Path, objects: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for obj in objects:
            fh.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")


# ---------------------------------------------------------------------------
# THE critical test: privacy allowlist
# ---------------------------------------------------------------------------

class TestPrivacyAllowlist:
    def test_no_planted_secret_appears_anywhere_in_output(self, tmp_path):
        report = _report(tmp_path, scope="all")
        blob = json.dumps(report)
        leaked = [s for s in PLANTED_SECRETS if s in blob]
        assert leaked == [], f"free-text leaked into output: {leaked}"

    def test_no_generic_secret_fragments_leak(self, tmp_path):
        report = _report(tmp_path, scope="all")
        blob = json.dumps(report).lower()
        for fragment in ("hunter2", "akia1234", "ghp_", "private_key", "codename", "do_not_leak"):
            assert fragment not in blob, f"fragment leaked: {fragment}"

    def test_privacy_block_declares_guarantee(self, tmp_path):
        report = _report(tmp_path, scope="all")
        privacy = report["privacy"]
        assert privacy["free_text_read"] is False
        assert privacy["prompt_or_response_text_in_output"] is False
        assert privacy["fields_read"] == [
            "type", "timestamp", "sessionId", "message.model",
            "message.usage.*", "tool_use.name",
        ]

    def test_tool_names_are_present_but_not_their_inputs(self, tmp_path):
        # Tool NAME is an allowed identifier; its input arguments are not.
        report = _report(tmp_path, scope="all")
        blob = json.dumps(report)
        assert "mcp__skilllayer__git_status" in blob  # name is fine
        assert "PLANTED_TOOLINPUT_LEAK_hunter2password" not in blob  # input is not


# ---------------------------------------------------------------------------
# Aggregation correctness
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_totals_reconcile(self, tmp_path):
        t = _report(tmp_path, scope="all")["totals"]
        assert t["assistant_messages"] == 4
        assert t["tokens"] == {
            "input": 207, "output": 103, "cache_write": 15,
            "cache_read": 300, "total": 625,
        }

    def test_by_session_split(self, tmp_path):
        sessions = {s["session_id"]: s for s in _report(tmp_path, scope="all")["by_session"]}
        assert set(sessions) == {"sess-alpha", "sess-beta"}
        assert sessions["sess-alpha"]["assistant_messages"] == 2
        assert sessions["sess-beta"]["assistant_messages"] == 2
        assert sessions["sess-alpha"]["tokens"]["total"] == 580
        assert sessions["sess-alpha"]["started_at"] == "2026-07-01T10:00:00Z"
        assert sessions["sess-alpha"]["ended_at"] == "2026-07-01T10:01:00Z"

    def test_by_model_split_and_pricing_flags(self, tmp_path):
        models = {m["model"]: m for m in _report(tmp_path, scope="all")["by_model"]}
        assert models["claude-opus-4-8"]["pricing_known"] is True
        assert models["claude-sonnet-5"]["pricing_known"] is True
        assert models["claude-imaginary-99"]["pricing_known"] is False
        assert models["claude-opus-4-8"]["assistant_messages"] == 2

    def test_by_tool_split(self, tmp_path):
        by_tool = _report(tmp_path, scope="all")["by_tool"]
        sl = {t["tool_name"]: t for t in by_tool["skilllayer_tools"]}
        other = {t["tool_name"]: t for t in by_tool["other_tools"]}
        assert set(sl) == {"mcp__skilllayer__git_status", "mcp__skilllayer__run_tests"}
        assert set(other) == {"Bash", "Read"}
        # git_status invoked in 2 messages (rec1: 360 + rec4: 10 = 370)
        assert sl["mcp__skilllayer__git_status"]["invoking_messages"] == 2
        assert sl["mcp__skilllayer__git_status"]["message_level_tokens"]["total"] == 370

    def test_totals_cost_sums_known_models_only(self, tmp_path):
        t = _report(tmp_path, scope="all")["totals"]
        expected = (
            estimate_cost_usd({"input": 100, "output": 50, "cache_write": 10, "cache_read": 200}, "claude-opus-4-8")
            + estimate_cost_usd({"input": 80, "output": 40, "cache_write": 0, "cache_read": 100}, "claude-opus-4-8")
            + estimate_cost_usd({"input": 20, "output": 10, "cache_write": 5, "cache_read": 0}, "claude-sonnet-5")
        )
        assert t["estimated_cost_usd"] == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Unknown-model handling
# ---------------------------------------------------------------------------

class TestUnknownModel:
    def test_unknown_model_cost_is_null(self, tmp_path):
        models = {m["model"]: m for m in _report(tmp_path, scope="all")["by_model"]}
        assert models["claude-imaginary-99"]["estimated_cost_usd"] is None

    def test_unknown_model_listed_in_pricing_reference(self, tmp_path):
        ref = _report(tmp_path, scope="all")["pricing_reference"]
        assert "claude-imaginary-99" in ref["models_unpriced"]
        assert "claude-opus-4-8" in ref["models_priced"]

    def test_never_guesses_a_rate(self, tmp_path):
        # A message on an unknown model must not contribute to any cost figure.
        objs = [{
            "type": "assistant", "sessionId": "u1", "timestamp": "2026-07-03T00:00:00Z",
            "message": {"model": "totally-made-up", "content": [],
                        "usage": {"input_tokens": 999999, "output_tokens": 999999,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        }]
        base = tmp_path / "projects" / SLUG
        _write_jsonl(base / "u.jsonl", objs)
        report = build_session_usage_artifacts(cwd=CWD, projects_dir=str(tmp_path / "projects"))
        assert report["totals"]["estimated_cost_usd"] is None


# ---------------------------------------------------------------------------
# Malformed-line resilience
# ---------------------------------------------------------------------------

class TestMalformedResilience:
    def test_parse_health_counts(self, tmp_path):
        ph = _report(tmp_path, scope="all")["parse_health"]
        assert ph["lines_skipped"] == 1  # the one malformed line
        assert ph["lines_parsed"] >= 7
        assert ph["lines_total"] == ph["lines_parsed"] + ph["lines_skipped"]

    def test_unknown_entry_type_recorded_not_fatal(self, tmp_path):
        ph = _report(tmp_path, scope="all")["parse_health"]
        assert ph["unknown_types"].get("weird-unknown-type") == 1

    def test_garbage_file_does_not_crash(self, tmp_path):
        base = tmp_path / "projects" / SLUG
        base.mkdir(parents=True)
        (base / "junk.jsonl").write_text("not json at all\n{still not}\n\n")
        report = build_session_usage_artifacts(cwd=CWD, projects_dir=str(tmp_path / "projects"))
        assert report["totals"]["assistant_messages"] == 0
        assert report["parse_health"]["lines_skipped"] == 2

    def test_non_dict_and_missing_usage_skipped(self, tmp_path):
        objs = [
            "[1, 2, 3]",  # valid json, not a dict
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},  # no usage
            {"type": "assistant", "message": "not-a-dict"},
        ]
        base = tmp_path / "projects" / SLUG
        _write_jsonl(base / "s.jsonl", objs)
        report = build_session_usage_artifacts(cwd=CWD, projects_dir=str(tmp_path / "projects"))
        assert report["totals"]["assistant_messages"] == 0


# ---------------------------------------------------------------------------
# Empty / missing directory handling
# ---------------------------------------------------------------------------

class TestMissingDirectory:
    def test_missing_projects_dir(self, tmp_path):
        report = build_session_usage_artifacts(cwd=CWD, projects_dir=str(tmp_path / "does_not_exist"))
        assert report["workflow"] == "ReportRealSessionUsageWorkflow"
        assert report["scope"]["sessions_included"] == 0
        assert report["note"]
        assert report["totals"]["assistant_messages"] == 0

    def test_missing_slug_for_current_scope(self, tmp_path):
        (tmp_path / "projects").mkdir()
        report = build_session_usage_artifacts(cwd="/no/such/project", projects_dir=str(tmp_path / "projects"))
        assert report["scope"]["sessions_included"] == 0
        assert "no session logs found" in report["note"]

    def test_empty_report_still_has_mandatory_blocks(self, tmp_path):
        report = build_session_usage_artifacts(cwd=CWD, projects_dir=str(tmp_path / "nope"))
        assert "methodology" in report
        assert "privacy" in report
        assert "parse_health" in report
        assert report["skilllayer_tool_usage"]["messages_invoking_a_skilllayer_tool"] == 0


# ---------------------------------------------------------------------------
# Multi-tool reconciliation math
# ---------------------------------------------------------------------------

class TestMultiToolReconciliation:
    def test_skilllayer_dedup_message_count(self, tmp_path):
        block = _report(tmp_path, scope="all")["skilllayer_tool_usage"]
        # rec1 (git_status), rec2 (run_tests + Bash), rec4 (git_status) -> 3 messages
        assert block["messages_invoking_a_skilllayer_tool"] == 3
        assert block["distinct_skilllayer_tools"] == 2

    def test_multi_tool_overlap_counter(self, tmp_path):
        block = _report(tmp_path, scope="all")["skilllayer_tool_usage"]
        # only rec2 invoked >1 distinct tool while touching a SkillLayer tool
        assert block["messages_with_multiple_tools"] == 1

    def test_global_by_tool_overlap_exceeds_totals(self, tmp_path):
        # The multi-tool message (rec2: run_tests + Bash) is counted in BOTH
        # tool buckets, so the global by_tool token sum exceeds session totals by
        # exactly that one message's tokens — the documented overlap.
        report = _report(tmp_path, scope="all")
        totals_total = report["totals"]["tokens"]["total"]  # 625
        buckets = report["by_tool"]["skilllayer_tools"] + report["by_tool"]["other_tools"]
        global_sum = sum(b["message_level_tokens"]["total"] for b in buckets)
        # git_status 370 + run_tests 220 + Bash 220 + Read 35 = 845
        assert global_sum == 845
        assert global_sum - totals_total == 220  # rec2 counted twice

    def test_skilllayer_dedup_matches_per_tool_when_no_sl_overlap(self, tmp_path):
        # No message here invoked two *SkillLayer* tools, so the deduped SL total
        # equals the SL per-tool sum: rec1(360) + rec2(220) + rec4(10) = 590.
        block = _report(tmp_path, scope="all")["skilllayer_tool_usage"]
        assert block["message_level_tokens"]["total"] == 590
        per_tool_total = sum(t["message_level_tokens"]["total"] for t in block["per_tool"])
        assert per_tool_total == 590

    def test_attribution_note_verbatim(self, tmp_path):
        block = _report(tmp_path, scope="all")["skilllayer_tool_usage"]
        assert block["attribution_note"] == SKILLLAYER_ATTRIBUTION_NOTE
        assert "message-level, not isolated tool-level" in block["attribution_note"]


# ---------------------------------------------------------------------------
# Scope / filters
# ---------------------------------------------------------------------------

class TestScope:
    def test_slug_transform(self):
        assert slug_for_cwd("/Users/x/Demo") == "-Users-x-Demo"
        assert slug_for_cwd("/a.b/c") == "-a-b-c"

    def test_current_scope_default(self, tmp_path):
        report = _report(tmp_path)  # scope defaults to current
        assert report["scope"]["mode"] == "current"
        assert report["totals"]["assistant_messages"] == 4

    def test_project_override(self, tmp_path):
        base = _projects_dir_with_fixture(tmp_path)
        report = build_session_usage_artifacts(cwd="/elsewhere", projects_dir=str(base), project=SLUG)
        assert report["scope"]["mode"] == "project"
        assert report["totals"]["assistant_messages"] == 4

    def test_since_until_filter(self, tmp_path):
        report = _report(tmp_path, scope="all", since="2026-07-02", until="2026-07-02")
        # only sess-beta's two records fall on 2026-07-02
        assert report["totals"]["assistant_messages"] == 2
        assert {s["session_id"] for s in report["by_session"]} == {"sess-beta"}

    def test_redact_paths(self, tmp_path):
        report = _report(tmp_path, scope="all", redact_paths=True)
        assert report["scope"]["projects_dir"] == "<redacted>"
        for s in report["by_session"]:
            assert "/" not in s["project"] and "-Users-" not in s["project"]


# ---------------------------------------------------------------------------
# Determinism, methodology, metadata, and the "savings" prohibition
# ---------------------------------------------------------------------------

class TestContractAndMethodology:
    def test_methodology_block_verbatim(self, tmp_path):
        methodology = _report(tmp_path, scope="all")["methodology"]
        assert methodology == METHODOLOGY
        assert "per assistant MESSAGE" in methodology["attribution_granularity"]
        assert "No counterfactual" in methodology["no_baseline"]
        assert "2.1.197" in methodology["format_stability"]

    def test_zero_llm_calls_via_mcp_wrapper(self, tmp_path):
        from src.skilllayer.mcp_server import skilllayer_report_session_usage
        base = _projects_dir_with_fixture(tmp_path)
        result = skilllayer_report_session_usage(scope="all", cwd=CWD, projects_dir=str(base))
        assert result["success"] is True
        assert result["llm_calls"] == 0
        assert result["workflow"] == "ReportRealSessionUsageWorkflow"

    def test_deterministic_repeated_runs(self, tmp_path):
        base = _projects_dir_with_fixture(tmp_path)
        a = build_session_usage_artifacts(cwd=CWD, projects_dir=str(base), scope="all")
        b = build_session_usage_artifacts(cwd=CWD, projects_dir=str(base), scope="all")
        a.pop("generated_at"), b.pop("generated_at")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_workflow_registered_and_stable(self):
        assert "ReportRealSessionUsageWorkflow" in WORKFLOWS
        meta = WORKFLOW_METADATA["ReportRealSessionUsageWorkflow"]
        assert meta["stability"] == "stable"

    def test_savings_word_absent_from_output(self, tmp_path):
        # redact_paths removes the (test-controlled) filesystem path from the
        # payload so the check targets SkillLayer's own text/keys, not the
        # pytest tmp-dir name.
        report = _report(tmp_path, scope="all", redact_paths=True)
        assert "savings" not in json.dumps(report).lower()

    def test_savings_word_absent_from_metadata(self):
        meta = WORKFLOW_METADATA["ReportRealSessionUsageWorkflow"]
        assert "savings" not in json.dumps(meta).lower()
