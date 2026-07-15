# Codex integration

## What SkillLayer adds

Codex remains the coding agent and terminal operator. SkillLayer supplies
professional workflows that return structured plans, validation evidence,
release-readiness checks, and persistent project context.

## Supported environment assumptions

This guide assumes a Codex environment with terminal access and an MCP client
that supports a stdio server. The generic stdio handshake is tested; direct
Codex UI behavior is expected but not independently verified in every client
build.

## Install with one prompt

Use the [one-prompt installer](../../INSTALL_WITH_AI.md) in a fresh dedicated
directory. Review and approve the isolated environment and project-scoped MCP
change before it runs.

## MCP configuration and verification

```bash
.venv/bin/python -m skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/python -m skilllayer mcp-config-check skilllayer-mcp.json --json
.venv/bin/python -m skilllayer --version
.venv/bin/python -m skilllayer doctor
.venv/bin/python -m skilllayer diagnostics --json
```

Add only the `skilllayer` server entry to the Codex MCP configuration, preserve
unrelated servers, then restart/reload Codex. Confirm initialize, `tools/list`,
and the three professional skills.

## Safe Code Change

> Help me make this change safely. Inspect and plan first, wait for approval,
> then validate the host-agent edit. Report changed files, selected Python,
> tests started, and a bounded verdict.

## Release Readiness

> Assess this repository’s release readiness without editing files. Distinguish
> blockers, warnings, and incomplete test evidence. Do not call an incomplete
> check ready.

## Resume Project Work

Save:

> Save the project purpose, current objective, constraints, completed work, and
> next action for a future session.

Restore:

> Resume the saved project context and state the constraints and next action.

## Diagnostics and safety

Use `skilllayer diagnostics --output skilllayer-diagnostics.md`, review it
before sharing, and remember that no upload occurs automatically. SkillLayer
does not install dependencies, make hidden writes, or replace the host agent’s
review responsibilities. Use committed or disposable repositories first.

## Troubleshooting and disable

For missing tools, validate the config and restart Codex. Missing pytest is an
incomplete environment result, not proof that code failed. Run
`skilllayer uninstall --dry-run` before removing the SkillLayer MCP entry; see
[UPDATE.md](../../UPDATE.md) for rollback and preservation rules.
