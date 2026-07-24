# Verified Task Execution Foundation D — Internal Orchestrator

Foundation D is an internal task-domain library (`skilllayer.tasks.orchestrator`).
It is not a workflow, CLI command, router target, or MCP tool. It never edits
source files, mutates Git, rolls anything back, calls an LLM, or runs a
background process. It connects Foundation A (consent-gated persistence),
B (baseline/scope), and C (checkpoint history/resume/ownership) into one
deterministic task lifecycle by composing their existing functions — no
changes were made to `persistence.py`, `baseline.py`, `checkpoint.py`,
`resume.py`, or `scope.py`.

## State machine

```
CREATED -> CONTRACT_READY -> BASELINE_READY -> READY_TO_START -> RUNNING
                                                                    |
                                          checkpoint_task (self-loop)
                                                                    |
                                      +------------- interrupt_task v
                                      |                        INTERRUPTED
                                      |                             |
                                      |                    resume_task (always
                                      |                    runs Foundation C's
                                      |                    assess_resume first)
                                      |                             v
                                      |                       RESUME_REVIEW --(confirmed)--> RUNNING
                                      |                             |
                                      |            (blocked/stale/corrupt/abandoned)
                                      |                             v
                                      +----------------------->  BLOCKED / FAILED / ABANDONED
                                                                    |
                                                     validate_task (RUNNING or BLOCKED retry)
                                                                    v
                                                               VALIDATING
                                                                    |
                                                        finalize_task (only from VALIDATING)
                                                                    v
                                                        COMPLETED / BLOCKED / FAILED
```

`COMPLETED`, `FAILED`, `ABANDONED` are terminal: no operation (other than
`get_task_status`) is valid from them. Every other state has an explicit,
enumerated set of operations that may leave it — an operation not listed for
the task's current state fails with `error_code="invalid_state_transition"`
rather than silently no-op'ing or guessing.

Two "logical steps in one call" simplifications, both still recorded as
separate immutable transitions: `create_task` writes `CREATED` then
`CONTRACT_READY`; a fully-safe `resume_task` writes `RESUME_REVIEW` then
`RUNNING` in the same call (a confirmation-pending resume stops at
`RESUME_REVIEW` and returns `CONFIRMATION_REQUIRED` instead).

`BASELINE_STALE → COMPLETED` (from the design brief) is not an orchestrator
state at all — it is Foundation B's own scope-verdict. `finalize_task`
re-derives scope validation from live evidence on every call and hard-gates on
it regardless of what the orchestrator's own state nominally says, so a stale
baseline can never reach `COMPLETED` through any code path.

## Transition history

`.skilllayer/tasks/<task-id>/transitions/<sequence:04d>-<transition-id>.json`,
one immutable file per transition, plus an atomic `state.json` latest
pointer — the same append-only-history-plus-pointer pattern Foundation C
already established for checkpoints. Every transition record has:
`schema_version`, `transition_id` (`tr-<16 hex>`), `task_id`, `sequence`,
`previous_state`, `next_state`, `triggered_at`, `operation`, `actor_type`
(`orchestrator` or `user`), `evidence_refs`, `prerequisites_checked`,
`outcome` (`SUCCESS`/`REJECTED`/`BLOCKED`), `limitations`. Reading recomputes
sequence/predecessor consistency from the files on disk exactly like
Foundation C's `load_checkpoint_chain` — a duplicate sequence, a
predecessor that doesn't match, or a symlinked directory is corruption,
never a guess.

## Preconditions

Every mutating operation checks its own preconditions explicitly and never
infers a missing one as satisfied:

- `start_task`: valid contract, `BASELINE_CAPTURED` baseline, active
  ownership held by the calling instance, no `BASELINE_STALE` scope verdict,
  no forbidden/unexpected pre-existing repository state (scope verdict must
  be `SCOPE_CLEAN` or `SCOPE_EMPTY`).
- `checkpoint_task` / `validate_task` / `finalize_task`: active ownership
  held by the calling instance, plus (checkpoint) a trustworthy checkpoint
  chain, or (finalize) scope validation complete, tests recorded, no
  unresolved blockers, no stale baseline.
- `resume_task`: `INTERRUPTED`/`RESUME_REVIEW` state, active ownership, and
  Foundation C's `assess_resume` run fresh on every call — never cached,
  never skipped.

## Idempotency

Every mutating operation accepts an optional `idempotency_key` (bounded:
`[a-z0-9][a-z0-9_-]{0,79}`). A stable key makes a retry recognizable and
side-effect-free:

- **Transitions**: `transition_id` is derived as
  `sha256(task_id:operation:idempotency_key)[:16]`. A retry with the same key
  that would produce the exact same transition (same target state, task
  already there) returns the existing transition instead of writing a new one
  or rejecting.
- **`create_task`**: a small index at
  `.skilllayer/tasks/.idempotency_index.json` maps `idempotency_key ->
  task_id`. A retry — even with a freshly-generated, different `task_id`
  argument, since the caller cannot always know the original one — returns
  the *original* task's status unchanged.
- **`checkpoint_task`**: the checkpoint_id is derived from
  `(task_id, sequence, idempotency_key)`. A retry recomputes whether the
  *current latest* checkpoint already matches what this exact key would
  produce at its own sequence, before ever advancing to a new sequence — a
  naive `latest.sequence + 1` re-derivation would otherwise mint a genuinely
  new checkpoint on every "retry" instead of recognizing the prior one.
- **`finalize_task`**: idempotency is structural, not key-based — a task
  already in a finalize-terminal state (`COMPLETED`/`FAILED`, or `BLOCKED`
  whose last operation was itself a `finalize_task` call) returns the exact
  persisted `final_result` shape read back from a dedicated evidence record
  (`evidence/final_result.json`), byte-identical to what the original call
  returned — not `result.json`'s different schema.
- **Conflicting retries** (a different target state, or a key reused across
  two different operations) are never silently coerced; they fail with
  `error_code="invalid_state_transition"` exactly like any other invalid
  transition.

## Ownership

Wraps Foundation C's `acquire_task_ownership`/`read_task_ownership` directly.

- `acquire_ownership`: the one-time initial acquisition,
  `BASELINE_READY -> READY_TO_START`.
- `reclaim_ownership`: a **separate** operation for re-acquiring an expired
  (or never-taken) lease from *any* non-terminal state later in the
  lifecycle — a lease can expire while `RUNNING`, `INTERRUPTED`, etc., and
  reclaiming it does not itself change the lifecycle state, only who holds
  the lease. It is still recorded as a same-state audit transition (a lease
  takeover is security-relevant). A currently-active lease held by a
  *different* instance still blocks, identically to `acquire_ownership`.
- Every mutating operation checks the calling `owner_instance_id` holds an
  **active** lease before proceeding; `get_task_status` and
  `assess_task_resume` never require ownership at all (read-only).
- No background heartbeat exists anywhere in this module — a lease's own
  bounded TTL (`lease_seconds`, ≤ 3600) is the only expiry mechanism.
- **Known gap**: Foundation C exposes no explicit ownership-*release*
  primitive yet. `finalize_task`'s successful completion does not actively
  release the lease; the lease's TTL is the only current release mechanism.
  `interrupt_task`'s `retain_ownership` parameter is accepted but has no
  effect beyond documentation intent for the same reason — see Known
  limitations.

## Failure semantics

Every operation returns one of a small, distinct set of states — never a
bare `success=False`:

| State | Meaning |
|---|---|
| `OPERATION_COMPLETED` | Succeeded (or a recognized idempotent retry). |
| `OPERATION_REJECTED` | Precondition failed before any write was attempted (invalid transition, missing consent, ownership conflict, invalid input). |
| `OPERATION_INCOMPLETE` | Ran, but produced an incomplete result that does not advance state (e.g. baseline capture with `BASELINE_INCOMPLETE`/`BASELINE_UNAVAILABLE`). |
| `OPERATION_FAILED_BEFORE_MUTATION` | State could not be safely determined at all (corruption) — nothing was attempted. |
| `OPERATION_PARTIALLY_PERSISTED` | An underlying Foundation A/B/C write succeeded but the orchestrator's own transition/evidence write then failed — the two are not atomic across module boundaries. |
| `TASK_BLOCKED` | `finalize_task` derived `TASK_BLOCKED`/`TASK_INCOMPLETE`. |
| `TASK_FAILED_STATE` | `resume_task` derived an unrecoverable verdict (`CHECKPOINT_CORRUPT`, contradictory `TASK_ALREADY_COMPLETE`). |
| `VALIDATION_FAILED` | An untrustworthy checkpoint chain blocked the operation. |
| `PERSISTENCE_FAILED` | An underlying atomic write raised `OSError`/lock timeout. |
| `CONFIRMATION_REQUIRED` | See below. |
| `READ_COMPLETED` | A read-only call (`get_task_status`, `assess_task_resume`) succeeded. |

`error_code` and (where applicable) `recovery_action` are always present on a
non-success result, naming a concrete next step
(`inspect_transition_history_manually`, `review_scope_validation`,
`retry_validate_task`, ...) rather than leaving the caller to guess.

## Finalization

`finalize_task` re-derives scope validation from **live** evidence on every
call (never trusts a prior `validate_task` snapshot) and hard-gates on:
scope verdict not `BASELINE_STALE`/`SCOPE_VIOLATED`/`VALIDATION_BLOCKED`,
`tests_status={"recorded": True, ...}` explicitly supplied by the caller
(never inferred), and complete evidence collection. The final result is
persisted twice, deliberately: once via Foundation A's `write_task_result`
(the project's existing, shared result record) and once as this module's
own `evidence/final_result.json` (the exact shape `finalize_task` returns,
so a retry is byte-identical — see Idempotency).

Verdict derivation:

| Condition | `final_verdict` | `final_state` |
|---|---|---|
| Any blocker (stale baseline, scope violation, tests not recorded) | `TASK_BLOCKED` | `BLOCKED` |
| Scope evidence incomplete | `TASK_INCOMPLETE` | `BLOCKED` |
| Clean, but non-fatal warnings present | `TASK_COMPLETE_WITH_LIMITATIONS` | `COMPLETED` |
| Clean, no warnings | `TASK_VERIFIED_COMPLETE` | `COMPLETED` |

`TASK_FAILED`/`TASK_ABANDONED` are reachable only via `resume_task`'s
corruption/abandonment paths, not via `finalize_task` directly — finalize
never fails a task outright, it blocks it (recoverable via a fresh
`validate_task` retry) or completes it.

## Read-only status

`get_task_status` never writes anything (verified by a dedicated test that
snapshots every file's mtime across repeated calls). It reports
`current_state`, `latest_transition`, `latest_checkpoint`, `ownership`,
`baseline_status`, freshly-recomputed `scope_status`, `resume_status` (only
when interrupted), `safe_operations`/`prohibited_operations` (derived
directly from the same transition table that gates real calls — never a
separately-maintained list that could drift), and `evidence_complete`.

## Recovery

| Condition | Behavior |
|---|---|
| Missing `state.json`, valid transition history present | Reconstructed in memory from the last transition record; never written back silently — a read returns the reconstructed value, a write recomputes and persists a fresh pointer atomically. |
| `state.json` present but stale (doesn't match the actual latest transition) | Detected and rejected (`OPERATION_FAILED_BEFORE_MUTATION`) — never trusted over the immutable history. |
| Corrupted/malformed transition entry, symlinked transitions directory, duplicate sequence, mismatched `previous_state` | All detected by re-validating the full history against its own sequence/predecessor chain (mirroring `load_checkpoint_chain`); any of these blocks further writes until fixed by hand — never auto-repaired. |
| Forged pointer + forged "COMPLETED" transition | A forged terminal state still refuses further mutation (terminal states are always refused) — forging the pointer cannot unlock a live task, only lock it further. |
| Another task's evidence copied into this task's evidence directory | Irrelevant: every real evidence-producing call (`validate_task`, `finalize_task`) recomputes evidence fresh from the current repository/contract/baseline; a stray foreign file is simply overwritten by the next real write, never trusted as this task's own state. |

## User confirmation

`resume_task` is the only operation that can require confirmation today: when
Foundation C's assessment returns `RESUME_SAFE_WITH_CONFIRMATION` (legacy
checkpoint or unresolved interruption questions) and the caller has not
passed `confirmed=True`, the response is:

```json
{
  "state": "CONFIRMATION_REQUIRED",
  "confirmation_required": true,
  "confirmation_reason": "resume_requires_confirmation",
  "confirmation_scope": "resume",
  "operation_after_confirmation": "resume_task"
}
```

No approval is ever created automatically; the task is left at
`RESUME_REVIEW` until the caller explicitly retries with `confirmed=True`.

### Self-ownership reinterpretation

Foundation C's `assess_resume` is conservative by design: it has no
`owner_instance_id` parameter, so it blocks on **any** active lease (a third
party must never barge into someone else's task) — including the caller's
own still-active lease from before the interruption. `resume_task` already
independently verifies the caller holds that active lease before calling
`assess_resume`; when the assessment's *only* objection is
`active_task_owner` and the caller is that verified owner, it is
reinterpreted (not re-derived — the raw assessment is still returned intact
under a modified `resume_reasons` marker) as requiring the same confirmation
any other non-trivially-safe resume needs, rather than as a hard block. The
deeper repository/scope-freshness checks Foundation C's early return skipped
in this path are not silently skipped end-to-end — they run for real via the
`validate_task` call the lifecycle already requires before `finalize_task`.

## Security

Every write reuses Foundation A's own primitives directly: `TaskConsent`
scoping, `atomic_write_json`/`memory_lock`, path confinement via
`task_record_paths` (all orchestrator-local paths — `state.json`,
`transitions/`, `evidence/` — are computed as children of the already
symlink-checked `task_dir`, with their own additional symlink checks before
every write, mirroring Foundation C's `checkpoints_dir` pattern exactly),
and `sanitize_persisted_value`/`_contains_rejected_secret` for any
free-text field (`abandon_task`'s `reason_label`). No raw logs, source file
contents, tokens, environment variables, or chain-of-thought are ever
persisted — every record is small, structured, and schema-bounded.

## Known limitations

- **No explicit ownership-release primitive.** Foundation C does not expose
  one yet; a lease's TTL is the only release mechanism today. Interruption's
  `retain_ownership=False` and finalize's "release ownership" (lifecycle
  step 12) are therefore documentation of intent, not an enforced action.
- **`.skilllayer/` must be gitignored for scope validation to behave
  correctly.** In a repository where it is not, the very first task's own
  contract/baseline writes appear to Git as one new untracked top-level
  `.skilllayer` entry (Git reports an entirely-untracked directory as a
  single path, not its individual files), which Foundation B's
  `is_protected_path` correctly classifies as forbidden — this is expected,
  matches the project's existing gitignore-suggestion convention throughout,
  and is not a bug to work around here.
- **Scope path syntax has no globbing.** `allowed_paths`/`forbidden_paths`
  entries are either exact file paths (`scope_mode="EXPLICIT"`, the default)
  or directory prefixes ending in `/` (`scope_mode="PREFIX"`) — there is no
  `**`/glob support anywhere in Foundation B, so callers must declare scope
  using one of those two exact forms.
- **`OPERATION_PARTIALLY_PERSISTED` is a real, not-fully-closeable gap.**
  Foundation A/B/C's own writes and this module's transition/evidence writes
  are separate atomic operations, not one cross-module transaction; a crash
  between the two can leave (e.g.) a baseline persisted with no matching
  `BASELINE_READY` transition yet. `get_task_status`/recovery detect and
  report this rather than silently proceeding, but do not auto-heal it.
- **No new capability beyond composition.** Foundation D adds no scope
  rules, no new evidence classification, and no new redaction patterns
  beyond what A/B/C already define — by design, matching the "connect these
  components" brief.

## Milestone E (public product surface) integration

`src/skilllayer/tasks/public_api.py` is the only module built on top of this
orchestrator without modifying it. It wraps every function here
(`create_task` through `get_task_status`) into six bounded MCP tools
(`vte_start`/`vte_status`/`vte_checkpoint`/`vte_resume`/`vte_finalize`/
`vte_abandon`) that never require a caller to know `owner_instance_id`
bookkeeping, evidence digests, or sequence numbers exist. One real gap in
this module was found while building that wrapper: `assess_task_resume` has
no `owner_instance_id` and therefore cannot apply the D5 self-ownership
reinterpretation that only `resume_task` performs — a caller gating its own
control flow on `assess_task_resume`'s raw verdict (as the first version of
`public_api.vte_resume` did) will reach a different, less-informed
conclusion than `resume_task` itself reaches a moment later. The fix lives
entirely in `public_api.py` (see `VERIFIED_TASK_EXECUTION_DECISIONS.md`'s
E3): always call `resume_task` and react to its actual returned state,
rather than pre-branching on `assess_task_resume`. See
[`VERIFIED_TASK_EXECUTION_USER_GUIDE.md`](VERIFIED_TASK_EXECUTION_USER_GUIDE.md)
and [`VTE_PUBLIC_MCP_API.md`](VTE_PUBLIC_MCP_API.md) for the full public
surface this orchestrator remains the source of truth for.
