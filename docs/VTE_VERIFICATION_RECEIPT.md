# Verified Task Execution — Verification Receipt

`skilllayer.tasks.receipt.build_receipt(project_root, task_id)` assembles a
stable, structured summary from already-persisted Foundation A-D evidence
(task status, the finalize-time `final_result` evidence record, the
checkpoint chain, transition history, and intervention records). It writes
nothing and never asks an LLM to state a fact — every field is copied or
counted from a record that already passed Foundation A's redaction/rejection
gate. `skilllayer_vte_finalize` returns it as `receipt`, plus a plain-text
rendering as `receipt_text`.

## Schema (version 1)

```json
{
  "receipt_version": 1,
  "task_id": "20260724T105441Z-fix-auth-timeout-90b73aeb",
  "skill_name": "verified_task_execution",
  "final_verdict": "TASK_VERIFIED_COMPLETE",
  "task_state": "COMPLETED",
  "baseline_status": "BASELINE_CAPTURED",
  "scope_status": "SCOPE_CLEAN",
  "resume_status": null,
  "changed_paths": ["src/auth.py"],
  "allowed_changes": ["src/auth.py"],
  "unexpected_changes": [],
  "tests_summary": {"recorded": true, "passed": true, "summary_label": "12 passed"},
  "checkpoints_created": 1,
  "interruptions_recovered": 0,
  "prevented_actions": [],
  "confirmations_required": [],
  "evidence_complete": true,
  "limitations": [],
  "created_at": "2026-07-24T10:54:41Z"
}
```

## Field notes

- `final_verdict` — one of `TASK_VERIFIED_COMPLETE`,
  `TASK_COMPLETE_WITH_LIMITATIONS`, `TASK_INCOMPLETE`, `TASK_BLOCKED`,
  `TASK_FAILED`, `TASK_ABANDONED`, or `null`/`"TASK_NOT_FOUND"` if the task
  never reached `finalize_task`. Derived by Foundation D's `finalize_task`
  from persisted evidence only — the receipt never overrides it.
- `changed_paths` / `allowed_changes` / `unexpected_changes` — from the
  `final_result` evidence record written at finalize time (empty before
  finalization has happened).
- `tests_summary` — exactly what `vte_finalize`'s caller reported
  (`tests_recorded`/`tests_passed`/`tests_summary_label`), never inferred.
- `checkpoints_created` — count from the immutable checkpoint chain
  (`load_checkpoint_chain`).
- `interruptions_recovered` — count of transitions where `resume_task`
  successfully reached `RUNNING`.
- `prevented_actions` — every intervention record for this task (see
  `skilllayer.tasks.interventions`); empty means nothing was ever blocked,
  not that nothing was checked.
- `confirmations_required` — currently pending (unconsumed) confirmation
  tokens for this task.
- `evidence_complete` — `True` only when the underlying scope/baseline
  evidence had no collection limitations.

## Human-readable rendering

`render_receipt_text(receipt)` turns the same fields into a fixed checklist —
every line states a fact already present in the receipt; nothing is
generated freely:

```
Verified Task Execution

✓ Baseline captured
✓ 1 changed file(s) matched approved scope
✓ No forbidden paths changed
✓ Tests passed

Verdict: VERIFIED COMPLETE
```

A blocked task instead shows the specific failing line(s) (e.g. "✗ 1
changed file(s) outside approved scope") and an "⚠ N unsafe action(s) were
prevented" line when `prevented_actions` is non-empty.
