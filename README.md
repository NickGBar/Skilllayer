# SkillLayer

SkillLayer is a local CLI and stdio MCP server for deterministic repository
workflows. It makes no LLM calls itself. It returns structured JSON, but a
deterministic workflow is not automatically safe or portable to every project.
It makes no industry-analyst token-cost prediction and does not claim to prove
token savings.

Current code registers 44 workflows: 38 `stable` and 6 `internal`. The
authoritative inventory is `skilllayer workflows --json`; it includes each
workflow’s stability and write behavior. MCP currently exposes 36 tools; the
runtime tool list is authoritative and can change with the installed version.

Python 3.10+ is required. The release path is verified on macOS in this
repository; Windows installer logic is checked statically, not executed here.

## Install

From a checkout:

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
./scripts/install.sh
./scripts/verify_install.sh
```

The installer creates `.venv` and installs the MCP runtime extra. It fails if
that required install fails; it does not silently fall back to a reduced MCP
installation. Use the generated environment explicitly when activation is not
convenient:

```bash
.venv/bin/python -m skilllayer doctor --json
.venv/bin/python -m skilllayer workflows --json
.venv/bin/python -m skilllayer inspect --repo /path/to/repo --json
```

## MCP

Generate a config using the installed interpreter, then validate it before
adding it to a client:

```bash
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Copy the `mcpServers.skilllayer` block into your Claude Code or Cursor MCP
configuration. The server uses stdio and does not need a checkout-relative
working directory. If the venv was moved or deleted, the checker reports a
clear regeneration command rather than claiming the config is valid.

Start with `skilllayer_inspect_repo`, `skilllayer_search`, or `skilllayer_run`
for a read-only task such as “Git status”. Internal workflows and the unsafe
profile/memory execution workflows are not registered over MCP.

## Writes, memory, and network

Read-only workflows do not intentionally write repository files. Stateful
memory commands write only under `.skilllayer/` and report written paths; they
never edit `.gitignore`. Snapshot/watch workflows persist a baseline only when
their explicit persistence option is enabled. A “watch” is snapshot-and-diff,
not a background real-time service.

Some workflows execute a project’s tests, make network requests, start browser
work, or run a target script. Their metadata marks these as
`external_side_effects_possible`; use committed copies or a clean branch first.
BrowserSmoke requires its configured browser backend and writes artifacts only
when explicitly enabled.

Automatic telemetry is off by default and no telemetry is uploaded. Session
usage reads local Claude Code logs and can measure recorded usage; it cannot
prove token savings or establish a counterfactual baseline.

## Disable or remove

To disable integration, remove only the `skilllayer` entry from your MCP
client configuration and restart that client. To remove the installation,
delete the SkillLayer virtual environment or uninstall the package from that
environment. Project memory under `.skilllayer/` is user data: delete it only
with an explicit project-level decision. Local telemetry/log directories, if
you explicitly enabled them, can be removed separately.

`scripts/uninstall.sh` and `scripts/uninstall.ps1` make those choices explicit:
use `--remove-venv`, `--remove-project-state`, or `--remove-user-data` only for
the data you intend to remove. They never remove project memory by default.

## What this feels like

After installation, Claude Code discovers the local SkillLayer tools through
MCP. You ask it to inspect a repository or search for `greet`; the result is
structured JSON rather than model-generated shell steps. Later, if you choose
to save context, SkillLayer reports the exact `.skilllayer/` paths written, and
you can rehydrate that context in a later session.
