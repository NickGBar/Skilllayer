"""Tests for MapDependenciesWorkflow.

Covers parsers for each supported format, the full SkillLayer().run() path
(verifies llm_calls == 0 and correct routing), and an integration test on the
SkillLayer repo itself (pyproject.toml with PEP 621 optional-dependencies).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.router import SkillRouter
from skilllayer.runner.core import (
    build_map_dependencies_artifacts,
    _depmap_parse_requirements_txt,
    _depmap_parse_package_json,
    _depmap_parse_pyproject_toml,
    _depmap_parse_pipfile,
)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_map_dependencies() -> None:
    for phrase in [
        "map dependencies",
        "list all dependencies",
        "show all project dependencies",
        "what dependencies are in this repo",
        "unpinned dependencies",
        "dependency map",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "map_dependencies", f"failed for: {phrase!r}"
        assert decision.workflow == "MapDependenciesWorkflow"
        assert decision.matched is True


def test_router_does_not_collide_with_single_dep_check() -> None:
    # Single-dependency lookups should still route to DependencyCheckWorkflow.
    for phrase in ["is pytest used", "where is requests imported", "check flask dependency"]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "dependency_check", f"unexpectedly routed for: {phrase!r}"


# ---------------------------------------------------------------------------
# requirements.txt parser
# ---------------------------------------------------------------------------


def test_req_txt_exact_pin(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "requests==2.31.0\nflask>=2.0\npandas\n"
    )
    deps = _depmap_parse_requirements_txt(tmp_path / "requirements.txt", "direct")
    by_name = {d["name"]: d for d in deps}

    assert by_name["requests"]["pinned"] is True
    assert by_name["requests"]["unpinned"] is False
    assert by_name["requests"]["version_spec"] == "==2.31.0"

    assert by_name["flask"]["pinned"] is False
    assert by_name["flask"]["unpinned"] is False
    assert by_name["flask"]["version_spec"] == ">=2.0"

    assert by_name["pandas"]["pinned"] is False
    assert by_name["pandas"]["unpinned"] is True
    assert by_name["pandas"]["version_spec"] is None


def test_req_txt_skips_comments_and_options(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "# comment\n-r other.txt\nhttps://example.com/pkg.zip\nnumpy==1.26.0\n"
    )
    deps = _depmap_parse_requirements_txt(tmp_path / "requirements.txt", "direct")
    assert len(deps) == 1
    assert deps[0]["name"] == "numpy"


def test_req_txt_handles_extras(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests[security]==2.31.0\n")
    deps = _depmap_parse_requirements_txt(tmp_path / "requirements.txt", "direct")
    assert deps[0]["name"] == "requests"
    assert deps[0]["version_spec"] == "==2.31.0"
    assert deps[0]["pinned"] is True


# ---------------------------------------------------------------------------
# package.json parser
# ---------------------------------------------------------------------------


def test_package_json_sections(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"express": "4.18.2", "lodash": "^4.17.21"},
        "devDependencies": {"jest": "*", "eslint": "latest"},
    }))
    deps = _depmap_parse_package_json(tmp_path / "package.json")
    by_name = {d["name"]: d for d in deps}

    # Plain "4.18.2" → pinned
    assert by_name["express"]["pinned"] is True
    assert by_name["express"]["unpinned"] is False
    assert by_name["express"]["type"] == "direct"

    # "^4.17.21" → neither pinned nor unpinned (semver range)
    assert by_name["lodash"]["pinned"] is False
    assert by_name["lodash"]["unpinned"] is False

    # "*" and "latest" → unpinned
    assert by_name["jest"]["unpinned"] is True
    assert by_name["eslint"]["unpinned"] is True
    assert by_name["jest"]["type"] == "dev"


def test_package_json_missing_file(tmp_path: Path) -> None:
    deps = _depmap_parse_package_json(tmp_path / "package.json")
    assert deps == []


# ---------------------------------------------------------------------------
# pyproject.toml parser (PEP 621 format used by SkillLayer)
# ---------------------------------------------------------------------------


def test_pyproject_pep621(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'dependencies = ["requests>=2.28", "flask==2.3.0"]\n'
        "\n"
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=8.0"]\n'
        'extras = ["redis"]\n'
    )
    deps = _depmap_parse_pyproject_toml(tmp_path / "pyproject.toml")
    by_name = {d["name"]: d for d in deps}

    assert by_name["requests"]["type"] == "direct"
    assert by_name["requests"]["pinned"] is False  # >= not ==
    assert by_name["requests"]["unpinned"] is False

    assert by_name["flask"]["pinned"] is True
    assert by_name["flask"]["type"] == "direct"

    assert by_name["pytest"]["type"] == "dev"
    assert by_name["pytest"]["pinned"] is False

    assert by_name["redis"]["type"] == "optional"
    assert by_name["redis"]["unpinned"] is True


# ---------------------------------------------------------------------------
# Pipfile parser
# ---------------------------------------------------------------------------


def test_pipfile_sections(tmp_path: Path) -> None:
    (tmp_path / "Pipfile").write_text(
        "[packages]\n"
        'requests = "==2.28.0"\n'
        'django = ">=4.0"\n'
        'celery = "*"\n'
        "\n"
        "[dev-packages]\n"
        'pytest = "*"\n'
    )
    deps = _depmap_parse_pipfile(tmp_path / "Pipfile")
    by_name = {d["name"]: d for d in deps}

    assert by_name["requests"]["pinned"] is True
    assert by_name["requests"]["type"] == "direct"
    assert by_name["django"]["pinned"] is False
    assert by_name["django"]["unpinned"] is False
    assert by_name["celery"]["unpinned"] is True
    assert by_name["pytest"]["type"] == "dev"
    assert by_name["pytest"]["unpinned"] is True


# ---------------------------------------------------------------------------
# build_map_dependencies_artifacts (builder)
# ---------------------------------------------------------------------------


def test_builder_records_files_found_and_not_found(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    result = build_map_dependencies_artifacts(tmp_path)
    assert "requirements.txt" in result["files_parsed"]
    assert "pyproject.toml" in result["files_not_found"]
    assert "package.json" in result["files_not_found"]


def test_builder_aggregates_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "4.18.2"}}))
    result = build_map_dependencies_artifacts(tmp_path)
    assert result["total_dependencies"] == 2
    names = {d["name"] for d in result["dependencies"]}
    assert "requests" in names
    assert "express" in names


def test_builder_flags_unpinned(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\nnumpy\nflask>=2.0\n")
    result = build_map_dependencies_artifacts(tmp_path)
    assert result["unpinned_count"] == 1
    assert "numpy" in result["unpinned_names"]
    assert result["pinned_count"] == 1


def test_builder_never_modifies_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    writes: list[str] = []

    real_write = Path.write_text
    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_map_dependencies_artifacts(tmp_path)
    assert writes == [], f"write_text was called on: {writes}"


# ---------------------------------------------------------------------------
# Integration: SkillLayer().run() on the SkillLayer repo itself
# ---------------------------------------------------------------------------


def test_run_on_skilllayer_repo() -> None:
    repo = Path(__file__).parent.parent  # project root

    result = SkillLayer().run(repo, "map dependencies")

    assert result["success"] is True
    assert result["workflow"] == "MapDependenciesWorkflow"
    assert result["llm_calls"] == 0

    artifacts = result["artifacts"]
    # pyproject.toml is always present in this repo
    assert "pyproject.toml" in artifacts["files_parsed"]
    # At least one candidate file is absent
    assert len(artifacts["files_not_found"]) > 0

    # Known optional-deps declared in pyproject.toml
    names = {d["name"] for d in artifacts["dependencies"]}
    assert "mcp" in names
    assert "pytest" in names

    # pytest is classified as dev from pyproject.toml (its group is named "dev")
    pytest_from_pyproject = [
        d for d in artifacts["dependencies"]
        if d["name"] == "pytest" and d["source_file"] == "pyproject.toml"
    ]
    assert pytest_from_pyproject, "expected pytest from pyproject.toml"
    assert pytest_from_pyproject[0]["type"] == "dev"

    # None are exact-pinned in this repo (all use >=)
    assert artifacts["pinned_count"] == 0
    assert isinstance(artifacts["summary"], str) and artifacts["summary"]
