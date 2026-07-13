from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..verifier import TestRunner


DEFAULT_SEARCH_IGNORE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".skilllayer",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "runs",
    "venv",
}

DEFAULT_SEARCH_IGNORE_SUFFIXES = {
    ".pyc",
}


def should_ignore_search_path(path: Path, repo_path: Path) -> bool:
    try:
        relative = path.relative_to(repo_path)
    except ValueError:
        return True
    if any(part in DEFAULT_SEARCH_IGNORE_DIRS for part in relative.parts):
        return True
    if path.suffix in DEFAULT_SEARCH_IGNORE_SUFFIXES:
        return True
    return False


class ProjectTools:
    def __init__(self, repo_path: Path, log: list[dict[str, Any]] | None = None, test_runner: TestRunner | None = None) -> None:
        self.repo_path = repo_path.resolve()
        self.log = log if log is not None else []
        self.test_runner = test_runner or TestRunner()
        self.tool_call_count = 0
        self.file_edits_count = 0
        self.last_test_result: dict[str, Any] | None = None
        self.last_replace_summary: dict[str, Any] | None = None
        self._repo_version = 0
        self._dirty_since_tests = False
        self._last_test_version: int | None = None
        self._list_files_cache: tuple[int, list[str]] | None = None
        self._read_cache: dict[str, tuple[int, str]] = {}
        self._search_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}

    def _record(self, tool: str, **kwargs: Any) -> None:
        self.tool_call_count += 1
        self.log.append({"tool": tool, **kwargs})

    def _record_cache_hit(self, tool: str, **kwargs: Any) -> None:
        self.log.append({"tool": tool, "cached": True, **kwargs})

    def _mark_dirty(self) -> None:
        self._repo_version += 1
        self._dirty_since_tests = True
        self._list_files_cache = None
        self._read_cache.clear()
        self._search_cache.clear()

    def path(self, relative_path: str) -> Path:
        target = (self.repo_path / relative_path).resolve()
        try:
            target.relative_to(self.repo_path)
        except ValueError:
            raise ValueError(f"path escapes repo: {relative_path}")
        return target

    def list_files(self) -> list[str]:
        if self._list_files_cache and self._list_files_cache[0] == self._repo_version:
            files = list(self._list_files_cache[1])
            self._record_cache_hit("list_files", count=len(files))
            return files
        files = sorted(
            str(path.relative_to(self.repo_path))
            for path in self.repo_path.rglob("*.py")
            if not should_ignore_search_path(path, self.repo_path)
        )
        self._list_files_cache = (self._repo_version, list(files))
        self._record("list_files", count=len(files))
        return files

    def read_file(self, relative_path: str) -> str:
        cached = self._read_cache.get(relative_path)
        if cached and cached[0] == self._repo_version:
            self._record_cache_hit("read_file", file=relative_path, chars=len(cached[1]))
            return cached[1]
        text = self.path(relative_path).read_text(encoding="utf-8", errors="ignore")
        self._read_cache[relative_path] = (self._repo_version, text)
        self._record("read_file", file=relative_path, chars=len(text))
        return text

    def open_file(self, relative_path: str) -> str:
        cached = self._read_cache.get(relative_path)
        if cached and cached[0] == self._repo_version:
            self._record_cache_hit("open_file", file=relative_path, chars=len(cached[1]))
            return cached[1]
        text = self.path(relative_path).read_text(encoding="utf-8", errors="ignore")
        self._read_cache[relative_path] = (self._repo_version, text)
        self._record("open_file", file=relative_path, chars=len(text))
        return text

    def search_symbol(self, symbol: str) -> list[dict[str, Any]]:
        cached = self._search_cache.get(symbol)
        if cached and cached[0] == self._repo_version:
            hits = [dict(item) for item in cached[1]]
            self._record_cache_hit("search_symbol", symbol=symbol, hits=len(hits))
            return hits
        self._record("search_symbol", symbol=symbol)
        hits = []
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        for relative_path in self.list_files():
            text = self.path(relative_path).read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append({"file": relative_path, "line": line_no, "text": line.strip()})
        self._search_cache[symbol] = (self._repo_version, [dict(item) for item in hits])
        return hits

    def replace_text(self, relative_path: str, old: str, new: str) -> bool:
        path = self.path(relative_path)
        text = path.read_text(encoding="utf-8", errors="ignore")
        replaced = old in text
        if replaced:
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            self.file_edits_count += 1
            self._mark_dirty()
        self._record("replace_text", file=relative_path, replaced=replaced)
        return replaced

    def replace_token_all(self, old: str, new: str) -> bool:
        pattern = re.compile(rf"\b{re.escape(old)}\b")
        changed = False
        changed_files = []
        replacements_by_file: dict[str, int] = {}
        replacements_performed = 0
        for path in self.repo_path.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            updated, count = pattern.subn(new, text)
            if count:
                path.write_text(updated, encoding="utf-8")
                self.file_edits_count += 1
                changed = True
                relative_path = str(path.relative_to(self.repo_path))
                changed_files.append(relative_path)
                replacements_by_file[relative_path] = count
                replacements_performed += count
        if changed:
            self._mark_dirty()
        self.last_replace_summary = {
            "old_symbol": old,
            "new_symbol": new,
            "files_modified": changed_files,
            "replacements_by_file": replacements_by_file,
            "replacements_performed": replacements_performed,
        }
        self._record(
            "replace_text",
            old=old,
            new=new,
            changed_files=changed_files,
            files_modified=changed_files,
            replacements_by_file=replacements_by_file,
            replacements_performed=replacements_performed,
        )
        return changed

    def insert_text(self, relative_path: str, text: str) -> bool:
        path = self.path(relative_path)
        current = path.read_text(encoding="utf-8", errors="ignore")
        inserted = text.strip() not in current
        if inserted:
            path.write_text(current.rstrip() + text + "\n", encoding="utf-8")
            self.file_edits_count += 1
            self._mark_dirty()
        self._record("insert_text", file=relative_path, inserted=inserted)
        return inserted

    def run_tests(self, force: bool = False) -> dict[str, Any]:
        if not force and not self._dirty_since_tests and self.last_test_result is not None and self._last_test_version == self._repo_version:
            self._record_cache_hit("run_tests", skipped=True, reason="clean_since_last_test")
            return self.last_test_result
        if not force and not self._dirty_since_tests and self.last_test_result is None:
            self.log.append({"tool": "run_tests", "skipped": True, "reason": "clean_repo_no_validation_needed"})
            return {"available": False, "returncode": None, "stdout": "", "stderr": "", "skipped": True}
        self._record("run_tests")
        self.last_test_result = self.test_runner.run(self.repo_path)
        self._dirty_since_tests = False
        self._last_test_version = self._repo_version
        return self.last_test_result

    def run_single_test(
        self, command: list[str], execution_environment: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._record("run_single_test", command=command)
        # A single, explicitly-resolved test target carries none of the
        # "rediscovers and re-executes the whole suite, including itself"
        # recursion risk — opt out of the nested-pytest guard.
        result = self.test_runner.run_command(
            self.repo_path, command, allow_nested=True,
            execution_environment=execution_environment,
        )
        self.last_test_result = result
        self._dirty_since_tests = False
        self._last_test_version = self._repo_version
        return result

    def inspect_error(self) -> dict[str, Any]:
        if self.last_test_result and self.last_test_result.get("returncode") == 0:
            self.log.append({"tool": "inspect_error", "skipped": True, "reason": "tests_passed"})
            return self.last_test_result
        self._record("inspect_error")
        return self.last_test_result or {"failed": 0, "stderr": "", "stdout": ""}
