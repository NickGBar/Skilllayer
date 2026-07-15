# Compatibility

This matrix separates what is tested from what is merely expected.

| Area | Status | Notes |
|---|---|---|
| macOS founder environment | FOUNDER_VERIFIED | The current golden path is isolated installation on macOS. |
| macOS arm64 | FOUNDER_VERIFIED | Verified on the founder Mac environment. |
| Python 3.10–3.13 | EXPECTED_BUT_UNVERIFIED | The package declares `>=3.10`; focused tests exercise supported selection. |
| Repository-local venv | VERIFIED | Used by installer and target-environment execution. |
| Pip GitHub installation | EXPECTED_BUT_UNVERIFIED | Documented; verify before relying on it for production work. |
| Editable installation | VERIFIED | Covered by source/development workflows. |
| Claude Code | VERIFIED_WITH_LIMITATION | Stdio config/handshake and documented prompts verified; Claude Code UI session was not available for this pass. |
| Codex | EXPECTED_BUT_UNVERIFIED | Uses the standard MCP contract; direct Codex UI session was not available for this pass. |
| Generic MCP stdio client | VERIFIED | Real initialize/tools-list/professional-tool smoke is covered by release tests on the current host. |
| Ubuntu 22.04 | UNKNOWN | No clean Ubuntu runtime was available during this evidence pass; CI matrix added for reproducible verification. |
| Ubuntu 24.04 | UNKNOWN | No clean Ubuntu runtime was available during this evidence pass; CI matrix added for reproducible verification. |
| Windows | NOT_SUPPORTED | Runtime support is not verified. |
| Professional skills | VERIFIED | Safe Code Change, Release Readiness, Resume Project Work. |
| Update check | VERIFIED | Read-only public release lookup with bounded timeout. |
| Diagnostics | VERIFIED | Local sanitized output; no automatic upload. |

Evidence date: 2026-07-15. Commit baseline: `37a4faa`. “VERIFIED” here means
the stated evidence type only; it does not imply external-user validation.
