"""Independent verification core: tests are executed and observed, never claimed."""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from skilllayer import verify as v

_GOOD_SRC = "def login():\n    return 'old'\n"
_TEST_SRC = "from src.auth import login\n\n\ndef test_login():\n    assert login() == 'old'\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, *, with_tests: bool = True, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src/auth.py").write_text(_GOOD_SRC)
    if with_tests:
        (repo / "tests").mkdir()
        (repo / "tests/test_auth.py").write_text(_TEST_SRC)
        (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLLAYER_VERIFY_DIR", str(tmp_path / "vdata"))


def _break(repo: Path) -> None:
    (repo / "src/auth.py").write_text("def login():\n    return 'new'\n")


def test_failing_tests_are_observed_and_block(tmp_path):
    repo = _repo(tmp_path)
    _break(repo)
    report = v.verify_repo(repo)
    assert report["verdict"] == v.TESTS_FAILING
    assert report["blocked"] is True
    assert report["tests"]["source"] == "observed"
    assert any("test_login" in name for name in report["tests"]["failed_tests"])
    assert "src/auth.py" in report["changes"]["paths"]


def test_passing_tests_verify_and_do_not_block(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/auth.py").write_text(_GOOD_SRC + "# harmless\n")
    report = v.verify_repo(repo)
    assert report["verdict"] == v.VERIFIED
    assert report["blocked"] is False
    assert report["tests"]["tests_passed"] is True


def test_no_tests_is_unverified_never_a_pass(tmp_path):
    """The scenario VTE used to wave through: an agent claims '12 passed' in a repo that
    has no tests at all. Here nothing is claimed and nothing is trusted — the absence of
    a test run is reported as exactly that."""
    repo = _repo(tmp_path, with_tests=False)
    _break(repo)
    report = v.verify_repo(repo)
    assert report["verdict"] == v.UNVERIFIED_NO_TESTS
    assert report["verdict"] != v.VERIFIED
    assert report["blocked"] is False
    assert v.verify_repo(repo, config=v.VerifyConfig(block_on_unverified=True))["blocked"] is True


def test_command_that_exits_nonzero_is_a_failure_not_an_unknown(tmp_path):
    repo = _repo(tmp_path)
    report = v.verify_repo(repo, test_command=["python3", "-c", "import sys; sys.exit(3)"])
    assert report["verdict"] in {v.TESTS_FAILING}
    assert report["tests"]["exit_code"] == 3


def test_timeout_is_unverified_not_a_pass(tmp_path):
    repo = _repo(tmp_path)
    report = v.verify_repo(
        repo, test_command=["python3", "-c", "import time; time.sleep(30)"],
        config=v.VerifyConfig(test_timeout_seconds=1),
    )
    assert report["verdict"] == v.UNVERIFIED_TIMEOUT
    assert report["blocked"] is False


def test_protected_path_blocks_even_when_tests_pass(tmp_path):
    repo = _repo(tmp_path)
    (repo / "migrations").mkdir()
    (repo / "migrations/001.sql").write_text("drop table users;")
    report = v.verify_repo(repo, config=v.VerifyConfig(protected_paths=("migrations/",)))
    assert report["verdict"] == v.POLICY_VIOLATION
    assert report["blocked"] is True
    assert report["changes"]["protected_hits"] == ["migrations/001.sql"]
    assert report["tests"]["tests_passed"] is True  # facts still reported together


def test_editing_the_policy_file_is_always_a_violation(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".skilllayer-policy.yml").write_text("version: 1\n")
    report = v.verify_repo(repo)
    assert report["verdict"] == v.POLICY_VIOLATION
    assert ".skilllayer-policy.yml" in report["changes"]["protected_hits"]


def test_weakened_tests_are_reported_as_observed_facts(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tests/test_auth.py").write_text("def test_login():\n    assert True\n")
    report = v.verify_repo(repo)
    assert report["verdict"] == v.VERIFIED  # they pass — but the tampering is visible
    touched = [f for f in report["findings"] if f["kind"] == "test_files_touched"]
    assert touched and "tests/test_auth.py" in touched[0]["modified"]
    assert "Test files were changed" in v.render_agent_message(
        {**report, "findings": touched}, attempt=1, max_attempts=2
    )


def test_deleted_test_file_is_reported(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tests/test_auth.py").unlink()
    report = v.verify_repo(repo)
    touched = [f for f in report["findings"] if f["kind"] == "test_files_touched"]
    assert touched and touched[0]["deleted"] == ["tests/test_auth.py"]


def test_commits_made_since_baseline_are_seen(tmp_path):
    """An agent that commits before stopping leaves a clean tree; changes must be measured
    from the baseline HEAD or the whole check is bypassed."""
    repo = _repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "migrations").mkdir()
    (repo / "migrations/002.sql").write_text("alter table x;")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "agent commit")
    assert v.take_snapshot(repo).dirty_paths == []  # tree is clean
    report = v.verify_repo(repo, config=v.VerifyConfig(protected_paths=("migrations/",)), baseline_head=baseline)
    assert "migrations/002.sql" in report["changes"]["paths"]
    assert report["verdict"] == v.POLICY_VIOLATION


def test_snapshot_hash_moves_on_edits_inside_untracked_directories(tmp_path):
    repo = _repo(tmp_path)
    (repo / "newpkg").mkdir()
    (repo / "newpkg/a.py").write_text("x = 1\n")
    first = v.take_snapshot(repo).tree_hash
    (repo / "newpkg/a.py").write_text("x = 2\n")
    assert v.take_snapshot(repo).tree_hash != first


def test_skilllayer_directory_is_not_treated_as_a_change(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".skilllayer").mkdir()
    (repo / ".skilllayer/state.json").write_text("{}")
    snap = v.take_snapshot(repo)
    assert snap.dirty_paths == []


def test_output_tail_drops_ansi_and_keeps_lines():
    tail, limits = v._sanitized_tail("\x1b[31mFAILED\x1b[0m tests/test_a.py::test_x\nAssertionError: boom\n")
    assert tail is not None and "FAILED tests/test_a.py::test_x" in tail and "\x1b" not in tail
    assert limits == []


def test_receipt_and_log_are_written_outside_the_repository(tmp_path):
    repo = _repo(tmp_path)
    _break(repo)
    report = v.verify_repo(repo)
    written = v.record_report(repo, report)
    assert all(str(repo) not in path for path in written)
    assert (v.data_dir(repo) / "log.jsonl").is_file()
    assert _git(repo, "status", "--porcelain").count(".skilllayer") == 0
    events = v.read_events(repo)
    assert events[-1]["verdict"] == v.TESTS_FAILING and events[-1]["blocked"] is True


def test_stats_count_recovery_after_block():
    events = [
        {"ts": "2026-01-01T00:00:01Z", "session_id": "a", "verdict": v.TESTS_FAILING, "blocked": True},
        {"ts": "2026-01-01T00:00:02Z", "session_id": "a", "verdict": v.TESTS_FAILING, "blocked": True},
        {"ts": "2026-01-01T00:00:03Z", "session_id": "a", "verdict": v.VERIFIED, "blocked": False},
        {"ts": "2026-01-01T00:00:04Z", "session_id": "b", "verdict": v.UNVERIFIED_NO_TESTS, "blocked": False},
        {"ts": "2026-01-01T00:00:05Z", "session_id": "c", "verdict": v.POLICY_VIOLATION, "blocked": True},
        {"ts": "2026-01-01T00:00:06Z", "session_id": "c", "verdict": v.POLICY_VIOLATION, "blocked": False, "loop_guard": True},
    ]
    stats = v.compute_stats(events)
    assert stats["blocked"] == 3
    assert stats["recovered_after_block"] == 1  # one block *sequence* that ended verified
    assert stats["loop_guard_allowed"] == 1
    assert stats["unverified_no_tests"] == 1
    assert stats["sessions"] == 3


# --------------------------------------------------------------- which interpreter runs the tests


def _bare_python(tmp_path: Path) -> str:
    """An interpreter with no pytest — what a pipx-installed skilllayer runs from."""
    target = tmp_path / "bare-env"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(target)
    return str(target / "bin" / "python")


def _path_with_python3(tmp_path: Path, monkeypatch, target: str) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    # A wrapper, not a symlink: a venv's interpreter only finds its site-packages when it is
    # started from its own bin directory.
    (shim / "python3").write_text(f'#!/bin/sh\nexec "{target}" "$@"\n')
    (shim / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def test_an_isolated_install_uses_the_interpreter_the_users_shell_resolves(tmp_path, monkeypatch):
    """`pipx install skilllayer` puts this code in a venv without the project's test tooling.
    Reporting UNVERIFIED_ENVIRONMENT there would make the feature useless on first run for
    anyone without an in-repo virtualenv — the user's own python3 can run their tests."""
    real = sys.executable
    repo = _repo(tmp_path)
    _break(repo)
    monkeypatch.setattr(sys, "executable", _bare_python(tmp_path))
    _path_with_python3(tmp_path, monkeypatch, real)
    report = v.verify_repo(repo)
    assert report["verdict"] == v.TESTS_FAILING  # observed by running them, not an environment excuse
    assert report["tests"]["command"].split()[0].endswith("shim/python3")
    assert any(item.startswith("test_python_fallback:") for item in report["tests"]["limitations"])


def test_no_interpreter_with_the_test_framework_is_reported_as_an_environment_gap(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _break(repo)
    bare = _bare_python(tmp_path)
    monkeypatch.setattr(sys, "executable", bare)
    _path_with_python3(tmp_path, monkeypatch, bare)
    report = v.verify_repo(repo)
    assert report["verdict"] == v.UNVERIFIED_ENVIRONMENT
    assert report["blocked"] is False
    assert "pytest is not installed" in report["tests"]["summary"]  # not the runner's "No test command detected."


def test_an_explicit_test_command_is_used_as_given(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(sys, "executable", _bare_python(tmp_path))
    _path_with_python3(tmp_path, monkeypatch, str(tmp_path / "bare-env" / "bin" / "python"))
    report = v.verify_repo(repo, test_command=[str(tmp_path / "bare-env" / "bin" / "python"), "-c", "print('ok')"])
    assert report["verdict"] == v.VERIFIED
    assert not any(item.startswith("test_python_fallback") for item in report["tests"]["limitations"])


def test_every_python_on_path_is_considered_not_just_the_first(tmp_path, monkeypatch):
    """`which python3` returns one hit; an earlier interpreter without pytest must not shadow
    a later one that has it."""
    real = sys.executable
    repo = _repo(tmp_path)
    _break(repo)
    monkeypatch.setattr(sys, "executable", _bare_python(tmp_path))
    other_bare = Path(_bare_python(tmp_path / "second")).parent
    _path_with_python3(tmp_path, monkeypatch, real)
    monkeypatch.setenv("PATH", f"{other_bare}{os.pathsep}{os.environ['PATH']}")
    report = v.verify_repo(repo)
    assert report["verdict"] == v.TESTS_FAILING
    assert report["tests"]["command"].split()[0].endswith("shim/python3")


def test_a_long_test_name_does_not_cut_the_assertion_off_the_failure_line(tmp_path):
    """pytest truncates its summary line to 80 columns without a terminal; the agent needs
    the assertion, which is what comes last."""
    repo = _repo(tmp_path)
    (repo / "tests/test_auth.py").write_text(
        "from src.auth import login\n\n\n"
        "def test_a_really_long_descriptive_test_name_that_pushes_the_summary_line_past_eighty_columns():\n"
        "    assert login() == 'never'\n"
    )
    report = v.verify_repo(repo)
    assert any("'old' == 'never'" in label for label in report["tests"]["failed_tests"]), report["tests"]["failed_tests"]


def test_a_relative_path_entry_never_runs_a_script_planted_in_the_repository(tmp_path, monkeypatch):
    """The fallback probes interpreters on PATH. A '.' entry would resolve inside the repository
    being judged and execute whatever it contains."""
    repo = _repo(tmp_path)
    marker = tmp_path / "planted-script-ran"
    planted = repo / "python3"
    planted.write_text(f'#!/bin/sh\necho ran > "{marker}"\nexit 1\n')  # a builtin: PATH here contains nothing else
    planted.chmod(0o755)
    monkeypatch.setattr(sys, "executable", _bare_python(tmp_path))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", ".")
    monkeypatch.chdir(repo)
    v.verify_repo(repo)
    assert not marker.exists()
