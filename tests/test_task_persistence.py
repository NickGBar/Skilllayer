"""Tests for the Verified Task Execution persistence foundation
(skilllayer.tasks.persistence) — VTE Foundation A.

Covers: task_id generation/validation (incl. adversarial shapes), path and
symlink confinement, consent enforcement, the write-once contract, atomic
result/checkpoint updates (incl. concurrency and monotonic checkpoint
versions), the redaction/rejection gate (incl. nested structures), recovery
safety on malformed/adversarial on-disk state, no-hidden-writes, and
cross-process reads. This module is internal (not routed, not an MCP tool);
these tests exercise it directly.
"""
from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skilllayer.tasks.persistence import (
    CONSTRAINT_KINDS,
    TASK_STATUSES,
    FieldPolicy,
    SanitizationResult,
    TaskIdError,
    TaskPathSafetyError,
    create_task_contract,
    generate_task_id,
    grant_task_consent,
    is_valid_task_id,
    read_task_checkpoint,
    read_task_contract,
    read_task_result,
    sanitize_persisted_value,
    task_record_paths,
    validate_task_id,
    write_task_checkpoint,
    write_task_result,
)


# ---------------------------------------------------------------------------
# Task ID generation and validation
# ---------------------------------------------------------------------------

class TestTaskIdGeneration:
    def test_shape(self, tmp_path):
        tid = generate_task_id(tmp_path, "Fix login timeout")
        assert is_valid_task_id(tid)
        assert tid.startswith("2")  # UTC timestamp prefix
        assert "fix-login-timeout" in tid

    def test_no_hint_falls_back_to_task(self, tmp_path):
        tid = generate_task_id(tmp_path, None)
        assert "-task-" in tid

    def test_collision_check_never_reuses_existing_directory(self, tmp_path, monkeypatch):
        import skilllayer.tasks.persistence as mod

        real_datetime = mod.datetime  # capture before patching, or strptime below breaks
        first = generate_task_id(tmp_path, "same objective")
        (tmp_path / ".skilllayer" / "tasks" / first).mkdir(parents=True)

        # Force the same timestamp+slug so only the random suffix can differ,
        # proving collision detection (not mere luck) prevented reuse.
        frozen_dt = real_datetime.strptime(first.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=mod.timezone.utc)

        class _FrozenDatetime:
            @staticmethod
            def now(tz):
                return frozen_dt

        monkeypatch.setattr(mod, "datetime", _FrozenDatetime)
        second = generate_task_id(tmp_path, "same objective")
        assert second != first
        assert not (tmp_path / ".skilllayer" / "tasks" / second).exists()

    def test_secret_in_objective_hint_never_reaches_task_id(self, tmp_path):
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
        tid = generate_task_id(tmp_path, f"rotate the leaked key {secret}")
        assert secret not in tid
        assert "sk-ant" not in tid
        # Falls back to the generic slug rather than a partially-scrubbed one.
        assert "-task-" in tid


class TestTaskIdValidationAdversarial:
    """Phase 7: adversarial task_id / path cases."""

    @pytest.mark.parametrize("bad_id", [
        "../../etc/passwd",
        "20260723T084552Z-fix-../../etc-abc123",
        "/etc/passwd",
        "20260723T084552Z-fix%2e%2e%2f-abc123",  # encoded traversal, never decoded
        "20260723T084552Z-FIX-abc123",  # uppercase -> case-collision structurally impossible
        "a" * 200,  # excessively long
        "20260723T084552Z-fix login-abc123",  # U+2028 line separator
        "20260723T084552Z-fix‮evil-abc123",  # right-to-left override
        "20260723T084552Z-fix-abc123\x00",  # embedded null byte
        "",
        "20260723T084552Z--abc123",  # empty slug segment
    ])
    def test_rejected(self, bad_id):
        assert not is_valid_task_id(bad_id)
        with pytest.raises(TaskIdError):
            validate_task_id(bad_id)

    def test_valid_generated_id_passes(self, tmp_path):
        tid = generate_task_id(tmp_path, "ok")
        validate_task_id(tid)  # must not raise


# ---------------------------------------------------------------------------
# Path confinement and symlink safety (Phase 7)
# ---------------------------------------------------------------------------

class TestPathConfinement:
    def test_normal_paths_are_confined_under_tasks_root(self, tmp_path):
        tid = generate_task_id(tmp_path, "normal")
        paths = task_record_paths(tmp_path, tid)
        assert paths.task_dir.parent == paths.tasks_root
        assert str(paths.contract_path).startswith(str(tmp_path.resolve()))

    def test_tasks_dir_symlink_outside_project_is_rejected(self, tmp_path):
        project = tmp_path / "project"
        (project / ".skilllayer").mkdir(parents=True)
        external = tmp_path / "external"
        external.mkdir()
        (project / ".skilllayer" / "tasks").symlink_to(external, target_is_directory=True)

        tid = "20260723T084552Z-x-abc123"
        with pytest.raises(TaskPathSafetyError) as excinfo:
            task_record_paths(project, tid)
        assert excinfo.value.reason == "symlink_escape"

    def test_task_dir_symlink_to_tmp_is_rejected(self, tmp_path):
        project = tmp_path / "project"
        (project / ".skilllayer" / "tasks").mkdir(parents=True)
        external = tmp_path / "external"
        external.mkdir()
        tid = "20260723T084552Z-x-abc123"
        (project / ".skilllayer" / "tasks" / tid).symlink_to(external, target_is_directory=True)

        with pytest.raises(TaskPathSafetyError) as excinfo:
            task_record_paths(project, tid)
        assert excinfo.value.reason == "symlink_escape"

    def test_skilllayer_dir_itself_symlinked_is_rejected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        external = tmp_path / "external_skilllayer"
        external.mkdir()
        (project / ".skilllayer").symlink_to(external, target_is_directory=True)

        tid = "20260723T084552Z-x-abc123"
        with pytest.raises(TaskPathSafetyError) as excinfo:
            task_record_paths(project, tid)
        assert excinfo.value.reason == "symlink_escape"

    def test_project_root_itself_symlinked_resolves_to_real_target(self, tmp_path):
        # A symlinked project root (e.g. a symlinked checkout) is a normal,
        # legitimate setup — unlike an internal ancestor being redirected,
        # this must resolve cleanly to the real underlying directory, not be
        # rejected.
        real_root = tmp_path / "real_root"
        real_root.mkdir()
        link_root = tmp_path / "link_root"
        link_root.symlink_to(real_root, target_is_directory=True)

        tid = generate_task_id(link_root, "via symlinked root")
        consent = grant_task_consent(link_root, tid)
        result = create_task_contract(link_root, tid, consent=consent, objective_label="test")
        assert result["success"], result
        assert (real_root / ".skilllayer" / "tasks" / tid / "contract.json").exists()

    def test_contract_path_already_exists_as_directory(self, tmp_path):
        tid = generate_task_id(tmp_path, "dir collision")
        task_dir = tmp_path / ".skilllayer" / "tasks" / tid
        (task_dir / "contract.json").mkdir(parents=True)  # a directory, not a file

        consent = grant_task_consent(tmp_path, tid)
        result = create_task_contract(tmp_path, tid, consent=consent, objective_label="test")
        assert result["state"] == "RECORD_ALREADY_EXISTS"

        read = read_task_contract(tmp_path, tid)
        assert read["state"] == "PERSISTENCE_BLOCKED"
        assert read["error"] == "io_error"


# ---------------------------------------------------------------------------
# Consent enforcement (Phase 6)
# ---------------------------------------------------------------------------

class TestConsentEnforcement:
    def test_no_consent_creates_nothing(self, tmp_path):
        tid = generate_task_id(tmp_path, "no consent")
        result = create_task_contract(tmp_path, tid, consent=None, objective_label="test")
        assert result["state"] == "CONSENT_REQUIRED"
        assert not (tmp_path / ".skilllayer").exists()
        assert result["planned_paths"]  # still disclosed for display
        assert result["written_paths"] == []

    def test_consent_for_wrong_task_id_is_invalid(self, tmp_path):
        tid_a = generate_task_id(tmp_path, "a")
        tid_b = generate_task_id(tmp_path, "b")
        consent = grant_task_consent(tmp_path, tid_a)
        result = create_task_contract(tmp_path, tid_b, consent=consent, objective_label="test")
        assert result["state"] == "CONSENT_INVALID"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid_b).exists()

    def test_consent_for_wrong_project_is_invalid(self, tmp_path):
        other_root = tmp_path / "other"
        other_root.mkdir()
        tid = generate_task_id(tmp_path, "x")
        consent = grant_task_consent(other_root, tid)  # granted for a *different* project
        result = create_task_contract(tmp_path, tid, consent=consent, objective_label="test")
        assert result["state"] == "CONSENT_INVALID"

    def test_consent_scoped_to_declared_record_types(self, tmp_path):
        tid = generate_task_id(tmp_path, "x")
        contract_only = grant_task_consent(tmp_path, tid, declared_record_types=frozenset({"contract"}))
        r = create_task_contract(tmp_path, tid, consent=contract_only, objective_label="test")
        assert r["success"]

        r2 = write_task_result(tmp_path, tid, consent=contract_only, verdict="CHANGE_INCOMPLETE",
                                written_by_component="test_component")
        assert r2["state"] == "CONSENT_INVALID"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid / "result.json").exists()

    def test_ungranted_consent_object_is_invalid(self, tmp_path):
        from skilllayer.tasks.persistence import TaskConsent

        tid = generate_task_id(tmp_path, "x")
        fake = TaskConsent(granted=False, granted_at="2026-01-01T00:00:00Z", task_id=tid,
                            project_root_fingerprint="sha256:whatever",
                            declared_record_types=frozenset({"contract"}))
        result = create_task_contract(tmp_path, tid, consent=fake, objective_label="test")
        assert result["state"] == "CONSENT_INVALID"


# ---------------------------------------------------------------------------
# Write-once contract (Phase 5)
# ---------------------------------------------------------------------------

class TestWriteOnceContract:
    def test_second_write_is_conflict_and_original_is_byte_for_byte_unchanged(self, tmp_path):
        tid = generate_task_id(tmp_path, "write once")
        consent = grant_task_consent(tmp_path, tid)
        first = create_task_contract(tmp_path, tid, consent=consent, objective_label="first objective")
        assert first["success"]

        contract_path = tmp_path / ".skilllayer" / "tasks" / tid / "contract.json"
        original_bytes = contract_path.read_bytes()

        second = create_task_contract(tmp_path, tid, consent=consent, objective_label="second objective, different")
        assert second["state"] == "RECORD_ALREADY_EXISTS"
        assert contract_path.read_bytes() == original_bytes

    def test_concurrent_contract_creation_produces_one_success_one_conflict(self, tmp_path):
        tid = generate_task_id(tmp_path, "concurrent")
        consent = grant_task_consent(tmp_path, tid)

        def attempt(i):
            return create_task_contract(tmp_path, tid, consent=consent, objective_label=f"attempt {i}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [1, 2]))

        states = sorted(r["state"] for r in results)
        assert states == ["RECORD_ALREADY_EXISTS", "WRITE_COMPLETED"]


# ---------------------------------------------------------------------------
# Atomic result/checkpoint updates
# ---------------------------------------------------------------------------

class TestResultCheckpointAtomicUpdate:
    def _make_task(self, tmp_path, hint="atomic"):
        tid = generate_task_id(tmp_path, hint)
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="test")
        return tid, consent

    def test_result_requires_existing_contract(self, tmp_path):
        tid = generate_task_id(tmp_path, "no contract")
        consent = grant_task_consent(tmp_path, tid)
        result = write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_INCOMPLETE",
                                    written_by_component="test_component")
        assert result["state"] == "RECORD_NOT_FOUND"

    def test_result_attempt_auto_increments(self, tmp_path):
        tid, consent = self._make_task(tmp_path)
        r1 = write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_INCOMPLETE",
                                written_by_component="test_component")
        r2 = write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_VALIDATED",
                                written_by_component="test_component")
        assert r1["success"] and r2["success"]
        record = read_task_result(tmp_path, tid)["record"]
        assert record["attempt"] == 2
        assert record["verdict"] == "CHANGE_VALIDATED"

    def test_checkpoint_version_must_be_strictly_increasing(self, tmp_path):
        tid, consent = self._make_task(tmp_path)
        first = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=1,
                                       status="ACTIVE", exact_next_action_label="do the thing")
        assert first["success"]
        stale = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=1,
                                       status="ACTIVE", exact_next_action_label="repeat")
        assert stale["state"] == "WRITE_REJECTED"
        assert stale["error"] == "checkpoint_version_not_monotonic"
        second = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=2,
                                        status="VALIDATED", exact_next_action_label="ship it")
        assert second["success"]
        record = read_task_checkpoint(tmp_path, tid)["record"]
        assert record["checkpoint_version"] == 2
        assert record["status"] == "VALIDATED"

    def test_checkpoint_status_must_be_allowlisted(self, tmp_path):
        tid, consent = self._make_task(tmp_path)
        result = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=1,
                                        status="MADE_UP_STATUS", exact_next_action_label="x")
        assert result["state"] == "WRITE_REJECTED"

    def test_a_serialization_failure_leaves_previous_record_untouched(self, tmp_path, monkeypatch):
        tid, consent = self._make_task(tmp_path)
        first = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=1,
                                       status="ACTIVE", exact_next_action_label="first")
        assert first["success"]
        checkpoint_path = tmp_path / ".skilllayer" / "tasks" / tid / "checkpoint.json"
        original = checkpoint_path.read_bytes()

        import skilllayer.tasks.persistence as mod

        def _boom(*a, **k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(mod, "atomic_write_json", _boom)
        second = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=2,
                                        status="VALIDATED", exact_next_action_label="second")
        assert second["state"] == "PERSISTENCE_BLOCKED"
        assert checkpoint_path.read_bytes() == original


# ---------------------------------------------------------------------------
# Redaction and rejection gate (Phase 4 / Phase 8)
# ---------------------------------------------------------------------------

SYNTHETIC_SECRETS = {
    "anthropic_key": "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789",
    "openai_key": "sk-" + "a" * 48,
    "github_token": "ghp_" + "a" * 40,
    "gitlab_token": "glpat-" + "a" * 24,
    "google_key": "AIza" + "a" * 35,
    "slack_token": "xoxb-" + "1" * 15,
    "aws_key": "AKIA" + "Q" * 16,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dGhpc2lzYXNpZ25hdHVyZQ",
    "bearer": "Bearer " + "a" * 30,
    "db_url": "postgres://admin:hunters3cret@db.internal:5432/prod",
    "password_url": "https://alice:hunters3cret@example.com/",
    "webhook": "https://hooks.slack.com/services/T00/B00/XXXXXXXXXXXXXXXXXXXXXXXX",
    "git_remote": "git@github.com:someorg/somerepo.git",
    "dotenv": "DATABASE_PASSWORD=hunters3cret",
    "cli_flag": "--api-key=sk-live-abcdefghijklmnopqrstuvwx",
}


class TestSafeEntropyShapeNegativeControls:
    """Structurally-verifiable, non-secret identifier shapes must NOT be
    rejected by the low-confidence entropy heuristic — only genuinely
    unrecognized high-entropy blobs should be. Regression coverage for a bug
    found during independent verification: these shapes were all being
    rejected purely for length+mixed-alnum before the safe-shape exemption."""

    @pytest.mark.parametrize("name,value", [
        ("git_sha_short", "9b8f6e8"),
        ("git_sha_full", "9b8f6e8c4114f6b2fed803e20024003eb2317c06"),
        ("sha256_hash", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85"),
        ("uuid4", "550e8400-e29b-41d4-a716-446655440000"),
        ("pip_integrity_hash", "sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"),
        ("npm_integrity_hash",
         "sha512-tv6VN4A5tSAj6VnLXKC9Ej9K4WMSuUfC3n8T7A0Cu+RQXeeIgS0YoDrTLpDs9x8dOQOgH1p5xrhI4E1Ovi2cM=="),
        ("opaque_short_identifier", "cus_NffrFeUfNV2Hib"),
    ])
    def test_safe_shape_is_accepted(self, name, value):
        result = sanitize_persisted_value(f"observed value: {value}", FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert result.accepted, name
        assert value in result.sanitized_value

    def test_a_generated_task_id_embedded_in_text_is_accepted(self, tmp_path):
        tid = generate_task_id(tmp_path, "sample")
        result = sanitize_persisted_value(f"resuming task {tid}", FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert result.accepted

    def test_known_secrets_are_still_rejected_after_the_exemption(self):
        # The exemption narrows only the low-confidence entropy fallback; the
        # specific high-confidence reject patterns run first and unconditionally.
        result = sanitize_persisted_value(
            "token: " + "ghp_" + "a" * 40, FieldPolicy.REDACTABLE_TEXT, max_length=200,
        )
        assert not result.accepted

    def test_unrecognized_high_entropy_blob_is_still_rejected(self):
        # Mixed-case, non-hex, non-UUID, non-task-id: no safe shape applies.
        token = "aZ9" * 15
        result = sanitize_persisted_value(f"value: {token}", FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted
        assert result.confidence == "low"


class TestRedactionAndRejection:
    @pytest.mark.parametrize("name,secret", SYNTHETIC_SECRETS.items())
    def test_known_secret_patterns_are_rejected_in_redactable_text(self, name, secret):
        result = sanitize_persisted_value(f"context around {secret} more context",
                                           FieldPolicy.REDACTABLE_TEXT, max_length=500)
        assert not result.accepted, name
        assert result.sanitized_value is None
        assert secret not in (result.rejection_reason or "")

    @pytest.mark.parametrize("name,secret", SYNTHETIC_SECRETS.items())
    def test_known_secret_patterns_are_rejected_in_safe_structured(self, name, secret):
        import re as _re
        result = sanitize_persisted_value(secret, FieldPolicy.SAFE_STRUCTURED,
                                           shape_pattern=_re.compile(r".{1,500}"))
        assert not result.accepted, name

    def test_home_path_is_redacted_not_rejected(self):
        result = sanitize_persisted_value(
            "the bug is in /Users/alice/project/foo.py near line 12",
            FieldPolicy.REDACTABLE_TEXT, max_length=200,
        )
        assert result.accepted
        assert "alice" not in result.sanitized_value
        assert "«redacted:home_path_unix»" in result.sanitized_value
        assert "home_path_unix" in result.redactions_applied

    def test_windows_home_path_is_redacted(self):
        result = sanitize_persisted_value(
            r"seen at C:\Users\alice\repo\foo.py", FieldPolicy.REDACTABLE_TEXT, max_length=200,
        )
        assert result.accepted
        assert "alice" not in result.sanitized_value

    def test_email_is_redacted(self):
        result = sanitize_persisted_value(
            "reported by alice@example.com", FieldPolicy.REDACTABLE_TEXT, max_length=200,
        )
        assert result.accepted
        assert "alice@example.com" not in result.sanitized_value

    def test_low_confidence_high_entropy_token_is_rejected(self):
        token = "aZ9" * 15  # 45 chars, mixed alnum, no known pattern match
        result = sanitize_persisted_value(f"value: {token}", FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted
        assert result.confidence == "low"

    def test_forbidden_free_text_always_rejects(self):
        result = sanitize_persisted_value("anything at all, even benign", FieldPolicy.FORBIDDEN_FREE_TEXT)
        assert not result.accepted
        assert result.rejection_reason.endswith("free_text_not_permitted")

    def test_overlong_value_is_rejected_not_truncated(self):
        result = sanitize_persisted_value("x" * 500, FieldPolicy.REDACTABLE_TEXT, max_length=80)
        assert not result.accepted
        assert result.sanitized_value is None  # never silently truncated/replaced

    def test_nested_list_with_one_bad_leaf_rejects_whole_value(self):
        secret = SYNTHETIC_SECRETS["github_token"]
        value = ["clean reason one", "clean reason two", f"leaked: {secret}"]
        result = sanitize_persisted_value(value, FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted
        assert result.sanitized_value is None

    def test_nested_mapping_with_bad_leaf_rejects_whole_value(self):
        secret = SYNTHETIC_SECRETS["aws_key"]
        value = {"a": "fine", "b": {"c": f"embedded {secret}"}}
        result = sanitize_persisted_value(value, FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted

    def test_mixed_type_container_with_ints_and_none_is_fine(self):
        value = {"count": 3, "note": "all clean", "flag": None, "items": ["a", "b"]}
        result = sanitize_persisted_value(value, FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert result.accepted
        assert result.sanitized_value == value

    def test_excessive_nesting_depth_is_rejected(self):
        value = "leaf"
        for _ in range(10):
            value = [value]
        result = sanitize_persisted_value(value, FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted
        assert "max_depth_exceeded" in result.rejection_reason

    def test_invalid_dict_key_is_rejected(self):
        result = sanitize_persisted_value({"../etc": "x"}, FieldPolicy.REDACTABLE_TEXT, max_length=200)
        assert not result.accepted


class TestRedactionIntegration:
    """Secrets injected through the real create/write entry points must never
    reach disk, and a rejected write must leave no target record at all."""

    def test_secret_in_objective_label_blocks_contract_and_creates_nothing(self, tmp_path):
        tid = generate_task_id(tmp_path, "clean hint")
        consent = grant_task_consent(tmp_path, tid)
        secret = SYNTHETIC_SECRETS["anthropic_key"]
        result = create_task_contract(tmp_path, tid, consent=consent, objective_label=f"rotate {secret}")
        assert result["state"] == "WRITE_REJECTED"
        assert secret not in json.dumps(result)
        assert not (tmp_path / ".skilllayer" / "tasks" / tid / "contract.json").exists()

    def test_secret_in_allowed_path_blocks_contract(self, tmp_path):
        tid = generate_task_id(tmp_path, "path secret")
        consent = grant_task_consent(tmp_path, tid)
        secret = SYNTHETIC_SECRETS["dotenv"]
        result = create_task_contract(tmp_path, tid, consent=consent, objective_label="ok",
                                       allowed_paths=[f"src/{secret}/file.py"])
        assert result["state"] == "WRITE_REJECTED"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid / "contract.json").exists()

    def test_secret_in_constraint_detail_blocks_contract(self, tmp_path):
        tid = generate_task_id(tmp_path, "constraint secret")
        consent = grant_task_consent(tmp_path, tid)
        secret = SYNTHETIC_SECRETS["db_url"]
        result = create_task_contract(
            tmp_path, tid, consent=consent, objective_label="ok",
            constraints=[{"kind": "custom", "detail_label": f"avoid touching {secret}"}],
        )
        assert result["state"] == "WRITE_REJECTED"
        assert secret not in json.dumps(result)

    def test_secret_in_verdict_reasons_blocks_result_write(self, tmp_path):
        tid = generate_task_id(tmp_path, "verdict secret")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        secret = SYNTHETIC_SECRETS["webhook"]
        result = write_task_result(
            tmp_path, tid, consent=consent, verdict="CHANGE_FAILED",
            verdict_reasons=["clean reason", f"leaked {secret}"], written_by_component="test_component",
        )
        assert result["state"] == "WRITE_REJECTED"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid / "result.json").exists()

    def test_secret_in_next_action_label_blocks_checkpoint_write(self, tmp_path):
        tid = generate_task_id(tmp_path, "next action secret")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        secret = SYNTHETIC_SECRETS["git_remote"]
        result = write_task_checkpoint(
            tmp_path, tid, consent=consent, checkpoint_version=1, status="ACTIVE",
            exact_next_action_label=f"push to {secret}",
        )
        assert result["state"] == "WRITE_REJECTED"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid / "checkpoint.json").exists()

    def test_repository_identity_never_contains_the_real_path(self, tmp_path):
        tid = generate_task_id(tmp_path, "identity")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        record = read_task_contract(tmp_path, tid)["record"]
        identity_blob = json.dumps(record["repository_identity"])
        assert str(tmp_path.resolve()) not in identity_blob
        assert record["repository_identity"]["path_fingerprint"].startswith("sha256:")

    def test_written_path_report_contains_no_absolute_path(self, tmp_path):
        tid = generate_task_id(tmp_path, "paths")
        consent = grant_task_consent(tmp_path, tid)
        result = create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        for p in result["written_paths"] + result["planned_paths"]:
            assert not p.startswith("/")
            assert str(tmp_path.resolve()) not in p


# ---------------------------------------------------------------------------
# Recovery safety (Phase 9)
# ---------------------------------------------------------------------------

class TestRecoverySafety:
    def test_missing_record(self, tmp_path):
        tid = generate_task_id(tmp_path, "missing")
        result = read_task_contract(tmp_path, tid)
        assert result["state"] == "RECORD_NOT_FOUND"

    def test_malformed_json_is_not_rewritten(self, tmp_path):
        tid = generate_task_id(tmp_path, "malformed")
        task_dir = tmp_path / ".skilllayer" / "tasks" / tid
        task_dir.mkdir(parents=True)
        contract_path = task_dir / "contract.json"
        contract_path.write_text("{not valid json, truncated", encoding="utf-8")
        before = contract_path.read_bytes()

        result = read_task_contract(tmp_path, tid)
        assert result["state"] == "PERSISTENCE_BLOCKED"
        assert result["error"] == "malformed_json"
        assert contract_path.read_bytes() == before  # never auto-fixed

    def test_unsupported_schema_version(self, tmp_path):
        tid = generate_task_id(tmp_path, "schema")
        task_dir = tmp_path / ".skilllayer" / "tasks" / tid
        task_dir.mkdir(parents=True)
        (task_dir / "contract.json").write_text(
            json.dumps({"schema_version": 999, "task_id": tid}), encoding="utf-8",
        )
        result = read_task_contract(tmp_path, tid)
        assert result["state"] == "PERSISTENCE_BLOCKED"
        assert result["error"] == "unsupported_schema_version"

    def test_record_with_unexpected_extra_fields_is_tolerated(self, tmp_path):
        tid = generate_task_id(tmp_path, "extra fields")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        contract_path = tmp_path / ".skilllayer" / "tasks" / tid / "contract.json"
        data = json.loads(contract_path.read_text())
        data["an_unexpected_future_field"] = "some value"
        contract_path.write_text(json.dumps(data), encoding="utf-8")

        result = read_task_contract(tmp_path, tid)
        assert result["success"]
        assert result["record"]["an_unexpected_future_field"] == "some value"

    def test_task_id_mismatch_between_result_and_directory(self, tmp_path):
        tid = generate_task_id(tmp_path, "mismatch")
        other_tid = generate_task_id(tmp_path, "other")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_INCOMPLETE",
                           written_by_component="test_component")

        result_path = tmp_path / ".skilllayer" / "tasks" / tid / "result.json"
        data = json.loads(result_path.read_text())
        data["task_id"] = other_tid
        result_path.write_text(json.dumps(data), encoding="utf-8")

        read = read_task_result(tmp_path, tid)
        assert read["state"] == "PERSISTENCE_BLOCKED"
        assert read["error"] == "task_id_mismatch"

    def test_manually_modified_contract_missing_schema_version(self, tmp_path):
        tid = generate_task_id(tmp_path, "manual edit")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        contract_path = tmp_path / ".skilllayer" / "tasks" / tid / "contract.json"
        data = json.loads(contract_path.read_text())
        del data["schema_version"]
        contract_path.write_text(json.dumps(data), encoding="utf-8")

        result = read_task_contract(tmp_path, tid)
        assert result["state"] == "PERSISTENCE_BLOCKED"
        assert result["error"] == "unsupported_schema_version"

    def test_file_replaced_by_symlink_is_not_followed(self, tmp_path):
        tid = generate_task_id(tmp_path, "symlink swap")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        contract_path = tmp_path / ".skilllayer" / "tasks" / tid / "contract.json"
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "task_id": tid}), encoding="utf-8")
        contract_path.unlink()
        contract_path.symlink_to(target)

        result = read_task_contract(tmp_path, tid)
        assert result["state"] == "PERSISTENCE_BLOCKED"
        assert result["error"] == "symlink_not_permitted"

    def test_write_refuses_to_blindly_overwrite_corrupt_existing_result(self, tmp_path):
        tid = generate_task_id(tmp_path, "corrupt result")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        result_path = tmp_path / ".skilllayer" / "tasks" / tid / "result.json"
        result_path.write_text("not json at all", encoding="utf-8")
        before = result_path.read_bytes()

        write = write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_INCOMPLETE",
                                   written_by_component="test_component")  # attempt omitted -> auto-derive path
        assert write["state"] == "PERSISTENCE_BLOCKED"
        assert result_path.read_bytes() == before


# ---------------------------------------------------------------------------
# No hidden writes
# ---------------------------------------------------------------------------

class TestNoHiddenWrites:
    def test_reads_on_a_fresh_project_create_nothing(self, tmp_path):
        tid = generate_task_id(tmp_path, "x")
        read_task_contract(tmp_path, tid)
        read_task_result(tmp_path, tid)
        read_task_checkpoint(tmp_path, tid)
        assert list(tmp_path.rglob("*")) == []

    def test_rejected_write_creates_nothing(self, tmp_path):
        tid = generate_task_id(tmp_path, "x")
        consent = grant_task_consent(tmp_path, tid)
        secret = SYNTHETIC_SECRETS["slack_token"]
        create_task_contract(tmp_path, tid, consent=consent, objective_label=f"leak {secret}")
        assert list(tmp_path.rglob("*")) == []

    def test_declined_consent_creates_nothing(self, tmp_path):
        tid = generate_task_id(tmp_path, "x")
        create_task_contract(tmp_path, tid, consent=None, objective_label="ok")
        assert list(tmp_path.rglob("*")) == []

    def test_no_gitignore_modification(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n", encoding="utf-8")
        tid = generate_task_id(tmp_path, "x")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="ok")
        assert gitignore.read_text(encoding="utf-8") == "node_modules/\n"


# ---------------------------------------------------------------------------
# Cross-process read
# ---------------------------------------------------------------------------

class TestCrossProcessRead:
    def test_record_readable_from_a_fresh_process(self, tmp_path):
        tid = generate_task_id(tmp_path, "cross process")
        consent = grant_task_consent(tmp_path, tid)
        create_task_contract(tmp_path, tid, consent=consent, objective_label="verify cross-process read")

        src_root = Path(__file__).resolve().parent.parent / "src"
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from skilllayer.tasks.persistence import read_task_contract\n"
            "result = read_task_contract(Path(%r), %r)\n"
            "assert result['success'], result\n"
            "assert result['record']['objective_label'] == 'verify cross-process read'\n"
            "print('CROSS_PROCESS_READ_OK')\n"
        ) % (str(src_root), str(tmp_path), tid)

        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert "CROSS_PROCESS_READ_OK" in proc.stdout


# ---------------------------------------------------------------------------
# Minimal integration boundary (Phase 10) — end-to-end, no routing/MCP
# ---------------------------------------------------------------------------

class TestFoundationIntegration:
    def test_full_lifecycle_proves_all_required_properties(self, tmp_path):
        tid = generate_task_id(tmp_path, "full lifecycle")

        # 1. planned task paths are computed
        paths = task_record_paths(tmp_path, tid)
        assert paths.contract_path.name == "contract.json"

        # 2. no consent means no write
        blocked = create_task_contract(tmp_path, tid, consent=None, objective_label="x")
        assert blocked["state"] == "CONSENT_REQUIRED"
        assert not paths.contract_path.exists()

        # 3. consent creates a contract
        consent = grant_task_consent(tmp_path, tid)
        created = create_task_contract(
            tmp_path, tid, consent=consent, objective_label="Ship the login timeout fix",
            allowed_paths=["src/auth/**"], expected_checks=["pytest:tests/auth"],
            constraints=[{"kind": "no_new_dependencies", "detail_label": "keep deps frozen"}],
        )
        assert created["success"]

        # 4. exact relative written paths are returned
        assert created["written_paths"] == [f".skilllayer/tasks/{tid}/contract.json"]

        # 5. a second contract write is rejected
        second = create_task_contract(tmp_path, tid, consent=consent, objective_label="different")
        assert second["state"] == "RECORD_ALREADY_EXISTS"

        # 6. result and checkpoint can be written atomically
        result_write = write_task_result(tmp_path, tid, consent=consent, verdict="CHANGE_VALIDATED",
                                          verdict_reasons=["tests passed"], written_by_component="foundation_test")
        checkpoint_write = write_task_checkpoint(tmp_path, tid, consent=consent, checkpoint_version=1,
                                                  status="VALIDATED", exact_next_action_label="open a PR")
        assert result_write["success"] and checkpoint_write["success"]

        # 7. records can be read in a new process
        src_root = Path(__file__).resolve().parent.parent / "src"
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from skilllayer.tasks.persistence import read_task_contract, read_task_result, read_task_checkpoint\n"
            "root = Path(%r)\n"
            "c = read_task_contract(root, %r); r = read_task_result(root, %r); k = read_task_checkpoint(root, %r)\n"
            "assert c['success'] and r['success'] and k['success']\n"
            "print('OK')\n"
        ) % (str(src_root), str(tmp_path), tid, tid, tid)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

        # 8. injected secrets are blocked
        tid2 = generate_task_id(tmp_path, "with a secret")
        consent2 = grant_task_consent(tmp_path, tid2)
        secret_result = create_task_contract(
            tmp_path, tid2, consent=consent2, objective_label=f"leak {SYNTHETIC_SECRETS['github_token']}",
        )
        assert secret_result["state"] == "WRITE_REJECTED"
        assert not (tmp_path / ".skilllayer" / "tasks" / tid2).exists() or not (
            tmp_path / ".skilllayer" / "tasks" / tid2 / "contract.json"
        ).exists()


# ---------------------------------------------------------------------------
# Constraint / status allowlists sanity
# ---------------------------------------------------------------------------

class TestAllowlists:
    def test_constraint_kind_not_in_allowlist_is_rejected(self, tmp_path):
        tid = generate_task_id(tmp_path, "bad constraint kind")
        consent = grant_task_consent(tmp_path, tid)
        result = create_task_contract(
            tmp_path, tid, consent=consent, objective_label="ok",
            constraints=[{"kind": "do_whatever_you_want", "detail_label": "no limits"}],
        )
        assert result["state"] == "WRITE_REJECTED"

    def test_all_constraint_kinds_are_documented_and_stable(self):
        assert CONSTRAINT_KINDS == frozenset({
            "no_new_dependencies", "no_public_api_change", "no_config_changes", "custom",
        })

    def test_task_statuses_include_full_lifecycle_vocabulary(self):
        assert "VALIDATED" in TASK_STATUSES
        assert "STALE" in TASK_STATUSES
        assert "CANCELLED" in TASK_STATUSES
