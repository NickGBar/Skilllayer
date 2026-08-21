# Professional skills — the user-facing contract

This closes a reference that existed in the source since the commit that introduced
`SafeCodeChangeWorkflow`, `ReleaseReadinessWorkflow`, and `ResumeProjectWorkWorkflow`
(`src/skilllayer/runner/core.py`, "See docs/SKILLS.md for the user-facing contract") —
the file was never written until now.

## What a "skill" is here, and what it is not

`skilllayer_run` and the ~50 individual MCP tools are the *primitives* — read-only
inspections, git queries, memory CRUD, one narrow operation each. A **professional
skill** is not a new execution mechanism. It is one of those primitives, or a small
aggregation of them, given a declarative catalog entry so a calling agent can discover
it, understand when to use it, and know what it promises — before invoking it.

Registering something as a skill never changes what it executes. If turning an existing
workflow into a skill required rewriting its runtime, that would be a sign the
abstraction is wrong, not a sign the work is done.

## The catalog entry

Call `skilllayer_list_skills()`. Each entry under `professional_skills` carries:

| Field | Meaning |
|---|---|
| `name` | Stable identifier, matches the underlying MCP tool's own vocabulary. |
| `purpose` | What it does and the boundary it holds — stated honestly, including what it never claims. |
| `activation_examples` | Phrasings that should select this skill. |
| `non_activation_examples` | Phrasings that should *not* — including ones that sound adjacent but belong to a different skill. |
| `required_mcp_tools` | The actual MCP tool(s) invoking this skill calls. Nothing here is a new tool. |
| `supported_lifecycle` / `supported_modes` | The phases or modes the underlying tool actually has (e.g. plan/validate, bounded/deep) — omitted if the skill is a single call with no such variation. |
| `expected_receipt_schema_version` | The `schema_version` the tool's own return value carries, so a caller can detect drift. |
| `safety_guarantees` | Non-negotiable behaviors — what the skill will never do, stated as commitments, not aspirations. |
| `known_limitations` | Honest gaps: what it cannot see, cannot guarantee, or requires the caller to provide. Never omitted to make a skill look more capable than it is. |

There is no separate "verdict enum" field in the catalog entry. Each skill's actual
bounded verdict lives in its own return value (`verdict` / `final_verdict`) and is
documented in that skill's own reference doc, linked below — the catalog entry says
*when to use it and what it promises*, not the full mechanics of every call.

## The four registered skills

| Skill | When to select it | When not to | Verdict range |
|---|---|---|---|
| **`verified_task_execution`** | "Implement this safely", "verify this change", resume an interrupted task | Read-only questions, throwaway edits nobody needs proof of | `TASK_VERIFIED_COMPLETE` … `TASK_BLOCKED`/`TASK_FAILED`/`TASK_ABANDONED` — see [VERIFIED_TASK_EXECUTION_USER_GUIDE.md](VERIFIED_TASK_EXECUTION_USER_GUIDE.md) |
| **`release_readiness`** | Before publishing, tagging a release, or handing a repository to external testers | Reviewing one specific change (use `safe_code_change`); needing a security certification (it never gives one) | `READY_FOR_CAREFUL_TESTERS` … `NOT_READY`/`BLOCKED_BY_POLICY` |
| **`safe_code_change`** | Making one narrow, bounded change and wanting it independently validated, not self-reported | Broad multi-area refactors (capped at 25 keyword-matched candidate files); audits with no intent to change anything | `CHANGE_VALIDATED` … `CHANGE_INCOMPLETE`/`VALIDATION_FAILED` |
| **`codebase_health`** | "Is this codebase healthy to build on" — before extending an unfamiliar or long-untouched area | Release decisions (use `release_readiness`); one specific change (use `safe_code_change`) | `HEALTHY` … `INCOMPLETE_ASSESSMENT`/`NOT_HEALTHY` |

`release_readiness` and `safe_code_change` are pre-existing, already-implemented
workflows (`build_release_readiness_artifacts` / `build_safe_change_artifacts` in
`runner/core.py`) — registering them here added zero new runtime behavior, only this
discovery metadata. `codebase_health` (`build_codebase_health_artifacts`) is the first
genuine composition: no primitive it calls is new — `skilllayer_find_conflicts`,
`skilllayer_detect_dead_code`, `skilllayer_map_dependencies`, and (deep mode only)
`skilllayer_watch_deps` all already existed — only their aggregation into one bounded
verdict is. It intentionally mirrors `release_readiness`'s exact shape
(`checks_requested`/`checks_completed`/`checks_incomplete`, a bounded/deep split, "an
incomplete check never becomes a false clean") — the structural test this was built
against: if a third skill needed a different contract shape, the abstraction would be
wrong. It didn't. Full field-level detail for each call still lives in the tool's own
docstring (`skilllayer_release_readiness`, `skilllayer_safe_change`,
`skilllayer_codebase_health` in `mcp_server.py`).

`codebase_health` and `release_readiness` both inspect dependencies — deliberately, not
duplicated by accident. They answer different questions ("can we ship" vs. "should we
build here") from overlapping facts; a primitive being used by more than one skill is
not a problem this contract tries to prevent.

## What is deliberately not a skill (yet)

`resume_work` (`ResumeProjectWorkWorkflow`) is not in the catalog above, even though
`skilllayer diagnostics` already reports it as available — that diagnostics field is
static and does not reflect the actual skill catalog; it predates this document and is
a known inconsistency, not a claim about what is registered here. Whether project
continuity is a standalone professional skill, or stays a primitive a caller reaches
for directly, is an open question, deferred rather than decided by default.
