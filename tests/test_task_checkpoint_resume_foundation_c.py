"""Focused Foundation C regression and adversarial tests (internal only)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skilllayer.tasks.baseline import capture_repository_baseline
from skilllayer.tasks.checkpoint import (
    acquire_task_ownership,
    build_checkpoint,
    read_task_ownership,
    write_checkpoint_history,
)
from skilllayer.tasks.persistence import (
    create_task_contract,
    generate_task_id,
    grant_task_consent,
    task_record_paths,
    write_task_baseline,
)
from skilllayer.tasks.resume import assess_resume, load_checkpoint_chain


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"; repo.mkdir(); _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid"); _git(repo, "config", "user.name", "SkillLayer Test")
    (repo / ".gitignore").write_text(".skilllayer/\n", encoding="utf-8")
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "initial")
    return repo


def _digest_path(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _evidence(rel: str, path: Path, kind: str) -> dict[str, object]:
    return {"evidence_type": kind, "record_path": rel, "schema_version": 1, "integrity_fingerprint": _digest_path(path), "required": True, "observed_status": "PERSISTED"}


def _setup(tmp_path: Path):
    repo = _repo(tmp_path); task_id = generate_task_id(repo, "resume")
    consent = grant_task_consent(repo, task_id)
    assert create_task_contract(repo, task_id, consent=consent, objective_label="resume", allowed_paths=["src/app.py", "tests/test_app.py"])["success"]
    contract = json.loads((repo / ".skilllayer/tasks" / task_id / "contract.json").read_text())
    baseline = capture_repository_baseline(repo, task_id, contract)
    assert write_task_baseline(repo, task_id, consent=consent, baseline=baseline)["success"]
    paths = task_record_paths(repo, task_id)
    refs = [_evidence("contract.json", paths.contract_path, "contract"), _evidence("baseline.json", paths.baseline_path, "baseline")]
    return repo, task_id, consent, refs


def _checkpoint(task_id: str, refs: list[dict[str, object]], sequence: int = 1, previous: str | None = None, *, checkpoint_id: str | None = None, status: str = "IN_PROGRESS", expected: list[str] | None = None, validation: bool = False):
    completed = []
    if sequence > 1:
        completed = [{"step_id": "implementation", "status": "COMPLETED", "action_type": "INSPECT_DIFF", "evidence_refs": refs[:1]}]
    validations = []
    if validation:
        validations = [{"validation_id": "tests", "validation_kind": "pytest", "target": "tests", "started_at": "2026-01-01T00:00:00Z", "finished_at": None, "started_successfully": True, "exit_code": None, "passed": False, "tests_collected": 0, "tests_passed": 0, "tests_failed": 0, "tests_skipped": 0, "evidence_complete": False, "limitations": ["interrupted"]}]
    return build_checkpoint(task_id=task_id, sequence=sequence, previous_checkpoint_id=previous, checkpoint_id=checkpoint_id, task_phase="implementation", task_status=status, completed_steps=completed, remaining_steps=[{"step_id": "validate", "status": "PENDING", "action_type": "RUN_TEST", "evidence_refs": []}], evidence_refs=refs, commands_attempted=[], validations_attempted=validations, files_observed=["src/app.py"], files_expected_next=expected or ["src/app.py"], unresolved_questions=[], known_failures=[], limitations=[], resume_requirements=[])


def test_checkpoint_history_is_immutable_and_resume_safe(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    first = _checkpoint(task_id, refs)
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=first)["success"]
    second = _checkpoint(task_id, refs, sequence=2, previous=first["checkpoint_id"])
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=second)["success"]
    chain = load_checkpoint_chain(repo, task_id)
    assert chain["status"] == "CHAIN_VALID" and chain["latest"]["checkpoint_id"] == second["checkpoint_id"]
    assessment = assess_resume(repo, task_id)
    assert assessment["resume_verdict"] == "RESUME_SAFE"
    assert assessment["evidence_complete"] is True


def test_interrupted_validation_requires_confirmation_not_false_success(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    cp = _checkpoint(task_id, refs, status="INTERRUPTED", validation=True)
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)["success"]
    result = assess_resume(repo, task_id)
    assert result["resume_verdict"] == "RESUME_SAFE_WITH_CONFIRMATION"
    assert result["required_user_actions"] == ["resume_confirmation"]
    assert any(step["action_type"] == "RUN_TEST" for step in result["safe_next_steps"])


def test_changed_head_is_stale_and_forbidden_change_blocks(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=_checkpoint(task_id, refs))["success"]
    (repo / ".env").write_text("SYNTHETIC=only\n", encoding="utf-8")
    assert assess_resume(repo, task_id)["resume_verdict"] == "RESUME_BLOCKED"
    (repo / ".env").unlink(); (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "src/app.py"); _git(repo, "commit", "-m", "external")
    assert assess_resume(repo, task_id)["resume_verdict"] == "CHECKPOINT_STALE"


def test_corrupt_chain_and_evidence_are_never_resumable(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    cp = _checkpoint(task_id, refs); assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)["success"]
    history = task_record_paths(repo, task_id).checkpoints_dir
    record = json.loads(next(history.glob("*.json")).read_text()); record["task_phase"] = "tampered"
    next(history.glob("*.json")).write_text(json.dumps(record), encoding="utf-8")
    assert assess_resume(repo, task_id)["resume_verdict"] == "CHECKPOINT_CORRUPT"


def test_history_rejects_sequence_conflict_and_is_idempotent(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path); cp = _checkpoint(task_id, refs)
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)["success"]
    assert write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)["written_paths"] == []
    conflict = _checkpoint(task_id, refs, checkpoint_id=None)
    result = write_checkpoint_history(repo, task_id, consent=consent, checkpoint=conflict)
    assert result["state"] == "WRITE_REJECTED" and result["error"] == "checkpoint_sequence_conflict"


def test_active_owner_blocks_second_writer_and_expired_lease_is_reclaimable(tmp_path: Path) -> None:
    repo, task_id, consent, _refs = _setup(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert acquire_task_ownership(repo, task_id, consent=consent, owner_instance_id="writer-one", now=now)["success"]
    assert acquire_task_ownership(repo, task_id, consent=consent, owner_instance_id="writer-two", now=now)["state"] == "PERSISTENCE_BLOCKED"
    later = now + timedelta(seconds=121)
    assert read_task_ownership(repo, task_id, now=later)["ownership_status"] == "EXPIRED"
    assert acquire_task_ownership(repo, task_id, consent=consent, owner_instance_id="writer-two", now=later)["success"]


def test_checkpoint_needs_explicit_consent_and_never_writes_outside_task(tmp_path: Path) -> None:
    repo, task_id, _consent, refs = _setup(tmp_path); cp = _checkpoint(task_id, refs)
    denied = write_checkpoint_history(repo, task_id, consent=None, checkpoint=cp)
    assert denied["state"] == "CONSENT_REQUIRED"
    assert not task_record_paths(repo, task_id).checkpoints_dir.exists()


def test_legacy_pointer_requires_confirmation_not_silent_upgrade(tmp_path: Path) -> None:
    repo, task_id, consent, _refs = _setup(tmp_path)
    pointer = task_record_paths(repo, task_id).checkpoint_path
    pointer.write_text(json.dumps({"schema_version": 1, "task_id": task_id, "checkpoint_version": 1}), encoding="utf-8")
    result = assess_resume(repo, task_id)
    assert result["resume_verdict"] == "RESUME_SAFE_WITH_CONFIRMATION"
    assert "legacy_checkpoint_requires_confirmation" in result["resume_reasons"]


def test_escaping_evidence_reference_is_rejected_before_write(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    refs[0] = {**refs[0], "record_path": "../contract.json"}
    cp = _checkpoint(task_id, refs)
    rejected = write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)
    assert rejected["state"] == "WRITE_REJECTED"
    assert rejected["error"] == "evidence_refs_invalid"


def test_completed_step_without_evidence_is_rejected_before_write(tmp_path: Path) -> None:
    repo, task_id, consent, refs = _setup(tmp_path)
    cp = _checkpoint(task_id, refs, sequence=2, previous="cp-123456789abc")
    cp["completed_steps"][0]["evidence_refs"] = []
    from skilllayer.tasks.checkpoint import _canonical_digest
    cp["integrity_fingerprint"] = _canonical_digest(cp)
    rejected = write_checkpoint_history(repo, task_id, consent=consent, checkpoint=cp)
    assert rejected["state"] == "WRITE_REJECTED"
    assert rejected["error"] == "completed_step_missing_evidence"
