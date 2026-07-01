# SkillLayer Quickstart

SkillLayer is a local CLI/MCP workflow layer for routine repository operations.
It is not a general coding agent, and it is not a replacement for Codex,
Claude Code, or Cursor. Use it as a small tool server for known workflows.

## 1. Install

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . --no-build-isolation
python -m pip install -r requirements.txt
```

All commands below assume the `.venv` is active. If it is not, use
`.venv/bin/python -m skilllayer ...` to avoid accidentally running the system
Python.

For a minimal CLI-only install, editable install is enough:

```bash
python -m pip install -e . --no-build-isolation
```

Optional browser smoke checks need Playwright browser assets:

```bash
python -m playwright install chromium
```

## 2. Doctor

```bash
python -m skilllayer doctor --json
```

Equivalent without shell activation:

```bash
.venv/bin/python -m skilllayer doctor --json
```

For a target repository:

```bash
python -m skilllayer doctor --repo /path/to/repo --json
```

## 3. Inspect A Repo

```bash
python -m skilllayer inspect --repo /path/to/repo --json
```

This reports file counts, Python LOC, test files, and the detected test command.

## 4. Dry-Run A Workflow

```bash
python -m skilllayer run \
  --repo /path/to/repo \
  --task "Find function parse_money" \
  --dry-run \
  --json
```

Dry-run routes and plans without editing files.

## 5. Run A Real Workflow

Stable workflows:

```text
FindFunctionWorkflow
RenameSymbolWorkflow
BrowserSmokeWorkflow
InspectRepo
Doctor
```

Experimental workflows:

```text
RunTestsWorkflow
ExplainFailureWorkflow
AddHelperWorkflow
FixFailingTestWorkflow
```

`RunTestsWorkflow` runs detected tests and reports structured output. It does
not modify files or fix failures.

`ExplainFailureWorkflow` diagnoses failing test output with deterministic rules.
It does not modify files, auto-fix, or use an LLM.

`FixFailingTestWorkflow` is experimental. It only applies conservative,
deterministic patches and may return `unsupported` when the failure is
ambiguous.

Internal route:

```text
FixBugWorkflow
```

Example:

```bash
python -m skilllayer run \
  --repo /path/to/repo \
  --task "Rename parse_money to parse_decimal_money" \
  --json
```

Use a git working tree or a copied repository when running real edits.

## 6. Configure Codex MCP

Copy `codex_mcp_config.example.json` and replace placeholders:

```json
{
  "mcpServers": {
    "skilllayer": {
      "command": "/ABSOLUTE/PATH/TO/PROJECT/.venv/bin/python",
      "args": ["-m", "skilllayer.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/TO/PROJECT",
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

Check tool schemas:

```bash
python -m skilllayer.mcp_server --list-tools
```

## 7. Check Telemetry

```bash
python -m skilllayer stats --json
```

Telemetry estimates are proxy metrics only. They do not prove real token
savings.
