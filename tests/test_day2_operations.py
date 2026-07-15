from __future__ import annotations

import json
from pathlib import Path

from skilllayer.cli import main
from skilllayer.operations import apply_uninstall_plan, build_uninstall_plan
from skilllayer.update_check import check_for_update


def test_update_check_current_and_unknown(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return b'{"tag_name":"v0.2.0"}'

    monkeypatch.setattr("skilllayer.update_check.product_version", lambda: "0.2.0")
    monkeypatch.setattr("skilllayer.update_check.urllib.request.urlopen", lambda *a, **k: Response())
    assert check_for_update(installation_type="VENV")["status"] == "UP_TO_DATE"
    monkeypatch.setattr("skilllayer.update_check.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    result = check_for_update()
    assert result["status"] == "UPDATE_STATUS_UNKNOWN"
    assert result["mutated_environment"] is False


def test_uninstall_preserves_unrelated_mcp_and_memory(tmp_path: Path) -> None:
    (tmp_path / ".skilllayer").mkdir()
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"skilllayer": {"command": "python"}, "other": {"command": "other"}}}), encoding="utf-8")
    plan = build_uninstall_plan(tmp_path)
    assert plan["status"] == "READY"
    assert plan["changes"][0]["action"] == "remove_mcp_entry"
    apply_uninstall_plan(plan)
    payload = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "skilllayer" not in payload["mcpServers"]
    assert "other" in payload["mcpServers"]
    assert (tmp_path / ".skilllayer").exists()


def test_uninstall_cli_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"skilllayer": {}}}), encoding="utf-8")
    assert main(["uninstall", "--repo", str(tmp_path), "--dry-run"]) == 0
    assert "skilllayer" in (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    assert "Dry run" in capsys.readouterr().out


def test_uninstall_cli_removes_only_skilllayer_entry(tmp_path: Path) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"skilllayer": {}, "other": {}}}), encoding="utf-8")
    assert main(["uninstall", "--repo", str(tmp_path)]) == 0
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert set(payload["mcpServers"]) == {"other"}


def test_tag_version_check(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text("[project]\nversion='0.2.0'\n", encoding="utf-8")
    from scripts.check_tag_version import main as check_tag
    assert check_tag(["v0.2.0", "--project", str(project)]) == 0
    assert check_tag(["v0.2.1", "--project", str(project)]) == 1
