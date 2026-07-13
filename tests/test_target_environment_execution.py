"""Focused truthfulness tests for target-repository Python test execution."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path

from skilllayer.runner.core import build_release_readiness_artifacts, build_safe_change_artifacts
from skilllayer.verifier import TestRunner


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


def _python_path(repo: Path, name: str = ".venv") -> Path:
    return repo / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv(repo: Path, name: str = ".venv", *, system_site_packages: bool = True) -> Path:
    venv.EnvBuilder(with_pip=False, system_site_packages=system_site_packages).create(repo / name)
    python = _python_path(repo, name)
    assert python.exists()
    return python


def _target_python_with_pytest(repo: Path, name: str = ".venv") -> tuple[Path, Path]:
    """Create a local selected-Python wrapper without depending on global packages."""
    python = _python_path(repo, name)
    python.parent.mkdir(parents=True)
    target_site = repo / name / "target-site-packages"
    target_site.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(target_site))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    python.chmod(0o755)
    return python, target_site


class TestInterpreterSelection:
    def test_dot_venv_is_selected_and_reported(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        python = _venv(repo)
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        runner = TestRunner()
        selection = runner.select_python_interpreter(repo).as_dict()
        assert selection["selected_python"] == str(python)
        assert selection["selection_source"] == "target_dot_venv"
        assert selection["target_environment_usable"] is True
        command = runner.detect(repo)
        assert command and command[:3] == [str(python), "-m", "pytest"]

    def test_priority_falls_through_venv_then_env(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        venv_python = _venv(repo, "venv")
        assert TestRunner().select_python_interpreter(repo).selected_python == str(venv_python)
        (repo / "venv").rename(repo / "env")
        assert TestRunner().select_python_interpreter(repo).selection_source == "target_env"

    def test_unusable_target_python_falls_back_with_warning(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        candidate = _python_path(repo)
        candidate.parent.mkdir(parents=True)
        candidate.write_text("not an executable")
        selection = TestRunner().select_python_interpreter(repo).as_dict()
        assert selection["selected_python"] == sys.executable
        assert selection["fallback_used"] is True
        assert selection["error_code"] == "target_python_unusable"
        assert "target_python_unusable_fallback_to_current" in selection["warnings"]

    def test_non_python_command_is_not_rewritten(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        result = TestRunner().run_command(repo, ["definitely-not-python-test-command"])
        assert result["selection_source"] == "non_python_command"
        assert result["selected_python"] is None

    def test_symlink_within_repository_is_usable(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        wrapper = repo / "tools" / "python-wrapper"
        wrapper.parent.mkdir()
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable!s} \"$@\"\n")
        wrapper.chmod(0o755)
        candidate = _python_path(repo)
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(wrapper)
        selection = TestRunner().select_python_interpreter(repo).as_dict()
        assert selection["selected_python"] == str(candidate)
        assert selection["target_environment_usable"] is True
        assert "target_python_symlink_resolves_outside_repository" not in selection["warnings"]

    def test_external_python_symlink_is_explicitly_classified(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        candidate = _python_path(repo)
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(sys.executable)
        selection = TestRunner().select_python_interpreter(repo).as_dict()
        assert selection["selected_python"] == str(candidate)
        assert selection["target_environment_usable"] is True
        assert "target_python_symlink_resolves_outside_repository" in selection["warnings"]


class TestTargetExecution:
    def test_target_python_runs_dependency_unavailable_to_skilllayer(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        python, target_site = _target_python_with_pytest(repo)
        (target_site / "target_only_dependency.py").write_text("VALUE = 'target'\n")
        (repo / "tests" / "test_target.py").write_text(
            "from target_only_dependency import VALUE\n\ndef test_target_dependency(): assert VALUE == 'target'\n"
        )
        current_probe = subprocess.run(
            [sys.executable, "-c", "import target_only_dependency"], capture_output=True, text=True
        )
        assert current_probe.returncode != 0
        result = TestRunner().run(repo)
        assert result["returncode"] == 0
        assert result["selected_python"] == str(python)
        assert result["selection_source"] == "target_dot_venv"
        assert result["tests_started"] is True

    def test_target_python_without_pytest_is_environment_error(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        python = _venv(repo, system_site_packages=False)
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        result = TestRunner().run(repo)
        assert result["selected_python"] == str(python)
        assert result["environment_error"] is True
        assert result["environment_error_code"] == "pytest_not_installed_in_selected_environment"
        assert result["tests_started"] is False

    def test_import_error_before_collection_is_not_test_failure(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _target_python_with_pytest(repo)
        (repo / "tests" / "test_app.py").write_text("import unavailable_fixture_dependency\n")
        result = TestRunner().run(repo)
        assert result["environment_error"] is True
        assert result["environment_error_code"] == "dependency_import_failed_before_collection"
        assert result["tests_started"] is False


class TestProfessionalSkillSemantics:
    def test_safe_change_reports_environment_incomplete_not_validation_failed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "app.py").write_text("VALUE = 2\n")
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        result = build_safe_change_artifacts(repo, "change value", phase="validate")
        assert result["verdict"] == "CHANGE_INCOMPLETE"
        assert result["environment_blocker"]["code"] == "pytest_not_installed_in_selected_environment"
        assert result["validation_complete"] is False

    def test_release_readiness_marks_environment_test_as_incomplete(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _venv(repo, system_site_packages=False)
        (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
        result = build_release_readiness_artifacts(repo, deep=True)
        assert result["test_status"]["environment_error"] is True
        assert result["test_status"]["tests_run"] is False
        assert result["verdict"] == "INCOMPLETE_ASSESSMENT"

    def test_release_readiness_keeps_actual_assertion_failure_not_ready(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _target_python_with_pytest(repo)
        (repo / "tests" / "test_app.py").write_text("def test_bad(): assert False\n")
        result = build_release_readiness_artifacts(repo, deep=True)
        assert result["test_status"]["tests_started"] is True
        assert result["test_status"]["environment_error"] is False
        assert result["verdict"] == "NOT_READY"
