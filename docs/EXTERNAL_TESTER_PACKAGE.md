# SkillLayer external tester session

SkillLayer gives AI coding agents professional engineering skills: safe code
changes, release readiness checks, and persistent project context.

Thank you for testing an early release. This is an independent usability
session, not a support session: please follow the instructions as written and
say what you think is happening before asking for help.

**Time:** 15–25 minutes.
**Public repository:** https://github.com/NickGBar/Skilllayer
**Canonical installation page:** [Install with your AI coding agent](../INSTALL_WITH_AI.md)

## Before you begin

Use macOS and a terminal you are comfortable with. Choose a disposable sample
repository, a fresh clone of an open-source project, or a fully committed copy
of your own repository. Do not use valuable uncommitted work, production
credentials, confidential code, or a repository containing real `.env`
secrets.

Start with read-only requests. Saving project context is explicit and writes
only under `.skilllayer/`; SkillLayer reports the paths written and never edits
`.gitignore`. Environment-remediation commands are advice only: SkillLayer does
not run them. Windows is not verified for this session.

Please do not inspect SkillLayer source code or look for alternative setup
instructions during the session. The point is to learn whether the public path
is sufficient.

## Session script

The session facilitator records time, failed commands, manual configuration
edits, and any intervention. They will not explain a step during your first
attempt unless you are at risk of exposing a secret, damaging valuable work, or
have been blocked for ten minutes.

### 1. First impression — 2 minutes

Read only the first section of the [README](../README.md). Without further
research, tell the facilitator:

- What you think SkillLayer does.
- Who it is for.
- Which sounds most useful: safer code changes, release readiness, or
  continuing project work in a later session.

### 2. Install and connect — 7 minutes

Follow the canonical installation page exactly:

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
./scripts/install.sh
./scripts/verify_install.sh
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Add the generated `mcpServers.skilllayer` entry through your coding client's
normal MCP configuration interface, then restart or reload the client as it
requires. Do not repair JSON by hand. Record every command that fails and any
question you have before seeking help.

Success means SkillLayer is discoverable by the client within ten minutes,
without source-code inspection, manual JSON repair, or author intervention.

### 3. Discover it naturally — 2 minutes

In your coding client, use natural language rather than tool names. Try one or
more of these intentions:

- “Check whether this repository is ready for careful external testing.”
- “Help me make a small code change safely.”
- “Save the current project state so I can continue later.”

Say whether the selected capability and its result make sense. Do not ask the
facilitator which tool should have been selected.

### 4. Safely validate one small change — 3 minutes

Use a small controlled change in the test repository, such as adding a tiny
helper, changing one CLI message, adding a focused test, or changing a harmless
configuration default. Ask the agent to help make the change safely, then ask
it to validate the result.

Afterward, explain in your own words whether SkillLayer edited the code or
whether the host coding agent did. Say whether you understand the changed-file
list, test result, selected Python environment, risks, and final verdict.

### 5. Recover from one environment problem — 4 minutes

The facilitator supplies a disposable, committed fixture with a local `.venv`,
a missing test dependency, and a `requirements-test.txt` or
`requirements-dev.txt` file. Run validation and interpret the result before
doing anything else.

If you are comfortable, decide whether to run the displayed command yourself,
then retry validation. The tool must not run the command for you. Describe what
you believe is incomplete, which Python was selected, and what you would do
next.

### 6. Release and project memory — 4 minutes

Ask whether the fixture is ready to release. Describe the difference between a
blocker, a warning, and an incomplete check, and whether this feels like a
security certification.

Then save this structured project state: purpose, current objective,
constraints, completed work, and next action. Close the coding-client session,
start a new one, and ask:

> What was I working on, what constraints matter, and what should I do next?

Say what was recovered correctly, what was missing, and what was irrelevant.

### 7. Disable or remove — 2 minutes

Follow the [README disable/remove instructions](../README.md#disable-or-remove).
Confirm that the MCP entry is removed, the requested installation files are
removed, project `.skilllayer/` data remains unless you explicitly chose to
delete it, and unrelated client configuration is unchanged.

## Facilitator-only observation record

Do not show this section before the independent attempt. For each tester,
record: time to understand, install, MCP discovery, and first useful result;
failed commands; interventions; manual configuration edits; skill-routing
attempts and correct selections; remediation completion; memory restart;
disable/uninstall outcome; and use-again answer. Record an intervention whenever
the ten-minute rule is used.

After both sessions, classify each observation as exactly one of:
`COMMERCIAL_BLOCKER`, `INSTALL_BLOCKER`, `MCP_BLOCKER`,
`SKILL_DISCOVERY_BLOCKER`, `TRUST_BLOCKER`, `REMEDIATION_BLOCKER`,
`MEMORY_VALUE_BLOCKER`, `OUTPUT_CLARITY`, `COSMETIC`, `FEATURE_REQUEST`,
`USER_ERROR`, or `DOCUMENTATION_GAP`.

Rank findings by testers affected, prevented task completion, trust damage,
effect on future willingness to pay, and estimated fix size. Keep compliments,
optional interest, and actual commitments separate. Successful installation is
not evidence of product value or demand.

## Submit feedback and optional diagnostics

Use [FEEDBACK_TEMPLATE.md](../FEEDBACK_TEMPLATE.md) immediately after the
session. Diagnostic sharing is optional and requires your explicit consent.
Before sending anything, manually redact usernames from paths and remove
source code, `.env` contents, API keys, tokens, private prompts, unrelated MCP
configuration, screenshots, diffs, and project-memory content unless you
separately approve it.

Useful diagnostics are SkillLayer version, macOS and Python versions, command
names and exit codes, structured error codes, sanitized stderr, selected Python
path with the username masked, skill verdicts, and elapsed times.

## After feedback: optional early-access follow-up

SkillLayer is free early access. A facilitator may ask whether the tester wants
to receive future early-access updates or would consider paying for a mature
version. There is no obligation, account, or payment request.
