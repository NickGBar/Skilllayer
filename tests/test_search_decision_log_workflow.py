"""Tests for SearchDecisionLogWorkflow (build_search_decisions_artifacts)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.memory.skilllayer_memory import track_decision
from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import build_search_decisions_artifacts


def _track(
    repo: Path,
    title: str,
    context: str = "ctx",
    decision: str = "dec",
    reasoning: str = "reasoning",
    consequences: str = "cons",
    status: str = "accepted",
    supersedes: str | None = None,
) -> str:
    result = track_decision(
        repo / ".skilllayer", title, context, decision, reasoning, consequences,
        status=status, supersedes=supersedes,
    )
    return result["decision_id"]


# ---------------------------------------------------------------------------
# No decisions
# ---------------------------------------------------------------------------

class TestNoDecisions:
    def test_no_decisions_dir_returns_empty_not_error(self, tmp_path):
        result = build_search_decisions_artifacts(tmp_path, "anything")
        assert result["error_code"] is None
        assert result["results"] == []
        assert result["total_matches"] == 0

    def test_decisions_exist_but_none_match(self, tmp_path):
        _track(tmp_path, "Use JWT tokens", reasoning="stateless auth")
        result = build_search_decisions_artifacts(tmp_path, "nonexistent_term_xyz")
        assert result["results"] == []
        assert result["total_matches"] == 0


# ---------------------------------------------------------------------------
# Multi-field matching
# ---------------------------------------------------------------------------

class TestMultiFieldMatching:
    def test_matches_title(self, tmp_path):
        _track(tmp_path, "Use refresh tokens", context="x", decision="y", reasoning="z", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert result["total_matches"] == 1
        assert "title" in result["results"][0]["matched_fields"]

    def test_matches_context(self, tmp_path):
        _track(tmp_path, "Auth approach", context="refresh token concerns", decision="y", reasoning="z", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert "context" in result["results"][0]["matched_fields"]

    def test_matches_decision(self, tmp_path):
        _track(tmp_path, "Auth approach", context="x", decision="adopt refresh rotation", reasoning="z", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert "decision" in result["results"][0]["matched_fields"]

    def test_matches_reasoning(self, tmp_path):
        _track(tmp_path, "Auth approach", context="x", decision="y", reasoning="reduces refresh token theft", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert "reasoning" in result["results"][0]["matched_fields"]

    def test_matches_consequences(self, tmp_path):
        _track(tmp_path, "Auth approach", context="x", decision="y", reasoning="z", consequences="requires refresh endpoint")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert "consequences" in result["results"][0]["matched_fields"]

    def test_matches_multiple_fields_at_once(self, tmp_path):
        _track(tmp_path, "Use refresh tokens", context="x", decision="refresh rotation", reasoning="z", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        matched = set(result["results"][0]["matched_fields"])
        assert matched == {"title", "decision"}

    def test_case_insensitive(self, tmp_path):
        _track(tmp_path, "Use REFRESH Tokens")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert result["total_matches"] == 1

    def test_fields_searched_reported(self, tmp_path):
        _track(tmp_path, "Use refresh tokens")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        assert set(result["fields_searched"]) == {"title", "context", "decision", "reasoning", "consequences"}


# ---------------------------------------------------------------------------
# Fields parameter (configurable scope)
# ---------------------------------------------------------------------------

class TestFieldsParameter:
    def test_narrowed_to_title_only(self, tmp_path):
        _track(tmp_path, "Some decision", reasoning="mentions refresh tokens here")
        result = build_search_decisions_artifacts(tmp_path, "refresh", fields=["title"])
        assert result["total_matches"] == 0  # only in reasoning, which is excluded

    def test_narrowed_field_still_matches_when_present(self, tmp_path):
        _track(tmp_path, "Use refresh tokens")
        result = build_search_decisions_artifacts(tmp_path, "refresh", fields=["title"])
        assert result["total_matches"] == 1

    def test_invalid_field_name_errors(self, tmp_path):
        _track(tmp_path, "Use refresh tokens")
        result = build_search_decisions_artifacts(tmp_path, "refresh", fields=["summary"])
        assert result["error_code"] == "invalid_fields"

    def test_fields_searched_reflects_custom_scope(self, tmp_path):
        _track(tmp_path, "Use refresh tokens")
        result = build_search_decisions_artifacts(tmp_path, "refresh", fields=["title", "decision"])
        assert result["fields_searched"] == ["title", "decision"]


# ---------------------------------------------------------------------------
# Ranking — all three tiers
# ---------------------------------------------------------------------------

class TestRanking:
    def test_title_match_ranks_above_body_only_match(self, tmp_path):
        _track(tmp_path, "Some other decision", reasoning="mentions caching strategy")  # body-only match
        _track(tmp_path, "Use caching layer", reasoning="unrelated")  # title match
        result = build_search_decisions_artifacts(tmp_path, "caching")
        assert result["results"][0]["title"] == "Use caching layer"
        assert result["results"][1]["title"] == "Some other decision"

    def test_more_matched_fields_ranks_higher_within_tier(self, tmp_path):
        # Neither has a title match, so both are in the same (no-title) tier;
        # the one matching in more fields should rank first.
        _track(tmp_path, "Decision A", context="mentions caching", decision="y", reasoning="z", consequences="w")
        _track(tmp_path, "Decision B", context="mentions caching", decision="also about caching", reasoning="z", consequences="w")
        result = build_search_decisions_artifacts(tmp_path, "caching")
        assert result["results"][0]["title"] == "Decision B"  # 2 fields matched
        assert result["results"][1]["title"] == "Decision A"  # 1 field matched

    def test_newest_first_as_final_tiebreak(self, tmp_path):
        # Same tier (title match), same field count (1) — decision_id
        # (a reliable, atomically-incrementing counter) breaks the tie,
        # since `created` only has second-level resolution and these two
        # calls happen within the same wall-clock second.
        _track(tmp_path, "Use caching approach A")
        _track(tmp_path, "Use caching approach B")
        result = build_search_decisions_artifacts(tmp_path, "caching")
        ids = [r["decision_id"] for r in result["results"]]
        assert ids == ["0002", "0001"]

    def test_all_three_tiers_together(self, tmp_path):
        _track(tmp_path, "Old caching note", reasoning="body only, older")            # 0001: body-only
        _track(tmp_path, "Use caching layer", reasoning="title match, single field")   # 0002: title, 1 field
        _track(tmp_path, "Use caching layer too", decision="also caching here")        # 0003: title, 2 fields
        result = build_search_decisions_artifacts(tmp_path, "caching")
        ids_in_order = [r["decision_id"] for r in result["results"]]
        # 0003 (title + 2 fields) > 0002 (title + 1 field) > 0001 (body-only)
        assert ids_in_order == ["0003", "0002", "0001"]


# ---------------------------------------------------------------------------
# Supersession chain resolution — single and double hop
# ---------------------------------------------------------------------------

class TestSupersessionChain:
    def test_single_hop_current_decision_id(self, tmp_path):
        d1 = _track(tmp_path, "Use session cookies", reasoning="mentions refresh concept")
        d2 = _track(tmp_path, "Use JWT tokens", reasoning="refresh handling", supersedes=d1)
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        r1 = next(r for r in result["results"] if r["decision_id"] == d1)
        assert r1["status"] == "superseded"
        assert r1["superseded_by"] == d2
        assert r1["current_decision_id"] == d2

    def test_double_hop_current_decision_id(self, tmp_path):
        # d1 -> superseded by d2 -> superseded by d3. A search hit on d1 must
        # resolve current_decision_id all the way to d3, not stop at d2.
        d1 = _track(tmp_path, "Use session cookies", reasoning="refresh mention")
        d2 = _track(tmp_path, "Use JWT tokens", reasoning="unrelated", supersedes=d1)
        d3 = _track(tmp_path, "Use JWT with rotation", reasoning="unrelated", supersedes=d2)
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        r1 = next(r for r in result["results"] if r["decision_id"] == d1)
        assert r1["status"] == "superseded"
        assert r1["superseded_by"] == d2
        assert r1["current_decision_id"] == d3  # not d2 — walked the full chain

    def test_triple_hop_current_decision_id(self, tmp_path):
        d1 = _track(tmp_path, "Approach one about caching")
        d2 = _track(tmp_path, "Approach two", supersedes=d1)
        d3 = _track(tmp_path, "Approach three", supersedes=d2)
        d4 = _track(tmp_path, "Approach four", supersedes=d3)
        result = build_search_decisions_artifacts(tmp_path, "caching")
        r1 = next(r for r in result["results"] if r["decision_id"] == d1)
        assert r1["current_decision_id"] == d4

    def test_current_decision_not_superseded_resolves_to_itself(self, tmp_path):
        d1 = _track(tmp_path, "Use caching layer")
        result = build_search_decisions_artifacts(tmp_path, "caching")
        r1 = result["results"][0]
        assert r1["status"] == "accepted"
        assert r1["superseded_by"] is None
        assert r1["current_decision_id"] == d1


# ---------------------------------------------------------------------------
# Status awareness — top-level superseded_count
# ---------------------------------------------------------------------------

class TestStatusAwareness:
    def test_superseded_count_at_top_level(self, tmp_path):
        d1 = _track(tmp_path, "Use session cookies for caching")
        _track(tmp_path, "Use JWT for caching", supersedes=d1)
        result = build_search_decisions_artifacts(tmp_path, "caching")
        assert result["total_matches"] == 2
        assert result["superseded_count"] == 1

    def test_zero_superseded_count_when_none_superseded(self, tmp_path):
        _track(tmp_path, "Use caching layer A")
        _track(tmp_path, "Use caching layer B")
        result = build_search_decisions_artifacts(tmp_path, "caching")
        assert result["superseded_count"] == 0

    def test_deprecated_status_reported_as_is(self, tmp_path):
        _track(tmp_path, "Use caching layer", status="deprecated")
        result = build_search_decisions_artifacts(tmp_path, "caching")
        assert result["results"][0]["status"] == "deprecated"
        # deprecated is not the same as superseded — should not count toward it.
        assert result["superseded_count"] == 0


# ---------------------------------------------------------------------------
# matched_snippets
# ---------------------------------------------------------------------------

class TestMatchedSnippets:
    def test_snippet_present_for_each_matched_field(self, tmp_path):
        _track(tmp_path, "Use refresh tokens", reasoning="reduces the refresh token theft window significantly")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        snippets = result["results"][0]["matched_snippets"]
        assert "title" in snippets
        assert "reasoning" in snippets
        assert "refresh" in snippets["reasoning"].lower()

    def test_no_snippet_for_unmatched_field(self, tmp_path):
        _track(tmp_path, "Use refresh tokens", context="unrelated context text")
        result = build_search_decisions_artifacts(tmp_path, "refresh")
        snippets = result["results"][0]["matched_snippets"]
        assert "context" not in snippets


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_does_not_modify_decision_files(self, tmp_path):
        _track(tmp_path, "Use caching layer")
        decisions_dir = tmp_path / ".skilllayer" / "decisions"
        decision_file = next(decisions_dir.glob("*.md"))
        before = decision_file.read_text()
        build_search_decisions_artifacts(tmp_path, "caching")
        after = decision_file.read_text()
        assert before == after

    def test_does_not_touch_index(self, tmp_path):
        _track(tmp_path, "Use caching layer")
        index_path = tmp_path / ".skilllayer" / "INDEX.md"
        before = index_path.read_text()
        build_search_decisions_artifacts(tmp_path, "caching")
        after = index_path.read_text()
        assert before == after

    def test_does_not_create_new_files(self, tmp_path):
        _track(tmp_path, "Use caching layer")
        decisions_dir = tmp_path / ".skilllayer" / "decisions"
        count_before = len(list(decisions_dir.glob("*.md")))
        build_search_decisions_artifacts(tmp_path, "caching")
        count_after = len(list(decisions_dir.glob("*.md")))
        assert count_before == count_after


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _track(tmp_path, "Use caching layer")
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_search_decisions_artifacts(tmp_path, "caching")
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router — including explicit verification against the two named existing
# patterns this workflow must not shadow or be shadowed by.
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_why_did_we_choose(self):
        assert self._route("why did we choose JWT tokens") == "search_decisions"

    def test_why_did_we_decide(self):
        assert self._route("why did we decide to use refresh tokens") == "search_decisions"

    def test_why_did_we_use(self):
        assert self._route("why did we use session cookies") == "search_decisions"

    def test_why_did_we_pick(self):
        assert self._route("why did we pick postgres") == "search_decisions"

    def test_search_decisions(self):
        assert self._route("search decisions for authentication") == "search_decisions"

    def test_find_decision_about(self):
        assert self._route("find decision about authentication") == "search_decisions"

    def test_look_up_decision(self):
        assert self._route("look up decision on tokens") == "search_decisions"

    def test_query_decisions(self):
        assert self._route("query decisions for caching") == "search_decisions"

    def test_search_the_decision_log(self):
        assert self._route("search the decision log for tokens") == "search_decisions"

    # --- The two named existing patterns must NOT be shadowed ---

    def test_does_not_shadow_track_decision_bare_phrases(self):
        assert self._route("track a decision about using JWT") == "track_decision"
        assert self._route("log a decision about caching") == "track_decision"
        assert self._route("record a decision about the schema") == "track_decision"
        assert self._route("add a decision about the API design") == "track_decision"

    def test_does_not_shadow_track_decision_architectural_decision_trigger(self):
        # _looks_like_track_decision_request's bare \barchitectural\s+decision\b
        # trigger has no anchoring to track/log/record — any text containing
        # this phrase must still reach track_decision, unchanged.
        assert self._route("why did we make this architectural decision") == "track_decision"
        assert self._route("this is an important architectural decision") == "track_decision"
        assert self._route("document this architectural decision about caching") == "track_decision"

    def test_does_not_shadow_rehydrate_context_show_decisions_trigger(self):
        # _looks_like_rehydrate_context_request's bare
        # \bshow\s+(?:recent\s+)?decisions?\b trigger must still reach
        # rehydrate_context, unchanged.
        assert self._route("show decisions") == "rehydrate_context"
        assert self._route("show recent decisions") == "rehydrate_context"
        assert self._route("show decisions about tokens") == "rehydrate_context"

    def test_does_not_shadow_rehydrate_context_generally(self):
        assert self._route("rehydrate context") == "rehydrate_context"
        assert self._route("show saved context") == "rehydrate_context"


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

class TestMCPTool:
    def test_tool_registered_in_schema_list(self):
        from skilllayer.mcp_server import list_tool_schemas
        names = [t["name"] for t in list_tool_schemas()["tools"]]
        assert "skilllayer_search_decisions" in names

    def test_tool_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_search_decisions
        _track(tmp_path, "Use caching layer")
        result = skilllayer_search_decisions(str(tmp_path), "caching")
        assert result["success"] is True
        assert result["workflow"] == "SearchDecisionLogWorkflow"
        assert result["llm_calls"] == 0
        assert result["total_matches"] == 1

    def test_tool_respects_fields_parameter(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_search_decisions
        _track(tmp_path, "Some decision", reasoning="mentions caching in reasoning only")
        result = skilllayer_search_decisions(str(tmp_path), "caching", fields=["title"])
        assert result["total_matches"] == 0


# ---------------------------------------------------------------------------
# Workflow stability / metadata
# ---------------------------------------------------------------------------

class TestWorkflowMetadata:
    def test_stability_is_stable(self):
        from skilllayer.config.defaults import WORKFLOW_METADATA
        assert WORKFLOW_METADATA["SearchDecisionLogWorkflow"]["stability"] == "stable"

    def test_macro_sequence_registered(self):
        from skilllayer.config.defaults import WORKFLOWS
        assert WORKFLOWS["SearchDecisionLogWorkflow"] == [
            "LoadDecisions", "MatchFields", "RankResults", "ResolveSupersession",
        ]
