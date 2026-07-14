# One-prompt SkillLayer sandbox trial

This is an optional test after SkillLayer is installed. To install and
configure SkillLayer without touching a user repository, start with
[INSTALL_WITH_AI.md](INSTALL_WITH_AI.md).

Copy the prompt below into Claude Code, Codex, or another capable coding agent
with terminal access. It is intentionally sandbox-first. Before sharing any
generated `results.md`, review it yourself.

```text
Safety rules — follow these before every action:

- Work only inside a new temporary trial directory and the two repositories
  cloned there. Do not inspect, search, or modify any unrelated repository,
  home-directory file, .env file, SSH key, global Git configuration, global
  Python environment, or unrelated MCP entry.
- Do not use sudo. Do not push, commit, upload results, send telemetry, or
  perform destructive cleanup.
- Do not install dependencies, create environments, or execute remediation
  commands automatically. Ask me before every command that mutates an
  environment or project state.
- Make source edits only to the requested sandbox files. Never read secrets.
- Record first-attempt failures before repairing anything. Keep results
  sanitized: no source code, credentials, full home paths, private remotes,
  unrelated MCP configuration, project memory contents, or private prompts.

Create a new temporary directory named skilllayer-beta-trial. Inside it, clone
only these two public repositories:

1. https://github.com/NickGBar/Skilllayer.git
2. https://github.com/NickGBar/skilllayer-tester-sandbox.git

Before installation, display and record both origin URLs and current commit
hashes. If either clone fails, record the first failure in results.md and stop;
do not substitute another repository.

Read SkillLayer's README and the sandbox's EXPECTED_BOUNDARIES.md and TASK.md.
Follow SkillLayer's public installation instructions literally. Record any
failed command, undocumented step, manual edit, or assistance required before
repairing it.

Generate a project-local MCP config in the trial directory and validate it.
Register only that local SkillLayer entry if this coding client supports
project-local MCP configuration. Perform an actual stdio MCP initialization and
tool-list request, or use this client's real MCP integration if available.
Confirm that Safe Code Change, Release Readiness, and Resume Project Work are
discoverable. Shut down the MCP process cleanly when each session ends.

In the sandbox, create .venv but do not install test dependencies. Ask
SkillLayer in natural language to help make the controlled TASK.md change
safely. Let the host agent edit only app.py and tests/test_app.py, adding
farewell(name) and its focused test. Ask SkillLayer to validate the result.

When validation reports that pytest or a test dependency is unavailable,
confirm that tests did not start, the selected Python is shown, the result is
incomplete rather than a code failure, and an advisory command based on
requirements-test.txt is shown. Do not run it yet. Show me the exact command,
its mutation warning, and ask whether I approve it. Only after I explicitly
approve may you run that one command. Retry validation and record the result.

Ask SkillLayer whether the sandbox is ready for careful external testing.
Record its Release Readiness verdict, blockers, warnings, and incomplete
checks. Do not describe this as a security certification.

Explicitly save a short project context containing the task, the completed
work, the environment constraint, and the next action. Record every written
.skilllayer path. Terminate the first MCP process/session. Start a fresh MCP
process/session and ask SkillLayer to resume project work; record whether the
objective, constraints, and next action were recovered without re-explaining
the task.

Disable the test-local MCP entry and uninstall only the trial-local SkillLayer
environment. Preserve the sandbox's .skilllayer memory unless I explicitly ask
to delete it. Create sanitized results.md in the sandbox from the sandbox's RESULTS_TEMPLATE.md,
show its local path, and do not upload it. State whether any file outside the
trial directory was modified and whether any child process remains.
```

The sandbox repository is intentionally separate from SkillLayer. It is for a
disposable trial, not a prerequisite for installation.
