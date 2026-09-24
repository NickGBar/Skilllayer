"""The plugin's hook wrapper only hands the stop to a `skilllayer` that can run `verify`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "plugin/hooks/skilllayer-hook.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo_with_failing_tests(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests/test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "notes.txt").write_text("changed this turn\n")
    return repo


def _command_dir(path: Path, body: str) -> Path:
    path.mkdir(parents=True)
    (path / "skilllayer").write_text("#!/bin/sh\n" + body)
    (path / "skilllayer").chmod(0o755)
    return path


def _working(tmp_path: Path) -> Path:
    return _command_dir(tmp_path / "working", f'PYTHONPATH="{ROOT / "src"}" exec "{sys.executable}" -m skilllayer "$@"\n')


def _broken(tmp_path: Path) -> Path:
    """An editable install whose checkout was deleted: every call dies on import."""
    return _command_dir(tmp_path / "broken", "echo \"ModuleNotFoundError: No module named 'skilllayer'\" >&2\nexit 1\n")


def _outdated(tmp_path: Path) -> Path:
    """A release from before `verify`: argparse rejects the subcommand with exit 2."""
    return _command_dir(tmp_path / "outdated", "echo 'skilllayer: error: invalid choice: verify' >&2\nexit 2\n")


def _stop(tmp_path: Path, repo: Path, *command_dirs: Path | str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.pathsep.join([*map(str, command_dirs), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path / "home"),
        "SKILLLAYER_VERIFY_DIR": str(tmp_path / "verify-data"),
    }
    payload = json.dumps({"session_id": "s", "cwd": str(repo)})
    return subprocess.run(["sh", str(WRAPPER), "stop"], input=payload, env=env, cwd=repo, capture_output=True, text=True, timeout=120)


def test_a_broken_install_earlier_on_path_is_skipped_for_a_working_one(tmp_path):
    repo = _repo_with_failing_tests(tmp_path)
    done = _stop(tmp_path, repo, _broken(tmp_path), _working(tmp_path))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["decision"] == "block"


def test_an_outdated_install_is_never_mistaken_for_a_block(tmp_path):
    """Its argparse exit 2 would read as "block this stop" to Claude Code, and the agent
    would be sent back to work with a usage message."""
    repo = _repo_with_failing_tests(tmp_path)
    done = _stop(tmp_path, repo, _outdated(tmp_path))
    assert done.returncode == 0
    message = json.loads(done.stdout)["systemMessage"]
    assert "broken or too old" in message and "NOT verified" in message


def test_only_a_broken_install_is_reported_not_silent(tmp_path):
    repo = _repo_with_failing_tests(tmp_path)
    done = _stop(tmp_path, repo, _broken(tmp_path))
    assert done.returncode == 0
    assert "broken or too old" in json.loads(done.stdout)["systemMessage"]


def test_a_relative_path_entry_never_runs_a_skilllayer_planted_in_the_repository(tmp_path):
    """Hooks run inside the repository being judged, so "." on PATH would pick up a
    `skilllayer` the repository itself provides."""
    repo = _repo_with_failing_tests(tmp_path)
    marker = tmp_path / "planted-ran"
    (repo / "skilllayer").write_text(f'#!/bin/sh\necho ran > "{marker}"\nexit 0\n')
    (repo / "skilllayer").chmod(0o755)
    done = _stop(tmp_path, repo, ".", _working(tmp_path))
    assert not marker.exists()
    assert json.loads(done.stdout)["decision"] == "block"
