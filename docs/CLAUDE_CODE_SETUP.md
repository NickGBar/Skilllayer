# Claude Code MCP setup

SkillLayer’s installed stdio MCP server has a real protocol smoke test in this
repository (initialize, tool discovery, and safe calls). This does not validate
every Claude Code UI version.

Clone with `git clone https://github.com/NickGBar/Skilllayer.git`, install from
a checkout with `./scripts/install.sh`, then generate and validate
the one supported contract:

```bash
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Add the `mcpServers.skilllayer` object from that file to Claude Code’s MCP
configuration through Claude Code’s configuration UI. It contains an absolute
venv executable, `-m skilllayer.mcp_server`, and `PYTHONUNBUFFERED=1`; stdio is
the transport. No checkout `cwd` is required.

After reloading Claude Code, begin with `skilllayer_inspect_repo`,
`skilllayer_search`, or `skilllayer_run` on “Git status”. Use
`skilllayer_list_workflows` for machine-readable write/stability metadata.
Internal workflows and ProfileCodeExecution/MeasureMemoryUsage are absent from
the MCP tool list. Telemetry remains off unless explicitly enabled.

If the venv or installation moved, `mcp-config-check` fails with a regeneration
command. Disable the integration by removing the `skilllayer` MCP entry; this
does not delete `.skilllayer/` project memory.
