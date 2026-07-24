# Verified Task Execution Foundation C — Safe Resume

Foundation C is an internal task-domain library. It is not a workflow, CLI
command, router target, or MCP tool. It never runs a resume plan, edits source
files, resets Git, stashes work, or generates a human summary.

## Checkpoints

With explicit task-lifecycle consent, immutable checkpoints are stored only at
`.skilllayer/tasks/<task-id>/checkpoints/<sequence>-<checkpoint-id>.json`.
`checkpoint.json` is an atomic latest pointer. A history record includes a
schema version, immutable sequence and predecessor ID, task/step states,
bounded command and validation facts, relative observed/expected paths, and
references to existing structured evidence. Source contents, raw terminal
output, prompts, credentials, and free-form reasoning are not stored.

Sequences start at one and each later checkpoint must point to the immediately
previous immutable ID. A retry with the same ID and integrity fingerprint is
idempotent; a duplicate sequence or broken predecessor is rejected. The
pointer is checked against the history, so a stale pointer is corruption, not
an invitation to guess.

## Resume assessment

`assess_resume(project_root, task_id)` is read-only. It validates the
checkpoint chain and evidence references, reloads the Foundation B contract
and baseline, collects current repository facts, and re-runs scope validation.
It returns `RESUME_SAFE` only with complete evidence, current baseline,
compatible scope, valid chain, and no active conflicting owner.
`RESUME_SAFE_WITH_CONFIRMATION` covers legacy records and interruptions.
`RESUME_BLOCKED` covers forbidden/unexpected changes, invalid contract,
missing evidence, out-of-scope next paths, or active ownership.
`CHECKPOINT_STALE` and `CHECKPOINT_CORRUPT` are explicit non-resumable states.

## Evidence, interruption, and ownership

Evidence references are task-relative JSON records with schema version and
SHA-256 integrity fingerprint. Escaping paths, symlinks, missing records, and
digest mismatches block resume. Interrupted command and validation records are
bounded facts; unknown outcomes require confirmation and revalidation.

An explicit operation may acquire a short task-local lease in `ownership.json`.
There is no background heartbeat. A second active owner blocks another writer;
an expired lease may be reclaimed. Foundation A consent, atomic writes, shared
locking, path confinement, and secret rejection remain in force.

## Compatibility and limitations

Foundation A's old latest-only `checkpoint.json` is recognised as legacy and
requires a new Foundation C checkpoint; it is never silently reinterpreted.
Foundation C does not prove authorship, execute plans, repair stale records,
or replace a user’s Git workflow.

## Foundation D integration

Foundation D's orchestrator calls `assess_resume` fresh on every
`resume_task` call — never cached, never skipped, and never bypassed by a
direct state transition (there is no orchestrator path from `INTERRUPTED` to
`RUNNING` other than through it). One reinterpretation happens at the
orchestrator layer, not inside this module: `assess_resume` has no
`owner_instance_id` parameter, so it blocks on *any* active lease, including
the caller's own. When `resume_task` has already independently verified the
caller holds that lease and the assessment's only objection is
`active_task_owner`, the orchestrator treats this as requiring the same
confirmation any other non-trivially-safe resume needs, rather than as a
hard block — the raw assessment is preserved and returned unmodified for
every other reason. See `VTE_FOUNDATION_D_ORCHESTRATOR.md` for the full
lifecycle this checkpoint/resume/ownership API is composed into.
