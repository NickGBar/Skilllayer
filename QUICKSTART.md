# SkillLayer quickstart

Requires Python 3.10+. On macOS/Linux, from a checkout:

```bash
./scripts/install.sh
./scripts/verify_install.sh
.venv/bin/skilllayer doctor --json
.venv/bin/skilllayer workflows --json
```

Use a committed fixture, copy, or clean branch for a first run:

```bash
.venv/bin/skilllayer inspect --repo /path/to/repo --json
.venv/bin/skilllayer run --repo /path/to/repo --task "Git status" --json
```

`workflows --json` is the source of truth for current workflow count,
stability, and write behavior. Workflows that run tests, browsers, target
programs, or network checks may cause project-defined or external side effects
even when SkillLayer does not directly edit source files.

For MCP:

```bash
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Copy `mcpServers.skilllayer` to the client’s MCP settings. The config uses the
created venv interpreter and stdio transport. Do not reuse it after moving or
deleting that venv; regenerate it instead.

Memory is explicit state under `.skilllayer/`. Saving context reports written
paths and never changes `.gitignore`; rehydrating context is read-only. File
“watching” is a snapshot comparison, not a background watcher.

To disable integration, remove the `skilllayer` MCP server entry. To remove the
installation, remove the venv. Delete `.skilllayer/` only when you deliberately
want to delete project memory.
