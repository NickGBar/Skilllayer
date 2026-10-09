"""Detect a change set that makes the test suite weaker — the shortcut agents take to go green.

An agent told to make failing tests pass can edit the code, or it can edit the tests: delete the
failing assertion, mark the test skipped, assert ``True``, lower the coverage threshold, or
deselect the test in the runner's configuration. CI runs whatever suite is left and reports
green; secret scanners and SAST do not look at tests at all. This module reads the net diff and
counts what the change set took away from the suite.

Every signal is deterministic and line-based — no model judges intent. Moving a test between
files is not a weakening (totals are counted across the whole change set), but a refactor that
genuinely deletes tests is reported, because whether that is legitimate is a person's call.
"""
from __future__ import annotations

import re
from typing import Any

from .verify import _git, _is_internal, _safe_line, is_test_path

_MAX_DIFF_CHARS = 5_000_000

# What counts, per kind of line, in test files. Python and JavaScript/TypeScript test idioms.
_TEST_FUNC = re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\(|^\s*(?:it|test)\s*\(\s*['\"`]")
_ASSERTION = re.compile(
    r"^\s*assert\b|\bself\.assert[A-Z]\w*\s*\(|\bpytest\.raises\s*\(|\bexpect\s*\(|\bassert\.\w+\s*\("
)
_SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\bpytest\.(?:skip|xfail)\s*\(|\bself\.skipTest\s*\("
    r"|\b(?:it|test|describe)\.(?:skip|todo)\s*\(|\bx(?:it|test|describe)\s*\("
)
_TAUTOLOGY = re.compile(
    r"^\s*assert\s+(?:True|1|not\s+False)\s*(?:#.*)?$"
    r"|\bself\.assertTrue\s*\(\s*(?:True|1)\s*\)"
    r"|\bexpect\s*\(\s*(?:true|1)\s*\)\s*\.\s*(?:toBe|toEqual|toBeTruthy)\s*\(\s*(?:true|1)?\s*\)"
)

# Runner and coverage configuration: where a threshold or a deselection weakens the suite
# without touching a test file.
_CONFIG_FILES = re.compile(
    r"(?:^|/)(?:pyproject\.toml|setup\.cfg|\.coveragerc|pytest\.ini|tox\.ini|conftest\.py|package\.json"
    r"|(?:jest|vitest)\.config\.[cm]?[jt]s(?:on)?|\.nycrc(?:\.json)?|codecov\.ya?ml"
    r"|\.gitlab-ci\.ya?ml|\.github/workflows/[^/]+\.ya?ml)$"
)
_THRESHOLD = re.compile(
    r"(fail[_-]under|cov-fail-under|coverageThreshold|branches|lines|functions|statements)"
    r"[\"']?\s*[:=]?\s*[\"']?(\d+(?:\.\d+)?)",
    re.I,
)
_DESELECT = re.compile(
    r"--deselect\b|--ignore(?:-glob)?[= ]|(?:^|\s)-k\s+['\"]?not\b|--testPathIgnorePatterns\b|\bcollect_ignore(?:_glob)?\b"
)


# English labels for each finding kind (the gate adds its own Russian ones).
LABELS = {
    "skip_added": "skip/xfail added",
    "tautological_assertion": "assertion that cannot fail",
    "tests_deselected": "tests deselected in configuration",
    "threshold_lowered": "threshold lowered",
    "tests_removed": "fewer tests",
    "assertions_removed": "fewer assertions",
    "test_file_deleted": "test file deleted",
}


def describe(item: dict[str, Any]) -> str:
    """One finding as a line: what, by how much, where."""
    where = item["file"] + (f":{item['line']}" if item.get("line") else "")
    by = f" by {item['count']}" if item.get("count") else ""
    detail = f" ({item['detail']})" if item.get("detail") else ""
    return f"{LABELS.get(item['kind'], item['kind'])}{by} — {where}{detail}"


def _diff(root, base: str, head: str | None) -> str | None:
    """``base..head``, or ``base`` against the working tree when ``head`` is None."""
    refs = [base] if head is None else [base, head]
    proc = _git(root, "-c", "core.quotepath=false", "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", *refs, timeout=120)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def _iter_changes(diff: str):
    """(path, sign, line_no, text) for every added ('+') or removed ('-') line."""
    current: str | None = None
    in_header = False
    old_no = new_no = 0
    hunk = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current, in_header = None, True
            continue
        match = hunk.match(line)
        if match:
            old_no, new_no, in_header = int(match.group(1)), int(match.group(2)), False
            continue
        if in_header:
            if line.startswith("+++ "):
                target = line[4:].strip().strip('"')
                current = target[2:] if target.startswith("b/") else current
            elif line.startswith("--- "):
                source = line[4:].strip().strip('"')
                current = source[2:] if source.startswith("a/") else None
            continue
        if current is None or _is_internal(current):
            continue
        if line.startswith("+"):
            yield current, "+", new_no, line[1:]
            new_no += 1
        elif line.startswith("-"):
            yield current, "-", old_no, line[1:]
            old_no += 1


def _finding(kind: str, path: str, line: int | None = None, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "file": _safe_line(path, 200) or "(path withheld)"}
    if line is not None:
        item["line"] = line
    if detail:
        item["detail"] = _safe_line(detail, 160) or "(withheld)"
    return item


def analyze_diff(diff: str, *, deleted_paths: list[str] | None = None) -> dict[str, Any]:
    """Count what the diff takes away from the test suite. Pure function of the diff text."""
    counts = {"test_functions": [0, 0], "assertions": [0, 0], "skips": [0, 0]}  # [added, removed]
    findings: list[dict[str, Any]] = []
    thresholds: dict[tuple[str, str], dict[str, float]] = {}
    first_removed: dict[str, tuple[str, int]] = {}  # kind -> where the first removal was
    added_lines: dict[str, list[tuple[int, str]]] = {}  # test file -> its added lines, in order
    added_skips: list[tuple[str, int]] = []  # (test file, index into added_lines)
    for path, sign, line_no, text in _iter_changes(diff):
        idx = 0 if sign == "+" else 1
        if is_test_path(path):
            if sign == "+":
                added_lines.setdefault(path, []).append((line_no, text))
            if _TEST_FUNC.search(text):
                counts["test_functions"][idx] += 1
                if sign == "-":
                    first_removed.setdefault("tests_removed", (path, line_no))
            if _ASSERTION.search(text):
                counts["assertions"][idx] += 1
                if sign == "-":
                    first_removed.setdefault("assertions_removed", (path, line_no))
            if _SKIP.search(text):
                counts["skips"][idx] += 1
                if sign == "+":
                    added_skips.append((path, len(added_lines[path]) - 1))
            if sign == "+" and _TAUTOLOGY.search(text):
                findings.append(_finding("tautological_assertion", path, line_no, text.strip()))
        if _CONFIG_FILES.search(path):
            if sign == "+" and _DESELECT.search(text):
                findings.append(_finding("tests_deselected", path, line_no, text.strip()))
            for key, value in _THRESHOLD.findall(text):
                slot = thresholds.setdefault((path, key.lower()), {})
                slot["new" if sign == "+" else "old"] = float(value)
    for (path, key), values in thresholds.items():
        if "old" in values and "new" in values and values["new"] < values["old"]:
            findings.append(_finding("threshold_lowered", path, None, f"{key}: {values['old']:g} → {values['new']:g}"))
    # A skip on a test this change set adds (a new test that does not run on Windows, say) takes
    # nothing away from the suite. Of the rest, only skips beyond those removed count: a
    # reformat that removes and re-adds the same marker is not a weakening.
    weakening_skips: list[tuple[str, int, str]] = []
    for path, index in added_skips:
        line_no, text = added_lines[path][index]
        # The def it decorates can sit far below: a multi-line skipif(...), a long parametrize
        # list. Follow the unbroken block of added lines down to the first test function.
        decorates_new_test, previous = False, line_no
        for number, later in added_lines[path][index + 1:index + 81]:
            if number != previous + 1:
                break
            previous = number
            if _TEST_FUNC.search(later):
                decorates_new_test = True
                break
        if decorates_new_test:
            continue
        weakening_skips.append((path, line_no, text))
    if len(weakening_skips) > counts["skips"][1]:
        findings.extend(_finding("skip_added", path, line_no, text.strip()) for path, line_no, text in weakening_skips)
    # Net totals across the whole change set, so a test moved between files is not counted.
    for kind, name in (("tests_removed", "test_functions"), ("assertions_removed", "assertions")):
        added, removed = counts[name]
        if removed > added:
            path, line_no = first_removed[kind]
            findings.append({**_finding(kind, path, line_no), "count": removed - added})
    if counts["test_functions"][1] > counts["test_functions"][0]:
        # A deleted test file counts only when the suite shrank: otherwise its tests moved.
        for path in deleted_paths or []:
            if is_test_path(path):
                findings.append(_finding("test_file_deleted", path))
    return {
        "counts": {name: {"added": pair[0], "removed": pair[1]} for name, pair in counts.items()},
        "findings": findings[:50],
    }


def check_test_integrity(root, base: str, head: str | None, *, deleted_paths: list[str] | None = None, approved: bool = False) -> dict[str, Any]:
    """The gate check: failed when the change set weakens the suite, unless the caller approved it."""
    diff = _diff(root, base, head)
    if diff is None:
        return {"name": "test_integrity", "status": "not_verified", "reason": "diff_unavailable", "findings": []}
    if len(diff) > _MAX_DIFF_CHARS:
        return {"name": "test_integrity", "status": "not_verified", "reason": "diff_too_large", "findings": []}
    result = analyze_diff(diff, deleted_paths=deleted_paths)
    status = "failed" if result["findings"] and not approved else "passed"
    check = {"name": "test_integrity", "status": status, **result}
    if result["findings"] and approved:
        # Approval comes from the caller's configuration (a CI job a person runs), never from the change.
        check["approved_by_caller"] = True
    return check
