# Verified Task Execution — Human-Readable Task Report

`skilllayer.tasks.human_report` turns an already-built VTE receipt
(`skilllayer.tasks.receipt`) into a deterministic, human-readable report. No
LLM is ever called and no fact is invented: every sentence is either a fixed
template string keyed by a verified reason code, or a direct copy/count of a
field already present in the receipt. The report can never contradict its
receipt — `validate_report_consistency` proves this and fails closed.

`skilllayer_vte_finalize` returns the report automatically, for a successful
finalization and a blocked one alike, so an agent never needs a second tool
call to understand what went wrong.

## Structured schema (report version 1)

```json
{
  "report_version": 1,
  "receipt_version": 1,
  "task_id": "20260724T105441Z-fix-auth-timeout-90b73aeb",
  "title": "fix auth timeout",
  "final_verdict": "TASK_VERIFIED_COMPLETE",
  "overall_status": "SUCCESS",
  "succeeded_items": ["Repository baseline was captured.", "..."],
  "failed_items": [],
  "blocked_items": [],
  "unknown_items": [],
  "problem_summary": null,
  "problem_details": [],
  "prevented_actions": [],
  "changed_paths": ["src/auth.py"],
  "tests": {"reported_recorded": true, "passed": true, "summary_label": "3 passed"},
  "scope": {"status": "SCOPE_CLEAN", "allowed_changes": ["src/auth.py"], "unexpected_changes": []},
  "resume": {"status": null, "interruptions_recovered": 0},
  "evidence_status": "COMPLETE",
  "limitations": [],
  "next_action": "No further verified action is required.",
  "created_at": "2026-07-24T10:54:41Z",
  "locale": "en"
}
```

`overall_status` is one of `SUCCESS`, `SUCCESS_WITH_LIMITATIONS`, `PARTIAL`,
`BLOCKED`, `FAILED`, `ABANDONED`, `UNKNOWN`. Arrays are bounded (20 items,
200 chars each; 50 changed paths); an omitted-count line is appended when a
list is truncated, e.g. "12 additional changed paths omitted from this view;
see the structured receipt."

## Deterministic mapping

| Receipt `final_verdict` | `overall_status` |
|---|---|
| `TASK_VERIFIED_COMPLETE` | `SUCCESS` |
| `TASK_COMPLETE_WITH_LIMITATIONS` | `SUCCESS_WITH_LIMITATIONS` |
| `TASK_INCOMPLETE` | `PARTIAL` |
| `TASK_BLOCKED` (tests recorded and definitely failed) | `FAILED` |
| `TASK_BLOCKED` (any other reason) | `BLOCKED` |
| `TASK_FAILED` | `FAILED` |
| `TASK_ABANDONED` | `ABANDONED` |
| not yet finalized | `UNKNOWN` |

Note: Foundation D's `finalize_task` itself only ever produces `TASK_BLOCKED`
for both a definite test failure and every other block (scope, staleness,
ownership) — it inspects `tests_status["recorded"]` but never `"passed"`.
This report layer distinguishes a genuine test **failure** from a **block**
using the receipt's own `tests_summary` evidence, without changing Foundation
D's verdict itself.

Incomplete evidence is never mapped to `SUCCESS` — `PARTIAL`/`BLOCKED`/
`UNKNOWN` are the only possible outcomes when evidence is incomplete.

## What succeeded / did not succeed

`succeeded_items` is populated only from confirmed facts (baseline captured,
scope clean, N approved files changed, tests passed, checkpoints created,
interruptions recovered, evidence complete) — never "the implementation
works" beyond what the evidence supports.

`failed_items`, `blocked_items`, and `unknown_items` are populated
separately and never conflated: an inconclusive test result is always
`unknown_items`, never `failed_items`; a test that never ran is never
described as passed; a scope block is `blocked_items`, not a "failure",
unless the receipt itself says otherwise.

## Problem and next action

`problem_summary` is one fixed sentence keyed by a verified reason code
(`UNKNOWN_TEST_RESULT`, `FORBIDDEN_PATH_CHANGE`, `BASELINE_STALE`,
`OWNERSHIP_CONFLICT`, `INCOMPLETE_EVIDENCE`, ...); `problem_details` is the
same template(s), never raw exception text. Every non-`SUCCESS`/
`SUCCESS_WITH_LIMITATIONS` report has exactly one `next_action`, derived
from the same reason code — never "reset", "checkout", "stash", or
"rollback" (the module contains no template that suggests any of these).

## Markdown rendering

```
# Verified Task Report

## Result

## What succeeded          (omitted if empty)
## What did not succeed    (omitted if empty)
## Problem                 (omitted if empty)
## Prevented by SkillLayer (omitted if empty)
## Changed files           (omitted if empty)
## Tests and validation    (omitted if tests were never reported)
## Limitations             (omitted if empty)

## Next action             (always present)
## Verdict                 (always present)
```

Rendering is deterministic: the same report always produces byte-identical
Markdown. The document is capped at 8,000 characters
(`human_report.MAX_MARKDOWN_CHARS`) — Next action and Verdict are always
preserved in full; only the sections above them are cut, with an explicit
`[Report truncated: ...]` marker, if the bound is exceeded.
`render_human_report_text` renders the same content without Markdown syntax.

## Persistence

`.skilllayer/tasks/<task-id>/report.json` (structured model) and
`report.md` (its Markdown rendering) are written together by
`write_human_report`, reusing Foundation A's consent/atomic-write/lock/
path-confinement primitives exactly. Rules:

- Requires the same task-lifecycle consent as every other VTE write.
- A retry with byte-identical `report`/`markdown` is idempotent (no error,
  nothing rewritten).
- A retry with *different* content for an existing report is rejected
  explicitly (`conflicting_report_rewrite`) — never silently overwritten.
- A `report` whose own `task_id` doesn't match the target task is rejected
  (`cross_task_report_reference`).
- `vte_status` never writes a report — it only previews one that
  `vte_finalize` already persisted, via the read-only `read_human_report`.

## Public API integration

`vte_finalize(..., locale="en", persist_report=True)` always returns
`human_report`, `human_report_markdown`, and `report_paths` (empty when
`persist_report=False` or on an idempotent retry) — for a `BLOCKED` result
too, not only `COMPLETED`. `locale` currently supports only `"en"`;
anything else is rejected explicitly (`unsupported_locale`), never silently
substituted or translated by an LLM.

`vte_status` returns `report_preview` — `null` until a report exists, else
`{"overall_status", "problem_summary", "next_action", "final_verdict"}`.

## Examples

**Blocked (unknown test result):**

```
# Verified Task Report

## Result

**BLOCKED** — fix auth timeout

## What did not succeed

- [UNKNOWN] Required tests were started, but no completed result was recorded.

## Problem

Required tests were started, but no completed result was recorded.

## Prevented by SkillLayer

- Finalization was blocked; the task was not marked complete.

## Next action

Rerun the required tests and record a definite pass/fail result.

## Verdict

TASK_BLOCKED
```

See [VTE_VERIFICATION_RECEIPT.md](VTE_VERIFICATION_RECEIPT.md) for the
underlying receipt this report is always derived from, and
[VTE_PUBLIC_MCP_API.md](VTE_PUBLIC_MCP_API.md) for the full tool reference.
