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
