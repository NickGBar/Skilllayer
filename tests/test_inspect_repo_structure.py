"""Tests for InspectRepoStructureWorkflow.

Covers the builder function in isolation, the full SkillLayer().run() path
(verifies llm_calls == 0 and correct routing), and exclusion of ignored paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.runner.core import build_inspect_repo_structure_artifacts
from skilllayer.router import SkillRouter


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_inspect_repo_structure() -> None:
    for phrase in [
        "inspect repo structure",
        "map the repository structure",
        "show project structure",
        "directory tree",
        "files by type",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "inspect_repo_structure", f"failed for: {phrase!r}"
        assert decision.workflow == "InspectRepoStructureWorkflow"
        assert decision.matched is True


# ---------------------------------------------------------------------------
# Builder unit tests (controlled tmp_path fixture)
# ---------------------------------------------------------------------------


def test_counts_files_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.py").write_text("y = 2")
    (tmp_path / "README.md").write_text("# hi")
    (tmp_path / "data.json").write_text("{}")

    result = build_inspect_repo_structure_artifacts(tmp_path)

    assert result["files_by_extension"][".py"] == 2
    assert result["files_by_extension"][".md"] == 1
    assert result["files_by_extension"][".json"] == 1
    assert result["total_files"] == 4


def test_reports_size_per_directory(tmp_path: Path) -> None:
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").write_text("x" * 100)
    (tmp_path / "README.md").write_text("y" * 50)

    result = build_inspect_repo_structure_artifacts(tmp_path)

    dir_map = {d["path"]: d for d in result["directories"]}
    assert dir_map["src"]["file_count"] == 1
    assert dir_map["src"]["size_bytes"] == 100
    assert dir_map["."]["size_bytes"] == 50


def test_detects_entry_points(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    (tmp_path / "cli.py").write_text("")
    (tmp_path / "index.js").write_text("")
    (tmp_path / "other.py").write_text("")

    result = build_inspect_repo_structure_artifacts(tmp_path)

    found = {ep["file"]: ep["type"] for ep in result["entry_points"]}
    assert found.get("main.py") == "python_main"
    assert found.get("cli.py") == "python_cli"
    assert found.get("index.js") == "node_entry"
    assert "other.py" not in found


def test_excludes_venv_and_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("real")
    venv = tmp_path / ".venv" / "lib" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "dep.py").write_text("dep")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-313.pyc").write_bytes(b"\x00")

    result = build_inspect_repo_structure_artifacts(tmp_path)

    assert result["total_files"] == 1
    assert not any(".venv" in d["path"] for d in result["directories"])
    assert not any("__pycache__" in d["path"] for d in result["directories"])


def test_never_reads_file_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "secret.py").write_text("SECRET = 'do not read'")

    read_calls: list[str] = []
    real_read = Path.read_text

    def spy_read(self: Path, *args: object, **kwargs: object) -> str:
        read_calls.append(str(self))
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy_read)
    build_inspect_repo_structure_artifacts(tmp_path)

    assert read_calls == [], f"read_text was called on: {read_calls}"


# ---------------------------------------------------------------------------
# Integration: SkillLayer().run() on the SkillLayer repo itself
# ---------------------------------------------------------------------------


def test_run_on_skilllayer_repo() -> None:
    repo = Path(__file__).parent.parent  # project root

    result = SkillLayer().run(repo, "inspect repo structure")

    assert result["success"] is True
    assert result["workflow"] == "InspectRepoStructureWorkflow"
    assert result["llm_calls"] == 0

    artifacts = result["artifacts"]
    assert artifacts["total_files"] > 0
    assert artifacts["total_directories"] > 0
    assert ".py" in artifacts["files_by_extension"]

    # .venv must not appear anywhere in the output
    all_dir_paths = [d["path"] for d in artifacts["directories"]]
    assert not any(".venv" in p for p in all_dir_paths)
    assert not any(".venv" in ep["file"] for ep in artifacts["entry_points"])

    # cli.py is a known entry point in this repo
    entry_files = {ep["file"] for ep in artifacts["entry_points"]}
    assert any("cli.py" in f for f in entry_files)

    # summary is a non-empty string
    assert isinstance(artifacts["summary"], str) and artifacts["summary"]
