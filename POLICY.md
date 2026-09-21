# Repository Policy v1

SkillLayer can read one small declarative policy from the selected repository:
`.skilllayer-policy.yml` or `.skilllayer-policy.yaml`. If both exist, the
result is `POLICY_CONFLICT`; SkillLayer never guesses precedence.

## Schema

```yaml
version: 1
required_checks:
  - tests
  - secrets
approval_required_for:
  - dependency_install
  - destructive_command
safe_change:
  require_clean_or_acknowledged_worktree: true
  require_validation: true
release:
  allow_incomplete: false
  require_tests: true
  require_secret_check: true
protected_paths:
  - migrations/
verify:
  mode: block
  max_consecutive_blocks: 2
  block_on_unverified: false
  test_timeout_seconds: 300
```

The supported fields are intentionally limited. Unknown fields, checks, types,
versions, duplicate keys, YAML tags/aliases, shell metacharacters, and
environment interpolation are rejected. The parser is bounded, local-only,
non-executable, and never creates a default policy.

## Protected paths and `skilllayer verify`

`protected_paths` (repo-relative paths; a trailing `/` means everything under that directory —
no absolute paths, `..`, backslashes or globs) and the `verify` block are read only by
`skilllayer verify` and its Claude Code Stop hook; see [docs/VERIFY.md](docs/VERIFY.md).
`verify.mode` is `block` or `warn`, `max_consecutive_blocks` is 1–10, `test_timeout_seconds` is
10–3600, `block_on_unverified` is a boolean. There is deliberately **no test-command field**:
the policy is non-executable, and the command that runs comes from the caller's side
(`SKILLLAYER_TEST_COMMAND` or `--test-command`). The hook reads the policy as *committed* at the
start of a turn, so an agent cannot loosen it mid-turn, and editing the policy file during a turn
is itself a violation.

## Check and dry-run

```bash
skilllayer policy check --repo /path/to/repo --json
skilllayer policy explain --repo /path/to/repo
skilllayer policy dry-run safe-change --repo /path/to/repo --json
skilllayer policy dry-run release-readiness --repo /path/to/repo --json
```

Dry-run reports effective rules, required checks, approval requirements, and a
bounded verdict. It does not run tests, scan secrets, install dependencies,
start processes, write files, or write telemetry.

## Workflow behavior

Policy v1 integrates only with Safe Code Change and Release Readiness. Safe Code
Change requires an acknowledged clean/dirty worktree and validation evidence
when configured. Release Readiness cannot return ready when required checks are
incomplete. Resume Project Work is unaffected.

Without a policy file, existing workflow behavior remains unchanged. Invalid,
conflicting, unsupported, or unsafe policy files block only the affected
policy-integrated workflow and return structured errors.

Policy describes requirements; it does not execute commands, guarantee safety,
provide team governance, or certify security.
