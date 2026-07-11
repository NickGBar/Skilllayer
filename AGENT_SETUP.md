# SkillLayer Agent Setup

This file is written for coding agents such as Codex, Claude Code, Cursor, or
similar tools.

User instruction snippet:

```text
Open your coding agent and say:
Follow AGENT_SETUP.md in this repository to install and validate SkillLayer.
```

## Objective

Install and validate SkillLayer without modifying user code or uploading data.

Do not add workflows. Do not change routing. Do not configure telemetry upload.
Telemetry is local by default and export is opt-in.

## Safe Setup Steps

1. Inspect the repository:

```bash
pwd
ls
python3 --version
```

2. Create a virtual environment if `.venv` does not already exist:

```bash
python3 -m venv .venv
```

3. Activate it:

```bash
source .venv/bin/activate
```

4. Install SkillLayer in editable mode:

```bash
python -m pip install -e . --no-build-isolation
```

5. Run tester validation:

```bash
python -m skilllayer tester-check
python -m skilllayer tester-check --json
```

6. Run direct verification:

```bash
python -m skilllayer doctor --json
python -m skilllayer workflows --json
python -m skilllayer skills --json
```

7. Generate MCP config snippets:

```bash
python scripts/generate_mcp_config.py
```

8. For Claude Code setup, follow:

```text
docs/CLAUDE_CODE_SETUP.md
```

Run the prep check before claiming readiness:

```bash
python -m skilllayer claude-code-prep-check --json
```

This prepares Claude Code testing only. It does not validate real Claude Code
tool discovery or invocation.

9. If asked to configure a client, use the generated snippet and clearly state
which paths the user must adapt.

## Do Not Modify User Code

For setup validation:

- do not edit the user's target repository
- do not run real workflows on private code unless explicitly asked
- prefer `--dry-run` examples
- validate MCP tools before using write workflows
- do not attach raw telemetry or logs
- do not upload anything

## Reporting Format

Report:

- install success or failure
- Python version
- whether `tester-check` passed
- whether `doctor` passed
- whether workflows listed
- where generated MCP config snippets are located or printed
- any next manual step required from the user

If a command fails, report:

- command
- exit code
- concise error summary
- likely fix

Do not overclaim. Passing setup means SkillLayer installed and local validation
worked; it does not prove real coding-agent cost savings.
