# Changelog

## Unreleased

### Added

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
