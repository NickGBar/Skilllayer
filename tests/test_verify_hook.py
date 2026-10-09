"""Stop / UserPromptSubmit hook behavior for ``skilllayer verify``."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from skilllayer import verify as v
from skilllayer import verify_hook as h
from skilllayer.cli import main as cli_main

_TEST_SRC = "from src.auth import login\n\n\ndef test_login():\n    assert login() == 'old'\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, *, with_tests: bool = True, policy: str | None = None) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src/auth.py").write_text("def login():\n    return 'old'\n")
    if with_tests:
        (repo / "tests").mkdir()
        (repo / "tests/test_auth.py").write_text(_TEST_SRC)
        (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    if policy is not None:
        (repo / ".skilllayer-policy.yml").write_text(policy)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLLAYER_VERIFY_DIR", str(tmp_path / "vdata"))


def _payload(repo: Path, **extra) -> dict:
    return {"session_id": "sess-1", "cwd": str(repo), **extra}


def _break(repo: Path) -> None:
    (repo / "src/auth.py").write_text("def login():\n    return 'new'\n")


def _fix(repo: Path) -> None:
    (repo / "src/auth.py").write_text("def login():\n    return 'old'  # fixed\n")


def _reason(outcome: h.HookOutcome) -> str | None:
    """The message the agent is sent back to work with, or None when the stop was allowed."""
    if outcome.stdout:
        data = json.loads(outcome.stdout)
        if data.get("decision") == "block":
            return data["reason"]
    return None


def test_prompt_hook_records_baseline_and_prints_nothing(tmp_path):
    repo = _repo(tmp_path)
    outcome = h.handle_prompt_submit(_payload(repo))
    assert (outcome.exit_code, outcome.stdout, outcome.stderr) == (0, "", "")
    baseline = h.load_state(repo, "sess-1")["baseline"]
    assert baseline["head"] == _git(repo, "rev-parse", "HEAD").strip()


def test_stop_blocks_failing_tests_and_returns_observed_facts_to_the_agent(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(_payload(repo))
    reason = _reason(outcome)
    assert outcome.exit_code == 0 and reason is not None
    assert "Tests failing (observed by running them)" in reason
    assert "test_login" in reason
    assert "Blocked attempt 1 of 2" in reason


def test_stop_allows_once_the_agent_fixes_it_and_stats_show_the_recovery(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    assert _reason(h.handle_stop(_payload(repo))) is not None
    _fix(repo)
    outcome = h.handle_stop(_payload(repo, stop_hook_active=True))
    assert (outcome.exit_code, outcome.stdout) == (0, "")
    stats = v.compute_stats(v.read_events(repo))
    assert stats["blocked"] == 1 and stats["recovered_after_block"] == 1 and stats["verified"] == 1


def test_stop_does_nothing_when_nothing_changed_this_turn(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    outcome = h.handle_stop(_payload(repo))
    assert (outcome.exit_code, outcome.stdout, outcome.stderr) == (0, "", "")
    assert v.read_events(repo) == []  # no tests were run, nothing recorded


def test_an_already_verified_state_is_not_rerun(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _fix(repo)
    assert h.handle_stop(_payload(repo)).exit_code == 0
    assert h.handle_stop(_payload(repo)).exit_code == 0
    assert len(v.read_events(repo)) == 1


def test_a_committed_protected_path_edit_is_still_flagged_by_the_hook(tmp_path):
    """Tests pass and the tree is clean, but the agent committed an edit to a protected
    path during the turn — only measuring from the baseline HEAD can see that."""
    repo = _repo(tmp_path, policy="version: 1\nprotected_paths:\n  - migrations/\n")
    (repo / "migrations").mkdir()
    (repo / "migrations/keep.sql").write_text("-- seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    h.handle_prompt_submit(_payload(repo))
    (repo / "migrations/keep.sql").write_text("drop table users;")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "agent: tweak migration")
    assert v.take_snapshot(repo).dirty_paths == []
    outcome = h.handle_stop(_payload(repo))
    assert "Protected path modified: migrations/keep.sql" in (_reason(outcome) or "")


def test_a_commit_during_the_turn_still_triggers_verification(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "agent: fix login")
    assert v.take_snapshot(repo).dirty_paths == []
    outcome = h.handle_stop(_payload(repo))
    assert "Tests failing" in (_reason(outcome) or "")


def test_loop_guard_lets_the_agent_stop_after_the_block_budget_and_says_so(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    assert _reason(h.handle_stop(_payload(repo))) is not None
    assert _reason(h.handle_stop(_payload(repo, stop_hook_active=True))) is not None
    third = h.handle_stop(_payload(repo, stop_hook_active=True))
    assert _reason(third) is None
    assert "UNVERIFIED" in json.loads(third.stdout)["systemMessage"]
    assert v.compute_stats(v.read_events(repo))["loop_guard_allowed"] == 1


def test_stop_hook_active_without_working_bookkeeping_never_blocks_twice(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _break(repo)
    monkeypatch.setattr(h, "save_state", lambda *a, **k: False)
    outcome = h.handle_stop(_payload(repo, stop_hook_active=True))
    assert outcome.exit_code == 0 and "UNVERIFIED" in json.loads(outcome.stdout)["systemMessage"]


def test_policy_comes_from_the_committed_version_not_the_working_tree(tmp_path):
    """The agent removes the protection in the working tree and edits the protected file.
    The committed policy still applies, and touching the policy is itself a violation."""
    repo = _repo(tmp_path, policy="version: 1\nprotected_paths:\n  - migrations/\n")
    (repo / "migrations").mkdir()
    (repo / "migrations/keep.sql").write_text("-- seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    h.handle_prompt_submit(_payload(repo))
    (repo / ".skilllayer-policy.yml").write_text("version: 1\nverify:\n  mode: warn\n")
    (repo / "migrations/keep.sql").write_text("drop table users;")
    reason = _reason(h.handle_stop(_payload(repo))) or ""
    assert "migrations/keep.sql" in reason and ".skilllayer-policy.yml" in reason


def test_invalid_committed_policy_falls_back_to_strict_defaults_and_is_reported(tmp_path):
    repo = _repo(tmp_path, policy="version: 1\nverify:\n  mode: off\n")
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(_payload(repo))
    assert _reason(outcome) is not None  # strict defaults, not silently disabled
    assert any(x.startswith("policy_ignored") for x in json.loads(next(iter(sorted((v.data_dir(repo) / "receipts").iterdir()))).read_text())["limitations"])


def test_warn_mode_reports_but_never_blocks(tmp_path):
    repo = _repo(tmp_path, policy="version: 1\nverify:\n  mode: warn\n")
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(_payload(repo))
    assert outcome.exit_code == 0
    assert "warn mode" in json.loads(outcome.stdout)["systemMessage"]


def test_no_tests_is_allowed_but_the_user_is_told_it_is_unverified(tmp_path):
    repo = _repo(tmp_path, with_tests=False)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(_payload(repo))
    assert outcome.exit_code == 0
    assert "UNVERIFIED_NO_TESTS" in json.loads(outcome.stdout)["systemMessage"]


def test_internal_errors_are_visible_never_a_silent_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(h, "verify_repo", boom)
    outcome = h.handle_stop(_payload(repo))
    assert outcome.exit_code == 0  # the harness shows a systemMessage inline; exit 1 hides the detail
    message = json.loads(outcome.stdout)["systemMessage"]
    assert "NOT verified" in message and "disk on fire" in message


def test_outside_a_git_repository_the_stop_hook_is_a_silent_allow(tmp_path):
    outcome = h.handle_stop({"session_id": "x", "cwd": str(tmp_path)})
    assert (outcome.exit_code, outcome.stdout, outcome.stderr) == (0, "", "")


def test_cli_hook_mode_reads_stdin_and_uses_the_exit_code_protocol(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))
    assert cli_main(["verify", "--hook", "prompt"]) == 0
    _break(repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))
    assert cli_main(["verify", "--hook", "stop"]) == 0  # the block travels as a JSON decision on stdout
    assert "Tests failing" in json.loads(capsys.readouterr().out)["reason"]


def test_cli_one_shot_exit_codes_and_json(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli_main(["verify", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "VERIFIED"
    _break(repo)
    assert cli_main(["verify", "--repo", str(repo), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "TESTS_FAILING"
    assert cli_main(["verify", "--repo", str(repo), "--mode", "warn", "--json"]) == 0
    capsys.readouterr()
    bare = _repo(tmp_path / "other", with_tests=False)
    assert cli_main(["verify", "--repo", str(bare)]) == 3  # unverified is its own exit code


def test_cli_stats_and_record(tmp_path, capsys):
    repo = _repo(tmp_path)
    _break(repo)
    assert cli_main(["verify", "--repo", str(repo), "--record", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["written_paths"]
    assert cli_main(["verify", "--repo", str(repo), "--stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["blocked"] == 1


def test_cli_verify_emits_no_telemetry_banner_into_the_agents_message(tmp_path, monkeypatch, capsys):
    """The hook's output is fed to the model verbatim; nothing else may leak into it."""
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))
    cli_main(["verify", "--hook", "stop"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["reason"].startswith("skilllayer: this work cannot be accepted as complete")
    assert "telemetry" not in captured.out.lower()


def test_the_hook_ends_a_slow_test_run_before_the_harness_would_kill_it(tmp_path):
    """The harness treats a hook that outlives its timeout as 'no decision' and lets the
    stop through silently. Clamping the run to the hook's budget makes the same situation a
    visible UNVERIFIED_TIMEOUT instead."""
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(
        _payload(repo), test_command=["python3", "-c", "import time; time.sleep(30)"], max_seconds=1,
    )
    assert outcome.exit_code == 0
    assert "UNVERIFIED_TIMEOUT" in json.loads(outcome.stdout)["systemMessage"]
    receipt = json.loads(next(iter(sorted((v.data_dir(repo) / "receipts").iterdir()))).read_text())
    assert "test_timeout_clamped_to_hook_budget" in receipt["limitations"]


def test_in_warning_mode_weakened_tests_are_allowed_but_the_user_is_told(tmp_path):
    """The agent 'fixes' a failing test by rewriting it. With block_on_weakened_tests off nothing
    is blocked — the tests do pass — but the one person who can judge whether that was
    legitimate must hear about it. (By default the stop is blocked: see the tests below.)"""
    repo = _repo(tmp_path, policy="version: 1\nverify:\n  block_on_weakened_tests: false\n")
    h.handle_prompt_submit(_payload(repo))
    (repo / "tests/test_auth.py").write_text("def test_login():\n    assert True\n")
    outcome = h.handle_stop(_payload(repo))
    assert outcome.exit_code == 0
    message = json.loads(outcome.stdout)["systemMessage"]
    assert "tests passed, but" in message and "tests/test_auth.py" in message


def test_an_untouched_test_suite_produces_no_noise(tmp_path):
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _fix(repo)
    outcome = h.handle_stop(_payload(repo))
    assert (outcome.exit_code, outcome.stdout, outcome.stderr) == (0, "", "")


def test_a_block_shows_the_human_why_and_gives_the_agent_the_facts(tmp_path):
    """The harness shows a generic "Stop hook error" for a block; the systemMessage is the only
    place the person watching learns what the agent was sent back to fix."""
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    data = json.loads(h.handle_stop(_payload(repo)).stdout)
    assert data["decision"] == "block"
    assert "sent the agent back to work (attempt 1 of 2)" in data["systemMessage"]
    assert "tests failing" in data["systemMessage"] and "test_login" in data["systemMessage"]
    assert "Tests failing (observed by running them)" in data["reason"]  # the agent gets the detail


def test_the_exit_code_protocol_remains_available_for_older_harnesses(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLLAYER_BLOCK_STYLE", "exit2")
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    outcome = h.handle_stop(_payload(repo))
    assert outcome.exit_code == 2 and outcome.stdout == ""
    assert "Tests failing (observed by running them)" in outcome.stderr


def test_a_protected_path_block_says_revert_first(tmp_path):
    """Asking the user means stopping, and a stop with the protected file still modified is
    blocked again — so the instruction is to revert, then mention it in the final message."""
    repo = _repo(tmp_path, policy="version: 1\nprotected_paths:\n  - migrations/\n")
    h.handle_prompt_submit(_payload(repo))
    (repo / "migrations").mkdir()
    (repo / "migrations/002.sql").write_text("drop table users;")
    reason = _reason(h.handle_stop(_payload(repo))) or ""
    assert "Protected path modified: migrations/002.sql — revert it." in reason
    assert "only the user can approve it" in reason
    assert "ask the user to approve" not in reason


def test_an_agent_that_deletes_the_failing_assertion_is_sent_back(tmp_path):
    """The tests now pass because the check is gone; the Stop is blocked anyway."""
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    (repo / "tests/test_auth.py").write_text("from src.auth import login\n\n\ndef test_login():\n    login()\n")
    outcome = h.handle_stop(_payload(repo))
    reason = _reason(outcome)
    assert reason is not None
    assert "Tests were weakened this turn" in reason
    assert "fewer assertions by 1 — tests/test_auth.py:5" in reason
    assert "tests weakened" in json.loads(outcome.stdout)["systemMessage"]


def test_deleting_every_test_is_not_a_way_out(tmp_path):
    """No tests found would be UNVERIFIED, which lets the stop through; the deletion blocks it."""
    repo = _repo(tmp_path)
    h.handle_prompt_submit(_payload(repo))
    _break(repo)
    (repo / "tests/test_auth.py").unlink()
    reason = _reason(h.handle_stop(_payload(repo)))
    assert reason is not None and "test file deleted — tests/test_auth.py" in reason


def test_policy_can_make_weakened_tests_a_warning(tmp_path):
    repo = _repo(tmp_path, policy="version: 1\nverify:\n  block_on_weakened_tests: false\n")
    h.handle_prompt_submit(_payload(repo))
    (repo / "tests/test_auth.py").write_text("import pytest\nfrom src.auth import login\n\n\n@pytest.mark.skip(reason='later')\ndef test_login():\n    assert login() == 'old'\n")
    outcome = h.handle_stop(_payload(repo))
    assert _reason(outcome) is None
    assert "tests were weakened this turn: skip/xfail added" in json.loads(outcome.stdout)["systemMessage"]
