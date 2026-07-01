# Claude Code Setup

SkillLayer can run as a local MCP server. This page prepares a Claude Code
tester path, but Claude Code integration is **not yet validated**.

**Python >= 3.10 required.**

Current status:

```text
Prepared, validation pending
```

This means:

- the SkillLayer MCP server exists locally
- tool schemas can be listed locally
- a Claude Code config snippet can be generated
- a real Claude Code client has not yet confirmed tool discovery or invocation

## Prerequisites

- Python 3.10 or newer
- Git
- A local clone of SkillLayer
- Claude Code installed separately
- No paid API key is required for this setup check

## Step 1: Clone and Install

Python >= 3.10 is required.

```bash
git clone git@github.com:NickGBar/Skilllayer.git
cd Skilllayer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
```

The base install is enough for local CLI workflows, but Claude Code MCP
integration needs the MCP extra. If `doctor` reports `mcp_sdk_available` as an
optional warning, run:

```bash
python -m pip install -e ".[mcp]" --no-build-isolation
```

## Step 2: Run Local Validation

```bash
python -m skilllayer tester-check
python -m skilllayer doctor --json
python -m skilllayer workflows --json
python -m skilllayer skills --json
```

## Step 3: Verify MCP Tools Locally

```bash
python -m skilllayer.mcp_server --list-tools
```

Run the command above to see the current tool list. The output changes as new
workflows are added; the command is the authoritative source.

## Step 4: Generate MCP Config

```bash
python scripts/generate_mcp_config.py
```

Use the `clients.claude_code` section. It contains:

- command path
- args: `["-m", "skilllayer.mcp_server"]`
- cwd
- env
- a placeholder config location

Do not assume the placeholder config path is correct for every Claude Code
installation. Use the MCP configuration location exposed by Claude Code on the
tester machine.

## Step 5: Configure Claude Code

In Claude Code:

1. Open MCP/server configuration.
2. Add a server named `skilllayer`.
3. Paste/adapt the generated `clients.claude_code.config` snippet.
4. Restart or reload Claude Code if required.
5. Confirm SkillLayer tools appear.

If tools do not appear, first run:

```bash
python -m skilllayer claude-code-prep-check --json
python -m skilllayer.mcp_server --list-tools
```

## Step 6: Verify Tool Behavior

Once the tools appear in Claude Code, ask Claude Code to run:

```text
Use skilllayer_doctor.
Use skilllayer_inspect_repo on this repository.
Use skilllayer_run with dry_run=true on task "Find function inspect_repo".
```

Prefer dry-run checks first. Do not run write workflows against a private user
repository until the tester explicitly asks for it.

## Feedback to Send Back

Please report:

- OS and Python version
- Claude Code version if available
- whether install worked
- whether `tester-check` passed
- whether MCP tools appeared
- which tools appeared
- whether `skilllayer_doctor` worked through Claude Code
- whether a dry-run `skilllayer_run` worked
- any error output

Telemetry is local by default.

## Limitations

- Claude Code support is prepared but not validated.
- This setup does not prove SkillLayer saves tokens or cost.
- SkillLayer is a local workflow layer, not a general autonomous coding agent.
- No automatic telemetry upload is configured.
