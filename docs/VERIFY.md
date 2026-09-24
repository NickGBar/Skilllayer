# Verify — check the agent's work instead of trusting it

`skilllayer verify` runs your tests itself and reads the repository's real git state, then
decides whether a piece of AI-agent work can be accepted as complete. As a Claude Code
**Stop hook** it runs when the agent tries to finish: if the tests fail or a protected path
was touched, the agent is sent back to work with the facts that were observed. Nothing the
agent says about its own work is an input.

```
skilllayer: this work cannot be accepted as complete — independent verification found problems.
- Tests failing (observed by running them): python -m pytest -q tests -> 1 failed, 3 passed in 0.01s (exit 1, 0.2s)
    tests/test_cart.py::test_discount_never_makes_the_total_negative — assert 5.0 == 0.0
Fix the issues above, then finish. Do not report success without re-running the tests.
(Blocked attempt 1 of 2; after that the hook lets you stop but records the work as UNVERIFIED.)
```

To watch three cases play out in a scratch repository — without a model or Claude Code —
run `examples/verify-demo/run_demo.sh`.

## Install

```bash
pipx install git+https://github.com/NickGBar/Skilllayer.git   # the `skilllayer` command
claude plugin marketplace add NickGBar/Skilllayer               # this repository is a plugin marketplace
claude plugin install skilllayer-verify@skilllayer              # enables the two hooks
```

From a local checkout, use the checkout path in the first two commands. The plugin adds two
hooks and one small skill; `claude plugin details skilllayer-verify` reports their cost as
about 70 tokens per session (the hooks themselves cost none — the harness runs them).

The hook uses `$SKILLLAYER_BIN`, then a `skilllayer` in any absolute directory on `PATH`, then
`~/.local/bin` — skipping any that cannot run `verify`, such as an editable install whose
checkout is gone or a version from before `verify` (whose argument-parser exit code 2 Claude Code
would otherwise read as "block"). Relative `PATH` entries are never used: they would resolve
inside the repository being judged. If nothing usable is found, every Stop says so — *"the
verify plugin is enabled but the `skilllayer` command was not found"*, or *"… is broken or too
old to run `verify`"*, *"so this work was NOT verified"* — rather than passing silently.

**Team rollout.** Committing `enabledPlugins` to the repository's `.claude/settings.json`
enables the plugin for a teammate whose machine already knows the marketplace (checked against
Claude Code 2.1.275); each developer also needs the `skilllayer` command.

```json
{
  "enabledPlugins": { "skilllayer-verify@skilllayer": true }
}
```

Claude Code's `extraKnownMarketplaces` setting is meant to register the marketplace for them too,
but a non-interactive session does not apply it, so it is not covered by the end-to-end check —
try it on a real machine before relying on it.

## How a verdict is reached

| Verdict | Meaning | Blocks the stop |
|---|---|---|
| `VERIFIED` | The tests were run and passed; no protected path was touched. | no |
| `TESTS_FAILING` | The tests ran and failed, or the test command exited non-zero. | yes |
| `POLICY_VIOLATION` | A protected path — or the policy file itself — changed during the turn. | yes |
| `UNVERIFIED_NO_TESTS` | No tests were found. Nothing was verified. | no¹ |
| `UNVERIFIED_ENVIRONMENT` | The tests could not run (pytest missing, import error before collection, …). | no¹ |
| `UNVERIFIED_TIMEOUT` | The test run exceeded its time budget. | no¹ |
| `UNVERIFIED_UNKNOWN` | The outcome could not be classified. | no¹ |

¹ Blocks with `block_on_unverified: true`.

An `UNVERIFIED_*` verdict is never reported as a pass. The user sees an inline notice that the
work was not verified, and the receipt records it as such. This is the rule the rest of
SkillLayer already follows: a check that did not run never becomes a clean result.

## What is independently verified — and what is not

Observed by `skilllayer` itself:

- **The test outcome.** It runs the test command and uses the real exit status and parsed
  failures. Auto-detected: pytest, unittest, and `npm|pnpm|yarn test` from `package.json`.
  Anything else: set `SKILLLAYER_TEST_COMMAND`.
- **Which files changed during the turn**, from live git state — including commits the agent
  made during the turn (the baseline `HEAD` is recorded when the turn starts).
- **Whether a protected path was touched.**
- **Whether test files were modified or deleted**, and whether the agent's own configuration
  (`.claude/settings*.json`) changed. These are reported to you, not blocked.

Not verified:

- **That the tests are meaningful.** A passing suite is not proof of correctness. Tests an
  agent weakened or deleted so that they pass are *reported* (an inline notice, the receipt)
  but not blocked — you are the one who can judge whether that was legitimate.
- **Anything the agent says in prose.** It is not read.
- **Task scope.** There is no "allowed paths for this task" check here, only protected paths.
  (The cooperative `vte_*` tools check scope, but the test result there is reported, not observed.)
- **Non-git directories.** Outside a git repository the hook does nothing.
- **Sandboxing.** Running the tests executes the repository's code with your permissions —
  exactly as running them yourself would.

## Policy

Enforcement is configured in the repository's committed `.skilllayer-policy.yml`
(see [POLICY.md](../POLICY.md)):

```yaml
version: 1
protected_paths:              # repo-relative; a trailing / means "everything under"
  - migrations/
  - .github/workflows/
verify:
  mode: block                 # block | warn — warn reports but never blocks
  max_consecutive_blocks: 2   # 1–10: after this many blocks the agent may stop (recorded UNVERIFIED)
  block_on_unverified: false  # true: also block when the tests were not observed to pass
  test_timeout_seconds: 300   # 10–3600
```

- **The policy is read as committed at the start of the turn**, not from the working tree, so
  an agent cannot loosen its own guardrails mid-turn — and editing the policy file during a
  turn is itself a `POLICY_VIOLATION`. A policy change takes effect once it is committed.
- **The policy never contains a test command**, so cloning someone else's repository cannot
  choose what runs on your machine. The command comes from your side: `SKILLLAYER_TEST_COMMAND`
  (an environment variable, e.g. `make test`) or `--test-command`.
- An **invalid committed policy** falls back to the strict defaults and says so in the receipt;
  it is never silently relaxed.

## How it stays out of the way

- **It only runs when something changed.** Verification happens when the working tree differs
  from when the turn began (or from the last verification), so conversational turns cost nothing.
- **The block budget is finite.** After `max_consecutive_blocks` (default 2) the agent is allowed
  to stop, with an inline notice — *"allowing this stop after 2 blocked attempt(s) — the work is
  UNVERIFIED"* — and the receipt records it. A verification layer that can trap a session forever
  gets uninstalled.
- **A slow test run ends as a visible verdict, not a silent pass.** Claude Code kills a hook that
  outlives its timeout and lets the stop through *without any message* (observed on 2.1.275). The
  plugin registers a 600 s timeout and passes `--max-seconds 540`, so a slow run ends first, as
  `UNVERIFIED_TIMEOUT`.
- **Internal errors are visible.** If verification itself fails, the Stop shows
  *"internal error …; this Stop was NOT verified"* — never a silent pass.

## Where the tests run

Project-local environment first (`.venv/`, `venv/`, `env/`). When there is none and
`skilllayer` was installed in its own isolated environment (as `pipx` does), the interpreter
your shell resolves is used instead — an activated virtualenv, then `python3`/`python` on
`PATH` — provided it can import the test framework. The interpreter used is visible in every
receipt. If none can, the verdict is `UNVERIFIED_ENVIRONMENT` with the reason.

## Receipts, stats and privacy

Everything is stored **outside** the repository, so verifying never dirties the tree it is
judging: `~/.skilllayer/verify/<repo>-<hash>/` (override with `SKILLLAYER_VERIFY_DIR`), holding
`receipts/*.json`, `log.jsonl` and per-session `state/`. A receipt contains the verdict, the
test command and outcome, changed paths, and a short output tail passed through SkillLayer's
sanitizer: lines with control characters or that look like credentials are withheld, and the receipt says how many (`output_tail_lines_withheld`). Nothing is sent anywhere.

```bash
skilllayer verify --stats
```

reports how many verifications ran, how many were blocked (failing tests / policy), how many
blocks ended in a later `VERIFIED` result in the same session (`recovered_after_block`), how
often the loop guard let the agent stop, how many were unverified, and how often test files
were touched.

## Command reference

```
skilllayer verify [--repo PATH] [--test-command CMD] [--mode block|warn] [--record] [--json]
skilllayer verify --stats [--json]
skilllayer verify --hook stop|prompt [--max-seconds N] [--no-record]      # what the plugin runs
```

One-shot exit codes: `0` verified, `2` blocked, `3` unverified, `1` error.

In hook mode a block is a JSON decision on stdout with exit `0` —
`{"decision": "block", "reason": <the facts, for the agent>, "systemMessage": <one line, for you>}`.
The agent receives the reason as "Stop hook feedback"; you see the summary inline, because the
harness itself only shows a generic "Stop hook error occurred" for a block. A warning without a
block is a bare `systemMessage`.

| Environment variable | Purpose |
|---|---|
| `SKILLLAYER_TEST_COMMAND` | Replace test auto-detection, e.g. `make test`. Never read from the repository. |
| `SKILLLAYER_BIN` | Path to the `skilllayer` executable when it is not on the hook's `PATH`. |
| `SKILLLAYER_VERIFY_DIR` | Where receipts, the event log and session state are written. |
| `SKILLLAYER_BLOCK_STYLE` | `exit2` blocks with exit code 2 and stderr instead of the JSON decision, for a harness that does not honour it. |

## Checked against

`scripts/e2e_claude_code_hook.py` installs the plugin into a throw-away configuration and drives
a real Claude Code binary against a scripted stand-in for the API (no credentials, no model). It
checks that both hooks run; that a block keeps the agent working and its message reaches the
model (by the JSON decision, and by exit code 2); that you are shown why; what the Stop payload
contains (`stop_hook_active` is `false` on the first stop and `true` after a block); and how a
missing binary, a custom test command and a hook timeout behave. Last run:
**Claude Code 2.1.275, macOS**. Other Claude Code versions, operating systems (Windows is not
supported) and other agents are not verified.

## Known limitations

- The tests run again at each Stop that follows changes. For a slow suite, point
  `SKILLLAYER_TEST_COMMAND` at a fast focused subset, or use `mode: warn`.
- One repository root: the git root of the session's working directory.
- The block message can change what the agent does, but a determined agent can still make the
  tests pass by other means; this layer reports what it observed, it does not judge intent.
