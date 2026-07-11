# SkillLayer First Tester Guide

SkillLayer is a local CLI/MCP workflow layer for routine repository operations.
It is meant to help coding agents reuse known workflows instead of repeatedly
performing the same file search, edit, and validation steps by hand.

SkillLayer is for developers who want to test whether a small workflow layer can
make routine coding-agent work more repeatable and observable.

SkillLayer is not:

- a general autonomous coding agent
- a replacement for Codex, Claude Code, or Cursor
- an LLM
- a token-savings product with proven savings
- a tool that automatically uploads telemetry

SkillLayer is currently exploring whether reusable workflows can reduce
repetitive agent work.

**Python >= 3.10 required.**

Platform status: macOS is verified for the current release candidate. Windows
runtime is **UNVERIFIED**; its PowerShell scripts are **STATICALLY_REVIEWED_ONLY**.
Linux runtime has not been exercised in this release candidate.

For cautious developers or work-machine testing, read `SECURITY_REVIEW.md`
before installing or enabling MCP. The security review kit also includes
`docs/SECURITY_CHECKLIST.md` and `docs/SAFE_MODE.md`.

## Supported Clients

Tested:

- Codex

Partially validated:

- Cursor: generated config, local MCP server startup, and tool schemas are validated; Cursor UI discovery still needs manual confirmation. See `docs/CURSOR_SETUP.md`.

Expected or pending:

- Claude Code: setup path prepared, validation pending. See `docs/CLAUDE_CODE_SETUP.md`.

## Workflow Stability

Run `python -m skilllayer workflows --json` for the complete, authoritative
list — every workflow and command reports its own `stability` of `stable`,
`internal`, or `maintainer`. This section only calls out a few of the 32
stable workflows in more detail; treat the live command output, not this
list, as the source of truth if the two ever disagree.

`RunTestsWorkflow` does not directly edit source files. It runs the repository's
test command, which may create caches, reports, databases, or other project-
defined side effects. It returns structured pass/failure output and does not fix tests.

`SingleTestWorkflow` does not directly edit source files. It runs one explicit
test target, whose project-defined setup may create files or external state. It
does not fix tests.

`ExplainFailureWorkflow` does not directly edit source files. It runs tests and returns
conservative rule-based diagnoses, likely causes, and suggested next steps. It
does not fix tests and does not use an LLM; test-defined side effects remain possible.

`GitStatusWorkflow` is stable and read-only. It summarizes branch, clean/dirty
state, staged changes, unstaged changes, untracked files, and diff stats. It
does not stage, commit, reset, checkout, push, or include full diffs by
default.

`DependencyCheckWorkflow` is stable and read-only. It checks supported
dependency declaration files and source imports/usages. It does not install,
update, remove, or run package managers.

Internal workflows — blocked from MCP by default, and from `run` unless the
maintainer opts in with `--allow-internal` or `SKILLLAYER_ALLOW_INTERNAL=1`:

- `FixBugWorkflow`
- `RenameSymbolWorkflow`
- `AddHelperWorkflow`
- `FixFailingTestWorkflow`

Internal workflows are not part of the recommended first-tester path.
`AddHelperWorkflow` is internal because safe helper insertion remains
repository-specific. `FixFailingTestWorkflow` is internal because a failed
repair attempt currently has no rollback and live-edit is the default rather
than an opt-in; if you do run it under `--allow-internal`, note that it applies
only a small catalog of deterministic repairs and returns `unsupported` instead
of guessing when a repair is not clearly safe.

## 10-Minute Tester Flow

Recommended first workflow path:

```text
install -> verify_install -> inspect_repo -> find function -> run tests
```

1. Clone the repo:

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
```

2. Create an environment and install:

Python >= 3.10 is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
```

Keep the `.venv` activated for the commands below. If you open a new terminal
and are not sure the environment is active, use `.venv/bin/python -m skilllayer`
instead of `python -m skilllayer`.

Base install is enough for local CLI workflows. The `.[mcp]` extra is needed
for Codex/Cursor/Claude Code MCP integration. Missing `pytest` in `doctor` is a
warning unless you are running pytest-based smoke tests.

3. Run the tester check:

```bash
python -m skilllayer tester-check
python -m skilllayer tester-check --json
```

Equivalent without shell activation:

```bash
.venv/bin/python -m skilllayer tester-check
.venv/bin/python -m skilllayer tester-check --json
```

4. Run doctor:

```bash
python -m skilllayer doctor --json
```

5. Inspect a local repository:

```bash
python -m skilllayer inspect --repo /path/to/your/repo --json
```

6. Run a dry-run `FindFunctionWorkflow`:

```bash
python -m skilllayer run \
  --repo /path/to/your/repo \
  --task "Find function parse_money" \
  --dry-run \
  --json
```

7. Run a test workflow on a clean branch or copy if the repo has tests:

```bash
python -m skilllayer run \
  --repo /path/to/your/repo \
  --task "Run tests" \
  --json
```

8. Try one modifying workflow only on a copied repo or clean git branch:

For Claude Code testers, first follow:

```text
docs/CLAUDE_CODE_SETUP.md
```

Then run:

```bash
python -m skilllayer claude-code-prep-check
```

This confirms local SkillLayer prep only. It does not prove Claude Code tool
discovery or invocation until the tester confirms tools inside Claude Code.

```bash
python -m skilllayer run \
  --repo /path/to/copied/repo \
  --task "Rename parse_money to parse_decimal_money" \
  --json
```

9. Optionally configure MCP for Codex:

```bash
python -m skilllayer.mcp_server --list-tools
```

Use `codex_mcp_config.example.json` as a template and replace the placeholder
paths.

10. Export anonymous telemetry:

```bash
python -m skilllayer telemetry-export
```

Review the generated export before sharing it. Do not send raw repo data,
secrets, screenshots, or private diffs.

11. Send feedback:

- fill out `FEEDBACK_TEMPLATE.md`
- or open a GitHub issue with the `tester_feedback` template
- optionally attach the reviewed anonymous telemetry export

## What To Send Back

Useful feedback includes:

- OS and Python version
- client used
- whether MCP tools appeared
- which workflow you tried
- whether it worked
- what was confusing
- which workflow you expected but did not find
- a reviewed anonymous telemetry export, if you are comfortable sharing it

Please do not share private source code, raw task text, secrets, credentials,
screenshots, or local absolute paths.

## Maintainer Release Check

`release-check` is for maintainers preparing a public export:

```bash
python -m skilllayer release-check
python -m skilllayer release-check --json
```

It is read-only and reports whether the checkout is ready for public export. It
does not push, commit, tag, delete files, or create releases. Normal first-time
testers do not need this command unless they are reviewing the release process.
