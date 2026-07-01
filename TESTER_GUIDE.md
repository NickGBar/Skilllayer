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

Stable workflows and commands:

- `FindFunctionWorkflow`
- `RenameSymbolWorkflow`
- `BrowserSmokeWorkflow`
- `InspectRepo`
- `Doctor`

Experimental workflows:

- `RunTestsWorkflow`
- `SingleTestWorkflow`
- `ExplainFailureWorkflow`
- `GitStatusWorkflow`
- `DependencyCheckWorkflow`
- `FixFailingTestWorkflow`

Experimental workflows may fail on repositories whose layout does not match the
current conservative assumptions.

Internal workflows:

- `AddHelperWorkflow`

Internal workflows are not part of the recommended first-tester path.

`RunTestsWorkflow` is read-only. It runs the detected project test command and
returns structured pass/failure output. It does not fix tests.

`SingleTestWorkflow` is read-only. It runs one explicit test file or test target
when the target can be validated. It does not fix tests and does not modify
files.

`ExplainFailureWorkflow` is read-only. It runs tests and returns conservative
rule-based diagnoses, likely causes, and suggested next steps. It does not fix
tests and does not use an LLM.

`GitStatusWorkflow` is read-only. It summarizes branch, clean/dirty state,
staged changes, unstaged changes, untracked files, and diff stats. It does not
stage, commit, reset, checkout, push, or include full diffs by default.

`DependencyCheckWorkflow` is read-only. It checks supported dependency
declaration files and source imports/usages. It does not install, update,
remove, or run package managers.

`AddHelperWorkflow` is internal and hidden from first-user guidance because safe
helper insertion remains repository-specific.

`FixFailingTestWorkflow` is experimental. It applies only small deterministic
repairs after structured test execution and rule-based diagnosis. If a repair is
not clearly safe, it returns `unsupported` instead of guessing.

Smoke coverage checks wrong constants, wrong operators, missing exports,
simple None guards, Node missing exports, unsupported complex failures,
ambiguous multi-file failures, no-test repos, and dry-run repair proposals. The
workflow verifies after patch and remains experimental.

Smoke coverage currently includes pytest passing/failing, a no-test Python repo,
and npm test passing/failing when Node/npm are available.

## 10-Minute Tester Flow

Recommended first workflow path:

```text
install -> verify_install -> inspect_repo -> find function -> run tests
```

1. Clone the repo:

```bash
git clone git@github.com:NickGBar/Skilllayer.git
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

7. Run a read-only test workflow if the repo has tests:

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

10. Send feedback:

- fill out `FEEDBACK_TEMPLATE.md`
- or open a GitHub issue with the `tester_feedback` template

## What To Send Back

Useful feedback includes:

- OS and Python version
- client used
- whether MCP tools appeared
- which workflow you tried
- whether it worked
- what was confusing
- which workflow you expected but did not find

Please do not share private source code, raw task text, secrets, credentials,
screenshots, or local absolute paths.
