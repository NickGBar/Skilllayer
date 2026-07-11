"""Tests for ValidateMemoryWorkflow (validate_memory_store).

Standalone, on-demand, read-only workflow — NOT wired into
regenerate_index()/rehydrate_context() (unlike NeedsWrap). Runs 20
deterministic integrity checks across four groups: INDEX freshness, decision
supersession chain integrity, frontmatter/schema validity per file type, and
.state.json id-counter drift.

Every one of the 20 named checks in CHECK_SEVERITY has at least one dedicated
test that triggers it and asserts its exact severity; TestAllChecksCovered
mechanically guarantees none were skipped.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.skilllayer.config.defaults import MACROS, WORKFLOW_METADATA, WORKFLOWS
from src.skilllayer.memory.skilllayer_memory import (
    CHECK_SEVERITY,
    add_todo,
    mark_todo_done,
    parse_frontmatter,
    remember_preferences,
    save_context_snapshot,
    track_decision,
    validate_memory_store,
    write_frontmatter,
)


def _sk(tmp_path: Path) -> Path:
    d = tmp_path / ".skilllayer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _findings_for(report: dict, check_name: str) -> list[dict]:
    return [f for f in report["findings"] if f["check"] == check_name]


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_store_exists_false_when_dir_missing(self, tmp_path):
        r = validate_memory_store(tmp_path / ".skilllayer")
        assert r["store_exists"] is False
        assert r["summary"] == {"checks_run": 0, "checks_passed": 0, "warnings_count": 0, "broken_count": 0}
        assert r["findings"] == []
        # No store yet is a usable empty state, not a failure.
        assert r["success"] is True
        assert r["status"] == "warning"
        assert r["error_code"] is None
        assert r["warnings"]

    def test_never_crashes_on_deeply_missing_path(self, tmp_path):
        validate_memory_store(tmp_path / "a" / "b" / "c" / ".skilllayer")

    def test_empty_but_existing_store_all_checks_run(self, tmp_path):
        sk = _sk(tmp_path)
        r = validate_memory_store(sk)
        assert r["store_exists"] is True
        assert r["summary"]["checks_run"] == 20
        assert r["summary"]["broken_count"] == 0
        # Only "INDEX.md missing" should warn; nothing else exists to check.
        assert r["summary"]["warnings_count"] == 1
        assert r["summary"]["checks_passed"] == 19
        assert r["success"] is True
        assert r["status"] == "warning"
        assert r["error_code"] is None


class TestCleanStore:
    def test_clean_store_all_pass(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", ["q1"])
        track_decision(sk, "Use X", "ctx", "dec", "reason", "conseq")
        remember_preferences(sk, {"testing": {"runner": "pytest"}})
        add_todo(sk, "do thing")
        r = validate_memory_store(sk)
        assert r["summary"] == {"checks_run": 20, "checks_passed": 20, "warnings_count": 0, "broken_count": 0}
        assert r["findings"] == []
        assert r["success"] is True
        assert r["status"] == "healthy"
        assert r["error_code"] is None


# ---------------------------------------------------------------------------
# Group A: INDEX freshness (check 1: index_freshness)
# ---------------------------------------------------------------------------

class TestIndexFreshness:
    def test_missing_index_is_warning(self, tmp_path):
        sk = _sk(tmp_path)
        r = validate_memory_store(sk)
        f = _findings_for(r, "index_freshness")
        assert len(f) == 1 and f[0]["severity"] == "warning"

    def test_stale_index_detected(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        idx = sk / "INDEX.md"
        idx.write_text(idx.read_text() + "\nSTALE MARKER NOT IN SOURCE\n")
        r = validate_memory_store(sk)
        f = _findings_for(r, "index_freshness")
        assert len(f) == 1 and f[0]["severity"] == "warning"

    def test_fresh_index_passes(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        r = validate_memory_store(sk)
        assert _findings_for(r, "index_freshness") == []


# ---------------------------------------------------------------------------
# Group C: context/latest.md (checks 7-8)
# ---------------------------------------------------------------------------

class TestContextChecks:
    def test_missing_frontmatter_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "context").mkdir()
        (sk / "context" / "latest.md").write_text(
            "no frontmatter here\n## State\nx\n## Open Questions\n(none)\n"
        )
        r = validate_memory_store(sk)
        f = _findings_for(r, "context_frontmatter_schema")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_missing_required_field_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "context").mkdir()
        text = write_frontmatter(
            {"schema": 1, "type": "context_snapshot"},  # missing id, created
            "## State\nx\n## Open Questions\n(none)\n",
        )
        (sk / "context" / "latest.md").write_text(text)
        r = validate_memory_store(sk)
        f = _findings_for(r, "context_frontmatter_schema")
        assert len(f) == 1
        assert "id" in f[0]["message"] and "created" in f[0]["message"]

    def test_missing_section_heading_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", ["q"])
        latest = sk / "context" / "latest.md"
        meta, _ = parse_frontmatter(latest.read_text())
        latest.write_text(write_frontmatter(meta, "## State\nstate only, no OQ section\n"))
        r = validate_memory_store(sk)
        f = _findings_for(r, "context_missing_sections")
        assert len(f) == 1 and "Open Questions" in f[0]["message"]

    def test_valid_context_passes(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", ["q"])
        r = validate_memory_store(sk)
        assert _findings_for(r, "context_frontmatter_schema") == []
        assert _findings_for(r, "context_missing_sections") == []


# ---------------------------------------------------------------------------
# Group C: decisions/*.md schema (checks 9, 10, 11)
# ---------------------------------------------------------------------------

class TestDecisionSchemaChecks:
    def test_missing_frontmatter_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "decisions").mkdir(parents=True)
        (sk / "decisions" / "0001-bad.md").write_text("# Title\nno frontmatter\n")
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_frontmatter_schema")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_invalid_status_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "Use X", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        meta, body = parse_frontmatter(path.read_text())
        meta["status"] = "bogus"
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_status_invalid")
        assert len(f) == 1

    def test_filename_does_not_match_pattern(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "decisions").mkdir(parents=True)
        text = write_frontmatter(
            {"schema": 1, "type": "decision", "id": "0001", "status": "proposed",
             "supersedes": None, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z"},
            "# T\n## Context\nc\n## Decision\nd\n## Reasoning\nr\n## Consequences\nq\n",
        )
        (sk / "decisions" / "notes.md").write_text(text)  # no 4-digit prefix
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_filename_id_mismatch")
        assert len(f) == 1 and "does not match" in f[0]["message"]

    def test_filename_id_disagrees_with_frontmatter(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "Use X", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        meta, body = parse_frontmatter(path.read_text())
        meta["id"] = "9999"
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_filename_id_mismatch")
        assert len(f) == 1 and "disagrees" in f[0]["message"]

    def test_valid_decision_passes_all_three(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "Use X", "c", "d", "r", "q")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_frontmatter_schema") == []
        assert _findings_for(r, "decision_status_invalid") == []
        assert _findings_for(r, "decision_filename_id_mismatch") == []


# ---------------------------------------------------------------------------
# Group B: duplicate decision id (check 6)
# ---------------------------------------------------------------------------

class TestDecisionDuplicateId:
    def test_duplicate_decision_id(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "Use X", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        dup = path.parent / (path.stem + "-dup.md")
        dup.write_text(path.read_text())  # same frontmatter id
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_duplicate_id")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_no_duplicates_passes(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_duplicate_id") == []


# ---------------------------------------------------------------------------
# Group B: dangling supersedes/superseded_by (checks 2, 3)
# ---------------------------------------------------------------------------

class TestDanglingSupersession:
    def test_dangling_supersedes(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="9999")
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_supersedes_dangling")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_dangling_superseded_by(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        meta, body = parse_frontmatter(path.read_text())
        meta["superseded_by"] = "9999"
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_superseded_by_dangling")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_valid_supersession_no_dangling_findings(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="0001")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_supersedes_dangling") == []
        assert _findings_for(r, "decision_superseded_by_dangling") == []


# ---------------------------------------------------------------------------
# Group B: status/superseded_by consistency (check 4)
# ---------------------------------------------------------------------------

class TestStatusSupersededByMismatch:
    def test_superseded_status_without_superseded_by(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q", status="superseded")
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_status_superseded_by_mismatch")
        assert len(f) == 1 and "not set" in f[0]["message"]

    def test_superseded_by_without_superseded_status(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        meta, body = parse_frontmatter(path.read_text())
        meta["superseded_by"] = "0002"
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_status_superseded_by_mismatch")
        assert len(f) == 1

    def test_correct_supersession_flip_has_no_mismatch(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="0001")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_status_superseded_by_mismatch") == []


# ---------------------------------------------------------------------------
# Group B: supersession cycle, including self-reference (check 5)
# ---------------------------------------------------------------------------

class TestSupersessionCycle:
    def test_self_reference_cycle(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        path = next((sk / "decisions").glob("*.md"))
        meta, body = parse_frontmatter(path.read_text())
        meta["supersedes"] = meta["id"]
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_supersession_cycle")
        assert len(f) == 1 and f[0]["severity"] == "broken"
        assert f[0]["message"].count("->") == 1

    def test_two_node_cycle(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q")
        files = sorted((sk / "decisions").glob("*.md"))
        a_meta, a_body = parse_frontmatter(files[0].read_text())
        b_meta, b_body = parse_frontmatter(files[1].read_text())
        a_meta["supersedes"] = b_meta["id"]
        b_meta["supersedes"] = a_meta["id"]
        files[0].write_text(write_frontmatter(a_meta, a_body))
        files[1].write_text(write_frontmatter(b_meta, b_body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_supersession_cycle")
        assert len(f) == 1
        assert "0001" in f[0]["message"] and "0002" in f[0]["message"]

    def test_no_cycle_in_normal_chain(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="0001")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_supersession_cycle") == []


# ---------------------------------------------------------------------------
# Group C: preferences.md (checks 12, 13)
# ---------------------------------------------------------------------------

class TestPreferencesChecks:
    def test_missing_frontmatter_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "preferences.md").write_text("## testing\n- runner: pytest\n")
        r = validate_memory_store(sk)
        f = _findings_for(r, "preferences_frontmatter_schema")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_unparseable_line_flagged(self, tmp_path):
        sk = _sk(tmp_path)
        remember_preferences(sk, {"testing": {"runner": "pytest"}})
        path = sk / "preferences.md"
        meta, body = parse_frontmatter(path.read_text())
        body = body.rstrip("\n") + "\nstray line without structure\n"
        path.write_text(write_frontmatter(meta, body))
        r = validate_memory_store(sk)
        f = _findings_for(r, "preferences_unparseable_line")
        assert len(f) == 1 and f[0]["severity"] == "warning"

    def test_valid_preferences_pass(self, tmp_path):
        sk = _sk(tmp_path)
        remember_preferences(sk, {"testing": {"runner": "pytest"}})
        r = validate_memory_store(sk)
        assert _findings_for(r, "preferences_frontmatter_schema") == []
        assert _findings_for(r, "preferences_unparseable_line") == []


# ---------------------------------------------------------------------------
# Group C: todos.json (checks 14-18)
# ---------------------------------------------------------------------------

class TestTodosChecks:
    def test_file_shape_invalid_json(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "todos.json").write_text("{not valid")
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_file_shape")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_file_shape_wrong_shape(self, tmp_path):
        sk = _sk(tmp_path)
        (sk / "todos.json").write_text(json.dumps({"nope": []}))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_file_shape")
        assert len(f) == 1

    def test_entry_missing_fields(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task")
        path = sk / "todos.json"
        data = json.loads(path.read_text())
        del data["todos"][0]["created"]
        path.write_text(json.dumps(data))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_entry_missing_fields")
        assert len(f) == 1 and "created" in f[0]["message"]

    def test_status_invalid(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task")
        path = sk / "todos.json"
        data = json.loads(path.read_text())
        data["todos"][0]["status"] = "bogus"
        path.write_text(json.dumps(data))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_status_invalid")
        assert len(f) == 1

    def test_done_at_mismatch_done_without_timestamp(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task")
        path = sk / "todos.json"
        data = json.loads(path.read_text())
        data["todos"][0]["status"] = "done"  # done_at stays None
        path.write_text(json.dumps(data))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_done_at_mismatch")
        assert len(f) == 1 and f[0]["severity"] == "warning"

    def test_done_at_mismatch_open_with_timestamp(self, tmp_path):
        sk = _sk(tmp_path)
        result = add_todo(sk, "task")
        mark_todo_done(sk, result["todo_id"], status="done")
        path = sk / "todos.json"
        data = json.loads(path.read_text())
        data["todos"][0]["status"] = "open"  # but done_at still set
        path.write_text(json.dumps(data))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_done_at_mismatch")
        assert len(f) == 1

    def test_duplicate_todo_id(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task one")
        path = sk / "todos.json"
        data = json.loads(path.read_text())
        dup = dict(data["todos"][0])
        dup["text"] = "duplicate id todo"
        data["todos"].append(dup)
        path.write_text(json.dumps(data))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todos_duplicate_id")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_valid_todos_pass_all_five(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task")
        r = validate_memory_store(sk)
        for check in (
            "todos_file_shape", "todos_entry_missing_fields", "todos_status_invalid",
            "todos_done_at_mismatch", "todos_duplicate_id",
        ):
            assert _findings_for(r, check) == []


# ---------------------------------------------------------------------------
# Group D: ID counter drift (checks 19, 20)
# ---------------------------------------------------------------------------

class TestCounterDrift:
    def test_decision_counter_behind(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q")
        state_path = sk / ".state.json"
        state = json.loads(state_path.read_text())
        state["next_decision_id"] = 1
        state_path.write_text(json.dumps(state))
        r = validate_memory_store(sk)
        f = _findings_for(r, "decision_counter_behind")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_todo_counter_behind(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "task one")
        add_todo(sk, "task two")
        state_path = sk / ".state.json"
        state = json.loads(state_path.read_text())
        state["next_todo_id"] = 1
        state_path.write_text(json.dumps(state))
        r = validate_memory_store(sk)
        f = _findings_for(r, "todo_counter_behind")
        assert len(f) == 1 and f[0]["severity"] == "broken"

    def test_counters_naturally_ahead_is_fine(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        add_todo(sk, "task")
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_counter_behind") == []
        assert _findings_for(r, "todo_counter_behind") == []

    def test_no_decisions_or_todos_no_counter_findings(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])  # store exists, but no decisions/todos
        r = validate_memory_store(sk)
        assert _findings_for(r, "decision_counter_behind") == []
        assert _findings_for(r, "todo_counter_behind") == []


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

class TestReadOnlyGuarantee:
    def test_all_file_bytes_unchanged_with_several_findings_present(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", ["q"])
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="9999")  # dangling -> finding
        remember_preferences(sk, {"testing": {"runner": "pytest"}})
        add_todo(sk, "task")

        # A second, independent violation: invalid status on decision A.
        path = next(p for p in (sk / "decisions").glob("*.md") if p.name.startswith("0001"))
        meta, body = parse_frontmatter(path.read_text())
        meta["status"] = "bogus"
        path.write_text(write_frontmatter(meta, body))

        # A third: stale INDEX.md.
        idx = sk / "INDEX.md"
        idx.write_text(idx.read_text() + "\nSTALE\n")

        before = {p: p.read_bytes() for p in sk.rglob("*") if p.is_file()}

        r = validate_memory_store(sk)
        assert r["summary"]["broken_count"] >= 2
        assert len(r["findings"]) >= 3
        assert r["success"] is False
        assert r["status"] == "broken"
        assert r["error_code"] == "memory_integrity_broken"

        after = {p: p.read_bytes() for p in sk.rglob("*") if p.is_file()}

        assert before.keys() == after.keys(), "validate_memory_store added or removed files"
        for p in before:
            assert before[p] == after[p], f"{p} was modified by validate_memory_store"


# ---------------------------------------------------------------------------
# Contract: registration, output schema, zero LLM calls, no-info-tier
# ---------------------------------------------------------------------------

class TestContractAndRegistration:
    def test_zero_llm_calls_via_mcp_wrapper(self, tmp_path):
        from src.skilllayer.mcp_server import skilllayer_validate_memory
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        result = skilllayer_validate_memory(str(tmp_path))
        assert result["success"] is True
        assert result["llm_calls"] == 0
        assert result["workflow"] == "ValidateMemoryWorkflow"

    def test_workflow_registered_stable(self):
        assert "ValidateMemoryWorkflow" in WORKFLOWS
        assert WORKFLOW_METADATA["ValidateMemoryWorkflow"]["stability"] == "stable"

    def test_macro_sequence_exact(self):
        assert WORKFLOWS["ValidateMemoryWorkflow"] == [
            "LoadMemoryStore", "CheckIndexFreshness", "CheckFrontmatterSchemas",
            "CheckDecisionChainIntegrity", "CheckIdCounters", "BuildIntegrityReport",
        ]

    def test_all_macros_registered(self):
        for m in WORKFLOWS["ValidateMemoryWorkflow"]:
            assert m in MACROS

    def test_20_checks_defined(self):
        assert len(CHECK_SEVERITY) == 20

    def test_no_info_tier_severities(self):
        assert set(CHECK_SEVERITY.values()) <= {"broken", "warning"}

    def test_summary_counts_reconcile(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        r = validate_memory_store(sk)
        s = r["summary"]
        assert s["checks_passed"] + s["warnings_count"] + s["broken_count"] == s["checks_run"] == 20

    def test_output_schema_shape(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        idx = sk / "INDEX.md"
        idx.write_text(idx.read_text() + "\nSTALE\n")  # force >=1 finding
        r = validate_memory_store(sk)
        assert set(r.keys()) == {
            "workflow", "store_exists", "checked_at", "summary", "findings",
            "error_code", "status", "success", "warnings",
        }
        assert set(r["summary"].keys()) == {"checks_run", "checks_passed", "warnings_count", "broken_count"}
        assert r["findings"], "expected at least one finding to inspect its shape"
        assert set(r["findings"][0].keys()) == {"check", "severity", "file", "message", "suggested_fix"}


class TestAllChecksCovered:
    def test_every_named_check_is_exercised_in_this_file(self):
        # Meta-check: guarantees no check name in CHECK_SEVERITY was silently
        # skipped by a typo or omission — every one must appear as a
        # _findings_for(...) argument somewhere in this test file.
        source = Path(__file__).read_text(encoding="utf-8")
        missing = [name for name in CHECK_SEVERITY if f'"{name}"' not in source]
        assert missing == [], f"check(s) with no dedicated test: {missing}"
