"""Tests for DependencyCheckWorkflow — stable-tier verification.

Covers all cases required for stable promotion:
  1. requirements.txt only
  2. pyproject.toml only
  3. package.json only
  4. No dependency file at all — explicit structured error
  5. No name extractable from task — explicit structured error
  6. Source import detection (used=True)
  7. Schema consistency across all package manager types
  8. No false success when check is inconclusive (no declaration files)
  9. Zero LLM calls — purely deterministic
 10. Read-only invariant — no file modification
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.config.defaults import WORKFLOW_METADATA
from skilllayer.router import SkillRouter
from skilllayer.runner.core import build_dependency_check_artifacts


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({
    "workflow",
    "dependency",
    "declared",
    "declared_in",
    "used",
    "usage_count",
    "usage_files",
    "usage_locations",
    "ecosystems",
    "summary",
    "error_code",
    "tests_run",
    "tests_passed",
    "validation_status",
})

_REQUIRED_USAGE_LOCATION_FIELDS = frozenset({"file", "line", "usage_type"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def req_txt_repo(tmp_path: Path) -> Path:
    """Repo with only requirements.txt, declaring requests and numpy."""
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\nnumpy\n")
    return tmp_path


@pytest.fixture
def pyproject_repo(tmp_path: Path) -> Path:
    """Repo with only pyproject.toml, declaring requests and numpy."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n    "requests>=2.0",\n    "numpy",\n]\n'
    )
    return tmp_path


@pytest.fixture
def pkg_json_repo(tmp_path: Path) -> Path:
    """Repo with only package.json, declaring lodash and typescript."""
    (tmp_path / "package.json").write_text(
        json.dumps({
            "dependencies": {"lodash": "4.17.21"},
            "devDependencies": {"typescript": "5.0.0"},
        })
    )
    return tmp_path


@pytest.fixture
def no_dep_file_repo(tmp_path: Path) -> Path:
    """Repo with no supported dependency declaration files."""
    return tmp_path


@pytest.fixture
def py_source_repo(tmp_path: Path) -> Path:
    """Repo with requirements.txt AND Python source file importing requests."""
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\n")
    (tmp_path / "main.py").write_text(
        "import requests\n\nresponse = requests.get('http://example.com')\n"
    )
    return tmp_path


@pytest.fixture
def multi_import_repo(tmp_path: Path) -> Path:
    """Repo with requirements.txt and two Python files, both importing requests."""
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\n")
    (tmp_path / "client.py").write_text("import requests\n\ndef fetch(url):\n    return requests.get(url)\n")
    (tmp_path / "util.py").write_text("from requests import Session\n\ndef make_session():\n    return Session()\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Stability metadata
# ---------------------------------------------------------------------------


def test_workflow_stability_is_stable() -> None:
    meta = WORKFLOW_METADATA["DependencyCheckWorkflow"]
    assert meta["stability"] == "stable", (
        f"DependencyCheckWorkflow stability must be 'stable', got {meta['stability']!r}"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_dependency_check_phrases() -> None:
    for phrase in [
        "is requests used?",
        "check dependency flask",
        "where is numpy imported?",
        "where is lodash used?",
        "is flask declared?",
        "show package requests",
        "find dependency numpy",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "dependency_check", (
            f"expected dependency_check for {phrase!r}, got {decision.task_type!r}"
        )
        assert decision.workflow == "DependencyCheckWorkflow"
        assert decision.matched is True


def test_router_does_not_route_install_to_dependency_check() -> None:
    for phrase in [
        "install requests",
        "update dependency requests",
        "upgrade package numpy",
        "remove package lodash",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type != "dependency_check", (
            f"should NOT route to dependency_check for {phrase!r}"
        )


# ---------------------------------------------------------------------------
# Schema consistency — all scenarios must produce identical key sets
# ---------------------------------------------------------------------------


def test_schema_req_txt_has_all_required_fields(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"requirements.txt repo: missing field {field!r}"


def test_schema_pyproject_has_all_required_fields(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is requests used?")
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"pyproject.toml repo: missing field {field!r}"


def test_schema_pkg_json_has_all_required_fields(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"package.json repo: missing field {field!r}"


def test_schema_no_dep_file_has_all_required_fields(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"no-dep-file repo: missing field {field!r}"


def test_schema_no_name_has_all_required_fields(tmp_path: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(tmp_path, "what are the dependencies?")
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"no-name case: missing field {field!r}"


def test_schema_keys_identical_across_all_cases(
    req_txt_repo: Path,
    pyproject_repo: Path,
    pkg_json_repo: Path,
    no_dep_file_repo: Path,
    tmp_path: Path,
) -> None:
    """All scenarios must produce the same set of top-level keys."""
    keys = [
        set(build_dependency_check_artifacts(req_txt_repo, "is requests used?")[0].keys()),
        set(build_dependency_check_artifacts(pyproject_repo, "is requests used?")[0].keys()),
        set(build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")[0].keys()),
        set(build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")[0].keys()),
        set(build_dependency_check_artifacts(tmp_path, "what are the dependencies?")[0].keys()),
    ]
    assert keys[0] == keys[1] == keys[2] == keys[3] == keys[4], (
        f"Key sets differ across cases: {keys}"
    )


# ---------------------------------------------------------------------------
# Case 1: requirements.txt only
# ---------------------------------------------------------------------------


def test_req_txt_declared_dep_found(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert artifacts["declared"] is True
    assert "requirements.txt" in artifacts["declared_in"]


def test_req_txt_declared_dep_not_found(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is flask used?")
    assert artifacts["declared"] is False
    assert artifacts["declared_in"] == []


def test_req_txt_ecosystem_is_python(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert "python" in artifacts["ecosystems"]
    assert "node" not in artifacts["ecosystems"]


def test_req_txt_no_error_code(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert artifacts["error_code"] is None


def test_req_txt_workflow_field(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert artifacts["workflow"] == "DependencyCheckWorkflow"


def test_req_txt_dependency_field(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert artifacts["dependency"] == "requests"


def test_req_txt_summary_non_empty(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert isinstance(artifacts["summary"], str)
    assert len(artifacts["summary"]) > 0


# ---------------------------------------------------------------------------
# Case 2: pyproject.toml only
# ---------------------------------------------------------------------------


def test_pyproject_declared_dep_found(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is requests used?")
    assert artifacts["declared"] is True
    assert "pyproject.toml" in artifacts["declared_in"]


def test_pyproject_declared_dep_not_found(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is flask used?")
    assert artifacts["declared"] is False
    assert artifacts["declared_in"] == []


def test_pyproject_ecosystem_is_python(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is requests used?")
    assert "python" in artifacts["ecosystems"]


def test_pyproject_no_error_code(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is requests used?")
    assert artifacts["error_code"] is None


def test_pyproject_second_dep_found(pyproject_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pyproject_repo, "is numpy used?")
    assert artifacts["declared"] is True
    assert "pyproject.toml" in artifacts["declared_in"]


# ---------------------------------------------------------------------------
# Case 3: package.json only
# ---------------------------------------------------------------------------


def test_pkg_json_declared_dep_found(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")
    assert artifacts["declared"] is True
    assert "package.json" in artifacts["declared_in"]


def test_pkg_json_dev_dep_found(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is typescript used?")
    assert artifacts["declared"] is True
    assert "package.json" in artifacts["declared_in"]


def test_pkg_json_declared_dep_not_found(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is react used?")
    assert artifacts["declared"] is False
    assert artifacts["declared_in"] == []


def test_pkg_json_ecosystem_is_node(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")
    assert "node" in artifacts["ecosystems"]
    assert "python" not in artifacts["ecosystems"]


def test_pkg_json_no_error_code(pkg_json_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")
    assert artifacts["error_code"] is None


# ---------------------------------------------------------------------------
# Case 4: No dependency file — explicit error, no silent failure
# ---------------------------------------------------------------------------


def test_no_dep_file_error_code(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    assert artifacts["error_code"] == "no_dependency_files_found", (
        f"expected 'no_dependency_files_found', got {artifacts['error_code']!r}"
    )


def test_no_dep_file_summary_mentions_no_files(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    summary = artifacts["summary"].lower()
    assert "no" in summary or "not found" in summary or "cannot" in summary, (
        f"summary must explain absence of files: {artifacts['summary']!r}"
    )


def test_no_dep_file_declared_false(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    assert artifacts["declared"] is False
    assert artifacts["declared_in"] == []


def test_no_dep_file_dependency_extracted(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    assert artifacts["dependency"] == "requests"


def test_no_dep_file_workflow_field(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    assert artifacts["workflow"] == "DependencyCheckWorkflow"


def test_no_dep_file_summary_non_empty(no_dep_file_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(no_dep_file_repo, "is requests used?")
    assert isinstance(artifacts["summary"], str) and len(artifacts["summary"]) > 0


# ---------------------------------------------------------------------------
# Case 5: No name extractable from task
# ---------------------------------------------------------------------------


def test_no_name_error_code(tmp_path: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(tmp_path, "what are all the packages?")
    assert artifacts["error_code"] == "dependency_name_not_found"


def test_no_name_dependency_is_none(tmp_path: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(tmp_path, "what are all the packages?")
    assert artifacts["dependency"] is None


def test_no_name_declared_false(tmp_path: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(tmp_path, "what are all the packages?")
    assert artifacts["declared"] is False


def test_no_name_summary_explains(tmp_path: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(tmp_path, "what are all the packages?")
    assert isinstance(artifacts["summary"], str) and len(artifacts["summary"]) > 0


# ---------------------------------------------------------------------------
# Source import detection
# ---------------------------------------------------------------------------


def test_source_used_true(py_source_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert artifacts["used"] is True


def test_source_usage_count(py_source_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert artifacts["usage_count"] >= 1


def test_source_usage_files_non_empty(py_source_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert len(artifacts["usage_files"]) >= 1


def test_source_usage_locations_schema(py_source_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    for loc in artifacts["usage_locations"]:
        for field in _REQUIRED_USAGE_LOCATION_FIELDS:
            assert field in loc, f"usage_location missing field {field!r}: {loc}"
        assert isinstance(loc["file"], str) and loc["file"]
        assert isinstance(loc["line"], int) and loc["line"] >= 1
        assert isinstance(loc["usage_type"], str) and loc["usage_type"]


def test_source_both_declared_and_used(py_source_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert artifacts["declared"] is True
    assert artifacts["used"] is True


def test_source_from_import_detected(py_source_repo: Path) -> None:
    """from requests import Session should be detected."""
    (py_source_repo / "extra.py").write_text("from requests import Session\n")
    artifacts, _ = build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert artifacts["used"] is True


def test_source_multi_file_usage(multi_import_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(multi_import_repo, "is requests used?")
    assert artifacts["used"] is True
    assert len(artifacts["usage_files"]) >= 2


def test_source_not_used_when_no_source_files(req_txt_repo: Path) -> None:
    artifacts, _ = build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    # no .py files in fixture → used=False
    assert artifacts["used"] is False
    assert artifacts["usage_count"] == 0
    assert artifacts["usage_files"] == []


# ---------------------------------------------------------------------------
# No false success when check is inconclusive (requirement #4)
# ---------------------------------------------------------------------------


def test_success_true_when_declaration_file_exists(req_txt_repo: Path) -> None:
    result = SkillLayer().run(req_txt_repo, "is requests used?")
    assert result["success"] is True


def test_success_false_when_no_dep_file(no_dep_file_repo: Path) -> None:
    result = SkillLayer().run(no_dep_file_repo, "is requests used?")
    assert result["success"] is False, (
        "success must be False when no declaration files found — check would be inconclusive"
    )


def test_success_false_when_no_name_extracted(no_dep_file_repo: Path) -> None:
    # "show package" routes to dependency_check but yields no extractable name
    result = SkillLayer().run(no_dep_file_repo, "show package")
    assert result["success"] is False


def test_success_true_even_when_dep_not_declared(req_txt_repo: Path) -> None:
    """Checking a dep that's not in the file is a valid conclusive result — success=True."""
    result = SkillLayer().run(req_txt_repo, "is flask used?")
    assert result["success"] is True


def test_no_dep_file_artifacts_has_error_code(no_dep_file_repo: Path) -> None:
    result = SkillLayer().run(no_dep_file_repo, "is requests used?")
    assert result["artifacts"]["error_code"] == "no_dependency_files_found"


# ---------------------------------------------------------------------------
# Zero LLM calls — purely deterministic
# ---------------------------------------------------------------------------


def test_llm_calls_zero_req_txt(req_txt_repo: Path) -> None:
    result = SkillLayer().run(req_txt_repo, "is requests used?")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_pyproject(pyproject_repo: Path) -> None:
    result = SkillLayer().run(pyproject_repo, "is requests used?")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_pkg_json(pkg_json_repo: Path) -> None:
    result = SkillLayer().run(pkg_json_repo, "is lodash used?")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_no_dep_file(no_dep_file_repo: Path) -> None:
    result = SkillLayer().run(no_dep_file_repo, "is requests used?")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_no_name(tmp_path: Path) -> None:
    result = SkillLayer().run(tmp_path, "show package")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_with_source_imports(py_source_repo: Path) -> None:
    result = SkillLayer().run(py_source_repo, "is requests used?")
    assert result["llm_calls"] == 0


# ---------------------------------------------------------------------------
# Read-only invariant — no file modification
# ---------------------------------------------------------------------------


def test_never_modifies_files_req_txt(req_txt_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_dependency_check_artifacts(req_txt_repo, "is requests used?")
    assert writes == [], f"write_text was called on: {writes}"


def test_never_modifies_files_pkg_json(pkg_json_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_dependency_check_artifacts(pkg_json_repo, "is lodash used?")
    assert writes == [], f"write_text was called on: {writes}"


def test_never_modifies_files_with_source(py_source_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_dependency_check_artifacts(py_source_repo, "is requests used?")
    assert writes == [], f"write_text was called on: {writes}"
