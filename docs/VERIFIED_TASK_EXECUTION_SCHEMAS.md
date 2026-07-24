# Verified Task Execution Schemas

This document records the internal, versioned data shapes currently persisted
by the Verified Task Execution foundations. They are not public workflow or
MCP contracts.

## Foundation B baseline (schema version 1)

`baseline.json` is write-once and contains:

```text
schema_version, task_id, captured_at, repository_identity, repository_kind,
git_available, git_head, git_branch, git_detached, worktree_clean,
changed_paths, staged_paths, untracked_paths, relevant_file_fingerprints,
test_config_fingerprints, baseline_status, limitations
```

`baseline_status` is one of `BASELINE_CAPTURED`, `BASELINE_INCOMPLETE`,
`BASELINE_UNAVAILABLE`, or `NOT_A_GIT_REPOSITORY`.

## Foundation B scope validation (schema version 1)

The optional `result.json.scope_validation` section contains:

```text
schema_version, task_id, evaluated_at, baseline_status, freshness_class,
observed_changed_paths, allowed_changes, forbidden_changes, unexpected_changes,
ignored_internal_changes, new_files, deleted_files, renamed_files,
changed_file_count, max_changed_files, violations, warnings, verdict,
verdict_reasons, evidence_complete
```

No schema field contains raw source code, raw Git output, an author identity,
or an attribution claim.

## Scope amendment record

`scope_amendments.json` has the Foundation A record wrapper and an append-only
`amendments` list. Each record has `amendment_id`, `approved_at`,
`added_allowed_paths`, `added_generated_paths`, `reason_label`, and
`consent_reference`.

## Foundation C checkpoint (schema version 1)

Immutable history records contain:

```text
schema_version, checkpoint_id, task_id, created_at, sequence,
previous_checkpoint_id, task_phase, task_status, completed_steps, active_step,
remaining_steps, blocked_steps, evidence_refs, repository_state_ref,
contract_ref, baseline_ref, scope_validation_ref, commands_attempted,
validations_attempted, files_observed, files_expected_next,
unresolved_questions, known_failures, limitations, resume_requirements,
interruption, integrity_fingerprint
```

The complete record is integrity-fingerprinted except its own fingerprint
field. Historical records are append-only; `checkpoint.json` is an atomic
pointer to the latest valid history record. Source contents, raw logs, prompts,
environment variables, and credentials are not part of this schema.

## Foundation D transition record (schema version 1)

`.skilllayer/tasks/<task-id>/transitions/<sequence:04d>-<transition-id>.json`,
one immutable file per transition:

```json
{
  "schema_version": 1,
  "transition_id": "tr-eba77728f4ce7e52",
  "task_id": "20260724T091250Z-fix-login-timeout-99fe0114",
  "sequence": 8,
  "previous_state": "INTERRUPTED",
  "next_state": "RESUME_REVIEW",
  "triggered_at": "2026-07-24T09:12:50Z",
  "operation": "resume_task",
  "actor_type": "orchestrator",
  "evidence_refs": [],
  "prerequisites_checked": ["resume_assessment_ran"],
  "outcome": "SUCCESS",
  "limitations": []
}
```

`outcome` is one of `SUCCESS`, `REJECTED`, `BLOCKED`. `actor_type` is one of
`orchestrator`, `user` (a user-triggered event such as `abandon_task` or a
confirmed `resume_task`). Sequence numbers and predecessor `previous_state`
values are re-validated against the full history on every read, mirroring
Foundation C's `load_checkpoint_chain`.

## Foundation D state pointer (schema version 1)

`.skilllayer/tasks/<task-id>/state.json`, an atomic latest-state pointer
(the same pattern as Foundation C's `checkpoint.json`):

```json
{
  "schema_version": 1,
  "task_id": "20260724T091250Z-fix-login-timeout-99fe0114",
  "current_state": "RUNNING",
  "latest_transition_id": "tr-04e51df6a3b2c9a1",
  "latest_sequence": 9,
  "updated_at": "2026-07-24T09:12:51Z"
}
```

A pointer that does not match the actual latest transition on disk is treated
as stale/corrupt, never trusted — see `VTE_FOUNDATION_D_ORCHESTRATOR.md`'s
Recovery section.

## Foundation D final result (schema version 1, evidence record)

`evidence/final_result.json` — the exact shape `finalize_task` returns, so a
repeated call can return it byte-identical rather than reconstructing it from
`result.json`'s different (Foundation A) schema:

```json
{
  "task_id": "20260724T091250Z-fix-login-timeout-99fe0114",
  "final_state": "COMPLETED",
  "completed_at": "2026-07-24T09:14:07Z",
  "contract_ref": {"record_path": "contract.json"},
  "baseline_ref": {"record_path": "baseline.json"},
  "latest_checkpoint_ref": {"record_path": "checkpoint.json"},
  "scope_validation_ref": {"record_path": "evidence/scope_validation.json"},
  "validation_evidence_refs": [{"record_path": "evidence/scope_validation.json"}],
  "changed_paths": ["src/auth/session.py"],
  "allowed_changes": ["src/auth/session.py"],
  "unexpected_changes": [],
  "tests_status": {"recorded": true, "passed": true},
  "evidence_complete": true,
  "blockers": [],
  "warnings": [],
  "final_verdict": "TASK_VERIFIED_COMPLETE",
  "limitations": []
}
```

`final_verdict` is one of `TASK_VERIFIED_COMPLETE`,
`TASK_COMPLETE_WITH_LIMITATIONS`, `TASK_INCOMPLETE`, `TASK_BLOCKED`,
`TASK_FAILED`, `TASK_ABANDONED`.
