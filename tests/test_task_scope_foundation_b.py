"""Regression tests for VTE Foundation B's internal repository evidence APIs."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skilllayer.tasks import baseline as baseline_module
from skilllayer.tasks.baseline import capture_repository_baseline, read_repository_state
from skilllayer.tasks.persistence import (
    append_scope_amendment,
    create_task_contract,
    generate_task_id,
    grant_task_consent,
    read_scope_amendments,
    read_task_baseline,
    task_record_paths,
    write_task_baseline,
    write_task_result,
)
from skilllayer.tasks.scope import classify_path, validate_scope, validate_scope_contract


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "SkillLayer Test")
    (repo / ".gitignore").write_text(".skilllayer/\n.venv/\n", encoding="utf-8")
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _contract(repo: Path, task_id: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "task_id": task_id, "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "forbidden_paths": [".env", ".git/"], "scope_mode": "EXPLICIT",
        "max_changed_files": 2, "allow_new_files": True, "allow_deleted_files": True,
        "allow_test_files": True, "allowed_generated_paths": [],
    }
    value.update(overrides)
    return value


class TestScopeContract:
    @pytest.mark.parametrize("path", ["/tmp/x", "../x", "a/../../x", "C:\\Users\\x"])
    def test_rejects_absolute_and_traversal_scope_paths(self, tmp_path: Path, path: str) -> None:
        checked = validate_scope_contract(_contract(tmp_path, "task", allowed_paths=[path]), task_id="task")
        assert not checked["valid"]

    def test_forbidden_overrides_allowed_prefix(self, tmp_path: Path) -> None:
        contract = _contract(tmp_path, "task", allowed_paths=["src/"], forbidden_paths=["src/secrets.py"], scope_mode="PREFIX")
        checked = validate_scope_contract(contract, task_id="task")
        assert checked["valid"]
        assert classify_path("src/secrets.py", checked["normalized"], task_id="task") == "forbidden"
        assert classify_path("src/module.py", checked["normalized"], task_id="task") == "allowed"

    def test_candidate_mode_is_not_automatically_permitted(self, tmp_path: Path) -> None:
        assert not validate_scope_contract(_contract(tmp_path, "task", scope_mode="CANDIDATE"), task_id="task")["valid"]

    def test_current_task_state_only_is_ignored(self, tmp_path: Path) -> None:
        rules = validate_scope_contract(_contract(tmp_path, "task"), task_id="task")["normalized"]
        assert classify_path(".skilllayer/tasks/task/baseline.json", rules, task_id="task") == "ignored_internal"
        assert classify_path(".skilllayer/tasks/other/baseline.json", rules, task_id="task") == "forbidden"


class TestBaselineAndValidation:
    def test_capture_is_read_only_and_has_bounded_facts(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id))
        after = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
        assert before == after == ""
        assert baseline["baseline_status"] == "BASELINE_CAPTURED"
        assert baseline["git_head"] and "remote" not in baseline
        assert all("path" in item and "sha256" in item for item in baseline["relevant_file_fingerprints"])

    def test_env_and_virtual_environment_are_not_read_or_fingerprinted(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        (repo / ".env").write_text("KEY=synthetic-only\n", encoding="utf-8")
        (repo / ".venv").mkdir(); (repo / ".venv" / "secret.txt").write_text("synthetic-only\n", encoding="utf-8")
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id, allowed_paths=[".env", ".venv/secret.txt"]))
        persisted = json.dumps(baseline)
        assert "synthetic-only" not in persisted
        assert ".env" not in [item["path"] for item in baseline["relevant_file_fingerprints"]]
        assert all(not item["path"].startswith(".venv/") for item in baseline["relevant_file_fingerprints"])

    def test_allowed_change_is_scope_clean(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id)
        baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "SCOPE_CLEAN"
        assert result["freshness_class"] == "EXPECTED_TASK_CHANGES"

    def test_unrelated_and_forbidden_change_violate_without_attribution(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id); baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "README.md").write_text("user concurrently changed this\n", encoding="utf-8")
        (repo / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "SCOPE_VIOLATED"
        assert ".env" in result["forbidden_changes"]
        assert "README.md" in result["unexpected_changes"]
        assert "agent" not in json.dumps(result).lower()

    def test_head_change_is_baseline_stale(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id); baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(repo, "add", "src/app.py"); _git(repo, "commit", "-m", "other actor commit")
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "BASELINE_STALE"

    def test_rename_evaluates_both_sides(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id, scope_mode="PREFIX", allowed_paths=["src/"])
        baseline = capture_repository_baseline(repo, task_id, contract)
        _git(repo, "mv", "src/app.py", ".env")
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "SCOPE_VIOLATED"

    def test_changed_count_and_new_file_rules(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id, max_changed_files=1, allow_new_files=False)
        baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (repo / "tests" / "new_test.py").write_text("pass\n", encoding="utf-8")
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "SCOPE_VIOLATED"
        assert "max_changed_files_exceeded" in result["violations"]
        assert any(item.startswith("new_file_not_allowed") for item in result["violations"])

    def test_staged_delete_and_untracked_files_are_factual(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id, allow_deleted_files=False)
        baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "src" / "app.py").unlink(); _git(repo, "add", "-u")
        (repo / "tests" / "scratch.py").write_text("pass\n", encoding="utf-8")
        state = read_repository_state(repo, task_id=task_id)
        assert "src/app.py" in state["staged_paths"]
        assert "src/app.py" in state["deleted_paths"]
        assert "tests/scratch.py" in state["untracked_paths"]
        assert validate_scope(contract, baseline, state)["verdict"] == "SCOPE_VIOLATED"

    def test_staged_added_file_is_new_file_evidence(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        contract = _contract(repo, task_id, allow_new_files=False)
        baseline = capture_repository_baseline(repo, task_id, contract)
        (repo / "tests" / "new_test.py").write_text("pass\n", encoding="utf-8"); _git(repo, "add", "tests/new_test.py")
        state = read_repository_state(repo, task_id=task_id)
        assert "tests/new_test.py" in state["added_paths"]
        assert any(item.startswith("new_file_not_allowed") for item in validate_scope(contract, baseline, state)["violations"])

    def test_dirty_baseline_is_external_drift_not_clean_validation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        (repo / "src" / "app.py").write_text("VALUE = preexisting\n", encoding="utf-8")
        contract = _contract(repo, task_id)
        baseline = capture_repository_baseline(repo, task_id, contract)
        result = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        assert result["verdict"] == "SCOPE_INCOMPLETE"
        assert result["freshness_class"] == "EXTERNAL_DRIFT_POSSIBLE"

    def test_git_unavailable_is_incomplete_not_clean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        monkeypatch.setattr(baseline_module, "_run_git", lambda *_args, **_kwargs: None)
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id))
        assert baseline["baseline_status"] == "BASELINE_UNAVAILABLE"
        result = validate_scope(_contract(repo, task_id), baseline, {"collection_status": "STATE_UNAVAILABLE"})
        assert result["verdict"] == "SCOPE_INCOMPLETE"

    def test_file_size_limit_is_explicit_incomplete_evidence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        (repo / "src" / "app.py").write_bytes(b"x" * 32)
        monkeypatch.setattr(baseline_module, "MAX_FINGERPRINT_FILE_BYTES", 8)
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id))
        assert baseline["baseline_status"] == "BASELINE_INCOMPLETE"
        assert any("file_too_large" in item for item in baseline["limitations"])

    def test_symlinked_file_is_not_fingerprinted_or_followed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); outside = tmp_path / "outside.py"; outside.write_text("secret = 'synthetic'\n", encoding="utf-8")
        (repo / "src" / "linked.py").symlink_to(outside)
        task_id = generate_task_id(repo, "scope")
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id, allowed_paths=["src/linked.py"]))
        assert all(item["path"] != "src/linked.py" for item in baseline["relevant_file_fingerprints"])


class TestFoundationAPersistenceIntegration:
    def test_baseline_requires_consent_then_writes_only_baseline(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        consent = grant_task_consent(repo, task_id)
        created = create_task_contract(repo, task_id, consent=consent, objective_label="scope", allowed_paths=["src/app.py"])
        assert created["success"]
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id))
        denied = write_task_baseline(repo, task_id, consent=None, baseline=baseline)
        assert denied["state"] == "CONSENT_REQUIRED"
        assert not task_record_paths(repo, task_id).baseline_path.exists()
        written = write_task_baseline(repo, task_id, consent=consent, baseline=baseline)
        assert written["success"] and written["written_paths"] == [f".skilllayer/tasks/{task_id}/baseline.json"]
        assert read_task_baseline(repo, task_id)["success"]

    def test_approved_amendment_is_append_only_and_cannot_remove_forbidden(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        consent = grant_task_consent(repo, task_id)
        create_task_contract(repo, task_id, consent=consent, objective_label="scope", allowed_paths=["src/app.py"])
        amendment = {"amendment_id": "a-1", "approved_at": "2026-07-23T00:00:00Z", "added_allowed_paths": ["tests/test_app.py"], "added_generated_paths": [], "reason_label": "add-test", "consent_reference": "user-confirmed"}
        assert append_scope_amendment(repo, task_id, consent=consent, amendment=amendment)["success"]
        record = read_scope_amendments(repo, task_id)
        assert record["record"]["amendments"] == [amendment]
        bad = {**amendment, "amendment_id": "a-2", "added_allowed_paths": [".git/config"]}
        assert append_scope_amendment(repo, task_id, consent=consent, amendment=bad)["state"] == "WRITE_REJECTED"

    def test_unauthorized_amendment_creates_no_record(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        consent = grant_task_consent(repo, task_id, declared_record_types=frozenset({"contract"}))
        create_task_contract(repo, task_id, consent=consent, objective_label="scope", allowed_paths=["src/app.py"])
        amendment = {"amendment_id": "a-1", "approved_at": "2026-07-23T00:00:00Z", "added_allowed_paths": ["tests/test_app.py"], "added_generated_paths": [], "reason_label": "add-test", "consent_reference": "user-confirmed"}
        result = append_scope_amendment(repo, task_id, consent=consent, amendment=amendment)
        assert result["state"] == "CONSENT_INVALID"
        assert not task_record_paths(repo, task_id).amendments_path.exists()

    def test_scope_section_persists_in_result(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        consent = grant_task_consent(repo, task_id)
        create_task_contract(repo, task_id, consent=consent, objective_label="scope", allowed_paths=["src/app.py"])
        contract = _contract(repo, task_id); baseline = capture_repository_baseline(repo, task_id, contract)
        scope = validate_scope(contract, baseline, read_repository_state(repo, task_id=task_id))
        result = write_task_result(repo, task_id, consent=consent, verdict="CHANGE_INCOMPLETE", written_by_component="scope_validator", scope_validation=scope)
        assert result["success"]

    def test_baseline_is_readable_cross_process(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path); task_id = generate_task_id(repo, "scope")
        consent = grant_task_consent(repo, task_id)
        create_task_contract(repo, task_id, consent=consent, objective_label="scope", allowed_paths=["src/app.py"])
        baseline = capture_repository_baseline(repo, task_id, _contract(repo, task_id))
        assert write_task_baseline(repo, task_id, consent=consent, baseline=baseline)["success"]
        code = (
            "from pathlib import Path; from skilllayer.tasks.persistence import read_task_baseline; "
            f"print(read_task_baseline(Path({str(repo)!r}), {task_id!r})['state'])"
        )
        completed = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, check=True)
        assert completed.stdout.strip() == "READ_COMPLETED"
