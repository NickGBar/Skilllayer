# SkillLayer

SkillLayer gives AI coding agents professional engineering skills: safe code
changes, release readiness checks, and persistent project context.

- **Make code changes safely.** Inspect the repository and git status, get a
  bounded change plan, then — after the AI agent makes the edit — validate
  the resulting diff and tests before you trust it.
- **Check whether a project is ready to release.** One honest, bounded
  verdict from git status, a secret scan, dependency and packaging checks,
  and memory integrity — blockers and incomplete checks are always disclosed,
  never hidden behind a false "clean."
- **Continue work across AI-agent sessions.** Save context once; a brand-new
  session — no prior conversation, no shared memory — recovers what was
  completed, what constraints matter, and what to do next.

SkillLayer runs locally and makes no LLM calls of its own. It has no cloud
service and no uploaded telemetry. It returns
deterministic, structured JSON, but a deterministic result is not
automatically safe or portable to every project. SkillLayer does not claim to prove token savings.

Python 3.10+ is required. The release path is verified on macOS in this
repository; Windows installer logic is checked statically, not executed here.
MCP integration is verified end-to-end with Claude Code and Codex; Cursor is
partially validated. Not every AI client is supported.

## Install

From a checkout:

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
./scripts/install.sh
./scripts/verify_install.sh
```

The installer creates `.venv` and installs the MCP runtime extra. It fails if
that required install fails; it does not silently fall back to a reduced MCP
installation. Use the generated environment explicitly when activation is not
convenient:

```bash
.venv/bin/python -m skilllayer doctor --json
.venv/bin/python -m skilllayer workflows --json
.venv/bin/python -m skilllayer inspect --repo /path/to/repo --json
```

## MCP

Generate a config using the installed interpreter, then validate it before
adding it to a client:

```bash
.venv/bin/skilllayer mcp-config --output skilllayer-mcp.json
.venv/bin/skilllayer mcp-config-check skilllayer-mcp.json --json
```

Copy the `mcpServers.skilllayer` block into your Claude Code or Cursor MCP
configuration. The server uses stdio and does not need a checkout-relative
working directory. If the venv was moved or deleted, the checker reports a
clear regeneration command rather than claiming the config is valid.

Start with `skilllayer_inspect_repo`, `skilllayer_search`, or `skilllayer_run`
for a read-only task such as “Git status”. Internal workflows and the unsafe
profile/memory execution workflows are not registered over MCP.

## Try it

Once connected, ask your AI coding agent things like:

> Help me implement this issue safely. Inspect the repository, propose a
> plan, and validate the final diff.

> Check whether this repository is ready for careful external testing. Do
> not modify files.

> Restore the project context and tell me what was completed, what
> constraints matter, and what I should do next.

These map to `skilllayer_safe_change`, `skilllayer_release_readiness`, and
`skilllayer_resume_work` — each returns a bounded verdict (e.g.
`CHANGE_VALIDATED`, `NOT_READY`, `READY_WITH_REPOSITORY_DRIFT`) rather than a
free-form summary, and never claims success when a check was incomplete or
skipped.

## Writes, memory, and network

Read-only workflows do not intentionally write repository files. Stateful
memory commands write only under `.skilllayer/` and report written paths; they
never edit `.gitignore`. Snapshot/watch workflows persist a baseline only when
their explicit persistence option is enabled. A “watch” is snapshot-and-diff,
not a background real-time service.

Some workflows execute a project’s tests, make network requests, start browser
work, or run a target script. Their metadata marks these as
`external_side_effects_possible`; use committed copies or a clean branch first.
BrowserSmoke requires its configured browser backend and writes artifacts only
when explicitly enabled.

For Python tests, SkillLayer uses a usable target-repository `.venv`, `venv`,
or `env` interpreter before falling back to its own interpreter. Structured
test results report the selected interpreter and fallback decision. SkillLayer
never installs dependencies or creates environments; a missing test dependency
is reported as incomplete validation rather than a claim that the code failed.

### Environment-aware validation

When a target environment cannot collect tests, SkillLayer reports an
evidence-based command you can review and run yourself. It never executes that
command automatically; validation remains incomplete until tests actually run.

Automatic telemetry is off by default and no telemetry is uploaded. Session
usage reads local Claude Code logs and can measure recorded usage; it cannot
prove token savings or establish a counterfactual baseline.

## Disable or remove

To disable integration, remove only the `skilllayer` entry from your MCP
client configuration and restart that client. To remove the installation,
delete the SkillLayer virtual environment or uninstall the package from that
environment. Project memory under `.skilllayer/` is user data: delete it only
with an explicit project-level decision. Local telemetry/log directories, if
you explicitly enabled them, can be removed separately.

`scripts/uninstall.sh` and `scripts/uninstall.ps1` make those choices explicit:
use `--remove-venv`, `--remove-project-state`, or `--remove-user-data` only for
the data you intend to remove. They never remove project memory by default.

## What this feels like

After installation, Claude Code discovers the local SkillLayer tools through
MCP. You ask it to inspect a repository or search for `greet`; the result is
structured JSON rather than model-generated shell steps. Later, if you choose
to save context, SkillLayer reports the exact `.skilllayer/` paths written, and
you can rehydrate that context in a later session.

## Try the safe sandbox

Use the [one-prompt sandbox trial](ONE_PROMPT_TEST.md) before trying SkillLayer
on a committed copy of a real repository. The sandbox is disposable, records a
sanitized `results.md`, and demonstrates explicit environment-remediation
consent. See the [Professional Beta offer](BETA_OFFER.md) and the static
[landing page](site/index.html) for the $49 one-time early-beta terms.

## Advanced: low-level tools

The three professional skills above are built from lower-level, independently
callable building blocks: repository inspection, git history and blame,
dependency mapping, secret scanning, dead-code detection, todo/decision
tracking, and more. Current code registers 47 workflows: 41 `stable` and 6
`internal`. The authoritative inventory is `skilllayer workflows --json`; it
includes each workflow’s stability and write behavior. MCP currently exposes
39 tools; the runtime tool list is authoritative and can change with the
installed version.
