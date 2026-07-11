from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from skilllayer import SkillLayer
from skilllayer.cli import is_writable_dir as cli_is_writable_dir
from skilllayer.cli import main as cli_main
from skilllayer.config.defaults import COMMAND_METADATA, WORKFLOWS, WORKFLOW_METADATA, WRITE_BEHAVIORS
from skilllayer.mcp_server import (
    is_writable_dir as mcp_is_writable_dir,
    skilllayer_inspect_repo_structure,
    skilllayer_list_workflows,
    skilllayer_doctor,
)
from skilllayer.runner.core import (
    build_add_todo_artifacts,
    build_detect_activity_artifacts,
    build_mark_todo_done_artifacts,
    build_measure_memory_artifacts,
    build_measure_test_speed_artifacts,
    build_profile_execution_artifacts,
    build_remember_preferences_artifacts,
    build_save_context_artifacts,
    build_track_decision_artifacts,
    build_watch_file_changes_artifacts,
)
from skilllayer.tools.browser import run_browser_smoke
from skilllayer.verifier.test_runner import TestRunner as _TestRunner


CAPABILITY_KEYS = {
    "write_behavior",
    "state_locations",
    "requires_write_consent",
    "may_dirty_worktree",
}


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def test_every_workflow_has_authoritative_write_metadata() -> None:
    assert set(WORKFLOWS) == set(WORKFLOW_METADATA)
    for name, metadata in WORKFLOW_METADATA.items():
        assert CAPABILITY_KEYS <= set(metadata), name
        assert metadata["write_behavior"] in WRITE_BEHAVIORS
        assert isinstance(metadata["state_locations"], list)
        assert isinstance(metadata["requires_write_consent"], bool)
        assert isinstance(metadata["may_dirty_worktree"], bool)


def test_command_discovery_metadata_is_complete() -> None:
    for name, metadata in COMMAND_METADATA.items():
        assert CAPABILITY_KEYS <= set(metadata), name


def test_cli_and_mcp_workflow_discovery_expose_same_write_metadata(capsys) -> None:
    assert cli_main(["workflows", "--json"]) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    mcp_payload = skilllayer_list_workflows()
    cli_items = {item["name"]: item for item in cli_payload["workflows"]}
    mcp_items = {item["name"]: item for item in mcp_payload["workflows"]}
    for name, metadata in WORKFLOW_METADATA.items():
        for key in CAPABILITY_KEYS:
            assert cli_items[name][key] == metadata[key]
            assert mcp_items[name][key] == metadata[key]


def test_watch_file_changes_default_is_read_only(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = build_watch_file_changes_artifacts(tmp_path)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before
    assert result["state_write_performed"] is False
    assert result["written_paths"] == []
    assert result["persist_snapshot"] is False


def test_watch_file_changes_explicit_persistence_reports_path_without_gitignore(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = build_watch_file_changes_artifacts(tmp_path, persist_snapshot=True)
    assert result["written_paths"] == [".skilllayer/file_watch_snapshot.json"]
    assert result["state_write_performed"] is True
    assert not (tmp_path / ".gitignore").exists()


def test_detect_activity_default_does_not_create_snapshot(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    result = build_detect_activity_artifacts(tmp_path)
    assert result["written_paths"] == []
    assert not (tmp_path / ".skilllayer").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_detect_activity_explicit_persistence_reports_snapshot(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    result = build_detect_activity_artifacts(tmp_path, persist_snapshot=True)
    assert result["written_paths"] == [".skilllayer/repo_activity_snapshot.json"]
    assert not (tmp_path / ".gitignore").exists()


def test_measure_test_speed_default_does_not_write_baseline(tmp_path: Path) -> None:
    run_result = {
        "returncode": 0,
        "passed": 1,
        "failed": 0,
        "test_count": 1,
        "duration_ms": 12,
        "stdout": "1 passed in 0.01s\n",
        "stderr": "",
    }
    with (
        patch.object(_TestRunner, "detect", return_value=["python", "-m", "pytest", "-q"]),
        patch.object(_TestRunner, "run_command", return_value=run_result),
    ):
        result = build_measure_test_speed_artifacts(tmp_path)
    assert result["written_paths"] == []
    assert not (tmp_path / ".skilllayer").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_measure_test_speed_explicit_baseline_reports_path(tmp_path: Path) -> None:
    run_result = {
        "returncode": 0,
        "passed": 1,
        "failed": 0,
        "test_count": 1,
        "duration_ms": 12,
        "stdout": "1 passed in 0.01s\n",
        "stderr": "",
    }
    with (
        patch.object(_TestRunner, "detect", return_value=["python", "-m", "pytest", "-q"]),
        patch.object(_TestRunner, "run_command", return_value=run_result),
    ):
        result = build_measure_test_speed_artifacts(tmp_path, persist_baseline=True)
    assert result["written_paths"] == [".skilllayer/test_speed_baseline.json"]
    assert not (tmp_path / ".gitignore").exists()


def test_memory_and_profile_measurements_default_to_no_baseline_write(tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text("value = sum(range(10))\n", encoding="utf-8")
    memory = build_measure_memory_artifacts(tmp_path, "target.py")
    profile = build_profile_execution_artifacts(tmp_path, "target.py")
    assert memory["written_paths"] == []
    assert profile["written_paths"] == []
    assert not (tmp_path / ".skilllayer").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_browser_smoke_default_does_not_create_artifacts(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text("<html><body>ok</body></html>", encoding="utf-8")
    output_dir = tmp_path / "browser-output"
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=None):
        result = run_browser_smoke(f"file://{page}", ["body"], output_dir=output_dir)
    assert result["written_paths"] == []
    assert result["screenshot_path"] is None
    assert result["report_path"] is None
    assert not output_dir.exists()


def test_browser_smoke_explicit_artifacts_report_every_file(tmp_path: Path) -> None:
    # No real browser backend (Playwright unavailable) -> the static fallback
    # never fabricates a screenshot, even with write_artifacts=True (no
    # placeholder PNG). Only the honest JSON report is written.
    page = tmp_path / "index.html"
    page.write_text("<html><body>ok</body></html>", encoding="utf-8")
    output_dir = tmp_path / "browser-output"
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=None):
        result = run_browser_smoke(
            f"file://{page}",
            ["body"],
            output_dir=output_dir,
            write_artifacts=True,
        )
    assert result["screenshot_path"] is None
    assert result["success"] is False
    assert result["status"] == "unsupported"
    assert result["written_paths"] == [result["report_path"]]
    assert all(Path(path).exists() for path in result["written_paths"])
    assert not (tmp_path / ".gitignore").exists()


def test_doctor_writability_checks_do_not_create_probe_or_directory(tmp_path: Path) -> None:
    target = tmp_path / "not-created" / "logs"
    assert cli_is_writable_dir(target) is True
    assert mcp_is_writable_dir(target) is True
    assert not target.exists()
    assert not (tmp_path / "not-created").exists()


def test_doctor_command_and_mcp_are_read_only_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLLAYER_TELEMETRY_ENABLED", raising=False)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert cli_main(["doctor", "--repo", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    mcp_result = skilllayer_doctor(repo_path=str(tmp_path))
    assert mcp_result["success"] is True
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before


def test_cli_and_mcp_read_only_calls_do_not_create_automatic_telemetry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLLAYER_TELEMETRY_ENABLED", raising=False)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert cli_main(["workflows", "--json"]) == 0
    capsys.readouterr()
    result = skilllayer_inspect_repo_structure(str(tmp_path))
    assert result["success"] is True
    assert not (tmp_path / "runs").exists()


def test_opted_in_mcp_telemetry_reports_its_written_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLLAYER_TELEMETRY_ENABLED", "1")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = skilllayer_inspect_repo_structure(str(tmp_path))
    assert result["telemetry_written_paths"] == [
        "runs/skilllayer_telemetry/telemetry.jsonl",
        "runs/skilllayer_telemetry/summary.json",
    ]
    assert (tmp_path / "runs" / "skilllayer_telemetry" / "telemetry.jsonl").exists()
    assert (tmp_path / "runs" / "skilllayer_telemetry" / "summary.json").exists()


def test_generic_read_only_run_has_no_default_logs_or_automatic_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLLAYER_TELEMETRY_ENABLED", raising=False)
    source = tmp_path / "app.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    before = source.read_bytes()
    result = SkillLayer().run(tmp_path, "Find function target")
    assert result["success"] is True
    assert result["logs_path"] is None
    assert result["written_paths"] == []
    assert source.read_bytes() == before
    assert not (tmp_path / "runs").exists()


def test_dry_run_skips_stateful_and_external_execution(tmp_path: Path) -> None:
    todo = SkillLayer().run(tmp_path, "Add a todo: should not be written", dry_run=True)
    assert todo["success"] is True
    assert todo["external_side_effects_skipped"] is True
    assert todo["written_paths"] == []
    assert not (tmp_path / ".skilllayer").exists()

    with patch.object(_TestRunner, "detect") as detect:
        speed = SkillLayer().run(tmp_path, "Measure test suite speed", dry_run=True)
    detect.assert_not_called()
    assert speed["external_side_effects_skipped"] is True
    assert speed["written_paths"] == []


def test_explicit_run_logging_reports_all_written_files(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    log_root = tmp_path / "explicit-logs"
    result = SkillLayer().run(tmp_path, "Find function target", log_dir=log_root)
    assert len(result["run_log_written_paths"]) == 5
    assert set(result["run_log_written_paths"]) <= set(result["written_paths"])
    assert all(Path(path).exists() for path in result["run_log_written_paths"])


def test_explicit_memory_write_reports_changed_paths(tmp_path: Path) -> None:
    result = build_add_todo_artifacts(tmp_path, "Document write behavior")
    assert result["state_write_performed"] is True
    assert ".skilllayer/todos.json" in result["written_paths"]
    assert ".skilllayer/.state.json" in result["written_paths"]
    assert ".skilllayer/INDEX.md" in result["written_paths"]
    assert not (tmp_path / ".gitignore").exists()


def test_all_explicit_memory_workflows_report_their_persistent_paths(tmp_path: Path) -> None:
    context = build_save_context_artifacts(tmp_path, "Investigating writes", ["What remains?"])
    assert {".skilllayer/context/latest.md", ".skilllayer/INDEX.md"} <= set(context["written_paths"])

    decision = build_track_decision_artifacts(
        tmp_path,
        title="Report writes",
        context="Hidden writes reduce trust.",
        decision="Return every persistent path.",
        reasoning="Callers can verify side effects.",
        consequences="Artifacts become auditable.",
    )
    assert ".skilllayer/.state.json" in decision["written_paths"]
    assert any(path.startswith(".skilllayer/decisions/") for path in decision["written_paths"])

    preferences = build_remember_preferences_artifacts(
        tmp_path,
        {"testing": {"runner": "pytest"}},
    )
    assert ".skilllayer/preferences.md" in preferences["written_paths"]
    assert ".skilllayer/INDEX.md" in preferences["written_paths"]

    added = build_add_todo_artifacts(tmp_path, "Verify every path")
    marked = build_mark_todo_done_artifacts(tmp_path, added["todo_id"])
    assert ".skilllayer/todos.json" in marked["written_paths"]
    assert ".skilllayer/INDEX.md" in marked["written_paths"]
