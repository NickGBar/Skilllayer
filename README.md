# SkillLayer

**Required checks for code written by AI agents.** A change from Claude Code, Cursor, GigaCode or
any other agent — or a person — lands only when its tests and required checks were observed
passing on the exact commits that would land. Every run leaves a signed receipt that security and
audit can re-check later.

> **По-русски:** [описание на сайте](https://nickgbar.github.io/Skilllayer/). Отчёт проверки
> выводится на русском с `--lang ru`.

**Status: prototype.** Nobody runs it in production yet; we are looking for the first pilot
teams.

## The problem

Agents now write more code than people can review line by line. And an agent:

- says "done, all tests pass" when they do not;
- makes failing tests pass by deleting the assertion, skipping the test or lowering the coverage
  bar — CI then runs what is left and reports green;
- changes what it should not, and loosens the rule that protected it in the same commit;
- commits a key and deletes it in the next commit — the change looks clean, the key is in history.

Afterwards, nobody can show which checks ran on which commit.

## What `skilllayer gate` does

It runs in CI (or a git pre-push hook) on `merge-base(base, head)..head`, needs nothing from the
agent, and decides:

| Check | Blocks the change when |
| --- | --- |
| Tests | they fail when the gate runs them itself; a failure that passes on re-run is flagged as flaky, not passed |
| Test integrity | the change set weakens the suite: fewer tests or assertions, an added `skip`/`xfail`, `assert True`, a lowered threshold, tests deselected in config |
| Protected paths | a path protected by the policy **of the base commit** changed — so a change cannot unprotect itself |
| Secrets | any commit in the range adds a key, even if a later commit deletes it |
| Agent scope | an agent account changed paths outside the scope the policy gives it |
| Your checks | a required command you pass (`--check sast="semgrep scan --error"`) exits non-zero |

A check that did not run is never a pass: no tests found, an environment error or a timeout make
the verdict `UNVERIFIED`, which blocks too. Exit codes: `0` accepted, `2` blocked, `3` unverified,
`1` error. Where a person decides — a legitimate test removal, a reviewed change to a protected
path — a CI job they run passes `--approve-test-changes` / `--approve-protected` /
`--approve-scope`, and the receipt records it.

## Try it

Four agent-made change sets and a tampered receipt, in a scratch repository, no model involved:

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
python3 -m venv .venv && .venv/bin/pip install . pytest
examples/gate/run_demo.sh            # DEMO_LANG=ru for Russian
```

## Add it to CI

Copy [`examples/gate/github-actions.yml`](examples/gate/github-actions.yml) or
[`examples/gate/gitlab-ci.yml`](examples/gate/gitlab-ci.yml). The core of both:

```bash
skilllayer gate --base "$BASE_SHA" --author "$PR_AUTHOR" \
  --check "sast=semgrep scan --error" \
  --receipt-dir gate-receipts --event-log gate-receipts/events.jsonl
```

Make the job a required check and keep `SKILLLAYER_RECEIPT_KEY` in a masked CI variable.
`--mode warn` reports without blocking — the way to start a pilot.

## Evidence

- **Receipt** per run: base, head, commits, changed paths, every check with its command, exit
  code and output hash, the verdict. Sealed with a SHA-256 digest and an HMAC; the gate takes the
  key out of the environment before it runs any project code, so the code under judgement cannot
  sign for itself. `skilllayer gate --verify-receipt FILE` re-checks one.
- **Event log** (`--event-log`): one JSON line per run for your SIEM, with the receipt's digest.
- Receipts never contain a matched secret, credentials from a remote URL or e-mail addresses.

## Limits, plainly

- **Evidence comes from CI.** The local pre-push hook is a convenience: `git push --no-verify`
  skips it.
- **The policy file must be protected** on the base branch (CODEOWNERS or branch rules): a merged
  change to it becomes the next base.
- **The key protects receipts from the code under judgement, not from CI administrators.** When
  that matters, keep the event-log digests where they cannot edit them.
- **The gate runs the change's code** (tests, checks) with the CI job's permissions. It does not
  sandbox it.
- **It does not look for vulnerabilities.** Scanners do; run them as required checks through
  `--check`, and their result lands in the receipt.
- **Test integrity** is line-based, for Python and JavaScript/TypeScript. On 1,800 recent changes
  of six open-source projects, 0.7% were flagged without reason (most flags were real test removals
  a person should approve). Not yet measured on agent-written code.
- **Secrets**: common key formats, not a full secret scanner.
- Tested on macOS and in Linux CI with Python 3.10–3.13; Windows is untested.
- No network calls, no model: the same change set gets the same verdict.

Details, trust boundaries and the measurement: [docs/GATE.md](docs/GATE.md).

## Also in this repository

- **`skilllayer verify`** — a Claude Code Stop hook that runs the same kind of checks while the
  agent is still working and sends it back when they fail. [docs/VERIFY.md](docs/VERIFY.md).
- **Agent tools** — the MCP tools SkillLayer started with (safe change, release readiness, resume
  work, Verified Task Execution). [docs/AGENT_TOOLS.md](docs/AGENT_TOOLS.md).

## License

[Business Source License 1.1](LICENSE): free for non-commercial use; it becomes Apache 2.0 on
2031-01-01. Use inside a company, including a pilot, needs a separate agreement — ask us.
