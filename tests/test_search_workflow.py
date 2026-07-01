"""Tests for SearchWorkflow."""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import _extract_search_query, build_search_artifacts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _scan(tmp_path: Path, query: str, **kw) -> dict:
    return build_search_artifacts(tmp_path, query, **kw)


# ---------------------------------------------------------------------------
# TestExtractSearchQuery
# ---------------------------------------------------------------------------

class TestExtractSearchQuery:
    def test_search_for(self):
        assert _extract_search_query("search for build_artifacts") == "build_artifacts"

    def test_find_all_occurrences_of(self):
        assert _extract_search_query("find all occurrences of my_function") == "my_function"

    def test_grep_for(self):
        assert _extract_search_query("grep for import requests") == "import requests"

    def test_where_is_used(self):
        q = _extract_search_query("where is my_function used")
        assert "my_function" in q

    def test_find_in_codebase(self):
        q = _extract_search_query("find my_function in the codebase")
        assert "my_function" in q

    def test_search_codebase_for(self):
        assert _extract_search_query("search codebase for error handling") == "error handling"

    def test_find_all_files_containing(self):
        assert _extract_search_query("find all files containing TODO") == "TODO"

    def test_strips_quotes(self):
        assert _extract_search_query("search for 'my_func'") == "my_func"
        assert _extract_search_query('search for "my_func"') == "my_func"


# ---------------------------------------------------------------------------
# TestLiteralSearch
# ---------------------------------------------------------------------------

class TestLiteralSearch:
    def test_finds_exact_match(self, tmp_path):
        _write(tmp_path, "src/main.py", "def build_artifacts():\n    pass\n")
        result = _scan(tmp_path, "build_artifacts")
        assert result["total_matches"] >= 1
        assert any(m["file"] == "src/main.py" for m in result["matches"])

    def test_match_line_number_correct(self, tmp_path):
        _write(tmp_path, "src/main.py", "line one\nbuild_artifacts here\nline three\n")
        result = _scan(tmp_path, "build_artifacts")
        found = [m for m in result["matches"] if m["file"] == "src/main.py"]
        assert found[0]["line"] == 2

    def test_no_match_returns_empty(self, tmp_path):
        _write(tmp_path, "src/main.py", "def hello(): pass\n")
        result = _scan(tmp_path, "xyzzy_not_here")
        assert result["total_matches"] == 0
        assert result["matches"] == []
        assert result["files_with_matches"] == 0

    def test_multiple_matches_in_one_file(self, tmp_path):
        _write(tmp_path, "src/main.py", "foo = 1\nfoo = 2\nfoo = 3\n")
        result = _scan(tmp_path, "foo")
        assert result["total_matches"] >= 3

    def test_matches_across_files(self, tmp_path):
        _write(tmp_path, "src/a.py", "TODO: fix this\n")
        _write(tmp_path, "src/b.py", "# TODO: another\n")
        result = _scan(tmp_path, "TODO")
        assert result["files_with_matches"] == 2

    def test_scanned_files_incremented(self, tmp_path):
        _write(tmp_path, "src/main.py", "content\n")
        result = _scan(tmp_path, "content")
        assert result["scanned_files"] >= 1

    def test_column_is_one_indexed(self, tmp_path):
        _write(tmp_path, "src/main.py", "hello world\n")
        result = _scan(tmp_path, "world")
        m = next(x for x in result["matches"] if x["file"] == "src/main.py")
        assert m["column"] == 7  # "world" starts at 0-indexed pos 6 → 1-indexed 7

    def test_column_after_indentation(self, tmp_path):
        _write(tmp_path, "src/main.py", "    target\n")
        result = _scan(tmp_path, "target")
        m = next(x for x in result["matches"] if x["file"] == "src/main.py")
        assert m["column"] == 5  # 4 spaces + 1


# ---------------------------------------------------------------------------
# TestRegexSearch
# ---------------------------------------------------------------------------

class TestRegexSearch:
    def test_regex_mode_matches_pattern(self, tmp_path):
        _write(tmp_path, "src/main.py", "foo_bar = 1\nfoo_baz = 2\n")
        result = _scan(tmp_path, r"foo_\w+", mode="regex")
        assert result["total_matches"] == 2

    def test_regex_alternation(self, tmp_path):
        _write(tmp_path, "src/main.py", "apple\nbanana\ncherry\n")
        result = _scan(tmp_path, r"apple|cherry", mode="regex")
        assert result["total_matches"] == 2

    def test_regex_anchors(self, tmp_path):
        _write(tmp_path, "src/main.py", "def my_func():\n    pass\n")
        result = _scan(tmp_path, r"^def ", mode="regex")
        assert result["total_matches"] == 1

    def test_invalid_regex_returns_error_code(self, tmp_path):
        result = _scan(tmp_path, r"[invalid", mode="regex")
        assert result.get("error_code") == "invalid_regex"
        assert result["total_matches"] == 0

    def test_text_mode_treats_regex_chars_literally(self, tmp_path):
        _write(tmp_path, "src/main.py", "price: $10.00\n")
        result = _scan(tmp_path, "$10.00", mode="text")
        assert result["total_matches"] == 1


# ---------------------------------------------------------------------------
# TestCaseSensitivity
# ---------------------------------------------------------------------------

class TestCaseSensitivity:
    def test_case_insensitive_by_default(self, tmp_path):
        _write(tmp_path, "src/main.py", "Hello World\nhello world\nHELLO WORLD\n")
        result = _scan(tmp_path, "hello world")
        assert result["total_matches"] == 3

    def test_case_sensitive_when_flag_set(self, tmp_path):
        _write(tmp_path, "src/main.py", "Hello World\nhello world\nHELLO WORLD\n")
        result = _scan(tmp_path, "hello world", case_sensitive=True)
        assert result["total_matches"] == 1

    def test_case_sensitive_no_match(self, tmp_path):
        _write(tmp_path, "src/main.py", "HELLO WORLD\n")
        result = _scan(tmp_path, "hello world", case_sensitive=True)
        assert result["total_matches"] == 0


# ---------------------------------------------------------------------------
# TestFilePattern
# ---------------------------------------------------------------------------

class TestFilePattern:
    def test_file_pattern_py_only(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        _write(tmp_path, "src/main.md", "needle\n")
        _write(tmp_path, "src/main.txt", "needle\n")
        result = _scan(tmp_path, "needle", file_pattern="*.py")
        assert result["files_with_matches"] == 1
        assert all(m["file"].endswith(".py") for m in result["matches"])

    def test_file_pattern_md_only(self, tmp_path):
        _write(tmp_path, "docs/README.md", "needle\n")
        _write(tmp_path, "src/main.py", "needle\n")
        result = _scan(tmp_path, "needle", file_pattern="*.md")
        assert result["files_with_matches"] == 1
        assert result["matches"][0]["file"].endswith(".md")

    def test_no_match_when_pattern_excludes_all(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        result = _scan(tmp_path, "needle", file_pattern="*.json")
        assert result["total_matches"] == 0

    def test_skipped_files_includes_filtered(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        _write(tmp_path, "src/main.md", "needle\n")
        result = _scan(tmp_path, "needle", file_pattern="*.py")
        assert result["skipped_files"] >= 1


# ---------------------------------------------------------------------------
# TestPreview
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_strips_leading_whitespace(self, tmp_path):
        _write(tmp_path, "src/main.py", "    indented content\n")
        result = _scan(tmp_path, "indented")
        m = result["matches"][0]
        assert not m["preview"].startswith(" ")
        assert m["preview"].startswith("indented")

    def test_preview_max_120_chars(self, tmp_path):
        long_line = "x" * 50 + "needle" + "y" * 200 + "\n"
        _write(tmp_path, "src/main.py", long_line)
        result = _scan(tmp_path, "needle")
        m = result["matches"][0]
        assert len(m["preview"]) <= 120

    def test_preview_short_line_not_padded(self, tmp_path):
        _write(tmp_path, "src/main.py", "short needle line\n")
        result = _scan(tmp_path, "needle")
        m = result["matches"][0]
        assert m["preview"] == "short needle line"


# ---------------------------------------------------------------------------
# TestMatchOffsets
# ---------------------------------------------------------------------------

class TestMatchOffsets:
    def test_match_start_and_end_correct(self, tmp_path):
        _write(tmp_path, "src/main.py", "hello needle world\n")
        result = _scan(tmp_path, "needle")
        m = result["matches"][0]
        preview = m["preview"]
        assert preview[m["match_start"]:m["match_end"]] == "needle"

    def test_match_offsets_after_stripped_indent(self, tmp_path):
        _write(tmp_path, "src/main.py", "    needle here\n")
        result = _scan(tmp_path, "needle")
        m = result["matches"][0]
        preview = m["preview"]
        assert preview[m["match_start"]:m["match_end"]] == "needle"
        assert m["match_start"] == 0

    def test_match_offsets_for_regex(self, tmp_path):
        _write(tmp_path, "src/main.py", "foo_bar baz\n")
        result = _scan(tmp_path, r"foo_\w+", mode="regex")
        m = result["matches"][0]
        preview = m["preview"]
        matched_text = preview[m["match_start"]:m["match_end"]]
        assert matched_text == "foo_bar"

    def test_match_end_greater_than_start(self, tmp_path):
        _write(tmp_path, "src/main.py", "find_me\n")
        result = _scan(tmp_path, "find_me")
        m = result["matches"][0]
        assert m["match_end"] > m["match_start"]


# ---------------------------------------------------------------------------
# TestTruncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_results_capped_at_limit(self, tmp_path):
        lines = "needle\n" * 150
        _write(tmp_path, "src/main.py", lines)
        result = _scan(tmp_path, "needle", limit=100)
        assert len(result["matches"]) == 100
        assert result["truncated"] is True

    def test_total_matches_exceeds_limit_when_truncated(self, tmp_path):
        lines = "needle\n" * 150
        _write(tmp_path, "src/main.py", lines)
        result = _scan(tmp_path, "needle", limit=100)
        assert result["total_matches"] == 150
        assert result["truncated"] is True

    def test_not_truncated_when_under_limit(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\nneedle\nneedle\n")
        result = _scan(tmp_path, "needle", limit=100)
        assert result["truncated"] is False
        assert result["total_matches"] == 3

    def test_custom_limit_respected(self, tmp_path):
        lines = "needle\n" * 20
        _write(tmp_path, "src/main.py", lines)
        result = _scan(tmp_path, "needle", limit=5)
        assert len(result["matches"]) == 5
        assert result["truncated"] is True
        assert result["limit"] == 5

    def test_files_with_matches_accurate_when_truncated(self, tmp_path):
        for i in range(10):
            _write(tmp_path, f"src/file{i}.py", "needle\n" * 20)
        result = _scan(tmp_path, "needle", limit=10)
        assert result["truncated"] is True
        assert result["files_with_matches"] == 10


# ---------------------------------------------------------------------------
# TestSkippedFiles
# ---------------------------------------------------------------------------

class TestSkippedFiles:
    def test_binary_file_skipped(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"needle\x00not_text")
        result = _scan(tmp_path, "needle")
        assert result["total_matches"] == 0
        assert result["skipped_files"] >= 1

    def test_large_file_skipped(self, tmp_path):
        f = tmp_path / "large.py"
        f.write_text("needle\n" * 200_000)
        result = _scan(tmp_path, "needle")
        assert result["total_matches"] == 0
        assert result["skipped_files"] >= 1

    def test_venv_excluded(self, tmp_path):
        _write(tmp_path, ".venv/lib/site.py", "needle\n")
        result = _scan(tmp_path, "needle")
        assert result["total_matches"] == 0
        assert result["skipped_files"] >= 1

    def test_pycache_excluded(self, tmp_path):
        _write(tmp_path, "src/__pycache__/module.pyc.py", "needle\n")
        result = _scan(tmp_path, "needle")
        assert result["total_matches"] == 0

    def test_gitignored_file_excluded(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("secret.py\n")
        _write(tmp_path, "secret.py", "needle\n")
        result = _scan(tmp_path, "needle")
        assert result["total_matches"] == 0
        assert result["skipped_files"] >= 1

    def test_normal_file_scanned(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        result = _scan(tmp_path, "needle")
        assert result["scanned_files"] >= 1


# ---------------------------------------------------------------------------
# TestReturnStructure
# ---------------------------------------------------------------------------

class TestReturnStructure:
    def test_all_required_keys_present(self, tmp_path):
        result = _scan(tmp_path, "foo")
        for key in ("query", "mode", "matches", "total_matches", "files_with_matches",
                    "scanned_files", "skipped_files", "truncated", "limit", "checked_at"):
            assert key in result, f"missing key: {key}"

    def test_query_preserved(self, tmp_path):
        result = _scan(tmp_path, "my_query")
        assert result["query"] == "my_query"

    def test_mode_default_text(self, tmp_path):
        result = _scan(tmp_path, "foo")
        assert result["mode"] == "text"

    def test_mode_regex_preserved(self, tmp_path):
        result = _scan(tmp_path, r"\w+", mode="regex")
        assert result["mode"] == "regex"

    def test_checked_at_is_iso8601(self, tmp_path):
        import re as _re
        result = _scan(tmp_path, "foo")
        assert _re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["checked_at"])

    def test_workflow_name_correct(self, tmp_path):
        result = _scan(tmp_path, "foo")
        assert result["workflow"] == "SearchWorkflow"

    def test_match_has_all_fields(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        result = _scan(tmp_path, "needle")
        m = result["matches"][0]
        for field in ("file", "line", "column", "preview", "match_start", "match_end"):
            assert field in m, f"missing field: {field}"


# ---------------------------------------------------------------------------
# TestZeroLLMCalls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_calls(self, tmp_path):
        _write(tmp_path, "src/main.py", "needle\n")
        with patch("skilllayer.runner.core.LLMClient") as mock_llm:
            _scan(tmp_path, "needle")
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# TestRouter
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str | None:
        route = self.router.route(text)
        return route.task_type if route else None

    def test_search_for_x(self):
        assert self._route("search for build_artifacts") == "search"

    def test_find_all_occurrences_of(self):
        assert self._route("find all occurrences of my_function") == "search"

    def test_grep_for(self):
        assert self._route("grep for import requests") == "search"

    def test_where_is_used(self):
        assert self._route("where is my_function used") == "search"

    def test_find_in_codebase(self):
        assert self._route("find my_function in the codebase") == "search"

    def test_search_codebase_for(self):
        assert self._route("search codebase for error handling") == "search"

    def test_find_all_files_containing(self):
        assert self._route("find all files containing TODO") == "search"

    def test_does_not_match_detect_dead_code(self):
        assert self._route("detect dead code") != "search"

    def test_does_not_match_detect_processes(self):
        assert self._route("detect running processes") != "search"

    def test_does_not_match_detect_secrets(self):
        assert self._route("detect secrets") != "search"

    def test_search_repo_for(self):
        assert self._route("search repo for TODO") == "search"

    def test_grep_for_pattern(self):
        assert self._route("grep for def main") == "search"


# ---------------------------------------------------------------------------
# TestCLIOutput
# ---------------------------------------------------------------------------

def _make_result(**overrides) -> dict:
    base = {
        "success": True,
        "workflow": "SearchWorkflow",
        "query": "needle",
        "mode": "text",
        "matches": [],
        "total_matches": 0,
        "files_with_matches": 0,
        "scanned_files": 5,
        "skipped_files": 1,
        "truncated": False,
        "limit": 100,
        "checked_at": "2026-01-01T00:00:00+00:00",
        "macro_sequence": ["Search", "CollectMatches", "FormatResults"],
        "validation_status": "not_applicable",
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
    }
    base.update(overrides)
    return base


class TestCLIOutput:
    def _run(self, result: dict) -> str:
        from skilllayer.cli import print_human_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        return buf.getvalue()

    def test_query_shown(self):
        out = self._run(_make_result())
        assert "needle" in out

    def test_matches_count_shown(self):
        out = self._run(_make_result(total_matches=3))
        assert "3" in out

    def test_files_count_shown(self):
        out = self._run(_make_result(files_with_matches=2))
        assert "2" in out

    def test_truncated_notice_shown(self):
        out = self._run(_make_result(truncated=True, total_matches=200))
        assert "truncated" in out

    def test_match_file_shown(self):
        match = {
            "file": "src/main.py",
            "line": 5,
            "column": 3,
            "preview": "needle here",
            "match_start": 0,
            "match_end": 6,
        }
        out = self._run(_make_result(matches=[match], total_matches=1))
        assert "src/main.py" in out

    def test_checked_at_shown(self):
        out = self._run(_make_result())
        assert "checked_at" in out
