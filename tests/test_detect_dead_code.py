"""Tests for DetectDeadCodeWorkflow.

Covers the static analyser in isolation, the full SkillLayer().run() path
(verifies llm_calls == 0 and correct routing), and an integration test on the
SkillLayer repo itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.router import SkillRouter
from skilllayer.runner.core import (
    _deadcode_is_test_file,
    build_detect_dead_code_artifacts,
)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_dead_code_phrases() -> None:
    for phrase in [
        "detect dead code",
        "find unused functions",
        "scan for dead code",
        "identify unused classes",
        "which functions are never called",
        "list unused symbols",
        "never called",
        "show unused code",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "detect_dead_code", f"failed for: {phrase!r}"
        assert decision.workflow == "DetectDeadCodeWorkflow"
        assert decision.matched is True


def test_router_does_not_steal_find_function() -> None:
    # Specific single-function lookup should still route to find_function.
    decision = SkillRouter().route("find where process_data is defined")
    assert decision.task_type == "find_function"


# ---------------------------------------------------------------------------
# _deadcode_is_test_file helper
# ---------------------------------------------------------------------------


def test_is_test_file_detects_various_patterns(tmp_path: Path) -> None:
    cases = {
        "tests/test_foo.py": True,
        "test/test_bar.py": True,
        "src/test_utils.py": True,
        "src/utils_test.py": True,
        "src/utils.py": False,
        "src/testing_helpers.py": False,  # "testing" prefix, not "test_"
        "conftest.py": False,
    }
    for rel, expected in cases.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        assert _deadcode_is_test_file(path, tmp_path) is expected, f"wrong for {rel!r}"


# ---------------------------------------------------------------------------
# Dunder exclusion
# ---------------------------------------------------------------------------


def test_dunders_are_never_flagged(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "class Foo:\n"
        "    def __init__(self): pass\n"
        "    def __repr__(self): return 'Foo'\n"
        "    def __len__(self): return 0\n"
    )
    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "__init__" not in names
    assert "__repr__" not in names
    assert "__len__" not in names


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------


def test_private_name_gets_certain_confidence(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def _internal_helper():\n    pass\n")
    result = build_detect_dead_code_artifacts(tmp_path)
    assert result["total_findings"] == 1
    finding = result["findings"][0]
    assert finding["name"] == "_internal_helper"
    assert finding["confidence"] == "certain"
    assert finding["kind"] == "function"


def test_public_name_gets_possible_confidence(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def orphaned_public():\n    pass\n")
    result = build_detect_dead_code_artifacts(tmp_path)
    assert result["total_findings"] == 1
    assert result["findings"][0]["confidence"] == "possible"


def test_unused_class_flagged_with_possible(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("class OrphanedConfig:\n    pass\n")
    result = build_detect_dead_code_artifacts(tmp_path)
    assert result["total_findings"] == 1
    assert result["findings"][0]["kind"] == "class"
    assert result["findings"][0]["confidence"] == "possible"


# ---------------------------------------------------------------------------
# Reference detection — alive when referenced
# ---------------------------------------------------------------------------


def test_function_called_in_same_file_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "def _helper():\n"
        "    return 42\n"
        "\n"
        "def main():\n"
        "    return _helper()\n"
    )
    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "_helper" not in names


def test_function_imported_in_other_file_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "utils.py").write_text("def compute():\n    return 1\n")
    (tmp_path / "main.py").write_text("from utils import compute\nresult = compute()\n")
    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "compute" not in names


def test_name_in_all_list_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        '__all__ = ["public_api"]\n\n'
        "def public_api():\n    pass\n"
    )
    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "public_api" not in names


def test_recursive_function_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "def factorial(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return factorial(n - 1)\n"
    )
    result = build_detect_dead_code_artifacts(tmp_path)
    # factorial appears twice (def line + recursive call) → alive
    names = {f["name"] for f in result["findings"]}
    assert "factorial" not in names


# ---------------------------------------------------------------------------
# Test file exclusion
# ---------------------------------------------------------------------------


def test_definitions_in_test_files_not_scanned(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    # Helper defined only in a test file — should NOT appear in findings
    (tests_dir / "test_foo.py").write_text("def _test_helper():\n    pass\n")
    # Separate unreferenced production symbol
    (tmp_path / "prod.py").write_text("def _unused_prod():\n    pass\n")

    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "_test_helper" not in names
    assert "_unused_prod" in names


def test_test_file_references_count_as_alive(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tmp_path / "utils.py").write_text("def _compute():\n    pass\n")
    # Test file calls _compute — prevents it from being flagged
    (tests_dir / "test_utils.py").write_text("from utils import _compute\n_compute()\n")

    result = build_detect_dead_code_artifacts(tmp_path)
    names = {f["name"] for f in result["findings"]}
    assert "_compute" not in names


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------


def test_output_has_required_fields(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def _unused():\n    pass\n")
    result = build_detect_dead_code_artifacts(tmp_path)
    for field in ("workflow", "repo_path", "files_scanned", "total_findings",
                  "certain_count", "possible_count", "findings", "summary"):
        assert field in result, f"missing field: {field!r}"

    finding = result["findings"][0]
    for field in ("file", "name", "line", "kind", "confidence"):
        assert field in finding, f"finding missing field: {field!r}"

    assert finding["confidence"] in ("certain", "possible")
    assert finding["kind"] in ("function", "class")
    assert isinstance(finding["line"], int) and finding["line"] >= 1


def test_line_numbers_are_correct(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "# header comment\n"        # line 1
        "\n"                         # line 2
        "def _alpha():\n"            # line 3
        "    pass\n"                 # line 4
        "\n"                         # line 5
        "def _beta():\n"             # line 6
        "    pass\n"
    )
    result = build_detect_dead_code_artifacts(tmp_path)
    by_name = {f["name"]: f["line"] for f in result["findings"]}
    assert by_name["_alpha"] == 3
    assert by_name["_beta"] == 6


def test_empty_repo_returns_clean_result(tmp_path: Path) -> None:
    result = build_detect_dead_code_artifacts(tmp_path)
    assert result["total_findings"] == 0
    assert result["findings"] == []
    assert result["files_scanned"] == 0
    assert isinstance(result["summary"], str)


def test_never_modifies_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "module.py").write_text("def _unused():\n    pass\n")
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_detect_dead_code_artifacts(tmp_path)
    assert writes == [], f"write_text was called on: {writes}"


# ---------------------------------------------------------------------------
# Integration: SkillLayer().run() on the SkillLayer repo itself
# ---------------------------------------------------------------------------


def test_run_on_skilllayer_repo() -> None:
    repo = Path(__file__).parent.parent

    result = SkillLayer().run(repo, "detect dead code")

    assert result["success"] is True
    assert result["workflow"] == "DetectDeadCodeWorkflow"
    assert result["llm_calls"] == 0

    artifacts = result["artifacts"]
    assert artifacts["files_scanned"] > 0
    assert isinstance(artifacts["total_findings"], int)
    assert isinstance(artifacts["certain_count"], int)
    assert isinstance(artifacts["possible_count"], int)
    assert artifacts["certain_count"] + artifacts["possible_count"] == artifacts["total_findings"]

    # No findings should point into test files
    test_file_findings = [
        f for f in artifacts["findings"]
        if f["file"].startswith("tests/") or "test_" in f["file"]
    ]
    assert test_file_findings == [], f"test file symbols in findings: {test_file_findings}"

    # No findings should point into .venv or other ignored paths
    bad = [f for f in artifacts["findings"] if ".venv" in f["file"] or "__pycache__" in f["file"]]
    assert bad == [], f"ignored-dir symbols in findings: {bad}"

    # Every finding has valid confidence and kind
    for finding in artifacts["findings"]:
        assert finding["confidence"] in ("certain", "possible"), f"bad confidence: {finding}"
        assert finding["kind"] in ("function", "class"), f"bad kind: {finding}"
        assert isinstance(finding["line"], int) and finding["line"] >= 1

    assert isinstance(artifacts["summary"], str) and artifacts["summary"]
