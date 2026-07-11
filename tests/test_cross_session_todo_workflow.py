"""Tests for the CrossSessionTodo cluster: AddTodoWorkflow, MarkTodoDoneWorkflow,
ListTodosWorkflow (build_add_todo_artifacts / build_mark_todo_done_artifacts /
build_list_todos_artifacts). Grouped in one file because all three share the
same .skilllayer/todos.json store, matching how test_memory_workflows.py
already groups SaveContextSnapshot/TrackDecisionLog/RememberUserPreferences/
RehydrateContext under one file rather than one-per-workflow.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer import SkillLayer
from skilllayer.memory.skilllayer_memory import claim_next_id, track_decision
from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import (
    build_add_todo_artifacts,
    build_list_todos_artifacts,
    build_mark_todo_done_artifacts,
)


# ---------------------------------------------------------------------------
# AddTodoWorkflow
# ---------------------------------------------------------------------------

class TestAddTodo:
    def test_first_id_is_0001(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "Fix flaky test")
        assert result["todo_id"] == "0001"
        assert result["status"] == "open"
        assert result["error_code"] is None

    def test_sequential_ids(self, tmp_path):
        r1 = build_add_todo_artifacts(tmp_path, "First")
        r2 = build_add_todo_artifacts(tmp_path, "Second")
        r3 = build_add_todo_artifacts(tmp_path, "Third")
        assert [r1["todo_id"], r2["todo_id"], r3["todo_id"]] == ["0001", "0002", "0003"]

    def test_text_is_stripped(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "  Fix flaky test  ")
        assert result["text"] == "Fix flaky test"

    def test_empty_text_rejected(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "")
        assert result["error_code"] == "empty_todo_text"
        assert result["todo_id"] is None

    def test_whitespace_only_text_rejected(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "   ")
        assert result["error_code"] == "empty_todo_text"

    def test_text_too_long_rejected(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "x" * 501)
        assert result["error_code"] == "todo_text_too_long"
        assert result["todo_id"] is None

    def test_text_at_exact_limit_accepted(self, tmp_path):
        result = build_add_todo_artifacts(tmp_path, "x" * 500)
        assert result["error_code"] is None
        assert result["todo_id"] == "0001"

    def test_written_to_todos_json(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        data = json.loads((tmp_path / ".skilllayer" / "todos.json").read_text())
        assert len(data["todos"]) == 1
        assert data["todos"][0]["text"] == "Fix flaky test"
        assert data["todos"][0]["status"] == "open"
        assert data["todos"][0]["done_at"] is None

    def test_first_add_creates_skilllayer_dir(self, tmp_path):
        # Regression test: claim_next_id/_write_state never create the
        # directory themselves — the caller must. add_todo is the first
        # write into a brand-new .skilllayer/ dir, so it must mkdir first.
        assert not (tmp_path / ".skilllayer").exists()
        result = build_add_todo_artifacts(tmp_path, "Fix flaky test")
        assert result["error_code"] is None
        assert (tmp_path / ".skilllayer" / "todos.json").exists()


# ---------------------------------------------------------------------------
# MarkTodoDoneWorkflow
# ---------------------------------------------------------------------------

class TestMarkTodoDone:
    def test_mark_done_sets_done_at(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        result = build_mark_todo_done_artifacts(tmp_path, "0001")
        assert result["status"] == "done"
        assert result["previous_status"] == "open"
        assert result["done_at"] is not None
        assert result["error_code"] is None

    def test_mark_done_persisted(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        build_mark_todo_done_artifacts(tmp_path, "0001")
        data = json.loads((tmp_path / ".skilllayer" / "todos.json").read_text())
        assert data["todos"][0]["status"] == "done"
        assert data["todos"][0]["done_at"] is not None

    def test_reopen_clears_done_at(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        build_mark_todo_done_artifacts(tmp_path, "0001")
        result = build_mark_todo_done_artifacts(tmp_path, "0001", status="open")
        assert result["status"] == "open"
        assert result["previous_status"] == "done"
        assert result["done_at"] is None

    def test_reopen_persisted(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        build_mark_todo_done_artifacts(tmp_path, "0001")
        build_mark_todo_done_artifacts(tmp_path, "0001", status="open")
        data = json.loads((tmp_path / ".skilllayer" / "todos.json").read_text())
        assert data["todos"][0]["status"] == "open"
        assert data["todos"][0]["done_at"] is None

    def test_not_found_error(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        result = build_mark_todo_done_artifacts(tmp_path, "9999")
        assert result["error_code"] == "memory_record_not_found"

    def test_not_found_on_empty_store(self, tmp_path):
        result = build_mark_todo_done_artifacts(tmp_path, "0001")
        assert result["error_code"] == "memory_record_not_found"

    def test_invalid_status_rejected(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        result = build_mark_todo_done_artifacts(tmp_path, "0001", status="in_progress")
        assert result["error_code"] == "invalid_status"

    def test_only_targeted_todo_affected(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        build_add_todo_artifacts(tmp_path, "Second")
        build_mark_todo_done_artifacts(tmp_path, "0001")
        data = json.loads((tmp_path / ".skilllayer" / "todos.json").read_text())
        statuses = {t["id"]: t["status"] for t in data["todos"]}
        assert statuses == {"0001": "done", "0002": "open"}


# ---------------------------------------------------------------------------
# ListTodosWorkflow
# ---------------------------------------------------------------------------

class TestListTodos:
    def test_default_filter_is_open(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        build_add_todo_artifacts(tmp_path, "Second")
        build_mark_todo_done_artifacts(tmp_path, "0002")
        result = build_list_todos_artifacts(tmp_path)
        assert result["filter_status"] == "open"
        assert [t["id"] for t in result["todos"]] == ["0001"]

    def test_filter_done(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        build_add_todo_artifacts(tmp_path, "Second")
        build_mark_todo_done_artifacts(tmp_path, "0002")
        result = build_list_todos_artifacts(tmp_path, status_filter="done")
        assert [t["id"] for t in result["todos"]] == ["0002"]

    def test_filter_all(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        build_add_todo_artifacts(tmp_path, "Second")
        build_mark_todo_done_artifacts(tmp_path, "0002")
        result = build_list_todos_artifacts(tmp_path, status_filter="all")
        assert [t["id"] for t in result["todos"]] == ["0001", "0002"]

    def test_counts_correct_regardless_of_filter(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        build_add_todo_artifacts(tmp_path, "Second")
        build_add_todo_artifacts(tmp_path, "Third")
        build_mark_todo_done_artifacts(tmp_path, "0002")
        result = build_list_todos_artifacts(tmp_path, status_filter="done")
        assert result["open_count"] == 2
        assert result["done_count"] == 1
        assert result["total_count"] == 3

    def test_empty_store(self, tmp_path):
        result = build_list_todos_artifacts(tmp_path)
        assert result["todos"] == []
        assert result["open_count"] == 0
        assert result["done_count"] == 0
        assert result["total_count"] == 0
        assert result["error_code"] is None

    def test_invalid_filter_rejected(self, tmp_path):
        result = build_list_todos_artifacts(tmp_path, status_filter="bogus")
        assert result["error_code"] == "invalid_status_filter"

    def test_read_only_never_writes(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "First")
        todos_path = tmp_path / ".skilllayer" / "todos.json"
        before = todos_path.read_text()
        build_list_todos_artifacts(tmp_path)
        build_list_todos_artifacts(tmp_path, status_filter="all")
        build_list_todos_artifacts(tmp_path, status_filter="done")
        after = todos_path.read_text()
        assert before == after


# ---------------------------------------------------------------------------
# Full round trip: add -> list -> mark done -> list -> reopen -> list
# ---------------------------------------------------------------------------

class TestFullRoundTrip:
    def test_add_list_done_list_reopen_list(self, tmp_path):
        add_result = build_add_todo_artifacts(tmp_path, "Fix flaky test")
        todo_id = add_result["todo_id"]

        after_add = build_list_todos_artifacts(tmp_path)
        assert after_add["open_count"] == 1
        assert after_add["todos"][0]["id"] == todo_id

        build_mark_todo_done_artifacts(tmp_path, todo_id)
        after_done = build_list_todos_artifacts(tmp_path)
        assert after_done["open_count"] == 0
        after_done_all = build_list_todos_artifacts(tmp_path, status_filter="done")
        assert after_done_all["todos"][0]["id"] == todo_id

        build_mark_todo_done_artifacts(tmp_path, todo_id, status="open")
        after_reopen = build_list_todos_artifacts(tmp_path)
        assert after_reopen["open_count"] == 1
        assert after_reopen["todos"][0]["id"] == todo_id
        assert after_reopen["todos"][0]["done_at"] is None


# ---------------------------------------------------------------------------
# Generalized ID counter — decisions and todos share .state.json but must
# not interfere with each other's sequence.
# ---------------------------------------------------------------------------

class TestGeneralizedIdCounterIntegration:
    def test_todo_and_decision_ids_independent_through_real_workflows(self, tmp_path):
        skilllayer_dir = tmp_path / ".skilllayer"
        r1 = build_add_todo_artifacts(tmp_path, "First todo")
        d1 = track_decision(skilllayer_dir, "First decision", "ctx", "dec", "reason", "conseq")
        r2 = build_add_todo_artifacts(tmp_path, "Second todo")
        d2 = track_decision(skilllayer_dir, "Second decision", "ctx", "dec", "reason", "conseq")

        assert r1["todo_id"] == "0001"
        assert r2["todo_id"] == "0002"
        assert d1["decision_id"] == "0001"
        assert d2["decision_id"] == "0002"

        state = json.loads((skilllayer_dir / ".state.json").read_text())
        assert state["next_todo_id"] == 3
        assert state["next_decision_id"] == 3

    def test_claim_next_id_still_works_for_decisions_directly(self, tmp_path):
        skilllayer_dir = tmp_path / ".skilllayer"
        skilllayer_dir.mkdir(parents=True)
        assert claim_next_id(skilllayer_dir, "next_decision_id") == "0001"
        assert claim_next_id(skilllayer_dir, "next_decision_id") == "0002"


# ---------------------------------------------------------------------------
# INDEX.md regeneration
# ---------------------------------------------------------------------------

class TestIndexRegeneration:
    def test_add_todo_triggers_regeneration(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        assert "Todos" in index_text
        assert "Fix flaky test" in index_text
        assert "1 open" in index_text

    def test_done_todo_disappears_from_index(self, tmp_path):
        add_result = build_add_todo_artifacts(tmp_path, "Fix flaky test")
        build_mark_todo_done_artifacts(tmp_path, add_result["todo_id"])
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        assert "Fix flaky test" not in index_text
        assert "0 open" in index_text

    def test_reopened_todo_reappears_in_index(self, tmp_path):
        add_result = build_add_todo_artifacts(tmp_path, "Fix flaky test")
        build_mark_todo_done_artifacts(tmp_path, add_result["todo_id"])
        build_mark_todo_done_artifacts(tmp_path, add_result["todo_id"], status="open")
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        assert "Fix flaky test" in index_text
        assert "1 open" in index_text

    def test_shows_all_open_todos_not_a_digest(self, tmp_path):
        # Unlike Decisions' "last 3", Todos should show everything open up
        # to the cap — 15 open todos (under the 20 cap) must ALL appear.
        for i in range(1, 16):
            build_add_todo_artifacts(tmp_path, f"Todo {i}")
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        for i in range(1, 16):
            assert f"Todo {i}" in index_text
        assert "15 open" in index_text

    def test_twenty_item_display_cap(self, tmp_path):
        for i in range(1, 26):  # 25 open todos, cap is 20
            build_add_todo_artifacts(tmp_path, f"Todo {i}")
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        assert "25 open" in index_text
        assert "Todo 20" in index_text
        assert "Todo 21" not in index_text
        assert "5 more open todo" in index_text

    def test_no_todos_section_when_no_todos_json(self, tmp_path):
        from skilllayer.memory.skilllayer_memory import regenerate_index
        skilllayer_dir = tmp_path / ".skilllayer"
        skilllayer_dir.mkdir(parents=True)
        index_text = regenerate_index(skilllayer_dir)
        assert "Todos" not in index_text

    def test_todos_alone_do_not_trigger_needs_wrap_section(self, tmp_path):
        # A store with only todos (no context snapshot, no file-watch data) has
        # nothing for NeedsWrap to reason about, so the section must not appear.
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        index_text = (tmp_path / ".skilllayer" / "INDEX.md").read_text()
        assert "Todos" in index_text
        assert "NeedsWrap" not in index_text


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_add_todo_no_llm_client(self, tmp_path):
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_add_todo_artifacts(tmp_path, "Fix flaky test")
            mock_cls.assert_not_called()

    def test_mark_todo_done_no_llm_client(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "Fix flaky test")
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_mark_todo_done_artifacts(tmp_path, "0001")
            mock_cls.assert_not_called()

    def test_list_todos_no_llm_client(self, tmp_path):
        with patch("skilllayer.runner.core.LLMClient") as mock_cls:
            build_list_todos_artifacts(tmp_path)
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Router — real invocations, including collision checks against existing
# patterns this cluster's phrasing could plausibly shadow.
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_add_todo_phrases(self):
        assert self._route("add a todo: fix the flaky test") == "add_todo"
        assert self._route("add todo review PR 42") == "add_todo"
        assert self._route("create a todo for updating docs") == "add_todo"
        assert self._route("new todo: refactor the parser") == "add_todo"

    def test_mark_todo_done_phrases(self):
        assert self._route("mark todo 0003 done") == "mark_todo_done"
        assert self._route("mark todo 3 as done") == "mark_todo_done"
        assert self._route("complete todo 0005") == "mark_todo_done"
        assert self._route("reopen todo 0002") == "mark_todo_done"

    def test_list_todos_phrases(self):
        assert self._route("show my todos") == "list_todos"
        assert self._route("show todos") == "list_todos"
        assert self._route("list my todos") == "list_todos"
        assert self._route("what is left to do") == "list_todos"
        assert self._route("what's left to do") == "list_todos"
        assert self._route("my todo list") == "list_todos"
        assert self._route("show all my todos") == "list_todos"
        assert self._route("show done todos") == "list_todos"

    def test_does_not_shadow_rehydrate_context(self):
        # The most likely collision risk identified at design time: none of
        # rehydrate_context's "show X" patterns mention "todo", but this
        # must be verified by real invocation, not just regex inspection.
        assert self._route("show saved context") == "rehydrate_context"
        assert self._route("show open questions") == "rehydrate_context"
        assert self._route("show recent decisions") == "rehydrate_context"
        assert self._route("show my preferences") == "rehydrate_context"

    def test_does_not_shadow_track_decision(self):
        assert self._route("add a decision about auth") == "track_decision"
        assert self._route("why did we make this architectural decision") == "track_decision"

    def test_does_not_shadow_add_helper(self):
        assert self._route("add a helper function called foo") == "add_helper_function"

    def test_does_not_shadow_search_decisions(self):
        assert self._route("search decisions about tokens") == "search_decisions"

    def test_does_not_shadow_run_tests_or_fix_failing_test(self):
        assert self._route("run the tests") == "run_tests"
        assert self._route("fix the failing tests") == "fix_failing_test"


# ---------------------------------------------------------------------------
# Full dispatch through SkillLayer().run() — confirms free-text extraction
# works end-to-end, not just the router matching a task_type.
# ---------------------------------------------------------------------------

class TestFullDispatch:
    def test_add_todo_end_to_end(self, tmp_path):
        result = SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        assert result["workflow"] == "AddTodoWorkflow"
        assert result["success"] is True
        assert result["artifacts"]["text"] == "fix the flaky test"
        assert result["todo_id"] == "0001"

    def test_add_todo_no_text_found(self, tmp_path):
        result = SkillLayer().run(tmp_path, "add todo")
        assert result["workflow"] == "AddTodoWorkflow"
        assert result["success"] is False
        assert result["artifacts"]["error_code"] == "no_todo_text_found"

    def test_mark_todo_done_end_to_end(self, tmp_path):
        SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        result = SkillLayer().run(tmp_path, "mark todo 0001 done")
        assert result["workflow"] == "MarkTodoDoneWorkflow"
        assert result["success"] is True
        assert result["status"] == "done"

    def test_reopen_todo_end_to_end(self, tmp_path):
        SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        SkillLayer().run(tmp_path, "mark todo 0001 done")
        result = SkillLayer().run(tmp_path, "reopen todo 0001")
        assert result["success"] is True
        assert result["status"] == "open"

    def test_mark_todo_done_no_id_found(self, tmp_path):
        result = SkillLayer().run(tmp_path, "mark todo done")
        assert result["workflow"] == "MarkTodoDoneWorkflow"
        assert result["success"] is False
        assert result["artifacts"]["error_code"] == "no_todo_id_found"

    def test_list_todos_end_to_end(self, tmp_path):
        SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        result = SkillLayer().run(tmp_path, "show my todos")
        assert result["workflow"] == "ListTodosWorkflow"
        assert result["success"] is True
        assert result["open_count"] == 1

    def test_list_done_todos_end_to_end(self, tmp_path):
        SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        SkillLayer().run(tmp_path, "mark todo 0001 done")
        result = SkillLayer().run(tmp_path, "show done todos")
        assert result["filter_status"] == "done"
        assert len(result["todos"]) == 1

    def test_todo_workflows_zero_llm_calls(self, tmp_path):
        r1 = SkillLayer().run(tmp_path, "add a todo: fix the flaky test")
        r2 = SkillLayer().run(tmp_path, "mark todo 0001 done")
        r3 = SkillLayer().run(tmp_path, "show my todos")
        assert r1["llm_calls"] == 0
        assert r2["llm_calls"] == 0
        assert r3["llm_calls"] == 0


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

class TestMCPTools:
    def test_all_three_registered_in_schema_list(self):
        from skilllayer.mcp_server import list_tool_schemas
        names = [t["name"] for t in list_tool_schemas()["tools"]]
        assert "skilllayer_add_todo" in names
        assert "skilllayer_mark_todo_done" in names
        assert "skilllayer_list_todos" in names

    def test_add_todo_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_add_todo
        result = skilllayer_add_todo(str(tmp_path), "Fix flaky test")
        assert result["success"] is True
        assert result["workflow"] == "AddTodoWorkflow"
        assert result["llm_calls"] == 0

    def test_mark_todo_done_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_add_todo, skilllayer_mark_todo_done
        skilllayer_add_todo(str(tmp_path), "Fix flaky test")
        result = skilllayer_mark_todo_done(str(tmp_path), "0001")
        assert result["success"] is True
        assert result["status"] == "done"

    def test_list_todos_callable_end_to_end(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_add_todo, skilllayer_list_todos
        skilllayer_add_todo(str(tmp_path), "Fix flaky test")
        result = skilllayer_list_todos(str(tmp_path))
        assert result["success"] is True
        assert result["open_count"] == 1


# ---------------------------------------------------------------------------
# Workflow stability / metadata
# ---------------------------------------------------------------------------

class TestWorkflowMetadata:
    def test_all_three_are_stable(self):
        from skilllayer.config.defaults import WORKFLOW_METADATA
        assert WORKFLOW_METADATA["AddTodoWorkflow"]["stability"] == "stable"
        assert WORKFLOW_METADATA["MarkTodoDoneWorkflow"]["stability"] == "stable"
        assert WORKFLOW_METADATA["ListTodosWorkflow"]["stability"] == "stable"

    def test_macro_sequences_registered(self):
        from skilllayer.config.defaults import WORKFLOWS
        assert WORKFLOWS["AddTodoWorkflow"] == ["ValidateTodoText", "ClaimTodoId", "WriteTodo"]
        assert WORKFLOWS["MarkTodoDoneWorkflow"] == ["LoadTodos", "UpdateTodoStatus", "SaveTodos"]
        assert WORKFLOWS["ListTodosWorkflow"] == ["LoadTodos", "FilterByStatus"]


# ---------------------------------------------------------------------------
# Regression: corrupt todos.json must never be silently wiped
#
# Previously, _read_todos() fell back to an empty in-memory todos list on any
# corrupt/wrong-shaped todos.json, and the very next add_todo/mark_todo_done
# write persisted that empty list back to disk — permanently erasing every
# real todo. _read_todos now raises TodosStoreCorruptError on genuine
# corruption (distinct from "file does not exist yet"), and add_todo/
# mark_todo_done/list_todos catch it and return error_code="todos_store_corrupt"
# WITHOUT writing anything.
# ---------------------------------------------------------------------------

class TestCorruptTodosFileNeverSilentlyWiped:
    def test_add_todo_after_corruption_does_not_lose_existing_todos(self, tmp_path):
        # Seed with real entries first.
        build_add_todo_artifacts(tmp_path, "first real todo")
        build_add_todo_artifacts(tmp_path, "second real todo")
        todos_path = tmp_path / ".skilllayer" / "todos.json"
        seeded = json.loads(todos_path.read_text())
        assert [t["text"] for t in seeded["todos"]] == ["first real todo", "second real todo"]

        # Corrupt the file.
        todos_path.write_text("{ this is not valid json at all")

        # The exact failure scenario: call add_todo on the corrupt file.
        result = build_add_todo_artifacts(tmp_path, "third todo added after corruption")

        assert result["error_code"] == "todos_store_corrupt"
        assert result["todo_id"] is None

        # The critical assertion: the file on disk was NOT overwritten. The
        # original (corrupt) bytes are still there — nothing was silently
        # wiped, and the two real todos are recoverable once the file is
        # manually repaired.
        assert todos_path.read_text() == "{ this is not valid json at all"

    def test_mark_todo_done_after_corruption_does_not_write(self, tmp_path):
        add_result = build_add_todo_artifacts(tmp_path, "a real todo")
        todos_path = tmp_path / ".skilllayer" / "todos.json"
        before = todos_path.read_text()
        todos_path.write_text("[]")  # valid JSON, but wrong shape (not {"todos": [...]})

        result = build_mark_todo_done_artifacts(tmp_path, add_result["todo_id"])

        assert result["error_code"] == "todos_store_corrupt"
        assert todos_path.read_text() == "[]"  # untouched, not overwritten with anything
        assert todos_path.read_text() != before  # sanity: we really did corrupt it first

    def test_list_todos_after_corruption_returns_error_not_empty_success(self, tmp_path):
        build_add_todo_artifacts(tmp_path, "a real todo")
        todos_path = tmp_path / ".skilllayer" / "todos.json"
        todos_path.write_text('{"todos": "not-a-list"}')

        result = build_list_todos_artifacts(tmp_path)

        assert result["error_code"] == "todos_store_corrupt"
        # Must not silently report zero todos as if the store were genuinely empty.
        assert result["total_count"] == 0
        assert result["error"] is not None

    def test_valid_missing_file_is_not_treated_as_corrupt(self, tmp_path):
        # A todos.json that simply doesn't exist yet is the normal new-store
        # case, not corruption — add_todo must succeed normally.
        result = build_add_todo_artifacts(tmp_path, "first todo ever")
        assert result["error_code"] is None
        assert result["todo_id"] == "0001"
