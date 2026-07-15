# Claude Code integration

## What SkillLayer adds

Claude Code remains the coding agent that reasons and edits. SkillLayer adds
bounded workflows for safe code changes, release-readiness evidence, and
persistent project context. The host agent still controls edits and approval.

## Install with one prompt

Use the [one-prompt installer](../../INSTALL_WITH_AI.md) from a fresh,
dedicated directory. It creates an isolated environment and asks before
changing project-scoped MCP configuration.

## Project-scoped MCP setup

Generate and validate a config with the installed interpreter:

```bash
.venv/bin/python -m skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/python -m skilllayer mcp-config-check skilllayer-mcp.json --json
```

Merge only the `mcpServers.skilllayer` entry into the selected project/client
configuration. Preserve unrelated servers, approve the change explicitly,
and restart or reload Claude Code before discovering tools.

## Verify installation

```bash
.venv/bin/python -m skilllayer --version
.venv/bin/python -m skilllayer doctor
.venv/bin/python -m skilllayer diagnostics --json
.venv/bin/python -m skilllayer update-check --json
```

Confirm the real stdio handshake and discover `Safe Code Change`, `Release
Readiness`, and `Resume Project Work`.

## First Safe Code Change

> Help me make this small change safely. Inspect the repository and git status,
> propose a bounded plan, wait for my approval, then validate the final diff
> and tests. Do not edit files until I approve the plan.

## Release Readiness

> Check whether this repository is ready for careful external testing. Do not
> modify files. Separate blockers, warnings, and incomplete checks, and show
> the selected test interpreter.

## Resume Project Work

Save context explicitly:

> Save the current project purpose, objective, constraints, completed work,
> and next action as project context.

In a new session:

> Restore the project context. Tell me what I was working on, which constraints
> matter, and what the next action is.

## Safety boundaries

- SkillLayer does not install dependencies automatically.
- Read-only workflows do not intentionally write repository files.
- Stateful writes are disclosed and limited to `.skilllayer/`.
- Commit or back up valuable work before first use on a real repository.
- Start with the disposable sandbox.

## Troubleshooting

- **MCP is not visible:** validate the generated config, confirm the executable
  still exists, then restart Claude Code.
- **Unsupported Python:** use the installer’s explicit `--python` option with
  a supported interpreter; SkillLayer does not install Python.
- **Missing pytest:** inspect the incomplete validation result and run the
  advisory project dependency command yourself.
- **Malformed config:** regenerate a project-scoped config and preserve other
  MCP entries.
- **Stale session:** stop the old server and reload the client.
- **Unknown update status:** the public release endpoint was unavailable;
  retry later rather than assuming “up to date”.
- **Sharing diagnostics:** review and sanitize the local file first; nothing
  is uploaded automatically.

## Disable and uninstall

Run `skilllayer uninstall --dry-run` first. The default operation removes only
SkillLayer’s MCP entry and preserves unrelated entries and `.skilllayer/`.
See [UPDATE.md](../../UPDATE.md) for explicit removal and rollback guidance.
