# SkillLayer Bug Inventory

This inventory is grounded in public commit `cb48b1b7d56ce4a523d38dfb1d80eb2838ffbda2`,
the current public test suite, the public sandbox behavior, and the founder
observations supplied for Bug Stabilization v1. No open or closed GitHub issues
were present when the inventory was created.

## BUG-001 — Professional-skill routing misses clear bilingual intent

### Source

Founder observation that professional skills are not always invoked, followed
by a deterministic English/Russian routing evaluation against public HEAD.

### Symptom

Ten of twelve clear professional-skill prompts fell through to
`clarify_intent`; all Russian prompts missed.

### Expected behavior

Clear Safe Code Change, Release Readiness, and Resume Project Work intent
should select the corresponding professional skill without turning unrelated
requests into professional-skill activations.

### Reproduction

Run
`pytest tests/test_professional_skills_routing.py::test_professional_routing_recall_evaluation`.
Before the fix, 10 of 12 cases fail. The same result was reproduced twice.

### Reproduction status

REPRODUCED

### Impact

- ROUTING
- USABILITY_ONLY

### Severity

HIGH

### Proposed fix

Extend the existing three professional-skill predicates with narrow English
and Russian phrase families. Keep the existing deterministic cascade and
negative controls.

### Regression test

`test_professional_routing_recall_evaluation` and
`test_professional_routing_negative_controls` in
`tests/test_professional_skills_routing.py`.

## BUG-002 — Safe Code Change reports validated when tests never started

### Source

Verdict-path audit required by the founder milestone.

### Symptom

For a repository with a real diff but no detected/provided test command,
validate returned `CHANGE_VALIDATED_WITH_WARNINGS` with `success: true`,
`tests_run: false`, and `validation_complete: false`.

### Expected behavior

Validation that never started must be incomplete, never validated.

### Reproduction

Create a committed Git repository containing only `app.py`, modify `app.py`,
then call Safe Code Change with `phase="validate"`. Before the fix, the focused
regression test fails with the false validated verdict. It was reproduced
twice.

### Reproduction status

REPRODUCED

### Impact

- VERDICT_CORRECTNESS

### Severity

CRITICAL

### Proposed fix

Make every `tests_run: false` validation path return `CHANGE_INCOMPLETE` and
`success: false`, independently of whether repository policy requires tests.

### Regression test

`TestValidatePhase.test_changed_files_without_started_tests_are_incomplete` in
`tests/test_safe_code_change_workflow.py`.

## BUG-003 — Bounded Release Readiness reports ready before tests run

### Source

Verdict-path audit required by the founder milestone.

### Symptom

Bounded Release Readiness detected tests, did not execute them, recorded
`test_status` under `checks_incomplete`, then returned
`READY_WITH_KNOWN_LIMITATIONS`.

### Expected behavior

When tests have not started, the assessment must be
`INCOMPLETE_ASSESSMENT`, not a ready verdict.

### Reproduction

Create a complete disposable package fixture with tests and a healthy memory
store, then run bounded Release Readiness. Before the fix, the focused
regression test fails with `READY_WITH_KNOWN_LIMITATIONS`. It was reproduced
twice.

### Reproduction status

REPRODUCED

### Impact

- VERDICT_CORRECTNESS

### Severity

HIGH

### Proposed fix

Treat any incomplete `test_status` check as `INCOMPLETE_ASSESSMENT`. Preserve
`NOT_READY` for real test failures and `READY_FOR_CAREFUL_TESTERS` only for
completed required checks.

### Regression test

`TestBoundedDefaults.test_bounded_mode_with_tests_not_started_is_incomplete` in
`tests/test_release_readiness_workflow.py`.

## BUG-004 — Requested context-save failure is laundered into validated change

### Source

Verdict-path audit required by the founder milestone and the existing memory
error contract.

### Symptom

When `save_context=True`, Safe Code Change ignored a failed context-save
result and could still return `CHANGE_VALIDATED`.

### Expected behavior

If the explicitly requested stateful action fails, the workflow must report
the memory error and return an incomplete verdict.

### Reproduction

Run a passing change validation while deterministically substituting the same
structured `memory_permission_denied` result produced by the memory layer.
Before the fix, validation still returns `CHANGE_VALIDATED`. It was reproduced
twice.

### Reproduction status

REPRODUCED

### Impact

- VERDICT_CORRECTNESS
- MEMORY

### Severity

HIGH

### Proposed fix

Propagate the context-save error into `blockers`, `error_code`, evidence, and
the final `CHANGE_INCOMPLETE` verdict. Do not invent written paths.

### Regression test

`TestValidatePhase.test_requested_context_save_failure_prevents_validated_verdict`
in `tests/test_safe_code_change_workflow.py`.

## BUG-005 — Commit after saved context is treated as current without activity baseline

### Source

Memory/resume correctness audit required by the founder milestone.

### Symptom

Save structured context, commit new repository work, and resume without first
creating a separate activity snapshot. Resume returned `READY_TO_CONTINUE`
even though the saved checkpoint predates the current commit.

### Expected behavior

Resume should disclose the committed drift and avoid treating the checkpoint
as current.

### Reproduction

Save context with an earlier deterministic timestamp, make and commit a new
file, then call Resume Project Work without an activity baseline. Before the
fix, the focused regression test returns `READY_TO_CONTINUE`. It was
reproduced twice.

### Reproduction status

REPRODUCED

### Impact

- MEMORY
- VERDICT_CORRECTNESS

### Severity

HIGH

### Proposed fix

Add a bounded read-only comparison between the saved context timestamp and the
current Git HEAD commit timestamp. Report `commit_after_context_save` and use
`READY_WITH_REPOSITORY_DRIFT` when the comparison proves drift.

### Regression test

`TestResumeWork.test_commit_after_context_save_is_not_treated_as_current_without_activity_baseline`
in `tests/test_resume_project_work_workflow.py`.

## BUG-006 — Unittest-only target inherits SkillLayer's pytest availability

### Source

Target-environment reliability audit required by the founder milestone.

### Symptom

When pytest is installed in SkillLayer's own environment but absent from a
target repository's local `.venv`, an otherwise runnable unittest-only project
was assigned `<target-python> -m pytest` and reported an environment mismatch.

### Expected behavior

Framework detection should use target-repository evidence. A unittest-only
project must run through the selected target interpreter without depending on
packages installed alongside SkillLayer.

### Reproduction

Create a local `.venv` without site packages and a test importing
`unittest.TestCase`, while running SkillLayer from a development environment
that contains pytest. Before the fix, the generated command uses pytest. It
was reproduced twice.

### Reproduction status

REPRODUCED

### Impact

- ENVIRONMENT
- VERDICT_CORRECTNESS

### Severity

HIGH

### Proposed fix

Remove the current-interpreter pytest probe from framework selection. Prefer
explicit pytest repository signals; otherwise detect bounded unittest source
signals and use the selected target interpreter.

### Regression test

`TestTargetExecution.test_unittest_only_project_does_not_depend_on_skilllayer_pytest`
in `tests/test_target_environment_execution.py`, plus the unconditional
no-signal unittest detection test in `tests/test_run_tests_workflow.py`.

## BUG-007 — Dependency tests inside a local venv are treated as project tests

### Source

Target-environment and no-tests reliability audit required by the founder
milestone.

### Symptom

A repository with no project tests but a dependency-owned
`.venv/.../site-packages/.../tests/test_*.py` received a generated project test
command instead of the honest no-tests result.

### Expected behavior

Test discovery must ignore supported local environments and other generated
dependency/build directories.

### Reproduction

Create an otherwise test-free repository with a local `.venv` and one
synthetic dependency test below `site-packages`. Before the fix,
`TestRunner.detect()` returns a pytest command. It was reproduced twice.

### Reproduction status

REPRODUCED

### Impact

- ENVIRONMENT
- VERDICT_CORRECTNESS

### Severity

HIGH

### Proposed fix

Filter local environments, dependency trees, VCS metadata, and build outputs
before classifying project test files.

### Regression test

`TestTargetExecution.test_dependency_tests_inside_local_venv_are_not_project_tests`
in `tests/test_target_environment_execution.py`.

## BUG-008 — Unspecified additional skill-reliability problems

### Source

Founder observation that some skills may not yet be reliable enough.

### Symptom

No exact workflow, input, output, or failure condition was supplied beyond the
specific routing and verdict cases above.

### Expected behavior

Every advertised skill should return a truthful bounded result for a
reproducible input.

### Reproduction

Public GitHub issues, skipped tests, known issues, runtime TODO/FIXME comments,
the public professional-skill tests, and the required focused safety suites
were reviewed. No additional deterministic product failure was isolated.

### Reproduction status

INSUFFICIENT_INFORMATION

### Impact

- USABILITY_ONLY

### Severity

MEDIUM

### Proposed fix

Collect the exact input and structured output when another failure occurs;
do not change product code without that reproduction.

### Regression test

Not applicable until a concrete failing path is supplied.
