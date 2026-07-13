"""Focused contract tests for actionable, non-executing environment remediation."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path

from skilllayer.mcp_server import skilllayer_release_readiness, skilllayer_safe_change
from skilllayer.runner.core import build_release_readiness_artifacts, build_safe_change_artifacts
from skilllayer.verifier.test_runner import TestRunner, build_environment_remediation
from skilllayer import SkillLayer


def _result(code: str, repo: Path, *, selected: str | None = None, stderr: str = "") -> dict:
    python = selected or str(repo / ".venv" / "bin" / "python")
    return {
        "environment_error_code": code,
        "selected_python": python,
        "execution_environment": {"selected_python": python, "error_code": None},
        "stdout": "",
        "stderr": stderr,
    }


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.0.1"\n')
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)
    return tmp_path


def _venv(repo: Path, *, system_site_packages: bool) -> Path:
    venv.EnvBuilder(with_pip=False, system_site_packages=system_site_packages).create(repo / ".venv")
    return repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _target_python_with_pytest(repo: Path) -> Path:
    """Local target interpreter fixture with pytest, independent of global site packages."""
    python = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    target_site = repo / ".venv" / "target-site-packages"
    target_site.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(target_site))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    python.chmod(0o755)
    return python


class TestMetadataRemediation:
    def test_requirements_test_has_highest_priority(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==1\n")
        (tmp_path / "requirements-dev.txt").write_text("pytest\n")
        (tmp_path / "requirements-test.txt").write_text("pytest\n")
        remediation = build_environment_remediation(tmp_path, _result("pytest_not_installed_in_selected_environment", tmp_path))
        assert remediation["remediation_argv"][-2:] == ["-r", "requirements-test.txt"]
        assert remediation["requires_user_confirmation"] is True
        assert remediation["mutates_environment"] is True

    def test_requirements_dev_precedes_generic_and_requirements(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==1\n")
        (tmp_path / "requirements-dev.txt").write_text("pytest\n")
        remediation = build_environment_remediation(tmp_path, _result("pytest_not_installed_in_selected_environment", tmp_path))
        assert remediation["remediation_argv"][-1] == "requirements-dev.txt"

    def test_requirements_txt_is_used_only_when_more_specific_absent(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("pytest\n")
        remediation = build_environment_remediation(tmp_path, _result("pytest_not_installed_in_selected_environment", tmp_path))
        assert remediation["remediation_argv"][-1] == "requirements.txt"

    def test_poetry_uv_and_pdm_need_direct_metadata_evidence(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n")
        poetry = build_environment_remediation(tmp_path, _result("pytest_not_installed_in_selected_environment", tmp_path))
        assert poetry["remediation_argv"] == ["poetry", "install"]
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "uv.lock").write_text("version = 1\n")
        uv = build_environment_remediation(tmp_path, _result("dependency_import_failed_before_collection", tmp_path, stderr="ModuleNotFoundError: No module named 'thing'"))
        assert uv["remediation_argv"] == ["uv", "sync"]
        (tmp_path / "uv.lock").unlink()
        (tmp_path / "pdm.lock").write_text("lock_version = '4.0'\n")
        pdm = build_environment_remediation(tmp_path, _result("pytest_not_installed_in_selected_environment", tmp_path))
        assert pdm["remediation_argv"] == ["pdm", "install"]

    def test_import_name_is_not_converted_to_package_guess(self, tmp_path: Path) -> None:
        remediation = build_environment_remediation(
            tmp_path,
            _result("dependency_import_failed_before_collection", tmp_path, stderr="ModuleNotFoundError: No module named 'my_private.module'"),
        )
        assert remediation["remediation_type"] == "inspect_import_failure"
        assert remediation["remediation_argv"] is not None
        assert "pip install my_private" not in (remediation["remediation_command"] or "")
        assert remediation["mutates_environment"] is False

    def test_broken_interpreter_returns_verification_not_destructive_command(self, tmp_path: Path) -> None:
        candidate = tmp_path / ".venv" / "bin" / "python"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("broken")
        remediation = build_environment_remediation(tmp_path, _result("target_python_unusable", tmp_path, selected=sys.executable))
        assert remediation["remediation_type"] == "manual_review_required"
        assert remediation["remediation_argv"][-2:] == ["-c", "import sys; print(sys.executable)"]
        assert remediation["mutates_environment"] is False

    def test_unknown_environment_has_no_speculative_install(self, tmp_path: Path) -> None:
        remediation = build_environment_remediation(tmp_path, _result("execution_environment_unknown", tmp_path))
        assert remediation["remediation_type"] == "manual_review_required"
        assert remediation["remediation_command"] is None

    def test_path_with_spaces_is_display_safe_and_argv_is_canonical(self, tmp_path: Path) -> None:
        repo = tmp_path / "target repo; still safe"
        repo.mkdir()
        remediation = build_environment_remediation(repo, _result("pytest_not_installed_in_selected_environment", repo))
        assert shlex.split(remediation["remediation_command"]) == remediation["remediation_argv"]
        assert all(token not in {"&&", ";", "|", ">"} for token in remediation["remediation_argv"])


class TestWorkflowIntegration:
    def test_safe_change_and_mcp_share_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "app.py").write_text("VALUE = 2\n")
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        direct = build_safe_change_artifacts(repo, "change value", phase="validate")
        via_mcp = skilllayer_safe_change(str(repo), "change value", phase="validate")
        assert direct["verdict"] == "CHANGE_INCOMPLETE"
        assert direct["remediation"] == via_mcp["remediation"]
        assert direct["remediation"]["remediation_available"] is True
        assert not (repo / ".skilllayer").exists()

    def test_release_readiness_and_mcp_share_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        direct = build_release_readiness_artifacts(repo, deep=True)
        via_mcp = skilllayer_release_readiness(str(repo), deep=True)
        assert direct["verdict"] == "INCOMPLETE_ASSESSMENT"
        assert direct["remediation"] == via_mcp["remediation"]
        assert direct["test_status"]["remediation"] == direct["remediation"]

    def test_actual_assertion_failure_has_no_install_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _target_python_with_pytest(repo)
        (repo / "app.py").write_text("VALUE = 2\n")
        (repo / "tests" / "test_app.py").write_text("def test_bad(): assert False\n")
        safe = build_safe_change_artifacts(repo, "change value", phase="validate")
        release = build_release_readiness_artifacts(repo, deep=True)
        assert safe["verdict"] == "VALIDATION_FAILED"
        assert safe["remediation"]["remediation_type"] == "none"
        assert release["verdict"] == "NOT_READY"
        assert release["remediation"] is None

    def test_successful_test_path_has_no_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _target_python_with_pytest(repo)
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        result = TestRunner().run(repo)
        assert result["remediation"]["remediation_available"] is False
        assert result["remediation"]["remediation_type"] == "none"

    def test_cli_human_and_json_output_surface_same_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "app.py").write_text("VALUE = 2\n")
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        base = [sys.executable, "-m", "skilllayer", "safe-change", "--repo", str(repo), "--task", "change value", "--phase", "validate"]
        human = subprocess.run(base, text=True, capture_output=True)
        assert human.returncode == 1
        assert "recommended_command:" in human.stdout
        assert "SkillLayer did not run it" in human.stdout
        structured = subprocess.run([*base, "--json"], text=True, capture_output=True)
        assert structured.returncode == 1
        import json
        payload = json.loads(structured.stdout)
        assert payload["remediation"]["remediation_argv"]

    def test_generic_dispatch_preserves_canonical_remediation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "requirements-test.txt").write_text("pytest\n")
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        result = SkillLayer().run(repo, "run tests")
        remediation = result["artifacts"]["remediation"]
        assert remediation["remediation_argv"][-1] == "requirements-test.txt"
