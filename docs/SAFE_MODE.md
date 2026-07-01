# SkillLayer Safe Mode Guide

This guide describes a cautious way to try SkillLayer with minimal risk.

Safe mode means:

- CLI-only usage
- no MCP client connected
- read-only workflows first
- no telemetry sharing
- disposable repository or clean git branch
- `--dry-run` before any modifying workflow

SkillLayer does not have a separate safe-mode daemon or service. Safe mode is a
usage pattern.

## Step 1: Use CLI Only

Start with the CLI instead of MCP:

```bash
python -m skilllayer doctor
python -m skilllayer workflows
python -m skilllayer skills
```

## Step 2: Inspect a Repository

Use a copied repository or a test fixture first:

```bash
python -m skilllayer inspect --repo .
```

## Step 3: Run Read-Only Workflows

Find a symbol:

```bash
python -m skilllayer run --repo . --task "Find function inspect_repo"
```

Run tests:

```bash
python -m skilllayer run --repo . --task "Run tests"
```

Run one explicit test target:

```bash
python -m skilllayer run --repo . --task "Run test tests/test_example.py::test_example"
```

Explain failing tests without applying fixes:

```bash
python -m skilllayer run --repo . --task "Explain failing tests"
```

Summarize git status without changing the repository:

```bash
python -m skilllayer run --repo . --task "Git status"
```

Check dependency declarations and imports without changing the repository:

```bash
python -m skilllayer run --repo . --task "Check dependency pytest"
```

These workflows are intended to observe and report.

## Step 4: Use Dry Run Before Modifying Workflows

For workflows that may edit files, start with dry run:

```bash
python -m skilllayer run --repo . --task "Fix failing tests" --dry-run
```

For rename workflows, use a branch or copied repository:

```bash
python -m skilllayer run --repo . --task "Rename old_name to new_name" --dry-run
```

## Step 5: Avoid MCP Until CLI Looks Good

MCP lets a connected coding agent invoke SkillLayer tools. Use MCP only after
you are comfortable with the CLI behavior.

Before enabling MCP:

```bash
python -m skilllayer.mcp_server --list-tools
python -m skilllayer security-check
```

## Safe Mode Summary

Recommended first session:

```bash
python -m skilllayer doctor
python -m skilllayer inspect --repo .
python -m skilllayer run --repo . --task "Find function inspect_repo"
python -m skilllayer run --repo . --task "Run tests"
python -m skilllayer run --repo . --task "Run test tests/test_example.py::test_example"
python -m skilllayer run --repo . --task "Explain failing tests"
python -m skilllayer run --repo . --task "Git status"
python -m skilllayer run --repo . --task "Fix failing tests" --dry-run
```

Stop if any result is surprising.
