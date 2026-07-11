"""Tests for CompareContextSnapshotsWorkflow (build_compare_context_snapshots_artifacts)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.memory.skilllayer_memory import save_context_snapshot
from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import build_compare_context_snapshots_artifacts


def _save(repo: Path, state: str, open_questions: list[str]) -> None:
    save_context_snapshot(repo / ".skilllayer", state, open_questions)


# ---------------------------------------------------------------------------
# Insufficient snapshots
# ---------------------------------------------------------------------------

class TestInsufficientSnapshots:
    def test_no_snapshots_at_all(self, tmp_path):
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["error_code"] == "insufficient_snapshots"

    def test_only_one_snapshot(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["error_code"] == "insufficient_snapshots"

    def test_error_response_has_null_snapshots(self, tmp_path):
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["from_snapshot"] is None
        assert result["to_snapshot"] is None


# ---------------------------------------------------------------------------
# Default selection: latest vs. most recent history
# ---------------------------------------------------------------------------

class TestDefaultSelection:
    def test_to_is_latest(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["to_snapshot"]["id"] == "latest"

    def test_from_is_one_snapshot_back(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        # "from" should be the rotated history file, not "latest" itself.
        assert result["from_snapshot"]["id"] != "latest"
        assert result["from_snapshot"]["source"].startswith(".skilllayer/context/history/")

    def test_three_snapshots_default_compares_last_two(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        _save(tmp_path, "State C", ["Q3?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["to_snapshot"]["id"] == "latest"
        assert result["state_diff"]["word_count_after"] == 2  # "State C"


# ---------------------------------------------------------------------------
# Index-based selection
# ---------------------------------------------------------------------------

class TestIndexSelection:
    def test_compare_index_2_vs_latest(self, tmp_path):
        # save_context_snapshot's history filenames have second-level
        # resolution (_utc_filename_ts), so two rotations within the same
        # wall-clock second collide and silently overwrite each other. Rather
        # than depend on real-time spacing between calls to get 3 distinct
        # history entries reliably, construct the third (oldest) snapshot
        # directly at a controlled, guaranteed-earlier timestamp.
        from skilllayer.memory.skilllayer_memory import write_frontmatter

        _save(tmp_path, "State B", [])
        _save(tmp_path, "State C", [])
        history_dir = tmp_path / ".skilllayer" / "context" / "history"
        oldest = history_dir / "20200101T000000Z.md"
        oldest.write_text(
            write_frontmatter(
                {"schema": 1, "type": "context_snapshot", "workflow": "SaveContextSnapshotWorkflow",
                 "id": "20200101T000000Z", "created": "2020-01-01T00:00:00Z", "updated": "2020-01-01T00:00:00Z"},
                "## State\nState A\n\n## Open Questions\n(none)\n",
            ),
            encoding="utf-8",
        )
        # index 0 = latest ("State C"), 1 = most recent history ("State B"), 2 = oldest ("State A")
        result = build_compare_context_snapshots_artifacts(tmp_path, from_index=2, to_index=0)
        assert result["error_code"] is None
        assert result["to_snapshot"]["id"] == "latest"
        assert result["from_snapshot"]["id"] == "20200101T000000Z"
        assert result["state_diff"]["unified_diff"]
        assert any("State A" in line for line in result["state_diff"]["unified_diff"])

    def test_invalid_index_out_of_range(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        result = build_compare_context_snapshots_artifacts(tmp_path, from_index=99)
        assert result["error_code"] == "snapshot_not_found"

    def test_negative_index_rejected(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        result = build_compare_context_snapshots_artifacts(tmp_path, from_index=-1)
        assert result["error_code"] == "snapshot_not_found"


# ---------------------------------------------------------------------------
# Timestamp-based selection
# ---------------------------------------------------------------------------

class TestTimestampSelection:
    def test_compare_by_explicit_timestamp(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        history_dir = tmp_path / ".skilllayer" / "context" / "history"
        history_file = next(history_dir.glob("*.md"))
        result = build_compare_context_snapshots_artifacts(
            tmp_path, from_timestamp=history_file.stem, to_timestamp="latest",
        )
        assert result["error_code"] is None
        assert result["from_snapshot"]["id"] == history_file.stem
        assert result["to_snapshot"]["id"] == "latest"

    def test_unknown_timestamp_errors(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        result = build_compare_context_snapshots_artifacts(tmp_path, from_timestamp="20000101T000000Z")
        assert result["error_code"] == "snapshot_not_found"


# ---------------------------------------------------------------------------
# Open questions diff: resolved / new / still_open
# ---------------------------------------------------------------------------

class TestOpenQuestionsDiff:
    def test_resolved_new_still_open_correctness(self, tmp_path):
        _save(tmp_path, "State A", ["Refresh tokens?", "Expiry time?"])
        _save(tmp_path, "State B", ["Expiry time?", "Token revocation?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        oq = result["open_questions_diff"]
        assert oq["resolved"] == ["Refresh tokens?"]
        assert oq["new"] == ["Token revocation?"]
        assert oq["still_open"] == ["Expiry time?"]
        assert oq["resolved_count"] == 1
        assert oq["new_count"] == 1
        assert oq["still_open_count"] == 1

    def test_no_questions_in_either_snapshot(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        oq = result["open_questions_diff"]
        assert oq == {
            "resolved": [], "new": [], "still_open": [],
            "resolved_count": 0, "new_count": 0, "still_open_count": 0,
        }

    def test_all_questions_carried_over_unchanged(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?", "Q2?"])
        _save(tmp_path, "State B", ["Q1?", "Q2?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        oq = result["open_questions_diff"]
        assert oq["still_open"] == ["Q1?", "Q2?"]
        assert oq["resolved"] == []
        assert oq["new"] == []


# ---------------------------------------------------------------------------
# THE REWORDING LIMITATION — explicit, named test
# ---------------------------------------------------------------------------

class TestRewordingLimitation:
    """Documents a real, deliberate limitation: this diff is exact-string-match
    only (zero LLM calls). A question that was reworded rather than resolved
    is NOT recognized as "the same question, changed" — it shows up as one
    resolved question plus one unrelated new question. This is the expected,
    documented behavior, not a bug to be fixed by this workflow."""

    def test_reworded_question_shows_as_resolved_plus_new(self, tmp_path):
        _save(tmp_path, "State A", ["Should we support refresh tokens?"])
        _save(tmp_path, "State B", ["Should refresh tokens be supported?"])  # same question, reworded
        result = build_compare_context_snapshots_artifacts(tmp_path)
        oq = result["open_questions_diff"]
        assert oq["resolved"] == ["Should we support refresh tokens?"]
        assert oq["new"] == ["Should refresh tokens be supported?"]
        assert oq["still_open"] == []
        # Explicitly NOT recognized as the same question:
        assert oq["resolved_count"] == 1
        assert oq["new_count"] == 1

    def test_reworded_question_with_unrelated_still_open(self, tmp_path):
        _save(tmp_path, "State A", ["Should we support refresh tokens?", "Expiry time?"])
        _save(tmp_path, "State B", ["Should refresh tokens be supported?", "Expiry time?"])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        oq = result["open_questions_diff"]
        assert oq["resolved"] == ["Should we support refresh tokens?"]
        assert oq["new"] == ["Should refresh tokens be supported?"]
        assert oq["still_open"] == ["Expiry time?"]


# ---------------------------------------------------------------------------
# State diff — textual, not semantic
# ---------------------------------------------------------------------------

class TestStateDiff:
    def test_changed_flag_true_on_real_change(self, tmp_path):
        _save(tmp_path, "State A", [])
        _save(tmp_path, "State B", [])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["state_diff"]["changed"] is True

    def test_changed_flag_false_on_identical_state(self, tmp_path):
        _save(tmp_path, "Same state text", [])
        _save(tmp_path, "Same state text", [])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["state_diff"]["changed"] is False
        assert result["state_diff"]["unified_diff"] == []

    def test_word_counts(self, tmp_path):
        _save(tmp_path, "one two three", [])
        _save(tmp_path, "one two three four five", [])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        assert result["state_diff"]["word_count_before"] == 3
        assert result["state_diff"]["word_count_after"] == 5

    def test_unified_diff_contains_actual_line_changes(self, tmp_path):
        _save(tmp_path, "Line one\nLine two", [])
        _save(tmp_path, "Line one\nLine two modified", [])
        result = build_compare_context_snapshots_artifacts(tmp_path)
        diff_text = "\n".join(result["state_diff"]["unified_diff"])
        assert "Line two" in diff_text
        assert "modified" in diff_text


# ---------------------------------------------------------------------------
# Read-only — never writes
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_does_not_modify_latest_snapshot(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        latest_path = tmp_path / ".skilllayer" / "context" / "latest.md"
        before = latest_path.read_text()
        build_compare_context_snapshots_artifacts(tmp_path)
        after = latest_path.read_text()
        assert before == after

    def test_does_not_touch_index(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        index_path = tmp_path / ".skilllayer" / "INDEX.md"
        before = index_path.read_text()
        build_compare_context_snapshots_artifacts(tmp_path)
        after = index_path.read_text()
        assert before == after

    def test_does_not_create_new_history_files(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        history_dir = tmp_path / ".skilllayer" / "context" / "history"
        count_before = len(list(history_dir.glob("*.md")))
        build_compare_context_snapshots_artifacts(tmp_path)
        count_after = len(list(history_dir.glob("*.md")))
        assert count_before == count_after


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_compare_context_snapshots_artifacts(tmp_path)
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_compare_context_snapshots(self):
        assert self._route("compare context snapshots") == "compare_context_snapshots"

    def test_diff_my_context_snapshots(self):
        assert self._route("diff my context snapshots") == "compare_context_snapshots"

    def test_compare_my_snapshots(self):
        assert self._route("compare my snapshots") == "compare_context_snapshots"

    def test_how_did_understanding_change(self):
        assert self._route("how did my understanding change") == "compare_context_snapshots"

    def test_does_not_shadow_rehydrate_context(self):
        assert self._route("rehydrate context") == "rehydrate_context"
        assert self._route("show saved context") == "rehydrate_context"


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

class TestMCPTool:
    def test_tool_registered_in_schema_list(self):
        from skilllayer.mcp_server import list_tool_schemas
        names = [t["name"] for t in list_tool_schemas()["tools"]]
        assert "skilllayer_compare_context_snapshots" in names

    def test_tool_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_compare_context_snapshots
        _save(tmp_path, "State A", ["Q1?"])
        _save(tmp_path, "State B", ["Q2?"])
        result = skilllayer_compare_context_snapshots(str(tmp_path))
        assert result["success"] is True
        assert result["workflow"] == "CompareContextSnapshotsWorkflow"
        assert result["llm_calls"] == 0

    def test_tool_reports_insufficient_snapshots(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_compare_context_snapshots
        result = skilllayer_compare_context_snapshots(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "insufficient_snapshots"


# ---------------------------------------------------------------------------
# Workflow stability / metadata
# ---------------------------------------------------------------------------

class TestWorkflowMetadata:
    def test_stability_is_stable(self):
        from skilllayer.config.defaults import WORKFLOW_METADATA
        assert WORKFLOW_METADATA["CompareContextSnapshotsWorkflow"]["stability"] == "stable"

    def test_macro_sequence_registered(self):
        from skilllayer.config.defaults import WORKFLOWS
        assert "CompareContextSnapshotsWorkflow" in WORKFLOWS
        assert len(WORKFLOWS["CompareContextSnapshotsWorkflow"]) > 0
