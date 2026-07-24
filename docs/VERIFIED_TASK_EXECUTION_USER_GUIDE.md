# Verified Task Execution — User Guide

Verified Task Execution (VTE) is the public product surface built on
Foundations A-D (`skilllayer.tasks`). It exposes six MCP tools so an agent
can implement a change with a bounded scope, checkpoint its progress, safely
resume after an interruption, and only ever report completion when backed by
recorded evidence — never by its own unverified claim.

You do not need to know Foundations A-D exist. This guide covers only the six
public tools.

## The six tools

| Tool | Purpose | Mutates? |
|---|---|---|
| `skilllayer_vte_start` | Create a task contract, capture a baseline, acquire ownership | Yes (with explicit consent) |
| `skilllayer_vte_status` | Report current state and safe next operations | No |
| `skilllayer_vte_checkpoint` | Record progress, or an interruption | Yes |
| `skilllayer_vte_resume` | Assess and resume an interrupted task | Yes (only after confirmation, when required) |
| `skilllayer_vte_finalize` | Validate scope/evidence and finalize | Yes |
| `skilllayer_vte_abandon` | Abandon a task from any non-terminal state | Yes |

Full parameter/return reference: [VTE_PUBLIC_MCP_API.md](VTE_PUBLIC_MCP_API.md).
Receipt schema: [VTE_VERIFICATION_RECEIPT.md](VTE_VERIFICATION_RECEIPT.md).

## When this activates

Call these tools for requests like "implement this safely", "make this
change and verify it", "continue this interrupted task", "do this as a
verified task", "fix this without touching unrelated files", or "finish this
and prove the tests passed". Do not use them for read-only questions,
translation, brainstorming, or a request that explicitly asks to skip
verification — see the `verified_task_execution` entry in
`skilllayer_list_skills` for the full activation/non-activation list.

VTE is never invoked automatically: every operation is an explicit tool call
you choose to make. Tell the user before the first write (`vte_start`
requires `persist_consent=True` precisely so this is a deliberate step, not a
silent one).

## Requirements

- The target must be a git repository.
- `.skilllayer/` must be listed in `.gitignore` — otherwise an untracked
  `.skilllayer/` directory shows up as one large unexpected change and every
  task will be blocked. Add it once per repository.
- `allowed_paths` are exact files (`scope_mode="EXPLICIT"`, the default) or
  directory prefixes ending in `/` (`scope_mode="PREFIX"`). There is no glob
  (`**`) support in either mode.

## The lifecycle

```
vte_start ──► RUNNING ──► vte_checkpoint (repeat) ──► vte_finalize ──► COMPLETED
                 │                                         ▲
                 └── vte_checkpoint(interruption_reason=…) │
                          │                                 │
                          ▼                                 │
                     INTERRUPTED ──► vte_resume ─────────────┘
                          (may require confirmation first)
```

### 1. Start

```
vte_start(repo_path, "fix login timeout", persist_consent=True,
          allowed_paths=["src/auth.py", "tests/test_auth.py"])
→ {"status": "READY", "task_id": "...", "scope": {...}, "baseline": "BASELINE_CAPTURED",
   "next_action": "Implement only within the approved scope. ..."}
```

If the repository already had unrelated changes before the task started, you
get `{"status": "ERROR", "error_code": "forbidden_preexisting_state", ...}`
with a `next_action` telling you exactly what to resolve.

### 2. Make progress; checkpoint

```
vte_checkpoint(repo_path, task_id, "writing the fix",
                completed_steps=["read auth.py"], active_step="apply the fix",
                remaining_steps=["run tests"], files_observed=["src/auth.py"])
→ {"status": "CHECKPOINTED", "sequence": 1, "next_action": "..."}
```

Steps are plain strings — you never build a Foundation C step/evidence
object, compute a digest, or track a sequence number yourself.

### 3. Interruption and resume

If work stops unexpectedly, checkpoint with a reason instead:

```
vte_checkpoint(repo_path, task_id, "writing the fix",
                interruption_reason="UNKNOWN_INTERRUPTION")
→ {"status": "INTERRUPTED", ...}
```

Then call `vte_resume`. The first call often requires confirmation:

```
vte_resume(repo_path, task_id)
→ {"status": "CONFIRMATION_REQUIRED", "plan": {...}, "confirmation_token": "cf-...",
   "next_action": "Review the plan, then call vte_resume again with confirmed=True and confirmation_token='cf-...'."}

vte_resume(repo_path, task_id, confirmed=True, confirmation_token="cf-...")
→ {"status": "RUNNING", "plan": {...}, "next_action": "..."}
```

The `plan` tells you what is certainly complete, what may be partially
complete, what must be rechecked, and whether the repository drifted since
the baseline — see the [Resume plan fields](#resume-plan-fields) below.
`vte_resume` never resets Git, stashes, checks out, or rebases anything.

### 4. Finalize

```
vte_finalize(repo_path, task_id, tests_recorded=True, tests_passed=True,
             tests_summary_label="12 passed")
→ {"status": "COMPLETED", "receipt": {...}, "receipt_text": "Verified Task Execution\n\n✓ ...",
   "next_action": "Task verified complete."}
```

Never pass `tests_recorded=True` unless you actually ran the required tests
and observed a definite result. A false claim is still caught by
scope/evidence checks (see [Prevented actions](#prevented-actions) below) —
it is not trusted at face value, but it costs a wasted round trip, so don't
do it.

### 5. Abandon (optional)

```
vte_abandon(repo_path, task_id, reason_label="user cancelled")
→ {"status": "ABANDONED", "next_action": "..."}
```

## Resume plan fields

`plan` (returned by `vte_resume`) has:

- `resume_verdict` — one of Foundation C's resume verdicts.
- `certainly_complete` — step labels from the latest checkpoint's completed steps.
- `may_be_partially_complete` — step labels still `IN_PROGRESS`.
- `must_be_rechecked` — safe next steps Foundation C identified.
- `repository_drift` — a freshness class (e.g. `CURRENT_BASELINE`, `BASELINE_STALE`).
- `reasons` — why resume needs confirmation or is blocked.
- `prohibited_actions` — always `["RESET_GIT", "STASH", "CHECKOUT", "REBASE", "AUTOMATIC_ROLLBACK"]`.

## Prevented actions

Every time an unsafe completion, resume, or scope expansion is actually
blocked, it is recorded as an intervention (never speculatively). You can see
these in `receipt["prevented_actions"]`:

| Type | When |
|---|---|
| `FALSE_COMPLETION_PREVENTED` | Finalize was called with `tests_recorded=False` |
| `UNKNOWN_TEST_RESULT` | Tests were recorded but the outcome was inconclusive |
| `FORBIDDEN_PATH_CHANGE` / `OUT_OF_SCOPE_CHANGE` | A change fell outside the approved scope |
| `STALE_RESUME_BLOCKED` | Resume was blocked by a stale baseline |
| `OWNERSHIP_CONFLICT` | Another instance held an active lease |
| `INCOMPLETE_EVIDENCE` | Finalize was blocked by a stale baseline discovered at finalize time |

## Confirmation tokens

A `confirmation_token` is scoped to one task and one operation, recorded on
disk, and single-use: consuming it once (successfully or not) invalidates
it for any later call, and it cannot be used for a different task. If you
lose a token, just call the tool again without one — a fresh token is issued.

## Known limitations

- No explicit ownership-release primitive; a lease is released only when its
  bounded TTL expires (default 120s).
- `vte_checkpoint` does not accept raw command/validation records; record
  test outcomes through `vte_finalize`'s `tests_recorded`/`tests_passed`.
- Same-owner resumption can mask a stale baseline at `vte_resume` time (see
  `docs/VTE_FOUNDATION_D_ORCHESTRATOR.md`'s documented limitation) — but
  `vte_finalize` re-validates scope and freshness live and independently, so
  a stale baseline is still caught before `VERIFIED_COMPLETE` is ever
  reported.
- `description` passed to `vte_start` is used only to help form a readable
  `task_id`; it is not persisted verbatim (Foundation A's contract schema has
  no free-text description field).
