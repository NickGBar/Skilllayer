from __future__ import annotations

import json
from pathlib import Path

from skilllayer.cli import main
from skilllayer.policy import load_policy, policy_dry_run
from skilllayer.runner.core import build_release_readiness_artifacts, build_safe_change_artifacts


VALID = """version: 1
required_checks:
  - tests
  - secrets
approval_required_for:
  - dependency_install
  - destructive_command
safe_change:
  require_clean_or_acknowledged_worktree: true
  require_validation: true
release:
  allow_incomplete: false
  require_tests: true
  require_secret_check: true
"""


def test_policy_valid_and_dry_run_is_read_only(tmp_path: Path) -> None:
    (tmp_path / ".skilllayer-policy.yml").write_text(VALID, encoding="utf-8")
    result = load_policy(tmp_path)
    assert result["status"] == "POLICY_VALID"
    dry = policy_dry_run(tmp_path, "safe-change")
    assert dry["verdict"] == "POLICY_WOULD_REQUIRE_APPROVAL"
    assert dry["execution_performed"] is False
    assert set(p.name for p in tmp_path.iterdir()) == {".skilllayer-policy.yml"}


def test_policy_conflict_and_unsafe_content(tmp_path: Path) -> None:
    (tmp_path / ".skilllayer-policy.yml").write_text(VALID, encoding="utf-8")
    (tmp_path / ".skilllayer-policy.yaml").write_text(VALID, encoding="utf-8")
    assert load_policy(tmp_path)["status"] == "POLICY_CONFLICT"
    (tmp_path / ".skilllayer-policy.yaml").unlink()
    (tmp_path / ".skilllayer-policy.yml").write_text("version: 1\ncommand: ${SECRET}\n", encoding="utf-8")
    assert load_policy(tmp_path)["status"] == "POLICY_INVALID"


def test_policy_symlink_escape_is_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-policy-day4.yml"
    outside.write_text(VALID, encoding="utf-8")
    path = tmp_path / ".skilllayer-policy.yml"
    path.symlink_to(outside)
    assert load_policy(tmp_path)["status"] == "POLICY_UNSAFE_PATH"
    outside.unlink()


def test_policy_cli_and_no_policy_regression(tmp_path: Path, capsys) -> None:
    assert main(["policy", "check", "--repo", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "POLICY_NOT_PRESENT"
    assert main(["policy", "dry-run", "safe-change", "--repo", str(tmp_path), "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["verdict"] == "POLICY_WOULD_ALLOW"


def test_invalid_policy_blocks_integrated_workflows(tmp_path: Path) -> None:
    (tmp_path / ".skilllayer-policy.yml").write_text("version: 2\n", encoding="utf-8")
    safe = build_safe_change_artifacts(tmp_path, "change hello", phase="plan")
    release = build_release_readiness_artifacts(tmp_path)
    assert safe["verdict"] == "POLICY_INVALID"
    assert release["verdict"] == "BLOCKED_BY_POLICY"
