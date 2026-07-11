# Cursor MCP setup

SkillLayer uses the same installed stdio MCP contract for Cursor and Claude
Code. Install it, then generate and validate the configuration:

```bash
./scripts/install.sh
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Copy `mcpServers.skilllayer` into Cursor’s MCP configuration UI and reload the
client. The runtime server has a real stdio handshake test; Cursor UI discovery
itself is not exercised by this repository’s automated tests.

Start with read-only tools (`skilllayer_inspect_repo`, `skilllayer_search`, or
`skilllayer_run` for “Git status”). Do not treat test execution, network checks,
or browser smoke as side-effect-free. Remove the `skilllayer` entry to disable
the integration; removing it does not remove project `.skilllayer/` data.
