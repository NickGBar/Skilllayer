# skilllayer gate — the agent-agnostic change gate

`skilllayer gate` accepts a change set only when its required checks were **observed** passing
on the exact commits that would land — whichever agent or person made them. It runs in CI or in
a git pre-push hook, so it needs neither the agent's cooperation nor agent-specific hooks.
Every run leaves a receipt: the evidence that a change passed, which can be re-checked later.

`skilllayer verify` (the Claude Code Stop hook, [VERIFY.md](VERIFY.md)) catches problems while
the agent is still working. The gate is the backstop where the code lands: it also covers
agents without hooks, commits hidden behind wrapper commands, and people.

## What it checks

The change set is `merge-base(base, head)..head`; `head` must be the checked-out commit.

| Check | Fails (blocks) when | Not verified when |
| --- | --- | --- |
| `tests` | the tests fail when the gate runs them, and still fail when the failures are re-run (`--flaky-reruns`, default 2) | no tests found, environment error, timeout, the checkout is not exactly `head`, or the failures passed on re-run — flaky or order-dependent, which is not a pass unless the caller passes `--accept-flaky` (recorded) |
| `protected_paths` | a path protected by the policy **at the base commit** changed | — |
| `secrets` | a commit in the range adds a critical or high-severity secret pattern outside test fixtures — even if a later commit deletes it, since history reaches the remote | the history is unavailable or too large to scan |
| `test_integrity` | the change set weakens the suite: fewer test functions or assertions overall, a `skip`/`xfail` added, an assertion that cannot fail (`assert True`), a lowered coverage threshold, tests deselected in the runner or CI configuration, a test file deleted — unless the caller passes `--approve-test-changes` | the diff is unavailable or too large |
| `check:<name>` | a required command (`--check name=command`) exits non-zero | the command is missing, cannot run, or times out |

Notes that do not block: test files changed, agent configuration changed, key-shaped strings in
test fixtures, medium-severity patterns.

Verdicts and exit codes: `VERIFIED` (0), `NO_CHANGES` (0, nothing new on top of the base),
`BLOCKED` (2), `UNVERIFIED` (3). An unverified check is never a pass: in the default `block` mode
it rejects the change set; `--mode warn` only reports. Errors (an unresolvable ref, an unwritable
receipt, an internal failure) exit 1 — never 0.

## Quick start

**Local, any agent:** install the pre-push hook. Pushes that fail the gate do not reach the remote.

```bash
cp examples/gate/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

**CI:** [`examples/gate/gitlab-ci.yml`](../examples/gate/gitlab-ci.yml) for merge requests and
[`examples/gate/github-actions.yml`](../examples/gate/github-actions.yml) for pull requests. The
job needs the full history (`GIT_DEPTH: "0"`, `fetch-depth: 0`) and the project's test
dependencies.

**Walk-through:** `examples/gate/run_demo.sh` runs four canned agent change sets through the gate
and then tampers with a receipt (`DEMO_LANG=ru` for a Russian narration and report).

```bash
skilllayer gate --base origin/main \
  --check "lint=ruff check ." --check "sast=semgrep scan --error" \
  --receipt-dir gate-receipts --event-log gate-receipts/events.jsonl
```

## Language

The console report and `--verify-receipt` speak English or Russian: `--lang ru`, or
`SKILLLAYER_LANG=ru` in the environment (`ru_RU.UTF-8` works too). Receipts, `--json` output and
the event log keep English keys and codes in every language, so SIEM rules and scripts read them
the same way. Error messages are English only for now.

## Trust boundaries

- **The policy comes from the base commit.** A change set that edits `.skilllayer-policy.yml` to
  drop a protected path protects nothing — and editing the policy is itself a protected change.
- **Commands come from the caller** — the CI configuration or the local hook file — never from
  files inside the repository being judged.
- **Approving a protected change is the caller's decision**: `--approve-protected` (for example a
  manual CI job that GitLab records a person running). The receipt says the change was approved.
- **The gate runs the change set's code** (tests, checks), with the caller's permissions, exactly
  as running the tests yourself would. It does not sandbox it.

## Weakened tests

Told to make failing tests pass, an agent can fix the code — or delete the failing assertion,
skip the test, or lower the coverage bar. CI then runs what is left and reports green.
`test_integrity` reads the net diff and counts what the change set took away from the suite;
every signal is a line-level pattern, no model judges intent. Counts are totals across the change
set, so a test moved from one file to another is not a weakening. A refactor that really removes
tests is reported too — whether it is legitimate is a person's call, made with
`--approve-test-changes` in a CI job they run, and recorded in the receipt.

## Receipts

Each run writes a JSON receipt (default: the per-user SkillLayer data directory, outside the
repository; `--receipt-dir` in CI) with the base, head and merge base, the commits (author name,
subject, AI-assistance markers), the changed paths, every check with its command, exit code,
duration and output hash, the verdict, and `integrity`:

- `digest` — SHA-256 of the canonical receipt. It fingerprints the receipt; stored elsewhere (the
  `--event-log` line carries it for your SIEM), it reveals any later edit.
- `hmac` — present when `SKILLLAYER_RECEIPT_KEY` is set (`SKILLLAYER_RECEIPT_KEY_ID` names the
  key). Only a key holder can produce it, so a receipt edited and re-digested by anyone else fails.
  The gate takes the key **out of the environment before it runs any project code**, so the code
  under judgement cannot sign a receipt for itself.

```bash
skilllayer gate --verify-receipt gate-receipts/<file>.json   # 0 valid, 2 invalid
```

Receipts never contain a matched secret, a remote URL's credentials or e-mail addresses.

## AI-assisted commits

A commit counts as AI-assisted when a trailer says so: `Co-Authored-By:` naming a known agent
(Claude, Copilot, Cursor, Codex, GigaCode and others), or `Generated-by:`, `Assisted-by:`,
`AI-Assisted:`, `AI-Agent:`. Agents are not obliged to add trailers, so this marks AI-assisted
work; it never proves a commit is human-only. The gate applies the same checks to every commit.

## Limits

- Tests are auto-detected for pytest and unittest; pass `--test-command` for anything else.
- The secret patterns are the ones `skilllayer_detect_secrets` uses — common key formats, not a
  full secret scanner. Binary files are not scanned (reported as a limitation).
- The checkout must be the commit under judgement; the gate does not check out other refs.
- Verified on macOS with git 2.39; Windows is untested.
