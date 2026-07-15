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
| Claude Code | FOUNDER_VERIFIED | Stdio MCP configuration and handshake verified. |
| Codex | EXPECTED_BUT_UNVERIFIED | Uses the standard MCP contract; no independent client campaign yet. |
| Generic MCP clients | EXPECTED_BUT_UNVERIFIED | Requires stdio MCP support. |
| Ubuntu | UNKNOWN | Not verified in this release cycle. |
| Windows | NOT_SUPPORTED | Runtime support is not verified. |
| Professional skills | VERIFIED | Safe Code Change, Release Readiness, Resume Project Work. |
| Update check | VERIFIED | Read-only public release lookup with bounded timeout. |
| Diagnostics | VERIFIED | Local sanitized output; no automatic upload. |
