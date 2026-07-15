# Install SkillLayer with your AI coding agent

Copy the prompt below into Claude Code, Codex, Cursor with terminal access, or
another capable coding agent. This installs and verifies SkillLayer only. It
does not require you to run the sandbox test or let the agent touch an existing
repository.

```text
Install SkillLayer using only its public repository. Follow these safety rules
before every action:

- Work only in a new dedicated installation directory that you create for
  SkillLayer. Do not inspect, search, or modify any existing repository,
  home-directory file, .env file, SSH key, global Git configuration, global
  Python environment, or unrelated MCP entry.
- Do not use sudo, install global packages, push, commit, upload diagnostics,
  send telemetry, search for credentials, or claim success without verifying it.
- Do not overwrite MCP configuration. Preserve unrelated entries. Do not edit
  client configuration until I explicitly approve the exact proposed change.
- Do not install dependencies or create an environment silently. First explain
  the proposed isolated SkillLayer installation and MCP configuration changes,
  including their paths, then ask for my confirmation.
- Stop before touching any existing repository. After installation, offer the
  disposable sandbox as an optional next step instead.

First identify the operating system and verify that Git and Python 3.10 or
newer are available. If either prerequisite is missing, report the exact
requirement and stop; do not install it.

Propose this plan, using a new directory such as a temporary directory or a
user-approved dedicated SkillLayer directory:

1. Clone only https://github.com/NickGBar/Skilllayer.git into that directory.
2. Create the repository-local .venv and install SkillLayer's documented MCP
   runtime there with ./scripts/install.sh. This changes only the new
   SkillLayer checkout and uses no sudo or global installation.
3. Run ./scripts/verify_install.sh and .venv/bin/python -m skilllayer doctor
   --json.
   Also run .venv/bin/python -m skilllayer --version and, if useful, generate
   local-only diagnostics with .venv/bin/python -m skilllayer diagnostics.
   Optionally run .venv/bin/python -m skilllayer update-check --json; this is a
   bounded read-only public release lookup and never performs an update.
4. Generate a separate MCP config with
   .venv/bin/skilllayer mcp-config --output skilllayer-mcp.json, then validate
   it with .venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json.
5. Show the single mcpServers.skilllayer entry that would be added to my coding
   client's MCP configuration. Ask for a separate confirmation before adding
   it, and preserve every unrelated MCP entry.
6. Start the installed MCP server over stdio, perform a real initialize and
   tools/list handshake, confirm Safe Code Change, Release Readiness, and
   Resume Project Work are discoverable, then shut the process down cleanly.

Show the plan and exact paths first. Ask for confirmation before cloning,
creating .venv, installing dependencies into it, generating configuration, or
editing any client MCP configuration. After I approve, perform only the
approved steps. If any step fails, retain useful sanitized error output, state
what was not verified, and do not substitute another interpreter, repository,
or configuration location without asking.

At the end, report:

- installation status and SkillLayer version;
- doctor status;
- MCP configuration and real-handshake status;
- discovered professional skills;
- whether the product source checkout was created;
- whether the isolated SkillLayer environment was created;
- every project-scoped MCP configuration file created or changed (for example,
  `.mcp.json`) and the approved SkillLayer entry it contains;
- whether existing project source files were modified (this must be no);
- whether global state was modified (this must be no);
- whether unrelated repositories were accessed (this must be no);
- rollback instructions: remove only mcpServers.skilllayer from the approved
  client config, then review `./scripts/uninstall.sh --dry-run` and, only after
  explicit confirmation, run `./scripts/uninstall.sh --remove-venv --confirm`
  from the SkillLayer checkout or remove that dedicated checkout after
  reviewing it;
- recommended next action: optionally try the disposable sandbox at
  https://github.com/NickGBar/skilllayer-tester-sandbox using the separate
  ONE_PROMPT_TEST.md guide.

Do not upload any diagnostic or result automatically. Free early access needs
no account or payment.
```

The separate [one-prompt sandbox test](ONE_PROMPT_TEST.md) exercises the three
professional skills in a disposable repository and produces a locally reviewed
`results.md`; it is optional feedback, not part of installation.
