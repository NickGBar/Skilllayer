# Changelog

## Unreleased

Version 0.3.0: the baseline for pilots — the gate, the case log, the review packet and context
cleaning as commands of one installed tool.

### Added

- `skilllayer review-packet`: a local Markdown summary for the reviewer — what changed, and which
  checks ran according to a gate receipt that matches the change (digest, head and merge base; the
  signature too when `SKILLLAYER_RECEIPT_KEY` is set). A verify receipt is context only. Never
  decides on merge. Russian and English (`--lang`). Replaces `scripts/review_packet.py`.
- `skilllayer context clean` / `restore`: replace listed client and project names, secrets and
  personal data (email, phones, ИНН, СНИЛС, cards, public IPs) with keyed tokens before context goes
  to a model; verify the result — nothing left, exact round trip, and for Python proof that only
  names and string values changed — and put the originals back into the answer. The key and the
  token table stay owner-only under `~/.skilllayer/context`. Moved from `experiments/`; see
  [docs/CONTEXT_CLEAN.md](docs/CONTEXT_CLEAN.md).
- `skilllayer cases`: a case log for pilots of the gate. `list` shows every failed or unverified
  check from the gate's receipts with a stable id; `label` records whether stopping the change was
  correct, a false positive or unsure, whether it was fixed and the minutes spent; `summary` gives
  the pilot's numbers — would-be stops, actual blocks, reasons, false-positive share, minutes.
  Receipts are never edited; labels are appended to `labels.jsonl` next to them and stay local.
- `skilllayer gate`: an agent-agnostic change gate for CI and git pre-push hooks. A change set is
  accepted only when its tests and the caller's required checks were observed passing on the exact
  commits that would land, no protected path changed (policy read from the base commit), and no
  commit in the range adds a secret — even one deleted again later. Unverified is never a pass.
  Every run leaves a receipt sealed with a SHA-256 digest and, with `SKILLLAYER_RECEIPT_KEY`, an
  HMAC that the code under judgement cannot read; `--event-log` writes one line per run for a SIEM.
  See [docs/GATE.md](docs/GATE.md); examples and a walk-through in `examples/gate/`.
- `skilllayer gate` check `test_integrity`: blocks a change set that weakens the tests — fewer test
  functions or assertions, an added `skip`/`xfail`, an assertion that cannot fail, a lowered coverage
  threshold, tests deselected in configuration, a deleted test file. `--approve-test-changes` for a
  person's approval, recorded in the receipt. The walk-through gains a scene where the agent removes
  the failing assertion.
- `skilllayer verify` (the Claude Code Stop hook) runs the test-weakening detector too: a turn that removes
  tests or assertions, skips tests, asserts `True`, lowers a threshold or deletes test files is sent back
  with verdict `TESTS_WEAKENED` — including a turn that deletes every test, which used to read as "no tests
  found". `verify.block_on_weakened_tests: false` turns it into a notice; `--stats` counts these blocks.
- Policy key `agent_scopes` and gate check `agent_scope`: each agent account may change only the paths
  the base policy gives it. `--author` takes the pull/merge request's author as the platform reports it
  (not commit metadata, not whoever re-ran the pipeline); `--approve-scope` records a person's approval.
  An agent cannot widen its own scope: the scope is read from the base and the policy file is protected.
- `test_integrity` false positives cut after measuring it on 1,800 merged changes of six projects
  (`scripts/measure_test_integrity.py`): a skip on a test the change adds, a removed-and-re-added skip and a
  test file moved elsewhere no longer count. Flag rate 3.4% of changes, 0.7% false positives; see docs/GATE.md.
- `skilllayer gate` re-runs failing tests (`--flaky-reruns`, default 2). A failure that then passes is
  reported as flaky or order-dependent — not verified, so not a pass — unless the caller passes
  `--accept-flaky`, which the receipt records.
- `skilllayer gate --lang ru` (or `SKILLLAYER_LANG=ru`): the console report and receipt checks in
  Russian. Receipts, JSON and the event log keep English keys. `DEMO_LANG=ru` for the walk-through.
- `skilllayer verify`: runs the project's tests itself and reads live git state to decide whether
  agent work can be accepted as complete — verdicts `VERIFIED`, `TESTS_FAILING`,
  `POLICY_VIOLATION` and `UNVERIFIED_*` (an unrun check never becomes a pass). See
  [docs/VERIFY.md](docs/VERIFY.md).
- `skilllayer-verify` Claude Code plugin (`plugin/`, marketplace in `.claude-plugin/`): a Stop hook
  that sends the agent back to work when the tests fail or a protected path was touched, with a
  finite block budget, receipts outside the repository, and `verify --stats`.
- Policy keys `protected_paths` and `verify` (mode, block budget, unverified handling, timeout).
- `examples/verify-demo/` scripted walk-through and `scripts/e2e_claude_code_hook.py`, which drives
  a real Claude Code binary against a scripted API stand-in.

### Fixed

- `skilllayer verify` dropped failing tests with long descriptive names (such as
  `test_save10_never_takes_off_more_than_50_dollars`) from the message the agent receives: the
  persistence gate's entropy heuristic took them for keys. Every failing test is now listed; the
  assertion text and parametrization ids still go through the full gate.
- The protected-path block told the agent to "ask the user to approve", but stopping to ask is
  itself a stop, blocked again while the file is modified. It now says to revert first and name
  the need in the final message.
- The plugin's hook wrapper used the first `skilllayer` on `PATH` even when it was broken or
  predated `verify` — an outdated install's exit 2 would read as "block this stop". It now skips
  unusable installs, never uses relative `PATH` entries, and reports when none works.
- Corrected documentation that implied VTE detects a false test claim. It does not: it records the
  result as reported. `skilllayer verify` is the path that observes tests.

### Changed

- Verified Task Execution receipts and reports now label the test result as **reported** by the
  agent (`source: "reported"`, limitation `tests_reported_not_independently_verified`) instead of
  presenting it as a verified fact; scope and baseline remain independently verified.


## 0.2.0 — Early access release preparation

### Added

- Safe Code Change, Release Readiness, and Resume Project Work.
- One-prompt AI-assisted installation and a disposable tester sandbox.
- Local sanitized diagnostics.
- Read-only public update checks and explicit uninstall dry runs.
- Local repository policy v1 with Safe Code Change and Release Readiness integration.

### Changed

- Professional-engineering-skill positioning and project-scoped MCP onboarding.
- Target-repository Python environment selection and advisory environment remediation.
- Consistent product version reporting and supported-Python installer selection.
- Tested update, rollback guidance, compatibility, support, and known-issue entry points.

### Fixed

- Environment mismatch false validation failures.
- MCP/package version mismatch and unsupported default-Python installation failures.
- Unsafe installer destination handling, source-install artifacts, and installation reporting.

### Safety

- No automatic dependency installation or telemetry.
- Explicit stateful writes, no hidden repository writes, and bounded remediation.
- Uninstall preserves project memory and unrelated MCP entries by default.

### Known limitations

- macOS is founder-verified; Linux and Windows are not verified.
- External-user evidence remains limited. SkillLayer is not a security certification and cannot guarantee every coding or security issue is detected.
