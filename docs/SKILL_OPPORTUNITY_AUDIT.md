# Skill Opportunity and Adoption Audit

`skilllayer.tasks.skill_audit` is local, deterministic observability over
which SkillLayer capabilities a session actually used, which were plausibly
applicable but skipped, which had no capability available at all, and what
ordinary actions duplicated SkillLayer functionality by hand. It exists to
turn a single, hand-written dogfood observation into a repeatable
measurement loop: **observe → classify → one hypothesis → improve → new
natural test.**

It never invokes a skill on the agent's behalf, never blocks an ordinary
tool, never rewrites agent instructions, never sends telemetry, and never
inspects arbitrary agent activity — it only classifies *explicit, bounded,
normalized-enum* observations the host chooses to report.

## The four tools

| Tool | Purpose | Mutates? |
|---|---|---|
| `skilllayer_audit_record_operation` | Record one bounded observation (an ordinary action, or a real SkillLayer call) | In-memory only |
| `skilllayer_audit_session` | Build the deterministic adoption report | Optional (only with `persist_report=True`) |
| `skilllayer_audit_status` | Read-only progress summary | No |
| `skilllayer_audit_reset` | Clear one session's in-memory state | In-memory only (never deletes a persisted report) |

## Recording observations

```
skilllayer_audit_record_operation(repo_path, session_id, operation="FILE_EDIT",
                                   attributes={"is_production_logic": True})
skilllayer_audit_record_operation(repo_path, session_id, operation="TEST_RUN",
                                   attributes={"completed": True})
skilllayer_audit_record_operation(repo_path, session_id, operation="GIT_DIFF")
```

`operation` is one of: `FILE_EDIT`, `FILE_CREATE`, `FILE_DELETE`,
`GIT_STATUS`, `GIT_DIFF`, `GIT_LOG`, `TEST_RUN`, `SECRET_REVIEW`,
`REMOTE_JOB_SUBMIT`, `REMOTE_JOB_POLL`, `CONTEXT_RESTORE`, `DECISION_RECORD`,
`TODO_UPDATE`, `RELEASE_ACTION`. `attributes` is a small allowlisted dict
per operation (e.g. `is_production_logic`, `path`, `completed`) — never an
unrestricted shell transcript. Every value passes through the same
redaction/rejection gate Verified Task Execution uses, so a secret or an
absolute private path is rejected outright, not merely warned about.

To record that a real SkillLayer tool was actually called, pass
`skilllayer_tool_name` instead of `operation`:

```
skilllayer_audit_record_operation(repo_path, session_id,
                                   skilllayer_tool_name="skilllayer_vte_start")
```

Pass exactly one of `operation`/`skilllayer_tool_name` per call. The session
is created automatically on first use, in the mode you name (default
`NATURAL_DOGFOOD`).

## Capability registry

Ten available capabilities plus one deliberately absent one:

| Capability | Related tools | Maturity |
|---|---|---|
| Verified Task Execution | `skilllayer_vte_*` | Available |
| Decision Tracking | `skilllayer_track_decision` | Available |
| Context Save / Resume | `skilllayer_save_context`, `skilllayer_resume_work`, `skilllayer_rehydrate_context` | Available |
| Todo Management | `skilllayer_add_todo`, `skilllayer_mark_todo_done`, `skilllayer_list_todos` | Available |
| Decision Search | `skilllayer_search_decisions` | Available |
| Context Snapshot Compare | `skilllayer_compare_context_snapshots` | Available |
| Scoped Git Inspection | `skilllayer_git_diff`, `skilllayer_git_log`, `skilllayer_git_blame`, ... | Available |
| Secret Detection | `skilllayer_detect_secrets` | Available |
| Release Readiness | `skilllayer_release_readiness` | Available |
| Safe Code Change | `skilllayer_safe_change` | Available |
| External Async Job Orchestration | *(none)* | **Missing** |

The full registry (`skill_audit.CAPABILITY_REGISTRY`) also records each
capability's applicability signals, exclusion signals, manual equivalents,
and expected unique value. No hypothetical functionality is ever registered
as available.

## Classification

Every event has one of seven classifications:

- `USED` — a capability with no defined applicability rule was called.
- `APPLICABLE_AND_USED` — a rule-governed capability was applicable *and* a
  real call was observed.
- `APPLICABLE_BUT_SKIPPED` — **HIGH confidence only.** Strong applicability
  evidence exists and no call was observed.
- `POSSIBLY_APPLICABLE` — MEDIUM/LOW confidence. Never described as a
  confirmed missed opportunity.
- `NOT_APPLICABLE` — positive evidence that the capability didn't apply.
- `CAPABILITY_MISSING` — applicable, but no SkillLayer capability exists
  yet (currently only `EXTERNAL_ASYNC_JOB_ORCHESTRATION`).
- `UNKNOWN` — insufficient evidence to judge (e.g. nothing was recorded, or
  no deterministic rule is defined for this capability).

Rules are defined for five capabilities (Verified Task Execution, Scoped
Git Inspection, Secret Detection, Decision Search, Context Snapshot
Compare); every other capability only ever produces `USED` or `UNKNOWN` —
never a fabricated skip claim without a real rule behind it.

## Manual reinvention

`detect_manual_reinventions` reports when ordinary recorded operations
reproduced part of a capability by hand — e.g. `FILE_EDIT` + `GIT_DIFF` +
`TEST_RUN` with no `vte_*` call. Each entry names the capability
duplicated, the observed manual steps, the missing SkillLayer evidence, and
a *qualitative* description of the likely extra work. No exact time-saved
figure is ever claimed without measured timing data.

## Recommendation

Exactly one recommendation, or none, chosen by priority: (1) a missing
capability was repeatedly observed, (2) a high-confidence skipped
capability exists, (3) a manual reinvention was detected, (4) low adoption
suggests a discoverability problem, (5) insufficient evidence → no
recommendation. Never "use SkillLayer more" for its own sake.

## Modes

- **NATURAL_DOGFOOD** (default) — tools remain fully optional; no
  applicability suggestion is ever shown during work; nothing is
  auto-invoked; nothing is blocked; only the end-of-session report exists.
  See [NATURAL_DOGFOOD_MODE.md](NATURAL_DOGFOOD_MODE.md).
- **ASSISTED** — `get_assisted_suggestion`/`skilllayer_audit_session` may
  surface one concise, bounded suggestion; it still never auto-starts a
  task or writes without consent.

There is no `ENFORCED` mode in this milestone.

## Privacy and persistence

In-memory only by default (bounded: 500 operations, 200 SkillLayer calls
per session; oldest excess is dropped and reported, never silently lost).
Persisting requires `persist_report=True` **and** the same task-lifecycle
consent every other VTE record requires — writes go to
`.skilllayer/session-audits/<session_id>/` (`operations.jsonl` is reserved
for future use; `opportunities.json`, `adoption-report.json`,
`adoption-report.md` are written today), reusing Foundation A's
consent/atomic-write/lock/path-confinement exactly. A retry with identical
content is idempotent; a divergent retry is rejected explicitly
(`conflicting_report_rewrite`); a report referencing a different
`session_id` is rejected (`cross_session_report_reference`). Never sent
over the network; no background daemon.

## Example (anonymized real-session pattern)

A real Claude Code dogfood session showed exactly this shape:

```
Used naturally
- Decision Tracking
- Context Save / Resume
- Todo Management

Applicable but skipped
- Verified Task Execution: repository files changed with strong completion
  signals but no VTE call was observed.

Manual reinvention
- Verified Task Execution: scope checking, checkpointing, and completion
  verification were done manually.
- Scoped Git Inspection: raw git output was inspected instead of a
  repository-confined, structured result.

Missing capability
- External Async Job Orchestration: remote job operations require custom
  polling code.

Recommendation
Reduce VTE entry friction; a high-confidence Verified Task Execution
opportunity was handled manually.
```

No private project names, paths, datasets, or account details are ever
part of a real report — every field above is either a fixed template or a
bounded, structurally-validated enum/count.
