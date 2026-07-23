# Verified Task Execution Foundation B — Scope Validation

Foundation B is an internal library, not a workflow, router target, or MCP
tool.  It captures bounded repository facts and compares observed changes with
an already-approved task contract.  It never edits source files, resets Git,
stashes work, installs dependencies, or attributes a change to an agent.

## Records

With task-lifecycle persistence consent, a task may write only inside its
existing `.skilllayer/tasks/<task-id>/` directory:

- `contract.json` remains write-once.
- `baseline.json` is write-once factual evidence.
- `scope_amendments.json` is an atomically updated append-only list of
  explicitly approved additions.
- `result.json` can carry a `scope_validation` section.

Capturing a baseline in memory is read-only.  Persistence is optional and
always reports the exact record path; Foundation B never edits `.gitignore`.

## Contract extension

```json
{
  "scope_mode": "EXPLICIT",
  "allowed_paths": ["src/example.py", "tests/test_example.py"],
  "forbidden_paths": [".env", ".git/"],
  "max_changed_files": 2,
  "allow_new_files": true,
  "allow_deleted_files": false,
  "allow_test_files": true,
  "allowed_generated_paths": [],
  "scope_amendments": []
}
```

All paths are normalized, repository-relative POSIX paths. Absolute paths,
empty segments, parent traversal, and protected source locations are rejected.
`.git/`, credential-like files, and other task records cannot be authorized.
`forbidden_paths` may name protected paths and always override allowed rules.

`EXPLICIT` matches exact files. `PREFIX` matches a directory entry ending in
`/` and its descendants; it never turns `src/module.py` into a match for
`src/module_extra.py`. `CANDIDATE` is represented for future explicit approval
but cannot be inferred or validated as permission in Foundation B.

## Baseline and observed state

`baseline.json` has schema version 1 and includes the task ID, capture time,
non-reversible project-root fingerprint, Git availability/head/branch/detached
state, clean state, changed/staged/untracked path sets, bounded SHA-256 file
fingerprints, selected test configuration fingerprints, status, and
limitations. It excludes remote URLs, commit messages, author identity,
ignored files, virtual environments, `.env`, raw Git output, and source text.

Fingerprints are bounded to 64 files, 512 KiB per file, and 2 MiB total read.
Limit exhaustion produces incomplete evidence; it is never silently omitted.
The observed collector reports neutral facts such as “observed change” and
“attribution unknown”; it never says a host agent made a change.

## Freshness and verdicts

Freshness is separate from scope:

- `CURRENT_BASELINE`: no relevant source change.
- `EXPECTED_TASK_CHANGES`: complete evidence contains only allowed changes.
- `EXTERNAL_DRIFT_POSSIBLE`: unrelated changes make attribution uncertain.
- `BASELINE_STALE`: repository HEAD changed after capture.
- `FRESHNESS_UNKNOWN`: Git, required files, or bounded evidence was unavailable.

Scope verdicts are `SCOPE_CLEAN`, `SCOPE_EMPTY`, `SCOPE_VIOLATED`,
`SCOPE_INCOMPLETE`, `BASELINE_STALE`, and `VALIDATION_BLOCKED`. A clean verdict
is impossible unless evidence is complete. Renames evaluate both origin and
destination. New files, deletions, staged-only changes, and untracked files
are compared as facts.

## Amendments and internal state

An amendment has an ID, approval timestamp, added allowed/generated paths,
reason label, and consent reference. It can only add bounded paths; it cannot
remove forbidden paths or authorize protected locations. Duplicate IDs are
idempotent. Foundation B never creates one automatically after a violation.

The validator ignores only the exact current task record directory and approved
task-local persistence files. It does not ignore all `.skilllayer/`: another
task record, global context memory, or preferences remain visible evidence.

## Limitations

Git state and selected content fingerprints establish factual comparison, not
authorship. Concurrent user edits can make attribution uncertain. Foundation B
does not execute validation, change user files, offer rollback, create a public
task workflow, route requests, or expose MCP tools.
