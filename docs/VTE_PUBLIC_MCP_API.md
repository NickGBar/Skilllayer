# Verified Task Execution — Public MCP API Reference

Six MCP tools, registered in `skilllayer.mcp_server.MCP_TOOL_HANDLERS`,
delegating to `skilllayer.tasks.public_api` (which wraps the internal
Foundation D orchestrator). No internal function, evidence digest, sequence
number, or consent object is ever the caller's responsibility.

All six accept `repo_path: str` first. All return a `dict` with at least a
`status`/`error_code`/`next_action`-shaped field on failure — never a stack
trace or an absolute filesystem path.

## `skilllayer_vte_start`

Create a task contract, capture a baseline, and acquire ownership, in order.

**Parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `repo_path` | str | — | required |
| `title` | str | — | required; short label, persisted as the contract's objective label |
| `persist_consent` | bool | `False` | must be `True` or nothing is written |
| `description` | str | `""` | only used to help form the task_id; not persisted verbatim |
| `allowed_paths` | list[str] | `None` | exact files (EXPLICIT) or dir prefixes ending `/` (PREFIX) |
| `forbidden_paths` | list[str] | `None` | always overrides `allowed_paths` |
| `scope_mode` | `"EXPLICIT"` \| `"PREFIX"` | `"EXPLICIT"` | no glob support |
| `allow_new_files` / `allow_deleted_files` / `allow_test_files` | bool | `True` | |
| `allowed_generated_paths` | list[str] | `None` | dir prefixes for generated output |
| `max_changed_files` | int \| None | `None` | |
| `expected_checks` | list[str] | `None` | labels only, e.g. `["tests"]` |

**Returns (success)**: `{"status": "READY", "task_id", "owner_instance_id", "scope": {"allowed_paths", "max_changed_files"}, "baseline": "BASELINE_CAPTURED", "next_action"}`

**Returns (error)**: `{"status": "ERROR", "task_id", "error_code", "explanation", "next_action"}`. Common `error_code`s: `persist_consent_required`, `forbidden_preexisting_state`, `task_owned_by_another_instance`.

## `skilllayer_vte_status`

Read-only.

**Parameters**: `repo_path`, `task_id`.

**Returns**: `{"status": "OK", "task_state", "baseline_status", "scope_status", "resume_status", "evidence_complete", "safe_operations", "prohibited_operations", "confirmations_required", "report_preview", "next_action"}`, or `{"status": "ERROR", "error_code": "task_not_found", ...}`. `report_preview` is `null` until `skilllayer_vte_finalize` has persisted a report; when present it has `{"overall_status", "problem_summary", "next_action", "final_verdict"}`. This tool never creates or rewrites a report.

## `skilllayer_vte_checkpoint`

**Parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `repo_path`, `task_id`, `task_phase` | str | — | required |
| `completed_steps` | list[str] | `None` | plain labels; evidence is attached internally |
| `active_step` | str | `None` | |
| `remaining_steps` | list[str] | `None` | |
| `files_observed` / `files_expected_next` | list[str] | `None` | |
| `interruption_reason` | str | `None` | one of `PROCESS_TERMINATED`, `HOST_SHUTDOWN`, `USER_STOPPED`, `TIME_LIMIT`, `DEPENDENCY_UNAVAILABLE`, `TOOL_FAILURE`, `VALIDATION_FAILURE`, `UNKNOWN_INTERRUPTION` |

**Returns**: `{"status": "CHECKPOINTED", "sequence", "next_action"}` or `{"status": "INTERRUPTED", "task_phase", "next_action"}`, or an error shape with `error_code` `"invalid_state_transition"` or `"interruption_reason_invalid"`.

## `skilllayer_vte_resume`

**Parameters**: `repo_path`, `task_id`, `confirmed: bool = False`, `confirmation_token: str | None = None`.

**Returns**:
- `{"status": "RUNNING", "plan", "next_action"}` — resumed.
- `{"status": "BLOCKED", "plan", "next_action"}` — unsafe to resume (see `plan.reasons`).
- `{"status": "CONFIRMATION_REQUIRED", "plan", "confirmation_token", "next_action"}` — call again with `confirmed=True` and the same token.

`plan` fields: `resume_verdict`, `certainly_complete`, `may_be_partially_complete`, `must_be_rechecked`, `repository_drift`, `reasons`, `prohibited_actions`.

## `skilllayer_vte_finalize`

**Parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `repo_path`, `task_id` | str | — | required |
| `tests_recorded` | bool | — | required; `True` only if tests actually ran |
| `tests_passed` | bool \| None | `None` | leave `None` if inconclusive |
| `tests_summary_label` | str | `None` | short label, e.g. `"12 passed"` |
| `locale` | str | `"en"` | only `"en"` supported; anything else is rejected explicitly |
| `persist_report` | bool | `True` | writes `report.json`/`report.md`; `False` returns the report without writing it |

**Returns**: `{"status": "COMPLETED" | "BLOCKED", "receipt", "receipt_text", "human_report", "human_report_markdown", "report_paths", "next_action"}`. `human_report`/`human_report_markdown` are always present — for a `BLOCKED` result too — and can never contradict `receipt` (see [VTE_HUMAN_REPORT.md](VTE_HUMAN_REPORT.md)). `report_paths` is `[]` when `persist_report=False` or on an idempotent retry. See [VTE_VERIFICATION_RECEIPT.md](VTE_VERIFICATION_RECEIPT.md) for the receipt schema.

## `skilllayer_vte_abandon`

**Parameters**: `repo_path`, `task_id`, `reason_label: str`.

**Returns**: `{"status": "ABANDONED", "next_action"}` or an error shape if the task is already terminal.

## Error shape (all tools)

```json
{"status": "ERROR", "task_id": "...", "error_code": "...", "explanation": "...", "next_action": "..."}
```

`next_action` always states a concrete recovery step (e.g. "Call vte_status
to see the task's current state and allowed operations.") — never just "an
error occurred."

## Discovery

`skilllayer_list_skills()` includes a `professional_skills` list with the
`verified_task_execution` entry (purpose, activation/non-activation examples,
required tools, supported lifecycle, safety guarantees, known limitations).
`skilllayer_list_workflows`/`mcp_tool_count`/`list_tool_schemas` reflect the
six VTE tools like any other registered tool.
