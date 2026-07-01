"""Tests for SkillLayer .skilllayer/ memory persistence system.

Covers:
- Core infrastructure: parse_frontmatter, write_frontmatter, .state.json, INDEX.md
- SaveContextSnapshotWorkflow
- TrackDecisionLogWorkflow
- RememberUserPreferencesWorkflow
- RehydrateContextWorkflow
"""
import json
from pathlib import Path

import pytest

from skilllayer.memory.skilllayer_memory import (
    claim_next_decision_id,
    parse_frontmatter,
    rehydrate_context,
    remember_preferences,
    save_context_snapshot,
    track_decision,
    write_frontmatter,
    write_index,
    _utc_date,
    _utc_iso,
    _slugify,
    regenerate_index,
    SCHEMA_VERSION,
    HISTORY_KEEP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def skilllayer_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".skilllayer"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_parses_string_value(self) -> None:
        text = "---\nkey: value\n---\nbody"
        meta, body = parse_frontmatter(text)
        assert meta["key"] == "value"
        assert body == "body"

    def test_parses_int_value(self) -> None:
        text = "---\nschema: 1\n---\nbody"
        meta, body = parse_frontmatter(text)
        assert meta["schema"] == 1

    def test_parses_bool_true(self) -> None:
        meta, _ = parse_frontmatter("---\nflag: true\n---\n")
        assert meta["flag"] is True

    def test_parses_bool_false(self) -> None:
        meta, _ = parse_frontmatter("---\nflag: false\n---\n")
        assert meta["flag"] is False

    def test_parses_null(self) -> None:
        meta, _ = parse_frontmatter("---\nfield: null\n---\n")
        assert meta["field"] is None

    def test_parses_tilde_null(self) -> None:
        meta, _ = parse_frontmatter("---\nfield: ~\n---\n")
        assert meta["field"] is None

    def test_parses_list(self) -> None:
        meta, _ = parse_frontmatter("---\ntags: [a, b, c]\n---\n")
        assert meta["tags"] == ["a", "b", "c"]

    def test_no_frontmatter_returns_empty_meta(self) -> None:
        meta, body = parse_frontmatter("just body text")
        assert meta == {}
        assert body == "just body text"

    def test_unclosed_frontmatter_returns_empty_meta(self) -> None:
        meta, body = parse_frontmatter("---\nkey: val\n")
        assert meta == {}

    def test_body_after_frontmatter(self) -> None:
        text = "---\nschema: 1\n---\n\n## Section\ncontent\n"
        _, body = parse_frontmatter(text)
        assert "## Section" in body


# ---------------------------------------------------------------------------
# write_frontmatter
# ---------------------------------------------------------------------------

class TestWriteFrontmatter:
    def test_roundtrip_string(self) -> None:
        meta = {"schema": 1, "type": "test"}
        body = "some body\n"
        result = write_frontmatter(meta, body)
        meta2, body2 = parse_frontmatter(result)
        assert meta2["schema"] == 1
        assert meta2["type"] == "test"
        assert "some body" in body2

    def test_roundtrip_bool(self) -> None:
        result = write_frontmatter({"active": True, "done": False}, "")
        meta, _ = parse_frontmatter(result)
        assert meta["active"] is True
        assert meta["done"] is False

    def test_roundtrip_null(self) -> None:
        result = write_frontmatter({"field": None}, "")
        meta, _ = parse_frontmatter(result)
        assert meta["field"] is None

    def test_roundtrip_list(self) -> None:
        result = write_frontmatter({"files": ["a.py", "b.py"]}, "")
        meta, _ = parse_frontmatter(result)
        assert meta["files"] == ["a.py", "b.py"]

    def test_output_starts_with_triple_dash(self) -> None:
        result = write_frontmatter({"k": "v"}, "body")
        assert result.startswith("---\n")


# ---------------------------------------------------------------------------
# .state.json sequence
# ---------------------------------------------------------------------------

class TestClaimNextDecisionId:
    def test_first_id_is_0001(self, skilllayer_dir: Path) -> None:
        d = claim_next_decision_id(skilllayer_dir)
        assert d == "0001"

    def test_second_id_is_0002(self, skilllayer_dir: Path) -> None:
        claim_next_decision_id(skilllayer_dir)
        d = claim_next_decision_id(skilllayer_dir)
        assert d == "0002"

    def test_state_file_created(self, skilllayer_dir: Path) -> None:
        claim_next_decision_id(skilllayer_dir)
        assert (skilllayer_dir / ".state.json").exists()

    def test_state_file_incremented(self, skilllayer_dir: Path) -> None:
        claim_next_decision_id(skilllayer_dir)
        claim_next_decision_id(skilllayer_dir)
        claim_next_decision_id(skilllayer_dir)
        state = json.loads((skilllayer_dir / ".state.json").read_text())
        assert state["next_decision_id"] == 4

    def test_corrupted_state_recovers(self, skilllayer_dir: Path) -> None:
        (skilllayer_dir / ".state.json").write_text("not json")
        d = claim_next_decision_id(skilllayer_dir)
        assert d == "0001"


# ---------------------------------------------------------------------------
# SaveContextSnapshot
# ---------------------------------------------------------------------------

class TestSaveContextSnapshot:
    def test_creates_latest_md(self, skilllayer_dir: Path) -> None:
        result = save_context_snapshot(
            skilllayer_dir,
            state="Working on memory workflows",
            open_questions=["Should we keep 5 or 10 history?"],
        )
        latest = skilllayer_dir / "context" / "latest.md"
        assert latest.exists()
        assert result["context_file"] == str(latest)

    def test_latest_has_valid_frontmatter(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="test state", open_questions=[])
        text = (skilllayer_dir / "context" / "latest.md").read_text()
        meta, _ = parse_frontmatter(text)
        assert meta["schema"] == SCHEMA_VERSION
        assert meta["type"] == "context_snapshot"
        assert meta["workflow"] == "SaveContextSnapshotWorkflow"

    def test_state_in_body(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="Refactoring router", open_questions=[])
        text = (skilllayer_dir / "context" / "latest.md").read_text()
        assert "Refactoring router" in text

    def test_open_questions_in_body(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(
            skilllayer_dir, state="X",
            open_questions=["Q1?", "Q2?"],
        )
        text = (skilllayer_dir / "context" / "latest.md").read_text()
        assert "Q1?" in text
        assert "Q2?" in text

    def test_rotation_creates_history_file(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="first", open_questions=[])
        save_context_snapshot(skilllayer_dir, state="second", open_questions=[])
        history = list((skilllayer_dir / "context" / "history").glob("*.md"))
        assert len(history) == 1

    def test_history_pruned_to_history_keep(self, skilllayer_dir: Path) -> None:
        for i in range(HISTORY_KEEP + 3):
            save_context_snapshot(skilllayer_dir, state=f"state {i}", open_questions=[])
        history = list((skilllayer_dir / "context" / "history").glob("*.md"))
        assert len(history) <= HISTORY_KEEP

    def test_latest_md_overwrites_not_appends(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="old state", open_questions=[])
        save_context_snapshot(skilllayer_dir, state="new state", open_questions=[])
        text = (skilllayer_dir / "context" / "latest.md").read_text()
        assert "new state" in text
        assert "old state" not in text

    def test_creates_index_md(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="X", open_questions=[])
        assert (skilllayer_dir / "INDEX.md").exists()

    def test_no_open_questions(self, skilllayer_dir: Path) -> None:
        result = save_context_snapshot(skilllayer_dir, state="clean", open_questions=[])
        assert result["open_questions"] == []

    def test_result_contains_summary(self, skilllayer_dir: Path) -> None:
        result = save_context_snapshot(skilllayer_dir, state="X", open_questions=["A", "B"])
        assert "2 open question" in result["summary"]

    def test_mkdir_if_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nested" / ".skilllayer"
        result = save_context_snapshot(missing, state="X", open_questions=[])
        assert "context_file" in result


# ---------------------------------------------------------------------------
# TrackDecisionLog
# ---------------------------------------------------------------------------

class TestTrackDecision:
    def _make(self, skilllayer_dir: Path, title: str = "Use pytest", status: str = "proposed") -> dict:
        return track_decision(
            skilllayer_dir,
            title=title,
            context="We need a test runner",
            decision="Use pytest",
            reasoning="Industry standard, fast",
            consequences="Dev must install pytest",
            status=status,
        )

    def test_creates_decisions_dir(self, skilllayer_dir: Path) -> None:
        self._make(skilllayer_dir)
        assert (skilllayer_dir / "decisions").is_dir()

    def test_decision_file_exists(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir)
        assert Path(result["file"]).exists()

    def test_decision_file_has_frontmatter(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir)
        text = Path(result["file"]).read_text()
        meta, _ = parse_frontmatter(text)
        assert meta["schema"] == SCHEMA_VERSION
        assert meta["type"] == "decision"
        assert meta["id"] == "0001"

    def test_decision_body_contains_sections(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir)
        text = Path(result["file"]).read_text()
        assert "## Context" in text
        assert "## Decision" in text
        assert "## Reasoning" in text
        assert "## Consequences" in text

    def test_sequential_ids(self, skilllayer_dir: Path) -> None:
        r1 = self._make(skilllayer_dir, "First")
        r2 = self._make(skilllayer_dir, "Second")
        assert r1["decision_id"] == "0001"
        assert r2["decision_id"] == "0002"

    def test_filename_includes_slug(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir, "Use pytest for tests")
        assert "use-pytest" in Path(result["file"]).name

    def test_supersedes_flips_old_status(self, skilllayer_dir: Path) -> None:
        r1 = self._make(skilllayer_dir, "Old decision")
        track_decision(
            skilllayer_dir,
            title="New decision",
            context="ctx",
            decision="use new approach",
            reasoning="better",
            consequences="none",
            supersedes="0001",
        )
        old_file = Path(r1["file"])
        old_meta, _ = parse_frontmatter(old_file.read_text())
        assert old_meta["status"] == "superseded"
        assert old_meta["superseded_by"] == "0002"

    def test_status_validated(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir, status="invalid_status")
        assert result["status"] == "proposed"

    def test_creates_index_md(self, skilllayer_dir: Path) -> None:
        self._make(skilllayer_dir)
        assert (skilllayer_dir / "INDEX.md").exists()

    def test_result_contains_summary(self, skilllayer_dir: Path) -> None:
        result = self._make(skilllayer_dir)
        assert "0001" in result["summary"]

    def test_related_files_in_meta(self, skilllayer_dir: Path) -> None:
        result = track_decision(
            skilllayer_dir,
            title="T",
            context="c",
            decision="d",
            reasoning="r",
            consequences="q",
            related_files=["src/foo.py"],
        )
        text = Path(result["file"]).read_text()
        meta, _ = parse_frontmatter(text)
        assert "src/foo.py" in meta["related_files"]


# ---------------------------------------------------------------------------
# RememberUserPreferences
# ---------------------------------------------------------------------------

class TestRememberPreferences:
    def test_creates_preferences_file(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"testing": {"runner": "pytest"}})
        assert (skilllayer_dir / "preferences.md").exists()

    def test_preferences_in_body(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"testing": {"runner": "pytest"}})
        text = (skilllayer_dir / "preferences.md").read_text()
        assert "runner: pytest" in text

    def test_has_valid_frontmatter(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"style": {"type_hints": "required"}})
        text = (skilllayer_dir / "preferences.md").read_text()
        meta, _ = parse_frontmatter(text)
        assert meta["schema"] == SCHEMA_VERSION
        assert meta["type"] == "preferences"

    def test_upsert_last_writer_wins(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"testing": {"runner": "pytest"}})
        remember_preferences(skilllayer_dir, {"testing": {"runner": "unittest"}})
        text = (skilllayer_dir / "preferences.md").read_text()
        assert "runner: unittest" in text
        assert "runner: pytest" not in text

    def test_preserves_other_domains(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"style": {"type_hints": "required"}})
        remember_preferences(skilllayer_dir, {"testing": {"runner": "pytest"}})
        text = (skilllayer_dir / "preferences.md").read_text()
        assert "type_hints: required" in text
        assert "runner: pytest" in text

    def test_multiple_domains_single_call(self, skilllayer_dir: Path) -> None:
        remember_preferences(
            skilllayer_dir,
            {"testing": {"runner": "pytest"}, "style": {"type_hints": "required"}},
        )
        result_text = (skilllayer_dir / "preferences.md").read_text()
        assert "runner: pytest" in result_text
        assert "type_hints: required" in result_text

    def test_sections_sorted_alphabetically(self, skilllayer_dir: Path) -> None:
        remember_preferences(
            skilllayer_dir,
            {"zebra": {"k": "v"}, "alpha": {"k": "v"}},
        )
        text = (skilllayer_dir / "preferences.md").read_text()
        idx_alpha = text.index("## alpha")
        idx_zebra = text.index("## zebra")
        assert idx_alpha < idx_zebra

    def test_domains_lowercase(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"TESTING": {"runner": "pytest"}})
        result = remember_preferences(skilllayer_dir, {"testing": {"linter": "ruff"}})
        assert "testing" in result["sections_updated"]

    def test_creates_index_md(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"x": {"y": "z"}})
        assert (skilllayer_dir / "INDEX.md").exists()

    def test_result_contains_sections_updated(self, skilllayer_dir: Path) -> None:
        result = remember_preferences(
            skilllayer_dir, {"testing": {"runner": "pytest"}, "style": {"k": "v"}}
        )
        assert "testing" in result["sections_updated"]
        assert "style" in result["sections_updated"]


# ---------------------------------------------------------------------------
# RehydrateContext
# ---------------------------------------------------------------------------

class TestRehydrateContext:
    def _populate(self, d: Path) -> None:
        save_context_snapshot(d, state="Working on X", open_questions=["Q1?"])
        track_decision(
            d, title="Use pytest", context="c", decision="d",
            reasoning="r", consequences="q",
        )
        remember_preferences(d, {"testing": {"runner": "pytest"}})

    def test_no_memory_store_returns_error(self, tmp_path: Path) -> None:
        d = tmp_path / ".skilllayer"
        d.mkdir()
        result = rehydrate_context(d)
        assert result["error_code"] == "no_memory_store"
        assert result["index"] is None

    def test_returns_index_by_default(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir)
        assert result["error_code"] is None
        assert result["index"] is not None
        assert "INDEX.md" not in result["index"]  # content, not path

    def test_index_contains_context_section(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir)
        assert "SaveContextSnapshot" in result["index"]

    def test_index_contains_decisions_section(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir)
        assert "TrackDecisionLog" in result["index"]

    def test_index_contains_preferences_section(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir)
        assert "RememberUserPreferences" in result["index"]

    def test_full_context_flag(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir, full_context=True)
        assert result["context"] is not None
        assert "Working on X" in result["context"]

    def test_decision_id_flag(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir, decision_id="0001")
        assert result["decision"] is not None
        assert "Use pytest" in result["decision"]

    def test_unknown_decision_id(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir, decision_id="9999")
        assert "not found" in result["decision"]

    def test_full_preferences_flag(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir, full_preferences=True)
        assert result["preferences"] is not None
        assert "runner: pytest" in result["preferences"]

    def test_no_extras_by_default(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir)
        assert result["context"] is None
        assert result["decision"] is None
        assert result["preferences"] is None

    def test_summary_includes_extras_when_requested(self, skilllayer_dir: Path) -> None:
        self._populate(skilllayer_dir)
        result = rehydrate_context(skilllayer_dir, full_context=True, full_preferences=True)
        assert "full context" in result["summary"]
        assert "full preferences" in result["summary"]


# ---------------------------------------------------------------------------
# INDEX.md regeneration
# ---------------------------------------------------------------------------

class TestRegenerateIndex:
    def test_empty_store_produces_valid_index(self, skilllayer_dir: Path) -> None:
        content = regenerate_index(skilllayer_dir)
        meta, _ = parse_frontmatter(content)
        assert meta["schema"] == SCHEMA_VERSION

    def test_write_index_creates_file(self, skilllayer_dir: Path) -> None:
        write_index(skilllayer_dir)
        assert (skilllayer_dir / "INDEX.md").exists()

    def test_index_includes_open_questions(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(
            skilllayer_dir, state="S",
            open_questions=["Will this work?", "Deadline?"],
        )
        content = (skilllayer_dir / "INDEX.md").read_text()
        assert "Will this work?" in content
        assert "Deadline?" in content

    def test_index_includes_decision_titles(self, skilllayer_dir: Path) -> None:
        track_decision(
            skilllayer_dir, title="Use ruff", context="c",
            decision="d", reasoning="r", consequences="q",
        )
        content = (skilllayer_dir / "INDEX.md").read_text()
        assert "use ruff" in content.lower()

    def test_index_shows_at_most_3_decisions(self, skilllayer_dir: Path) -> None:
        for i in range(5):
            track_decision(
                skilllayer_dir, title=f"Decision {i}", context="c",
                decision="d", reasoning="r", consequences="q",
            )
        content = (skilllayer_dir / "INDEX.md").read_text()
        assert "last 3" in content

    def test_index_includes_preference_digest(self, skilllayer_dir: Path) -> None:
        remember_preferences(skilllayer_dir, {"testing": {"runner": "pytest"}})
        content = (skilllayer_dir / "INDEX.md").read_text()
        assert "runner: pytest" in content

    def test_index_updated_after_each_write_workflow(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="A", open_questions=[])
        content1 = (skilllayer_dir / "INDEX.md").read_text()
        track_decision(
            skilllayer_dir, title="New ADR", context="c",
            decision="d", reasoning="r", consequences="q",
        )
        content2 = (skilllayer_dir / "INDEX.md").read_text()
        assert content1 != content2

    def test_index_frontmatter_updated_timestamp(self, skilllayer_dir: Path) -> None:
        save_context_snapshot(skilllayer_dir, state="X", open_questions=[])
        text = (skilllayer_dir / "INDEX.md").read_text()
        meta, _ = parse_frontmatter(text)
        assert "updated" in meta


# ---------------------------------------------------------------------------
# _slugify helper
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_lowercase(self) -> None:
        assert _slugify("Use PyTest") == "use-pytest"

    def test_special_chars_become_hyphens(self) -> None:
        assert _slugify("use (pytest) for testing!") == "use-pytest-for-testing"

    def test_max_length_60(self) -> None:
        long = "a" * 80
        assert len(_slugify(long)) <= 60

    def test_no_leading_trailing_hyphens(self) -> None:
        s = _slugify("  -test-  ")
        assert not s.startswith("-")
        assert not s.endswith("-")


# ---------------------------------------------------------------------------
# CLI error-surfacing — print_human_run and _ERROR_CODE_MESSAGES
# ---------------------------------------------------------------------------

import io
from contextlib import redirect_stdout

from skilllayer.cli import _ERROR_CODE_MESSAGES, print_human_run


def _make_run_result(workflow: str, success: bool, **extra: object) -> dict:
    """Minimal result dict that satisfies print_human_run's required keys."""
    base: dict = {
        "success": success,
        "repo_path": "/tmp/repo",
        "task": "test task",
        "workflow": workflow,
        "macro_sequence": [],
        "validation_status": "not_applicable",
        "tests_run": False,
        "tests_passed": None,
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
    }
    base.update(extra)
    return base


class TestErrorCodeMessages:
    """_ERROR_CODE_MESSAGES must cover every known error code."""

    def test_no_memory_store_has_exact_message(self) -> None:
        msg = _ERROR_CODE_MESSAGES["no_memory_store"]
        assert "No .skilllayer/ store found" in msg
        assert "Save context snapshot" in msg

    def test_no_memory_store_mentions_initialize(self) -> None:
        msg = _ERROR_CODE_MESSAGES["no_memory_store"]
        assert "initialize" in msg.lower() or "first" in msg.lower()

    def test_no_test_command_detected_present(self) -> None:
        assert "no_test_command_detected" in _ERROR_CODE_MESSAGES

    def test_not_git_repository_present(self) -> None:
        assert "not_git_repository" in _ERROR_CODE_MESSAGES

    def test_dependency_name_not_found_present(self) -> None:
        assert "dependency_name_not_found" in _ERROR_CODE_MESSAGES

    def test_no_dependency_files_found_present(self) -> None:
        assert "no_dependency_files_found" in _ERROR_CODE_MESSAGES

    def test_all_messages_are_nonempty_strings(self) -> None:
        for code, msg in _ERROR_CODE_MESSAGES.items():
            assert isinstance(msg, str) and len(msg) > 0, (
                f"error code {code!r} has empty or non-string message"
            )


class TestPrintHumanRunMemoryWorkflows:
    """print_human_run must surface summary and error_code for memory workflows."""

    def test_rehydrate_no_memory_store_prints_error_message(self) -> None:
        result = _make_run_result(
            "RehydrateContextWorkflow",
            success=False,
            error_code="no_memory_store",
            summary="No .skilllayer/ memory store found at this repository root.",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "No .skilllayer/ store found" in output, (
            f"Expected no_memory_store human message in output:\n{output}"
        )
        assert "Save context snapshot" in output, (
            f"Expected 'Save context snapshot' hint in output:\n{output}"
        )

    def test_rehydrate_success_prints_summary(self) -> None:
        result = _make_run_result(
            "RehydrateContextWorkflow",
            success=True,
            summary="Memory rehydrated from INDEX.md.",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "Memory rehydrated from INDEX.md." in output, (
            f"Expected summary in output:\n{output}"
        )

    def test_save_context_success_prints_summary(self) -> None:
        result = _make_run_result(
            "SaveContextSnapshotWorkflow",
            success=True,
            summary="Context snapshot saved. 2 open question(s). 0 history file(s) retained.",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "Context snapshot saved" in output, (
            f"Expected summary in output:\n{output}"
        )

    def test_track_decision_success_prints_summary(self) -> None:
        result = _make_run_result(
            "TrackDecisionLogWorkflow",
            success=True,
            summary="Decision 0001 recorded: 'Use pytest' (accepted).",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "Decision 0001 recorded" in output, (
            f"Expected summary in output:\n{output}"
        )

    def test_remember_preferences_success_prints_summary(self) -> None:
        result = _make_run_result(
            "RememberUserPreferencesWorkflow",
            success=True,
            summary="Preferences updated: testing.",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "Preferences updated" in output, (
            f"Expected summary in output:\n{output}"
        )

    def test_no_error_printed_on_success(self) -> None:
        result = _make_run_result(
            "RehydrateContextWorkflow",
            success=True,
            summary="Memory rehydrated from INDEX.md.",
            error_code=None,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "error:" not in output.lower() or "error_code:" not in output.lower(), (
            f"Unexpected error output on success:\n{output}"
        )


class TestPrintHumanRunGenericErrorBlock:
    """Generic error block must fire for ANY workflow with error_code set."""

    def test_unknown_workflow_with_error_code_prints_code(self) -> None:
        result = _make_run_result(
            "SomeUnknownWorkflow",
            success=False,
            error_code="some_unmapped_code",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "some_unmapped_code" in output, (
            f"Expected raw error_code in output for unmapped code:\n{output}"
        )

    def test_git_status_not_git_repo_prints_human_message(self) -> None:
        result = _make_run_result(
            "GitStatusWorkflow",
            success=False,
            error_code="not_git_repository",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "not a git repository" in output.lower(), (
            f"Expected human message for not_git_repository:\n{output}"
        )

    def test_dependency_check_no_dep_files_prints_human_message(self) -> None:
        result = _make_run_result(
            "DependencyCheckWorkflow",
            success=False,
            error_code="no_dependency_files_found",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "No dependency files found" in output, (
            f"Expected human message for no_dependency_files_found:\n{output}"
        )

    def test_no_error_code_none_prints_nothing_extra(self) -> None:
        result = _make_run_result(
            "GitStatusWorkflow",
            success=False,
            error_code=None,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        output = buf.getvalue()
        assert "error:" not in output, (
            f"Expected no 'error:' line when error_code is None:\n{output}"
        )
