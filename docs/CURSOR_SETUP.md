# Cursor MCP Setup

SkillLayer can run as a local MCP server for Cursor.

**Python >= 3.10 required.**

Current validation status:

```text
partially validated
```

What is validated:

- SkillLayer MCP server imports and starts locally.
- Tool schemas can be listed with `python -m skilllayer.mcp_server --list-tools`.
- The generated Cursor MCP config is valid JSON.
- The generated config uses absolute local paths.

What still requires a manual Cursor check:

- SkillLayer tools appear inside Cursor.
- Cursor can invoke `skilllayer_doctor`.
- Cursor can invoke `skilllayer_run` on a dry-run task.

This means Cursor support is still external testing in progress. Do not treat
this page as a production readiness claim.

## Step 1: Install SkillLayer

From the repository root:

```bash
./scripts/install.sh
./scripts/verify_install.sh
```

Or manually:

Python >= 3.10 is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
python -m skilllayer tester-check
```

The MCP extra is required for Cursor integration. If `doctor` reports
`mcp_sdk_available` as an optional warning, run:

```bash
python -m pip install -e ".[mcp]" --no-build-isolation
```

## Step 2: Generate MCP Config

```bash
python scripts/generate_mcp_config.py
```

Copy the `clients.cursor.config` object from the output.

It should look like:

```json
{
  "mcpServers": {
    "skilllayer": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "skilllayer.mcp_server"],
      "cwd": "/absolute/path/to/Skilllayer",
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

## Step 3: Open Cursor MCP Settings

In Cursor:

1. Open Settings.
2. Find the MCP configuration section.
3. Open or edit the MCP JSON configuration.
4. Paste the `mcpServers` block for SkillLayer.

Screenshot placeholder:

```text
[Cursor Settings -> MCP -> configuration JSON]
```

## Step 4: Restart or Reload Cursor

Cursor may need a restart or MCP reload before tools appear.

## Step 5: Verify Tools Appear

Expected SkillLayer tools:

```text
skilllayer_doctor
skilllayer_inspect_repo
skilllayer_run
skilllayer_list_workflows
skilllayer_list_skills
```

## Step 6: Run a Safe Dry-Run

Ask Cursor to use SkillLayer:

```text
Use SkillLayer to inspect this repository.
```

Then try a dry-run task:

```text
Use skilllayer_run with dry_run=true on this repo:
Find function inspect_repo
```

## Common Failure Points

- The generated Python path points to a deleted virtual environment.
- The repo was moved after generating config.
- MCP SDK is installed in one environment but Cursor uses another.
- Cursor needs restart/reload after config changes.
- The MCP server looks frozen when run manually; this is normal because it waits on stdio for an MCP client.

## Privacy

SkillLayer runs locally. It does not upload telemetry automatically.
