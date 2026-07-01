# Release Notes

## Public update - 2026-06-21, feedback closure

This update improves the local feedback lifecycle so fixed tester issues do not
look fully closed before external confirmation.

### Added

- `WAITING_VALIDATION` feedback status.
- Feedback aging fields:
  - `created_date`
  - `fixed_date`
  - `validated_date`
  - `age_days`
  - `days_to_fix`
  - `days_waiting_validation`
- Feedback CLI filters:
  - `feedback-status --waiting-validation`
  - `feedback-status --stale`
  - `feedback-status --recently-validated`
- Feedback health summary:
  - oldest open item
  - oldest waiting-validation item
  - recently validated items
  - stale open / stale validation wait counts
  - validation rate
  - average days to fix / validate
- Validation module:
  - `python -m skilllayer.feedback_closure_validation`

### Safety

- Feedback remains local and file-based.
- No database, server, network sync, or automatic telemetry upload was added.

## Public update - 2026-06-21, focused test rerun

This update adds a small read-only workflow for rerunning one test target.

### Added

- `SingleTestWorkflow` for focused test execution:
  - pytest file targets such as `tests/test_api.py`
  - pytest node targets such as `tests/test_api.py::test_login`
  - dotted unittest targets when explicitly provided
  - best-effort reuse of the most recent failed test within the same
    `SkillLayer` session
- Validation module:
  - `python -m skilllayer.single_test_workflow_validation`

### Safety

- `SingleTestWorkflow` does not modify files and does not auto-fix tests.
- `Run failing test` returns `no_failed_test_available` if there is no prior
  failed test target in the current session.
- Unsupported or ambiguous targets return `single_test_not_supported` instead
  of guessing.

## Public update - 2026-06-21, later

This update focuses on first-run trust, release safety, and external tester
feedback tracking. It does not add new agent intelligence or network services.

### Added

- Feedback registry:
  - `FEEDBACK_STATUS.md`
  - `feedback/feedback_registry.json`
  - `feedback-status` CLI command
- Feedback CLI filters:
  - `--status`
  - `--source`
  - `--platform`
  - `--needs-validation`
  - `--open-only`
  - `--id`
- Validation modules:
  - `python -m skilllayer.release_validation_pipeline_validation`
  - `python -m skilllayer.feedback_registry_validation`
  - `python -m skilllayer.feedback_cli_validation`
  - `python -m skilllayer.install_hardening_validation`

### Fixed

- Zero-test runs are no longer reported as passing test suites.
- Missing `pytest` is reported as an optional warning for core first-run checks
  instead of making a basic install look broken.
- Windows setup now has PowerShell install and verify scripts.
- Windows installer now fails if `.venv` uses Python older than 3.10.
- Windows install and verify scripts now return nonzero status on required
  failures instead of printing misleading success.
- `ExplainFailureWorkflow` no longer emits fake `unknown_failure` diagnoses when
  no tests are discovered.
- MCP extra installation guidance is clearer in setup docs.
- Human CLI output points users toward `--json` when full structured artifacts
  are available.

### Safety

- Feedback tracking is local and file-based. It uses no database, server,
  network sync, or automatic upload.
- Maintainer commands are not exposed through MCP.

## Public update - 2026-06-21

This update adds two small read-only workflows for routine coding-agent context
gathering.

### Added

- `GitStatusWorkflow` summarizes branch state, staged changes, unstaged
  changes, untracked files, and diff stats using read-only git commands.
- `DependencyCheckWorkflow` checks whether a Python or Node dependency is
  declared and where it is imported or required.
- Validation modules for both workflows:
  - `python -m skilllayer.git_status_workflow_validation`
  - `python -m skilllayer.dependency_check_workflow_validation`

### Safety

- `GitStatusWorkflow` does not stage, commit, reset, checkout, push, or include
  full diffs by default.
- `DependencyCheckWorkflow` does not install, update, remove, or run package
  managers.
- Both workflows report `Validation: Not Applicable` because they are read-only
  inspection workflows and do not execute tests.

### Notes

- `DependencyCheckWorkflow` is intentionally simple and deterministic. It
  inspects supported dependency files and scans import/require statements. It
  does not perform dependency resolution.
- These workflows are convenience tools for coding agents and developers. They
  do not make SkillLayer a general coding agent.
