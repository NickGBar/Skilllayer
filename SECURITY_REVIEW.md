# SkillLayer Security Review

This document is for cautious developers, workplace reviewers, and testers who
want to understand SkillLayer before installing or enabling it in a coding
agent.

SkillLayer is early-stage local software. It has not received a formal
third-party security audit. Treat this document as a review aid, not as a
security guarantee.

## What SkillLayer Is

SkillLayer is a local CLI/MCP workflow layer for routine coding-agent tasks. It
helps external coding agents reuse known workflows such as repository
inspection, function lookup, symbol rename, test execution, failure explanation,
and minimal browser smoke checks.

SkillLayer is not:

- an LLM
- a general autonomous coding agent
- a replacement for Claude Code, Cursor, Codex, or other coding agents
- a background cloud service
- a remote telemetry collector

The design goal is to offload repetitive workflow operations while keeping
execution visible and auditable.

## Local-First Model

SkillLayer runs locally on the user's machine.

- There is no automatic upload.
- There is no hosted SkillLayer cloud service.
- There is no background daemon unless the user explicitly starts the MCP
  server.
- CLI commands run only when invoked by the user or by a connected local agent.
- Local logs and telemetry are written under `runs/`.

## Network Behavior

Core workflows do not require external API calls.

SkillLayer does not automatically upload telemetry, code, logs, screenshots, or
task text.

Network activity may occur only in user-controlled situations such as:

- installing dependencies with `pip`
- pulling package dependencies if they are not already available
- running a user project's own test command if that command itself accesses the
  network
- opening a user-provided browser smoke URL, if the user invokes
  `BrowserSmokeWorkflow`

The anonymous telemetry export command writes a local file only. It does not
send data anywhere.

## File Modification Behavior

Some workflows are read-only and some can modify files.

Read-only workflows and commands:

- `Doctor`
- `InspectRepo`
- `FindFunctionWorkflow`
- `RunTestsWorkflow`
- `SingleTestWorkflow`
- `ExplainFailureWorkflow`
- `GitStatusWorkflow`
- `DependencyCheckWorkflow`
- `BrowserSmokeWorkflow`, except for writing its screenshot/report artifacts
  outside the target repository

Potentially modifying workflows:

- `RenameSymbolWorkflow`
- `FixFailingTestWorkflow`
- `AddHelperWorkflow`

Modifying workflows should be run on a branch or copied repository first.
Use `--dry-run` where available before allowing edits.

`FixFailingTestWorkflow` is experimental. It applies only conservative,
deterministic patches after structured test execution and rule-based failure
diagnosis. If a repair is ambiguous or outside the supported safe repair set,
it should return `unsupported` instead of guessing.

## Telemetry

Telemetry is local by default.

- Local telemetry is stored under `runs/`.
- CLI activity counts may be recorded locally.
- Workflow events may include local task context.
- Raw task text may exist locally in logs or local telemetry.
- Anonymous export is explicit opt-in.
- Anonymous export redacts task text, local paths, emails, secrets, and other
  sensitive fields by default.
- There is no automatic telemetry upload.

Users should review any export before sharing it.

## MCP Security

The SkillLayer MCP server exposes local tools to a connected MCP client. Enable
it only for trusted clients.

Important MCP implications:

- A connected agent can invoke SkillLayer tools.
- Modifying workflows can edit files if invoked without `--dry-run`.
- The MCP server does not start automatically.
- The server should be configured with an explicit repository path by the
  calling tool.
- Use read-only workflows first when validating a new MCP client.

Recommended first MCP checks:

```bash
python -m skilllayer.mcp_server --list-tools
python -m skilllayer doctor --json
python -m skilllayer workflows --json
```

## Recommended Safe Review Flow

For cautious review:

1. Read this document.
2. Inspect `pyproject.toml`.
3. Inspect `scripts/install.sh`.
4. Inspect `scripts/verify_install.sh`.
5. Inspect `scripts/generate_mcp_config.py`.
6. Inspect `src/skilllayer/`.
7. Install in a disposable clone or virtual environment.
8. Run `python -m skilllayer tester-check`.
9. Run read-only workflows first.
10. Use `--dry-run` for modifying workflows.
11. Review telemetry exports before sharing.

## Work Machine Caution

For workplace environments:

- Do not run SkillLayer on confidential repositories without approval.
- Do not enable MCP for untrusted clients.
- Do not share telemetry exports without reviewing them.
- Do not attach raw logs containing private code.
- Prefer a personal machine, disposable repository, or approved sandbox first.
- Consider asking a security or platform team to review this repository before
  installation.

## Known Limitations

- SkillLayer is early-stage.
- External validation is limited.
- Modifying workflows are experimental unless documented otherwise.
- MCP client support varies by client.
- There has been no formal third-party security audit.
- This project provides no legal or security guarantee.

## Quick Security Check

SkillLayer includes a local posture check:

```bash
python -m skilllayer security-check
python -m skilllayer security-check --json
```

The check reports whether security documents are present, which workflows may
modify files, and whether network upload or automatic telemetry upload is
enabled.
