"""Focused regression coverage for public one-prompt installation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from skilllayer.cli import main as cli_main
from skilllayer.mcp_server import create_mcp_server
from skilllayer.version import product_version


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"


def _minimal_checkout(root: Path) -> Path:
    checkout = root / "SkillLayer checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALL, checkout / "scripts" / "install.sh")
    (checkout / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return checkout


def _preflight(checkout: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(checkout / "scripts" / "install.sh"), "--preflight", *args],
        cwd=checkout,
        env=full_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_and_doctor_use_installed_distribution_version(capsys) -> None:
    expected = metadata.version("skilllayer")
    assert product_version() == expected
    assert cli_main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == expected

    assert cli_main(["doctor", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["required_checks"]["product_version"]["value"] == expected


def test_mcp_server_uses_product_version() -> None:
    assert create_mcp_server()._mcp_server.version == product_version()


def test_explicit_python_is_selected_and_reported(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path)
    result = _preflight(checkout, "--python", sys.executable)
    assert result.returncode == 0, result.stderr
    assert f"resolved path: {Path(sys.executable).resolve()}" in result.stdout
    assert "Python version:" in result.stdout
    assert "installation target:" in result.stdout
    assert not (checkout / ".venv").exists()


def test_active_venv_precedes_discovered_candidates(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path)
    active = tmp_path / "active venv"
    (active / "bin").mkdir(parents=True)
    (active / "bin" / "python").symlink_to(Path(sys.executable))
    result = _preflight(checkout, env={"VIRTUAL_ENV": str(active)})
    assert result.returncode == 0, result.stderr
    assert f"resolved path: {Path(sys.executable).resolve()}" in result.stdout


def test_supported_candidate_beats_unsupported_system_python(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    unsupported = fake_bin / "python3"
    unsupported.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    unsupported.chmod(0o755)
    (fake_bin / "python3.13").symlink_to(Path(sys.executable))
    result = _preflight(checkout, env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "VIRTUAL_ENV": ""})
    assert result.returncode == 0, result.stderr
    assert f"selected executable: {Path(sys.executable).resolve()}" in result.stdout


def test_no_supported_interpreter_stops_before_venv_creation(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path)
    # macOS system paths deliberately omit Homebrew's supported Python, leaving
    # only the unsupported system python3 (or no python3 at all).
    result = _preflight(checkout, env={"PATH": "/usr/bin:/bin", "VIRTUAL_ENV": ""})
    assert result.returncode != 0
    assert "No supported Python interpreter was found" in result.stderr
    assert not (checkout / ".venv").exists()


def test_colon_destination_stops_before_partial_install(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path / "unsafe:destination")
    result = _preflight(checkout, "--python", sys.executable)
    assert result.returncode != 0
    assert "contains ':'" in result.stderr
    assert not (checkout / ".venv").exists()


def test_installer_cleans_only_artifacts_it_created(tmp_path: Path) -> None:
    checkout = _minimal_checkout(tmp_path)
    fake_python = tmp_path / "python"
    fake_python.write_text(
        """#!/bin/sh
if [ \"$1\" = \"-c\" ]; then
  printf '%s\\n3.13.5\\n' \"$0\"
  exit 0
fi
if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"venv\" ]; then
  mkdir -p \"$3/bin\"
  cp \"$0\" \"$3/bin/python\"
  exit 0
fi
if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then
  test -n \"$PIP_CACHE_DIR\" || exit 1
  mkdir -p \"$FAKE_REPO_ROOT/build\" \"$FAKE_REPO_ROOT/dist\" \"$FAKE_REPO_ROOT/src/generated.egg-info\"
  exit 0
fi
if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"skilllayer\" ]; then
  printf '{\"success\": true}\\n'
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = subprocess.run(
        ["bash", str(checkout / "scripts" / "install.sh"), "--python", str(fake_python)],
        cwd=checkout,
        env={**os.environ, "FAKE_REPO_ROOT": str(checkout)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (checkout / "build").exists()
    assert not (checkout / "dist").exists()
    assert not (checkout / "src" / "generated.egg-info").exists()
